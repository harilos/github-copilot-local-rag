from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ingestion_paths import IngestionScope, resolve_ingestion_scope
from .jsonl import write_jsonl
from .manifest import write_manifest
from .paths import clean_dir, logs_dir
from .profile import update_profile_from_clean
from .progress import emit_event, write_progress
from .records import build_records_for_file, file_content_hash, iter_input_files, sha256_text
from .catalog import delete_chunks as delete_catalog_chunks, reset_catalog, upsert_records as upsert_catalog_records
from .store import collection_count, delete_ids, reset_collection, upsert_records


def add_or_update_root(
    root: Path,
    source_id: str,
    scan_subdir: str | None = None,
    include_root_name_in_path: bool = True,
    batch_size_files: int = 20,
    reset_db: bool = False,
    reset_clean: bool = False,
    retry_errors: bool = False,
    operation: str = "add",
    chunk_max_chars: int = 1400,
    chunk_overlap: int = 160,
    resume: bool = False,
) -> dict[str, Any]:
    if batch_size_files <= 0:
        raise ValueError("batch_size_files must be positive")
    if chunk_max_chars <= 0:
        raise ValueError("chunk_max_chars must be positive")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must be zero or positive")
    if resume and (reset_db or reset_clean):
        raise ValueError(
            "resume cannot be combined with reset_db or reset_clean"
        )

    if include_root_name_in_path is not True:
        raise ValueError("root-name inclusion is mandatory")
    scope = resolve_ingestion_scope(root, scan_subdir)
    if reset_clean:
        _reset_clean_dir()
        state = _initial_state()
    else:
        state = _load_state()
    if resume:
        _validate_resume_state(state, scope, source_id)
    state["ingestion"] = scope.state_fields(source_id=source_id)
    # Persist the effective scope before discovery.  If scanning is
    # interrupted, a later --resume must still validate against the exact
    # root/source/scope that started the run.
    _save_state(state)
    scope_fields = scope.state_fields(source_id=source_id)

    started_at = datetime.now(timezone.utc).isoformat()
    write_progress(
        status="running",
        phase="scan",
        operation=operation,
        **scope_fields,
        batch_size_files=batch_size_files,
        reset_db=reset_db,
        reset_clean=reset_clean,
        retry_errors=retry_errors,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        files_total=0,
        files_done=0,
        indexed_files=0,
        skipped_files=0,
        error_files=0,
        upserted_records=0,
        deleted_records=0,
        started_at=started_at,
        completed_at="",
        last_error="",
    )
    emit_event(
        "run_started",
        operation=operation,
        **scope_fields,
        reset_db=reset_db,
        reset_clean=reset_clean,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
    )

    if reset_db:
        reset_collection()
        reset_catalog()
        emit_event("collection_reset")

    files = list(iter_input_files(scope.scan_root))
    discovered_keys = {
        _state_key(source_id, scope.file(path).stored_path)
        for path in files
    }
    summary = {
        "operation": operation,
        **scope_fields,
        "file_count": len(files),
        "indexed_files": 0,
        "skipped_files": 0,
        "error_files": 0,
        "upserted_records": 0,
        "deleted_records": 0,
    }
    write_progress(status="running", phase="extract", files_total=len(files))

    try:
        pending: list[dict[str, Any]] = []
        force_index = reset_db or reset_clean
        for path in files:
            rel = scope.file(path).stored_path
            write_progress(status="running", phase="extract", current_file=rel)
            item = _prepare_file(
                scope,
                path,
                source_id,
                state,
                retry_errors,
                force_index=force_index,
                chunk_max_chars=chunk_max_chars,
                chunk_overlap=chunk_overlap,
            )
            status = item["status"]
            if status == "skip":
                summary["skipped_files"] += 1
                write_progress(
                    status="running",
                    phase="extract",
                    files_done=_files_done(summary),
                    skipped_files=summary["skipped_files"],
                    current_file=rel,
                )
                continue
            if status == "error":
                _record_error(state, item)
                _save_state(state)
                summary["error_files"] += 1
                write_progress(
                    status="running",
                    phase="extract",
                    files_done=_files_done(summary),
                    error_files=summary["error_files"],
                    current_file=rel,
                    last_error=item["error"],
                )
                emit_event("file_failed", path=rel, error=item["error"])
                print(f"ERROR {item['rel']}: {item['error']}")
                continue

            pending.append(item)
            emit_event("file_extracted", path=rel, records=len(item["records"]))
            if len(pending) >= batch_size_files:
                _flush_batch(pending, state, summary, reset_db=reset_db)
                pending.clear()
                _save_state(state)
                print(_progress_line(summary))

        if pending:
            _flush_batch(pending, state, summary, reset_db=reset_db)
            pending.clear()
            _save_state(state)
            print(_progress_line(summary))

        reconciled = _reconcile_missing_files(
            state,
            scope=scope,
            source_id=source_id,
            discovered_keys=discovered_keys,
        )
        summary["deleted_records"] += reconciled["deleted_records"]
        summary["deleted_files"] = reconciled["deleted_files"]
        _save_state(state)

        write_progress(status="running", phase="verify", current_file="")
        count = collection_count()
        write_manifest(count)
        profile_updated = update_profile_from_clean()
        summary["collection_count"] = count
        summary["profile_updated"] = profile_updated
        summary["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_errors_report(state)
        write_progress(
            status="completed",
            phase="completed",
            files_done=_files_done(summary),
            collection_count=count,
            deleted_records=summary["deleted_records"],
            completed_at=summary["completed_at"],
            current_file="",
        )
        emit_event("run_completed", **summary)
        return summary
    except Exception as exc:
        write_progress(status="failed", phase="failed", last_error=f"{type(exc).__name__}: {exc}")
        emit_event("run_failed", error=f"{type(exc).__name__}: {exc}")
        raise


def _prepare_file(
    scope: IngestionScope,
    path: Path,
    source_id: str,
    state: dict[str, Any],
    retry_errors: bool,
    force_index: bool = False,
    chunk_max_chars: int = 1400,
    chunk_overlap: int = 160,
) -> dict[str, Any]:
    rel = scope.file(path).stored_path
    key = _state_key(source_id, rel)
    content_hash = file_content_hash(path)
    prev = state["files"].get(key)
    chunker_config = {"max_chars": chunk_max_chars, "overlap": chunk_overlap}

    if not force_index and prev and prev.get("content_hash") == content_hash and prev.get("chunker_config") == chunker_config:
        if prev.get("status") == "indexed":
            return {"status": "skip", "rel": rel}
        if prev.get("status") == "error" and not retry_errors:
            return {"status": "skip", "rel": rel}

    try:
        records = build_records_for_file(
            scope.logical_root,
            path,
            source_id=source_id,
            content_hash=content_hash,
            chunk_max_chars=chunk_max_chars,
            chunk_overlap=chunk_overlap,
            ingestion_scope=scope,
        )
    except Exception as exc:
        return {
            "status": "error",
            "key": key,
            "rel": rel,
            "source_id": source_id,
            "scan_subdir": scope.scan_subdir,
            "resolved_root": str(scope.resolved_root),
            "content_hash": content_hash,
            "chunker_config": chunker_config,
            "previous_record_ids": list((prev or {}).get("record_ids") or []),
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "status": "ready",
        "key": key,
        "rel": rel,
        "source_id": source_id,
        "scan_subdir": scope.scan_subdir,
        "resolved_root": str(scope.resolved_root),
        "content_hash": content_hash,
        "chunker_config": chunker_config,
        "previous_record_ids": list((prev or {}).get("record_ids") or []),
        "records": records,
    }


def _flush_batch(
    items: list[dict[str, Any]],
    state: dict[str, Any],
    summary: dict[str, Any],
    reset_db: bool,
) -> None:
    delete_targets: list[str] = []
    records: list[dict[str, Any]] = []
    batch_files = [str(item["rel"]) for item in items]
    for item in items:
        if not reset_db:
            delete_targets.extend(item["previous_record_ids"])
        records.extend(item["records"])

    write_progress(
        status="running",
        phase="delete",
        current_batch_files=batch_files,
        pending_records=len(records),
        files_done=_files_done(summary),
    )
    deleted = delete_ids(delete_targets)
    delete_catalog_chunks(delete_targets)
    write_progress(status="running", phase="embedding", deleted_records=summary["deleted_records"] + deleted)

    def on_upsert(done: int, total: int) -> None:
        write_progress(
            status="running",
            phase="embedding",
            current_batch_files=batch_files,
            current_batch_records_done=done,
            current_batch_records_total=total,
            upserted_records=summary["upserted_records"] + done,
        )

    upserted = upsert_records(records, progress_callback=on_upsert)
    write_progress(status="running", phase="catalog", upserted_records=summary["upserted_records"] + upserted)
    upsert_catalog_records(records)
    summary["deleted_records"] += deleted
    summary["upserted_records"] += upserted
    summary["indexed_files"] += len(items)
    write_progress(
        status="running",
        phase="save_state",
        indexed_files=summary["indexed_files"],
        deleted_records=summary["deleted_records"],
        upserted_records=summary["upserted_records"],
        files_done=_files_done(summary),
        current_batch_records_done=upserted,
        current_batch_records_total=len(records),
    )
    emit_event("batch_upserted", files=len(items), records=upserted, deleted_records=deleted)

    for item in items:
        record_path = _record_jsonl_path(item["source_id"], item["rel"])
        record_ids = [str(record["id"]) for record in item["records"]]
        write_jsonl(record_path, item["records"])
        state["files"][item["key"]] = {
            "source_id": item["source_id"],
            "path": item["rel"],
            "stored_path": item["rel"],
            "scan_subdir": item.get("scan_subdir") or ".",
            "resolved_root": item.get("resolved_root") or "",
            "content_hash": item["content_hash"],
            "chunker_config": item.get("chunker_config") or {},
            "record_ids": record_ids,
            "record_count": len(record_ids),
            "records_path": record_path.relative_to(clean_dir()).as_posix(),
            "status": "indexed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _record_error(state: dict[str, Any], item: dict[str, Any]) -> None:
    state["files"][item["key"]] = {
        "source_id": item["source_id"],
        "path": item["rel"],
        "stored_path": item["rel"],
        "scan_subdir": item.get("scan_subdir") or ".",
        "resolved_root": item.get("resolved_root") or "",
        "content_hash": item["content_hash"],
        "record_ids": item["previous_record_ids"],
        "record_count": len(item["previous_record_ids"]),
        "status": "error",
        "error": item["error"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _initial_state() -> dict[str, Any]:
    return {"version": 2, "files": {}, "ingestion": {}}


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _initial_state()
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    version = int(data.get("version") or 1)
    if "files" not in data or not isinstance(data["files"], dict):
        data["files"] = {}
    if version < 2 and data["files"]:
        raise RuntimeError(
            "Existing index state uses pre-root-prefixed document paths. "
            "Rebuild the database once with --force-rebuild."
        )
    data["version"] = 2
    if not isinstance(data.get("ingestion"), dict):
        data["ingestion"] = {}
    return data


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _state_path() -> Path:
    return logs_dir() / "index_state.json"


def _write_errors_report(state: dict[str, Any]) -> None:
    errors = [
        {
            "source_id": item.get("source_id"),
            "path": item.get("path"),
            "error": item.get("error"),
            "updated_at": item.get("updated_at"),
        }
        for item in state.get("files", {}).values()
        if item.get("status") == "error"
    ]
    path = logs_dir() / "prepare_errors.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _record_jsonl_path(source_id: str, rel: str) -> Path:
    safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id).strip("._-") or "local"
    name = sha256_text(f"{source_id}:{rel}")[:24] + ".jsonl"
    return clean_dir() / "records" / safe_source / name


def _state_key(source_id: str, rel: str) -> str:
    return f"{source_id}:{rel}"


def _validate_resume_state(
    state: dict[str, Any],
    scope: IngestionScope,
    source_id: str,
) -> None:
    saved = state.get("ingestion")
    if not isinstance(saved, dict) or not saved:
        return
    expected = scope.state_fields(source_id=source_id)
    mismatches = [
        key
        for key in ("resolved_root", "source_id", "scan_subdir")
        if str(saved.get(key) or "") != str(expected.get(key) or "")
    ]
    if mismatches:
        raise ValueError(
            "resume settings do not match saved index state: "
            + ", ".join(mismatches)
        )


def _reconcile_missing_files(
    state: dict[str, Any],
    *,
    scope: IngestionScope,
    source_id: str,
    discovered_keys: set[str],
) -> dict[str, int]:
    missing_keys: list[str] = []
    record_ids: list[str] = []
    record_paths: list[Path] = []
    for key, item in list(state.get("files", {}).items()):
        if not isinstance(item, dict):
            continue
        stored_path = str(
            item.get("stored_path") or item.get("path") or ""
        )
        if (
            str(item.get("source_id") or "") != source_id
            or str(item.get("resolved_root") or "")
            != str(scope.resolved_root)
            or not scope.contains_stored_path(stored_path)
            or key in discovered_keys
        ):
            continue
        missing_keys.append(key)
        record_ids.extend(
            str(value) for value in item.get("record_ids") or []
        )
        record_paths.append(_record_jsonl_path(source_id, stored_path))

    if not missing_keys:
        return {"deleted_files": 0, "deleted_records": 0}
    deleted = delete_ids(record_ids)
    delete_catalog_chunks(record_ids)
    clean_root = clean_dir().resolve()
    for record_path in record_paths:
        try:
            resolved = record_path.expanduser().resolve()
            resolved.relative_to(clean_root)
        except (OSError, ValueError):
            continue
        resolved.unlink(missing_ok=True)
    for key in missing_keys:
        state["files"].pop(key, None)
    emit_event(
        "scope_reconciled",
        source_id=source_id,
        scan_subdir=scope.scan_subdir,
        deleted_files=len(missing_keys),
        deleted_records=deleted,
    )
    return {
        "deleted_files": len(missing_keys),
        "deleted_records": deleted,
    }


def _reset_clean_dir() -> None:
    directory = clean_dir()
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    reset_catalog()


def _progress_line(summary: dict[str, Any]) -> str:
    return (
        "PROGRESS "
        f"indexed_files={summary['indexed_files']} "
        f"skipped_files={summary['skipped_files']} "
        f"error_files={summary['error_files']} "
        f"upserted_records={summary['upserted_records']} "
        f"deleted_records={summary['deleted_records']}"
    )


def _files_done(summary: dict[str, Any]) -> int:
    return int(summary["indexed_files"]) + int(summary["skipped_files"]) + int(summary["error_files"])
