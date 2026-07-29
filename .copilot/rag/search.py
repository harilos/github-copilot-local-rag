#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


RAG_ROOT = Path(__file__).resolve().parent
_QUERY_ROOT = RAG_ROOT / "query"
if str(_QUERY_ROOT) not in sys.path:
    sys.path.insert(0, str(_QUERY_ROOT))

from reference_contract import (  # noqa: E402
    install_result_bundle_reference_contract,
    install_search_command_reference_contract,
)
from setup_contract import completion_contract_valid  # noqa: E402
from wrapper import search_command  # noqa: E402


TEMPORARY_REPAIR_LABEL_JA = "検索利用判定を修復する（一時的）"
TEMPORARY_REPAIR_ACTION = "repair_completion_marker_temporarily"


def _setup_gate_projection(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("status") or "") != "setup_required":
        return payload

    projected = copy.deepcopy(payload)
    venv_python = _QUERY_ROOT / ".venv" / (
        "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
    )
    marker = _QUERY_ROOT / ".venv" / ".rag-deps-installed"
    marker_valid, marker_reason = completion_contract_valid(marker, RAG_ROOT)
    repair_available = bool(venv_python.is_file() and not marker_valid)

    projected["setup_gate"] = {
        "runtime_python_present": venv_python.is_file(),
        "completion_marker_valid": marker_valid,
        "completion_marker_reason": marker_reason,
        "repair_available": repair_available,
        "repair_action": (
            TEMPORARY_REPAIR_ACTION if repair_available else None
        ),
        "repair_label_ja": (
            TEMPORARY_REPAIR_LABEL_JA if repair_available else None
        ),
        "repair_command": (
            {
                "script": "query/setup.py",
                "arguments": [
                    "--repair-completion-marker",
                    "--format",
                    "json",
                ],
            }
            if repair_available
            else None
        ),
    }
    if repair_available:
        projected["required_action"] = TEMPORARY_REPAIR_ACTION
        projected["message"] = (
            "The installed runtime exists, but its lookup completion marker "
            f"is not valid ({marker_reason}). Run the temporary completion-"
            "marker repair; it does not reinstall the model or rebuild a DB."
        )
        projected["message_ja"] = (
            f"検索実行環境は存在しますが、検索利用判定が無効です"
            f"（{marker_reason}）。{TEMPORARY_REPAIR_LABEL_JA}を実行してください。"
            "モデルやDB、検索索引は再構築しません。"
        )
    elif not venv_python.is_file():
        projected["required_action"] = "initial_setup"
        projected["message_ja"] = (
            "Local RAGの仮想環境が見つかりません。初期設定を実行してください。"
        )
    return projected


def _install_setup_gate_projection() -> None:
    original = search_command._resolve_source_uris

    def projected_resolver(*args: Any, **kwargs: Any) -> dict[str, Any]:
        resolved = original(*args, **kwargs)
        return _setup_gate_projection(resolved)

    search_command._resolve_source_uris = projected_resolver


install_result_bundle_reference_contract()
install_search_command_reference_contract(search_command)
_install_setup_gate_projection()


if __name__ == "__main__":
    raise SystemExit(search_command.main())
