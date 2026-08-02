from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

COMPLETION_SCHEMA = "local-rag.setup-completion.v1"
REQUIRED_RUNTIME_PASSES = (
    "venv",
    "dependencies",
    "requirements",
    "pip_check",
    "model_files",
    "model_manifest",
    "model_load",
    "list_dbs",
)
EXPECTED_EMBEDDING_DIMENSION = 256
_COPY_RESTORE_SKIP_ARGUMENTS = frozenset(
    {
        "--verify-only",
        "--migrate-legacy-marker",
        "--refresh-completion-marker",
        "--repair-completion-marker",
    }
)
COPY_RESTORE_RESULT: dict[str, Any] | None = None


def completion_marker_for(query_root: Path) -> Path:
    """Resolve the completion state for a normal managed setup runtime."""

    return query_root / ".venv" / ".rag-deps-installed"


def _restore_copy_only_installation_for_setup() -> None:
    """Perform post-copy path rebinding only during a modifying setup run."""

    global COPY_RESTORE_RESULT
    if Path(sys.argv[0]).name.casefold() != "setup.py":
        return
    if any(argument in _COPY_RESTORE_SKIP_ARGUMENTS for argument in sys.argv[1:]):
        return
    try:
        from setup_copy import restore_copied_installation

        COPY_RESTORE_RESULT = restore_copied_installation(
            Path(__file__).resolve().parents[1]
        )
    except Exception as exc:
        # Setup itself still performs its normal structured verification.  Keep
        # copy restoration diagnostics non-secret and machine-readable without
        # corrupting setup.py JSON stdout during module import.
        COPY_RESTORE_RESULT = {
            "status": "warning",
            "error_kind": type(exc).__name__,
        }


_restore_copy_only_installation_for_setup()


def requirements_fingerprint(rag_root: Path) -> str:
    paths = (
        rag_root / "query" / "requirements.txt",
        rag_root
        / "gen_db"
        / "software_rag_tool"
        / "requirements.txt",
    )
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(rag_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def completion_contract_valid(
    marker: Path,
    rag_root: Path,
) -> tuple[bool, str | None]:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False, "completion_marker_not_object"
        if payload.get("schema") != COMPLETION_SCHEMA:
            return False, "completion_marker_schema"
        if payload.get("status") != "complete":
            return False, "completion_marker_status"
        if payload.get("requirements_sha256") != requirements_fingerprint(
            rag_root
        ):
            return False, "completion_marker_requirements"
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            return False, "completion_marker_runtime"
        for key in REQUIRED_RUNTIME_PASSES:
            if runtime.get(key) != "pass":
                return False, f"completion_marker_{key}"
        if (
            runtime.get("embedding_dimension")
            != EXPECTED_EMBEDDING_DIMENSION
        ):
            return False, "completion_marker_embedding_dimension"
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False, "completion_marker_unreadable"
    return True, None


def completion_contract_payload(
    *,
    runtime: dict[str, Any],
    rag_root: Path,
    verified_at: str,
) -> dict[str, Any]:
    payload = {
        "schema": COMPLETION_SCHEMA,
        "status": "complete",
        "verified_at": verified_at,
        "requirements_sha256": requirements_fingerprint(rag_root),
        "runtime": runtime,
    }
    return payload
