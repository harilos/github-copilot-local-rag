#!/usr/bin/env python3
from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


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
_SELF_HEAL_ACTIVE_ENV = "LOCAL_RAG_SETUP_GATE_SELF_HEAL_ACTIVE"
_SELF_HEAL_DISABLED_ENV = "LOCAL_RAG_DISABLE_SETUP_GATE_SELF_HEAL"
_SELF_HEAL_TIMEOUT_SECONDS = 180.0
_DETAIL_OPTIONS = frozenset(
    {
        "--result-set-id",
        "--item-id",
        "--detail-level",
    }
)


def _venv_python() -> Path:
    return _QUERY_ROOT / ".venv" / (
        "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
    )


def _completion_marker() -> Path:
    return _QUERY_ROOT / ".venv" / ".rag-deps-installed"


def _truthy_environment(name: str) -> bool:
    return os.getenv(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _argument_names(arguments: Sequence[str]) -> set[str]:
    return {
        str(argument).split("=", 1)[0]
        for argument in arguments
        if str(argument).startswith("-")
    }


def _should_self_heal(arguments: Sequence[str]) -> bool:
    names = _argument_names(arguments)
    if _truthy_environment(_SELF_HEAL_ACTIVE_ENV):
        return False
    if _truthy_environment(_SELF_HEAL_DISABLED_ENV):
        return False
    if "-h" in names or "--help" in names:
        return False
    if names & _DETAIL_OPTIONS:
        return False
    return "--db" in names or "--auto" in names


def _self_heal_lookup_gate(
    arguments: Sequence[str] | None = None,
) -> bool:
    """Repair only a stale lookup marker before a normal public search.

    The repair is deliberately silent. It performs the existing offline
    runtime verification, never installs packages or rebuilds a model or DB,
    and rechecks the marker as the source of truth. Concurrent repair attempts
    are harmless because a valid marker after the subprocess exits is accepted
    regardless of that subprocess's return code.
    """

    effective_arguments = list(
        sys.argv[1:] if arguments is None else arguments
    )
    if not _should_self_heal(effective_arguments):
        return False

    marker = _completion_marker()
    marker_valid, _marker_reason = completion_contract_valid(marker, RAG_ROOT)
    if marker_valid:
        return False

    python = _venv_python()
    if not python.is_file():
        return False

    environment = os.environ.copy()
    environment[_SELF_HEAL_ACTIVE_ENV] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    try:
        subprocess.run(
            [
                str(python),
                str(_QUERY_ROOT / "setup.py"),
                "--repair-completion-marker",
                "--format",
                "json",
            ],
            check=False,
            cwd=str(RAG_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_SELF_HEAL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    repaired, _reason = completion_contract_valid(marker, RAG_ROOT)
    return repaired


def _setup_gate_projection(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("status") or "") != "setup_required":
        return payload

    projected = copy.deepcopy(payload)
    venv_python = _venv_python()
    marker = _completion_marker()
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
            f"is not valid ({marker_reason}). Automatic repair did not restore "
            "the gate; run the temporary completion-marker repair. It does not "
            "reinstall the model or rebuild a DB."
        )
        projected["message_ja"] = (
            "検索利用判定を自動修復できませんでした。"
            f"原因: {marker_reason}。{TEMPORARY_REPAIR_LABEL_JA}を実行してください。"
            "モデルやDB、検索索引は再構築しません。"
        )
    elif not venv_python.is_file():
        projected["required_action"] = "initial_setup"
        projected["message_ja"] = (
            "Local RAGの仮想環境が見つかりません。初期設定を実行してください。"
        )
    return projected


def _install_setup_gate_projection() -> None:
    marker_name = "_local_rag_setup_gate_projection_installed"
    if bool(getattr(search_command, marker_name, False)):
        return

    original = search_command._resolve_source_uris

    def projected_resolver(*args: Any, **kwargs: Any) -> dict[str, Any]:
        resolved = original(*args, **kwargs)
        return _setup_gate_projection(resolved)

    search_command._resolve_source_uris = projected_resolver
    setattr(search_command, marker_name, True)


def main() -> int:
    _self_heal_lookup_gate()
    result = search_command.main()
    return int(result) if result is not None else 0


install_result_bundle_reference_contract()
install_search_command_reference_contract(search_command)
_install_setup_gate_projection()


if __name__ == "__main__":
    raise SystemExit(main())
