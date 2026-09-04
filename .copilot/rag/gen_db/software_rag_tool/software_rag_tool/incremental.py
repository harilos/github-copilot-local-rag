from __future__ import annotations

import errno
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .atomic_io import atomic_write_json
from .ingestion_paths import IngestionScope, resolve_ingestion_scope
from .jsonl import write_jsonl
from .manifest import validate_existing_index_tokenizer, write_manifest
from .paths import clean_dir, logs_dir
from .profile import update_profile_from_clean
from .progress import emit_event, write_progress, observability_run
from .records import (
    SUPPORTED_EXTENSIONS,
    build_records_for_file,
    file_content_hash,
    is_office_temporary_file,
    iter_input_files,
    sha256_text,
)
from .catalog import delete_chunks as delete_catalog_chunks, reset_catalog, upsert_records as upsert_catalog_records
from .config import DEFAULT_INGESTION_BATCH_SIZE_FILES
from .embeddings import DocumentTokenBudget, get_document_token_budget
from .store import collection_count, delete_ids, reset_collection, upsert_records
from .records import chunker_config
from .tokenize import require_index_tokenizer


@observability_run
def add_or_update_root(
    root: Path,
    source_id: str,
    scan_subdir: str | None = None,
    include_root_name_in_path: bool = True,
    batch_size_files: int | None = None,
    reset_db: bool = False,
    reset_clean: bool = False,
    retry_errors: bool = False,
    operation: str = "add",
    chunk_max_chars: int = 1400,
    chunk_overlap: int = 160,
    resume: bool = False,
    document_token_budget: DocumentTokenBudget | None = None,
    privacy_safe_root: bool = False,
    persistent_root_identity: str | None = None,
) -> dict[str, Any]:
    if chunk_max_chars <= 0:
        raise ValueError("chunk_max_chars must be positive")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must be zero or positive")
    if chunk_overlap >= chunk_max_chars:
        raise ValueError("chunk_overlap must be smaller than chunk_max_chars")
    if resume and (reset_db or reset_clean):
        raise ValueError(
            "resume cannot be combined with reset_db or reset_clean"
        )

    if include_root_name_in_path is not True:
        raise ValueError("root-name inclusion is mandatory")
    # Tokenizer availability is an index-generation precondition.  Resolve it
    # after pure argument validation but before reset, state/progress, vector,
    # catalog, or manifest writes.
    require_index_tokenizer()
    if not reset_db:
        validate_existing_index_tokenizer()
    token_budget = document_token_budget or get_document_token_budget()
    current_chunker_config = chunker_config(
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        document_token_budget=token_budget,
    )
    scope = resolve_ingestion_scope(root, scan_subdir)
    if reset_db:
        # Vector reset is the destructive gate.  Do it before clean/state or
        # catalog mutation so a lock, permission, corruption, or client error
        # cannot leave a mixed old/new generation behind.
        reset_collection()
        reset_catalog()
    if reset_clean:
        _reset_clean_dir()
        state = _initial_state()
    else:
        state = _load_state()
    persistent_scope_fields = _persistent_scope_fields(
        scope,
        source_id=source_id,
        privacy_safe_root=privacy_safe_root,
    )
    if persistent_root_identity is not None:
        persistent_scope_fields["resolved_root"] = str(persistent_root_identity)
    if privacy_safe_root:
        _migrate_private_scope_paths(
            state,
            scope=scope,
            source_id=source_id,
            persistent_root_identity=str(
                persistent_scope_fields["resolved_root"]
            ),
        )
    effective_batch_size_files = _effective_batch_size_files(
        state,
        requested=batch_size_files,
        resume=resume,
    )
    if resume:
        _validate_resume_state(
            state,
            scope,
            source_id,
            effective_batch_size_files,
            privacy_safe_root=privacy_safe_root,
        )
    state["ingestion"] = {
        **persistent_scope_fields,
        "batch_size_files": effective_batch_size_files,
        "operation": operation,
        "chunk_max_chars": chunk_max_chars,
        "chunk_overlap": chunk_overlap,
    }
    # This is the durable recovery/rebuild authority, independent of the
    # best-effort progress snapshot.  Save before discovery or any progress
    # emission so interrupted runs retain their effective extraction scope.
    _save_state(state)
    scope_fields = {
        **persistent_scope_fields,
        "batch_size_files": effective_batch_size_files,
    }

    started_at = datetime.now(timezone.utc).isoformat()
    write_progress(
        status="running",
        phase="scan",
        operation=operation,
        **scope_fields,
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
        input_error_files=0,
        extract_error_files=0,
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
        emit_event("collection_reset")

    try:
        files = list(iter_input_files(scope.scan_root))
        unsupported_paths = _unsupported_input_paths(scope)
    except Exception as exc:
        error_text = _run_failure_text(
            exc,
            scope=scope,
            privacy_safe_root=privacy_safe_root,
        )
        write_progress(status="failed", phase="failed", last_error=error_text)
        emit_event("run_failed", error=error_text)
        if privacy_safe_root:
            raise RuntimeError(error_text) from None
        raise
    discovered_keys = {
        _state_key(source_id, _safe_stored_path(scope, path))
        for path in files
    }
    summary = {
        "operation": operation,
        **scope_fields,
        "file_count": len(files),
        "indexed_files": 0,
        "skipped_files": 0,
        "error_files": 0,
        "input_error_files": 0,
        "extract_error_files": 0,
        "error_details": [],
        "ingestion_diagnostics": {
            "unsupported": {
                "count": len(unsupported_paths),
                "paths": unsupported_paths[:100],
            },
            "zero_text": {"count": 0, "paths": []},
            "extraction_error": {"count": 0, "paths": []},
        },
        "upserted_records": 0,
        "deleted_records": 0,
    }
    write_progress(status="running", phase="extract", files_total=len(files))

    try:
        pending: list[dict[str, Any]] = []
        force_index = reset_db or reset_clean
        for path in files:
            rel = _safe_stored_path(scope, path)
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
                document_token_budget=token_budget,
                current_chunker_config=current_chunker_config,
                persistent_root_identity=str(
                    persistent_scope_fields["resolved_root"]
                ),
            )
            status = item["status"]
            if status == "skip":
                key = _state_key(source_id, rel)
                previous = state["files"].get(key) or {}
                if (
                    previous.get("status") == "indexed"
                    and int(previous.get("record_count") or 0) == 0
                ):
                    _add_ingestion_diagnostic(summary, "zero_text", rel)
                    _mark_legacy_zero_text(previous, rel)
                    state["files"][key] = previous
                    _save_state(state)
                elif (
                    previous.get("status") == "error"
                    and previous.get("error_kind") != "input_read"
                ):
                    category = (
                        "zero_text"
                        if previous.get("error_kind") == "zero_text"
                        else "extraction_error"
                    )
                    _add_ingestion_diagnostic(summary, category, rel)
                else:
                    summary["skipped_files"] += 1
                    write_progress(
                        status="running",
                        phase="extract",
                        files_done=_files_done(summary),
                        skipped_files=summary["skipped_files"],
                        current_file=rel,
                    )
                    continue
                _record_persisted_ingestion_failure(
                    summary,
                    previous,
                    rel,
                )
                write_progress(
                    status="running",
                    phase="extract",
                    files_done=_files_done(summary),
                    error_files=summary["error_files"],
                    extract_error_files=summary["extract_error_files"],
                    current_file=rel,
                    last_error=str(previous.get("error") or ""),
                )
                continue
            if status == "error":
                _record_error(state, item)
                _save_state(state)
                summary["error_files"] += 1
                error_kind = str(item.get("error_kind") or "extract")
                if error_kind == "input_read":
                    summary["input_error_files"] += 1
                else:
                    summary["extract_error_files"] += 1
                    _add_ingestion_diagnostic(
                        summary,
                        "extraction_error",
                        rel,
                    )
                if len(summary["error_details"]) < 100:
                    summary["error_details"].append(
                        dict(item.get("diagnostic") or {})
                    )
                write_progress(
                    status="running",
                    phase="extract",
                    files_done=_files_done(summary),
                    error_files=summary["error_files"],
                    input_error_files=summary["input_error_files"],
                    extract_error_files=summary["extract_error_files"],
                    current_file=rel,
                    last_error=item["error"],
                )
                emit_event(
                    "file_failed",
                    path=rel,
                    error=item["error"],
                    diagnostic=item.get("diagnostic") or {},
                )
                print(f"ERROR {item['rel']}: {item['error']}")
                continue

            if not item["records"]:
                _add_ingestion_diagnostic(summary, "zero_text", rel)
                diagnostic = _zero_text_diagnostic(rel)
                deleted = _record_zero_text(state, item, diagnostic)
                _save_state(state)
                summary["deleted_records"] += deleted
                summary["error_files"] += 1
                summary["extract_error_files"] += 1
                if len(summary["error_details"]) < 100:
                    summary["error_details"].append(diagnostic)
                write_progress(
                    status="running",
                    phase="extract",
                    files_done=_files_done(summary),
                    error_files=summary["error_files"],
                    extract_error_files=summary["extract_error_files"],
                    deleted_records=summary["deleted_records"],
                    current_file=rel,
                    last_error="No searchable text was extracted.",
                )
                emit_event(
                    "file_failed",
                    path=rel,
                    error="No searchable text was extracted.",
                    diagnostic=diagnostic,
                )
                continue
            pending.append(item)
            emit_event("file_extracted", path=rel, records=len(item["records"]))
            if len(pending) >= effective_batch_size_files:
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
            persistent_root_identity=str(
                persistent_scope_fields["resolved_root"]
            ),
        )
        summary["deleted_records"] += reconciled["deleted_records"]
        summary["deleted_files"] = reconciled["deleted_files"]
        _save_state(state)

        write_progress(status="running", phase="verify", current_file="")
        count = collection_count()
        write_manifest(count, chunker_config=current_chunker_config)
        profile_updated = update_profile_from_clean()
        summary["collection_count"] = count
        summary["profile_updated"] = profile_updated
        summary["completed_at"] = datetime.now(timezone.utc).isoformat()
        summary["result_status"] = _result_status(summary)
        if summary["result_status"] == "partial":
            summary["warning_ja"] = (
                f"{summary['input_error_files']:,}件のファイルを読み取れませんでした。"
                "読めたファイルは反映済みで、失敗ファイルは次回自動再試行します。"
            )
        _write_errors_report(state)
        write_progress(
            status=summary["result_status"],
            phase="completed",
            files_done=_files_done(summary),
            error_files=summary["error_files"],
            input_error_files=summary["input_error_files"],
            extract_error_files=summary["extract_error_files"],
            result_status=summary["result_status"],
            collection_count=count,
            deleted_records=summary["deleted_records"],
            completed_at=summary["completed_at"],
            current_file="",
        )
        emit_event(
            {
                "success": "run_completed",
                "partial": "run_partial",
                "failure": "run_failed",
            }[summary["result_status"]],
            **summary,
        )
        return summary
    except Exception as exc:
        error_text = _run_failure_text(
            exc,
            scope=scope,
            privacy_safe_root=privacy_safe_root,
        )
        write_progress(status="failed", phase="failed", last_error=error_text)
        emit_event("run_failed", error=error_text)
        if privacy_safe_root:
            # Prevent the uncaught child-process traceback from reintroducing
            # the external SharePoint root after persisted diagnostics were
            # sanitized above.
            raise RuntimeError(error_text) from None
        raise


