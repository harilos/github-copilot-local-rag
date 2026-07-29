from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import catalog
from .jsonl import read_jsonl
from .paths import clean_dir, index_dir, logs_dir
from .store import delete_ids


def delete_source_data(source_id: str) -> dict[str, Any]:
    """Remove one Source from vector, catalog, clean records, and ADD state.

    The operation is deliberately idempotent.  A failed run can be repeated;
    each phase uses exact ``source_id`` equality and never removes another
    Source merely because a path or filename matches.
    """
    value = str(source_id or "")
    if not value or any(character in value for character in "\x00\r\n"):
        raise ValueError("source_id is required and must not contain controls")

    state = _read_json_object(logs_dir() / "index_state.json", default={})
    state_files = state.get("files")
    state_files = state_files if isinstance(state_files, dict) else {}
    matching_state_keys = [
        key
        for key, item in state_files.items()
        if isinstance(item, dict)
        and str(item.get("source_id") or "") == value
    ]
    state_record_ids = {
        str(record_id)
        for key in matching_state_keys
        for record_id in (
            state_files[key].get("record_ids") or []
            if isinstance(state_files[key].get("record_ids"), list)
            else []
        )
        if str(record_id)
    }

    clean_actions = _plan_clean_deletion(value)
    catalog_ids = set(catalog.source_chunk_ids(value))
    record_ids = sorted(catalog_ids | state_record_ids)

    # Vector deletion is first.  If a later phase fails, state and/or catalog
    # IDs allow an immediate retry and Chroma deletion is idempotent.
    deleted_vector_records = delete_ids(record_ids) if record_ids else 0
    catalog_result = catalog.delete_source_documents(value)
    deleted_clean_files, rewritten_clean_files = _apply_clean_deletion(
        clean_actions
    )

    for key in matching_state_keys:
        state_files.pop(key, None)
    if isinstance(state, dict) and state:
        state["files"] = state_files
        ingestion = state.get("ingestion")
        if (
            isinstance(ingestion, dict)
            and str(ingestion.get("source_id") or "") == value
        ):
            state["ingestion"] = {}
        state["updated_at"] = _now()
        _atomic_json_write(logs_dir() / "index_state.json", state)
    _remove_source_errors(value)
    _update_manifest_count(int(catalog.counts().get("chunks") or 0))
    return {
        "status": "deleted",
        "source_id": value,
        "documents_deleted": int(catalog_result["documents"]),
        "chunks_deleted": int(catalog_result["chunks"]),
        "vector_records_requested": len(record_ids),
        "vector_records_deleted": deleted_vector_records,
        "clean_files_deleted": deleted_clean_files,
        "clean_files_rewritten": rewritten_clean_files,
        "state_entries_deleted": len(matching_state_keys),
    }


def _plan_clean_deletion(
    source_id: str,
) -> list[tuple[Path, list[dict[str, Any]] | None]]:
    root = clean_dir()
    if not root.is_dir():
        return []
    actions: list[tuple[Path, list[dict[str, Any]] | None]] = []
    for path in sorted(root.rglob("*.jsonl")):
        if path.is_symlink():
            raise RuntimeError(
                f"clean record path must not be a symlink: {path.name}"
            )
        records = list(read_jsonl(path))
        matching = [
            record
            for record in records
            if _record_source_id(record) == source_id
        ]
        if not matching:
            continue
        remaining = [
            record
            for record in records
            if _record_source_id(record) != source_id
        ]
        actions.append((path, remaining or None))
    return actions


def _apply_clean_deletion(
    actions: Iterable[tuple[Path, list[dict[str, Any]] | None]],
) -> tuple[int, int]:
    deleted = 0
    rewritten = 0
    for path, remaining in actions:
        if remaining is None:
            path.unlink(missing_ok=True)
            deleted += 1
            _remove_empty_parents(path.parent, clean_dir())
            continue
        encoded = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in remaining
        ).encode("utf-8")
        _atomic_bytes_write(path, encoded)
        rewritten += 1
    return deleted, rewritten


def _record_source_id(record: dict[str, Any]) -> str:
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and metadata.get("source_id") is not None:
        return str(metadata.get("source_id") or "")
    return str(record.get("source_id") or "")


def _remove_source_errors(source_id: str) -> None:
    path = logs_dir() / "prepare_errors.json"
    payload = _read_json_value(path, default=[])
    if not isinstance(payload, list):
        return
    remaining = [
        value
        for value in payload
        if not isinstance(value, dict)
        or str(value.get("source_id") or "") != source_id
    ]
    if remaining != payload:
        _atomic_json_write(path, remaining)


def _update_manifest_count(record_count: int) -> None:
    path = index_dir() / "manifest.json"
    payload = _read_json_object(path, default={})
    if not payload:
        return
    payload["record_count"] = int(record_count)
    payload["generated_at"] = _now()
    _atomic_json_write(path, payload)


def _read_json_object(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    value = _read_json_value(path, default=default)
    return value if isinstance(value, dict) else dict(default)


def _read_json_value(path: Path, *, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _atomic_json_write(path: Path, payload: Any) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_bytes_write(path, encoded)


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _remove_empty_parents(path: Path, boundary: Path) -> None:
    boundary = boundary.resolve()
    current = path
    while current != boundary:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