def _unsupported_input_paths(scope: IngestionScope) -> list[str]:
    paths: list[str] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, child_directories, filenames in os.walk(
        scope.scan_root,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        child_directories[:] = sorted(
            name for name in child_directories if name not in {".git", ".svn"}
        )
        for filename in sorted(filenames):
            if is_office_temporary_file(filename):
                continue
            path = Path(directory) / filename
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                paths.append(_safe_stored_path(scope, path))
    return sorted(paths)


def _add_ingestion_diagnostic(
    summary: dict[str, Any],
    category: str,
    path: str,
) -> None:
    diagnostic = summary["ingestion_diagnostics"][category]
    diagnostic["count"] += 1
    if len(diagnostic["paths"]) < 100:
        diagnostic["paths"].append(path)


def _zero_text_diagnostic(path: str) -> dict[str, Any]:
    return {
        "path": str(path)[:2_048],
        "stage": "extract",
        "error_type": "ZeroText",
        "retryable": False,
    }


def _mark_legacy_zero_text(previous: dict[str, Any], path: str) -> None:
    previous.update(
        {
            "status": "error",
            "error": "No searchable text was extracted.",
            "error_kind": "zero_text",
            "retryable": False,
            "diagnostic": _zero_text_diagnostic(path),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _record_persisted_ingestion_failure(
    summary: dict[str, Any],
    previous: dict[str, Any],
    path: str,
) -> None:
    summary["error_files"] += 1
    summary["extract_error_files"] += 1
    diagnostic = previous.get("diagnostic")
    if not isinstance(diagnostic, dict):
        diagnostic = (
            _zero_text_diagnostic(path)
            if previous.get("error_kind") == "zero_text"
            else {"path": str(path)[:2_048], "stage": "extract"}
        )
    if len(summary["error_details"]) < 100:
        summary["error_details"].append(dict(diagnostic))


def _record_zero_text(
    state: dict[str, Any],
    item: dict[str, Any],
    diagnostic: dict[str, Any],
) -> int:
    previous_record_ids = list(item.get("previous_record_ids") or [])
    deleted = delete_ids(previous_record_ids)
    delete_catalog_chunks(previous_record_ids)
    record_path = _record_jsonl_path(item["source_id"], item["rel"])
    write_jsonl(record_path, [])
    state["files"][item["key"]] = {
        "source_id": item["source_id"],
        "path": item["rel"],
        "stored_path": item["rel"],
        "scan_subdir": item.get("scan_subdir") or ".",
        "resolved_root": item.get("resolved_root") or "",
        "content_hash": item["content_hash"],
        "chunker_config": item.get("chunker_config") or {},
        "record_ids": [],
        "record_count": 0,
        "records_path": record_path.relative_to(clean_dir()).as_posix(),
        "status": "error",
        "error": "No searchable text was extracted.",
        "error_kind": "zero_text",
        "retryable": False,
        "diagnostic": dict(diagnostic),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return deleted


def _prepare_file(
    scope: IngestionScope,
    path: Path,
    source_id: str,
    state: dict[str, Any],
    retry_errors: bool,
    force_index: bool = False,
    chunk_max_chars: int = 1400,
    chunk_overlap: int = 160,
    document_token_budget: DocumentTokenBudget | None = None,
    current_chunker_config: dict[str, Any] | None = None,
    persistent_root_identity: str | None = None,
) -> dict[str, Any]:
    rel = _safe_stored_path(scope, path)
    key = _state_key(source_id, rel)
    prev = state["files"].get(key)
    token_budget = document_token_budget or get_document_token_budget()
    active_chunker_config = current_chunker_config or chunker_config(
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        document_token_budget=token_budget,
    )
    root_identity = persistent_root_identity or str(scope.resolved_root)

    try:
        stored = scope.file(path)
    except OSError as exc:
        return _error_item(
            key=key,
            rel=rel,
            source_id=source_id,
            scope=scope,
            root_identity=root_identity,
            active_chunker_config=active_chunker_config,
            previous=prev,
            exc=exc,
            stage="enumerate",
            error_kind="input_read",
        )

    try:
        content_hash = file_content_hash(stored.resolved_path)
    except OSError as exc:
        return _error_item(
            key=key,
            rel=rel,
            source_id=source_id,
            scope=scope,
            root_identity=root_identity,
            active_chunker_config=active_chunker_config,
            previous=prev,
            exc=exc,
            stage="hash-read",
            error_kind="input_read",
        )

    if not force_index and prev and prev.get("content_hash") == content_hash and prev.get("chunker_config") == active_chunker_config:
        if prev.get("status") == "indexed":
            return {"status": "skip", "rel": rel}
        if (
            prev.get("status") == "error"
            and prev.get("error_kind") != "input_read"
            and not retry_errors
        ):
            return {"status": "skip", "rel": rel}

    try:
        records = build_records_for_file(
            scope.logical_root,
            stored.resolved_path,
            source_id=source_id,
            content_hash=content_hash,
            chunk_max_chars=chunk_max_chars,
            chunk_overlap=chunk_overlap,
            ingestion_scope=scope,
            document_token_budget=token_budget,
        )
    except OSError as exc:
        input_read_error = _is_input_read_oserror(exc, stored.resolved_path)
        return _error_item(
            key=key,
            rel=rel,
            source_id=source_id,
            scope=scope,
            root_identity=root_identity,
            active_chunker_config=active_chunker_config,
            previous=prev,
            exc=exc,
            stage="extract-open" if input_read_error else "extract",
            error_kind="input_read" if input_read_error else "extract",
            failed_content_hash=content_hash,
        )
    except Exception as exc:
        return _error_item(
            key=key,
            rel=rel,
            source_id=source_id,
            scope=scope,
            root_identity=root_identity,
            active_chunker_config=active_chunker_config,
            previous=prev,
            exc=exc,
            stage="extract",
            error_kind="extract",
            failed_content_hash=content_hash,
        )

    return {
        "status": "ready",
        "key": key,
        "rel": rel,
        "source_id": source_id,
        "scan_subdir": scope.scan_subdir,
        "resolved_root": root_identity,
        "content_hash": content_hash,
        "chunker_config": active_chunker_config,
        "previous_record_ids": list((prev or {}).get("record_ids") or []),
        "records": records,
    }


def _error_item(
    *,
    key: str,
    rel: str,
    source_id: str,
    scope: IngestionScope,
    root_identity: str,
    active_chunker_config: dict[str, Any],
    previous: Any,
    exc: BaseException,
    stage: str,
    error_kind: str,
    failed_content_hash: str = "",
) -> dict[str, Any]:
    previous_value = previous if isinstance(previous, dict) else {}
    diagnostic = _safe_file_diagnostic(
        rel=rel,
        exc=exc,
        stage=stage,
        retryable=error_kind == "input_read",
    )
    return {
        "status": "error",
        "key": key,
        "rel": rel,
        "source_id": source_id,
        "scan_subdir": scope.scan_subdir,
        "resolved_root": root_identity,
        "content_hash": str(previous_value.get("content_hash") or ""),
        "failed_content_hash": failed_content_hash,
        "chunker_config": active_chunker_config,
        "previous_chunker_config": dict(
            previous_value.get("chunker_config") or {}
        ),
        "previous_record_ids": list(
            previous_value.get("record_ids") or []
        ),
        "previous_entry": dict(previous_value),
        "had_previous_entry": isinstance(previous, dict),
        "error_kind": error_kind,
        "retryable": error_kind == "input_read",
        "diagnostic": diagnostic,
        "error": _diagnostic_text(diagnostic),
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
    upsert_catalog_records(records, delete_ids=delete_targets)
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
    previous = item.get("previous_entry")
    value = dict(previous) if isinstance(previous, dict) else {}
    value.update({
        "source_id": item["source_id"],
        "path": item["rel"],
        "stored_path": item["rel"],
        "scan_subdir": item.get("scan_subdir") or ".",
        "resolved_root": item.get("resolved_root") or "",
        "content_hash": item["content_hash"],
        "failed_content_hash": item.get("failed_content_hash") or "",
        "chunker_config": (
            item.get("previous_chunker_config")
            if item.get("had_previous_entry")
            else item.get("chunker_config")
        )
        or {},
        "failed_chunker_config": item.get("chunker_config") or {},
        "record_ids": item["previous_record_ids"],
        "record_count": len(item["previous_record_ids"]),
        "status": "error",
        "error": item["error"],
        "error_kind": item.get("error_kind") or "extract",
        "retryable": bool(item.get("retryable")),
        "diagnostic": dict(item.get("diagnostic") or {}),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    state["files"][item["key"]] = value


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


def _effective_batch_size_files(
    state: dict[str, Any],
    *,
    requested: int | None,
    resume: bool,
) -> int:
    if requested is not None and requested <= 0:
        raise ValueError("batch_size_files must be positive")
    saved_ingestion = state.get("ingestion")
    saved = (
        saved_ingestion.get("batch_size_files")
        if isinstance(saved_ingestion, dict)
        else None
    )
    if resume and saved is not None:
        try:
            saved_value = int(saved)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "saved batch_size_files is invalid"
            ) from exc
        if saved_value <= 0:
            raise ValueError("saved batch_size_files must be positive")
        if requested is not None and requested != saved_value:
            raise ValueError(
                "resume settings do not match saved index state: "
                "batch_size_files"
            )
        return saved_value
    return requested or DEFAULT_INGESTION_BATCH_SIZE_FILES


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(path, state)


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
    atomic_write_json(path, errors, sort_keys=False)


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
    batch_size_files: int | None = None,
    privacy_safe_root: bool = False,
) -> None:
    saved = state.get("ingestion")
    if not isinstance(saved, dict) or not saved:
        return
    expected = _persistent_scope_fields(
        scope,
        source_id=source_id,
        privacy_safe_root=privacy_safe_root,
    )
    if batch_size_files is not None:
        expected["batch_size_files"] = batch_size_files
    keys = ["resolved_root", "source_id", "scan_subdir"]
    if "batch_size_files" in saved and batch_size_files is not None:
        keys.append("batch_size_files")
    mismatches = [
        key
        for key in keys
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
    persistent_root_identity: str | None = None,
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
            != str(persistent_root_identity or scope.resolved_root)
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


def _result_status(summary: dict[str, Any]) -> str:
    errors = int(summary.get("error_files") or 0)
    if errors == 0:
        return "success"
    completed = int(summary.get("indexed_files") or 0) + int(
        summary.get("skipped_files") or 0
    )
    if int(summary.get("extract_error_files") or 0) > 0 or completed == 0:
        return "failure"
    return "partial"


def _safe_stored_path(scope: IngestionScope, path: Path) -> str:
    """Build the normal portable path without opening or resolving the file."""

    candidate = Path(os.path.abspath(Path(path)))
    try:
        relative = candidate.relative_to(scope.resolved_root)
    except ValueError:
        relative = Path(candidate.name)
    return PurePosixPath(scope.root_display_name, *relative.parts).as_posix()


def _safe_file_diagnostic(
    *,
    rel: str,
    exc: BaseException,
    stage: str,
    retryable: bool,
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "path": str(rel)[:2_048],
        "stage": str(stage)[:80],
        "error_type": type(exc).__name__[:120],
        "retryable": bool(retryable),
    }
    error_number = getattr(exc, "errno", None)
    if isinstance(error_number, int):
        diagnostic["errno"] = error_number
    windows_error = getattr(exc, "winerror", None)
    if isinstance(windows_error, int):
        diagnostic["winerror"] = windows_error
    return diagnostic


def _is_input_read_oserror(exc: OSError, source_path: Path) -> bool:
    """Distinguish source-open failures from extractor workspace failures."""

    filenames = [
        value
        for value in (
            getattr(exc, "filename", None),
            getattr(exc, "filename2", None),
        )
        if value
    ]
    if not filenames:
        if isinstance(exc, FileNotFoundError):
            return True
        if isinstance(exc, PermissionError) and getattr(exc, "errno", None) in {
            errno.EACCES,
            errno.EPERM,
        }:
            return True
        return getattr(exc, "winerror", None) in _RETRYABLE_INPUT_WINERRORS
    expected = os.path.normcase(os.path.abspath(os.fspath(source_path)))
    for value in filenames:
        try:
            candidate = os.path.normcase(os.path.abspath(os.fsdecode(value)))
        except (TypeError, ValueError):
            continue
        if candidate == expected:
            return True
    return False


def _diagnostic_text(diagnostic: dict[str, Any]) -> str:
    values = [
        str(diagnostic.get("error_type") or "OSError"),
        f"stage={diagnostic.get('stage') or 'input-read'}",
        f"path={diagnostic.get('path') or ''}",
    ]
    if "errno" in diagnostic:
        values.append(f"errno={diagnostic['errno']}")
    if "winerror" in diagnostic:
        values.append(f"winerror={diagnostic['winerror']}")
    return " ".join(values)


def _private_root_identity(scope: IngestionScope) -> str:
    normalized = os.path.normcase(str(scope.resolved_root))
    return "sha256:" + sha256_text(normalized)


_CLOUD_FILES_WINERRORS = frozenset(
    {
        358,
        362,
        363,
        364,
        365,
        366,
        369,
        370,
        371,
        372,
        374,
        375,
        376,
        377,
        378,
        379,
        380,
        381,
        382,
        383,
        385,
        386,
        387,
        388,
        389,
        390,
        391,
        392,
        393,
        394,
        395,
        396,
        397,
        398,
    }
)
_RETRYABLE_INPUT_WINERRORS = frozenset({2, 3, 5, 32, 33}) | (
    _CLOUD_FILES_WINERRORS
)


def _run_failure_text(
    exc: BaseException,
    *,
    scope: IngestionScope,
    privacy_safe_root: bool,
) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if not privacy_safe_root:
        return text
    values = [type(exc).__name__, "stage=run"]
    error_number = getattr(exc, "errno", None)
    if isinstance(error_number, int):
        values.append(f"errno={error_number}")
    windows_error = getattr(exc, "winerror", None)
    if isinstance(windows_error, int):
        values.append(f"winerror={windows_error}")
    return " ".join(values)


def _redact_private_root_text(value: Any, scope: IngestionScope) -> str:
    text = str(value or "")
    roots = {str(scope.logical_root), str(scope.resolved_root)}
    for root in sorted(roots, key=len, reverse=True):
        parts = [part for part in re.split(r"[\\/]+", root) if part]
        if not parts:
            continue
        pattern = r"[\\/]+".join(re.escape(part) for part in parts)
        text = re.sub(
            pattern,
            "<EXTERNAL_SOURCE_ROOT>",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _persistent_scope_fields(
    scope: IngestionScope,
    *,
    source_id: str,
    privacy_safe_root: bool,
) -> dict[str, Any]:
    if not privacy_safe_root:
        return scope.state_fields(source_id=source_id)
    identity = _private_root_identity(scope)
    return {
        "root": "<EXTERNAL_SOURCE_ROOT>",
        "resolved_root": identity,
        "root_display_name": scope.root_display_name,
        "scan_subdir": scope.scan_subdir,
        "scan_root": scope.scan_subdir,
        "stored_path_prefix": scope.stored_path_prefix,
        "include_root_name_in_path": True,
        "source_id": source_id,
        "privacy_safe_root": True,
    }


def _migrate_private_scope_paths(
    state: dict[str, Any],
    *,
    scope: IngestionScope,
    source_id: str,
    persistent_root_identity: str,
) -> None:
    """Remove the current external root from state while retaining identity."""

    raw_root = str(scope.resolved_root)
    ingestion = state.get("ingestion")
    if isinstance(ingestion, dict) and str(
        ingestion.get("source_id") or ""
    ) == source_id:
        ingestion.update(
            _persistent_scope_fields(
                scope,
                source_id=source_id,
                privacy_safe_root=True,
            )
        )
    for item in state.get("files", {}).values():
        if not isinstance(item, dict):
            continue
        if str(item.get("source_id") or "") != source_id:
            continue
        if str(item.get("resolved_root") or "") in {
            raw_root,
            persistent_root_identity,
        }:
            item["resolved_root"] = persistent_root_identity
