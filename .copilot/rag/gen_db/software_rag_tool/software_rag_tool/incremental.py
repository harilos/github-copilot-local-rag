from __future__ import annotations

import errno
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .ingestion_paths import IngestionScope, resolve_ingestion_scope
from .jsonl import write_jsonl
from .manifest import read_manifest, validate_existing_index_tokenizer, write_manifest
from .paths import clean_dir, logs_dir
from .profile import update_profile_from_clean
from .progress import emit_event, write_progress
from .records import (
    FileBuildResult,
    build_records_for_file,
    file_content_hash,
    iter_input_files,
    sha256_text,
)
from .catalog import delete_chunks as delete_catalog_chunks, reset_catalog, upsert_records as upsert_catalog_records
from .config import DEFAULT_INGESTION_BATCH_SIZE_FILES
from .embeddings import DocumentTokenBudget, get_document_token_budget
from .store import collection_count, delete_ids, reset_collection, upsert_records
from .records import chunker_config
from .tokenize import require_index_tokenizer
from .pipeline import build_pipeline_contract
from .structured_extraction import ExtractionResult
from .ingestion_workers import (
    DoclingExtractionPool,
    choose_worker_plan,
    is_docling_worker_candidate,
)


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
    extractor_backend_policy: str = "legacy",
    ingestion_workers: int | None = None,
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
    if reset_db or reset_clean:
        existing_sources = _saved_state_source_ids_for_reset()
        sibling_sources = existing_sources - {source_id}
        if sibling_sources:
            raise RuntimeError(
                "single-source reset refused because the saved index contains "
                "other sources; use an orchestrated all-source rebuild"
            )
    # Tokenizer availability is an index-generation precondition.  Resolve it
    # after pure argument validation but before reset, state/progress, vector,
    # catalog, or manifest writes.
    index_tokenizer_fingerprint = str(require_index_tokenizer())
    if not reset_db:
        validate_existing_index_tokenizer()
    token_budget = document_token_budget or get_document_token_budget()
    current_chunker_config = chunker_config(
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        document_token_budget=token_budget,
    )
    current_pipeline_contract = build_pipeline_contract(
        chunker=current_chunker_config,
        backend_policy=extractor_backend_policy,
        lexical_tokenizer=index_tokenizer_fingerprint,
    )
    scope = resolve_ingestion_scope(root, scan_subdir)
    persistent_scope_fields = _persistent_scope_fields(
        scope,
        source_id=source_id,
        privacy_safe_root=privacy_safe_root,
    )
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
    auto_pipeline_rebuild = False
    migration_required = (
        not reset_db
        and not reset_clean
        and _pipeline_migration_required(
            state,
            str(current_pipeline_contract["fingerprint"]),
        )
    )
    if resume and migration_required:
        raise ValueError(
            "resume settings do not match saved index state: "
            "pipeline_fingerprint"
        )
    if migration_required:
        if not _auto_pipeline_rebuild_safe(
            state,
            persistent_scope_fields=persistent_scope_fields,
        ):
            raise RuntimeError(
                "pipeline fingerprint changed across an ingestion scope "
                "that cannot be rebuilt safely by this ADD; use an "
                "orchestrated all-source rebuild"
            )
        # A persisted-content contract change is a generation boundary.  Clear
        # every store before reprocessing so old vectors/FTS rows cannot be
        # presented under the new manifest when one file later fails.
        reset_collection()
        reset_catalog()
        _reset_clean_dir()
        state = _initial_state()
        reset_db = True
        reset_clean = True
        auto_pipeline_rebuild = True
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
            pipeline_fingerprint=str(
                current_pipeline_contract["fingerprint"]
            ),
        )
    state["ingestion"] = {
        **persistent_scope_fields,
        "batch_size_files": effective_batch_size_files,
        "pipeline_fingerprint": current_pipeline_contract["fingerprint"],
        "pipeline": current_pipeline_contract["descriptor"],
    }
    # Persist the effective scope before discovery.  If scanning is
    # interrupted, a later --resume must still validate against the exact
    # root/source/scope that started the run.
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
        auto_pipeline_rebuild=auto_pipeline_rebuild,
        pipeline_fingerprint=current_pipeline_contract["fingerprint"],
        pipeline=current_pipeline_contract["descriptor"],
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        files_total=0,
        files_done=0,
        indexed_files=0,
        skipped_files=0,
        error_files=0,
        input_error_files=0,
        extract_error_files=0,
        zero_text_files=0,
        unsupported_files=0,
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
        pipeline_fingerprint=current_pipeline_contract["fingerprint"],
    )

    if reset_db:
        emit_event("collection_reset")

    try:
        files = list(iter_input_files(scope.scan_root))
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
        "zero_text_files": 0,
        "unsupported_files": 0,
        "error_details": [],
        "upserted_records": 0,
        "deleted_records": 0,
        "auto_pipeline_rebuild": auto_pipeline_rebuild,
    }
    write_progress(status="running", phase="extract", files_total=len(files))

    try:
        pending: list[dict[str, Any]] = []
        force_index = reset_db or reset_clean
        docling_candidate_count = sum(
            is_docling_worker_candidate(path) for path in files
        )
        allow_docling_workers = (
            extractor_backend_policy != "legacy"
            and docling_candidate_count > 0
        )
        worker_plan = choose_worker_plan(
            docling_candidate_count,
            requested_workers=ingestion_workers,
        )
        summary["worker_plan"] = {
            "workers": worker_plan.workers,
            "threads_per_worker": worker_plan.threads_per_worker,
            "logical_cpus": worker_plan.logical_cpus,
            "available_memory_bytes": worker_plan.available_memory_bytes,
            "reason": worker_plan.reason,
        }
        write_progress(
            status="running",
            phase="extract",
            worker_plan=summary["worker_plan"],
        )
        emit_event("extraction_worker_plan", **summary["worker_plan"])
        with DoclingExtractionPool(
            worker_plan,
            allow_docling=allow_docling_workers,
        ) as extraction_pool:
            # Extraction submissions stay bounded independently of the
            # caller's persistence checkpoint size. The pool itself remains
            # alive across every group and checkpoint in this ADD.
            group_size = extraction_pool.group_size
            for group_start in range(0, len(files), group_size):
                group = files[group_start : group_start + group_size]
                preextract_hashes: dict[Path, str] = {}
                if extraction_pool.enabled:
                    for path in group:
                        if not is_docling_worker_candidate(path):
                            continue
                        value = _preextract_candidate_hash(
                            scope=scope,
                            path=path,
                            source_id=source_id,
                            state=state,
                            force_index=force_index,
                            chunker_config_value=current_chunker_config,
                            pipeline_fingerprint=str(
                                current_pipeline_contract["fingerprint"]
                            ),
                        )
                        if value:
                            preextract_hashes[path] = value
                preextracted = extraction_pool.extract(
                    preextract_hashes,
                    backend_policy=extractor_backend_policy,
                )
                for path in group:
                    rel = _safe_stored_path(scope, path)
                    write_progress(
                        status="running",
                        phase="extract",
                        current_file=rel,
                    )
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
                        current_pipeline_contract=current_pipeline_contract,
                        extractor_backend_policy=extractor_backend_policy,
                        preextracted_result=preextracted.get(path),
                        preextracted_content_hash=preextract_hashes.get(
                            path, ""
                        ),
                        persistent_root_identity=str(
                            persistent_scope_fields["resolved_root"]
                        ),
                    )
                    item_status = item["status"]
                    if item_status == "skip":
                        summary["skipped_files"] += 1
                        write_progress(
                            status="running",
                            phase="extract",
                            files_done=_files_done(summary),
                            skipped_files=summary["skipped_files"],
                            current_file=rel,
                        )
                        continue
                    if item_status == "error":
                        _record_error(state, item)
                        _save_state(state)
                        summary["error_files"] += 1
                        error_kind = str(
                            item.get("error_kind") or "extract"
                        )
                        if error_kind == "input_read":
                            summary["input_error_files"] += 1
                        else:
                            summary["extract_error_files"] += 1
                        extraction_status = str(
                            item.get("extraction_status") or ""
                        )
                        if extraction_status == "zero_text":
                            summary["zero_text_files"] += 1
                        elif extraction_status == "unsupported":
                            summary["unsupported_files"] += 1
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
                            extract_error_files=summary[
                                "extract_error_files"
                            ],
                            zero_text_files=summary["zero_text_files"],
                            unsupported_files=summary[
                                "unsupported_files"
                            ],
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

                    pending.append(item)
                    emit_event(
                        "file_extracted",
                        path=rel,
                        records=len(item["records"]),
                    )
                    if len(pending) >= effective_batch_size_files:
                        _flush_batch(
                            pending,
                            state,
                            summary,
                            reset_db=reset_db,
                        )
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
        write_manifest(
            count,
            chunker_config=current_chunker_config,
            pipeline_contract=current_pipeline_contract,
        )
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
            zero_text_files=summary["zero_text_files"],
            unsupported_files=summary["unsupported_files"],
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
    current_pipeline_contract: dict[str, Any] | None = None,
    extractor_backend_policy: str = "legacy",
    preextracted_result: ExtractionResult | None = None,
    preextracted_content_hash: str = "",
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
    active_pipeline_contract = current_pipeline_contract or build_pipeline_contract(
        chunker=active_chunker_config,
        backend_policy=extractor_backend_policy,
    )
    active_pipeline_fingerprint = str(
        active_pipeline_contract["fingerprint"]
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
            active_pipeline_contract=active_pipeline_contract,
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
            active_pipeline_contract=active_pipeline_contract,
            previous=prev,
            exc=exc,
            stage="hash-read",
            error_kind="input_read",
        )

    if (
        not force_index
        and prev
        and prev.get("chunker_config") == active_chunker_config
        and prev.get("pipeline_fingerprint") == active_pipeline_fingerprint
    ):
        if (
            prev.get("status") == "indexed"
            and prev.get("content_hash") == content_hash
        ):
            return {"status": "skip", "rel": rel}
        if (
            prev.get("status") != "indexed"
            and prev.get("failed_content_hash") == content_hash
            and not bool(prev.get("retryable"))
            and not retry_errors
        ):
            return {"status": "skip", "rel": rel}

    try:
        build = build_records_for_file(
            scope.logical_root,
            stored.resolved_path,
            source_id=source_id,
            content_hash=content_hash,
            chunk_max_chars=chunk_max_chars,
            chunk_overlap=chunk_overlap,
            ingestion_scope=scope,
            document_token_budget=token_budget,
            extraction_result=(
                preextracted_result
                if preextracted_result is not None
                and preextracted_content_hash == content_hash
                else None
            ),
            pipeline_contract=active_pipeline_contract,
            backend_policy=extractor_backend_policy,
            return_result=True,
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
            active_pipeline_contract=active_pipeline_contract,
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
            active_pipeline_contract=active_pipeline_contract,
            previous=prev,
            exc=exc,
            stage="extract",
            error_kind="extract",
            failed_content_hash=content_hash,
        )

    if isinstance(build, FileBuildResult):
        extraction = build.extraction
        records = build.records
        if not extraction.is_indexed:
            return _extraction_error_item(
                key=key,
                rel=rel,
                source_id=source_id,
                scope=scope,
                root_identity=root_identity,
                active_chunker_config=active_chunker_config,
                active_pipeline_contract=active_pipeline_contract,
                previous=prev,
                failed_content_hash=content_hash,
                extraction=extraction,
            )
    else:
        # Compatibility for existing extension/test hooks that return records.
        records = list(build)
        extraction = ExtractionResult(
            status="indexed",
            backend="external-hook",
            backend_version="",
            source_format=stored.resolved_path.suffix.lower().lstrip("."),
            structure_origin="external_hook",
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
        "pipeline_fingerprint": active_pipeline_fingerprint,
        "pipeline": active_pipeline_contract["descriptor"],
        "extraction": _extraction_state(extraction),
        "previous_record_ids": list((prev or {}).get("record_ids") or []),
        "records": records,
    }


def _preextract_candidate_hash(
    *,
    scope: IngestionScope,
    path: Path,
    source_id: str,
    state: dict[str, Any],
    force_index: bool,
    chunker_config_value: dict[str, Any],
    pipeline_fingerprint: str,
) -> str:
    if not is_docling_worker_candidate(path):
        return ""
    rel = _safe_stored_path(scope, path)
    previous = state.get("files", {}).get(_state_key(source_id, rel))
    try:
        stored = scope.file(path)
        content_hash = file_content_hash(stored.resolved_path)
    except OSError:
        return ""
    if force_index or not isinstance(previous, dict):
        return content_hash
    known_hash = (
        previous.get("content_hash")
        if previous.get("status") == "indexed"
        else previous.get("failed_content_hash")
    )
    same_generation = (
        known_hash == content_hash
        and previous.get("chunker_config") == chunker_config_value
        and previous.get("pipeline_fingerprint") == pipeline_fingerprint
    )
    if not same_generation:
        return content_hash
    if previous.get("status") == "indexed":
        return ""
    if bool(previous.get("retryable")):
        return content_hash
    return ""


def _error_item(
    *,
    key: str,
    rel: str,
    source_id: str,
    scope: IngestionScope,
    root_identity: str,
    active_chunker_config: dict[str, Any],
    active_pipeline_contract: dict[str, Any],
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
        "pipeline_fingerprint": active_pipeline_contract["fingerprint"],
        "pipeline": active_pipeline_contract["descriptor"],
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


def _extraction_error_item(
    *,
    key: str,
    rel: str,
    source_id: str,
    scope: IngestionScope,
    root_identity: str,
    active_chunker_config: dict[str, Any],
    active_pipeline_contract: dict[str, Any],
    previous: Any,
    failed_content_hash: str,
    extraction: ExtractionResult,
) -> dict[str, Any]:
    previous_value = previous if isinstance(previous, dict) else {}
    diagnostic = {
        "path": str(rel)[:2_048],
        "stage": "extract",
        "error_type": extraction.status[:120],
        "extraction_status": extraction.status[:120],
        "backend": extraction.backend[:120],
        "reason": extraction.reason[:160],
        "retryable": bool(extraction.retryable),
    }
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
        "pipeline_fingerprint": active_pipeline_contract["fingerprint"],
        "pipeline": active_pipeline_contract["descriptor"],
        "previous_chunker_config": dict(
            previous_value.get("chunker_config") or {}
        ),
        "previous_record_ids": list(previous_value.get("record_ids") or []),
        "previous_entry": dict(previous_value),
        "had_previous_entry": isinstance(previous, dict),
        "error_kind": "extract",
        "extraction_status": extraction.status,
        "extraction": _extraction_state(extraction),
        "retryable": bool(extraction.retryable),
        "diagnostic": diagnostic,
        "error": _diagnostic_text(diagnostic),
    }


def _extraction_state(extraction: ExtractionResult) -> dict[str, Any]:
    return {
        "status": extraction.status,
        "backend": extraction.backend,
        "backend_version": extraction.backend_version,
        "source_format": extraction.source_format,
        "structure_origin": extraction.structure_origin,
        "retryable": bool(extraction.retryable),
        "reason": extraction.reason,
        "encoding": extraction.encoding,
        "encoding_reason": extraction.encoding_reason,
        "replacement_count": extraction.replacement_count,
        "fallback_reason": extraction.fallback_reason,
        "diagnostics": dict(extraction.diagnostics),
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
            "pipeline_fingerprint": item.get("pipeline_fingerprint") or "",
            "pipeline": item.get("pipeline") or {},
            "extraction": item.get("extraction") or {},
            "record_ids": record_ids,
            "record_count": len(record_ids),
            "records_path": record_path.relative_to(clean_dir()).as_posix(),
            "status": "indexed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _record_error(state: dict[str, Any], item: dict[str, Any]) -> None:
    previous = item.get("previous_entry")
    value = dict(previous) if isinstance(previous, dict) else {}
    extraction_status = str(item.get("extraction_status") or "")
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
        "pipeline_fingerprint": item.get("pipeline_fingerprint") or "",
        "pipeline": item.get("pipeline") or {},
        "extraction": item.get("extraction") or {},
        "record_ids": item["previous_record_ids"],
        "record_count": len(item["previous_record_ids"]),
        "status": extraction_status or "error",
        "error": item["error"],
        "error_kind": item.get("error_kind") or "extract",
        "retryable": bool(item.get("retryable")),
        "diagnostic": dict(item.get("diagnostic") or {}),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    state["files"][item["key"]] = value


def _initial_state() -> dict[str, Any]:
    return {"version": 2, "files": {}, "ingestion": {}}


def _pipeline_migration_required(
    state: dict[str, Any],
    current_fingerprint: str,
) -> bool:
    manifest = read_manifest()
    if manifest:
        return str(manifest.get("pipeline_fingerprint") or "") != str(
            current_fingerprint
        )
    files = state.get("files") or {}
    if not isinstance(files, dict) or not files:
        return False
    ingestion = state.get("ingestion") or {}
    saved = (
        str(ingestion.get("pipeline_fingerprint") or "")
        if isinstance(ingestion, dict)
        else ""
    )
    if saved:
        return saved != str(current_fingerprint)
    persisted = {
        str(value.get("pipeline_fingerprint") or "")
        for value in files.values()
        if isinstance(value, dict)
    }
    persisted.discard("")
    if not persisted:
        # Pre-fingerprint state without a manifest keeps the historical
        # per-file replacement path.  A normal pre-Phase-2 DB still has a
        # manifest and is reset as one generation above.
        return False
    return persisted != {str(current_fingerprint)}


def _auto_pipeline_rebuild_safe(
    state: dict[str, Any],
    *,
    persistent_scope_fields: dict[str, Any],
) -> bool:
    """Only auto-reset when the current ADD fully owns the saved database."""

    saved = state.get("ingestion") or {}
    if not isinstance(saved, dict) or not saved:
        return False
    for key in ("source_id", "scan_subdir", "resolved_root"):
        if str(saved.get(key) or "") != str(
            persistent_scope_fields.get(key) or ""
        ):
            return False
    expected_source = str(persistent_scope_fields.get("source_id") or "")
    files = state.get("files") or {}
    if not isinstance(files, dict):
        return False
    saved_sources = {
        str(value.get("source_id") or "")
        for value in files.values()
        if isinstance(value, dict)
    }
    saved_sources.discard("")
    return not saved_sources or saved_sources == {expected_source}


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


def _state_source_ids(state: dict[str, Any]) -> set[str]:
    files = state.get("files") or {}
    if not isinstance(files, dict):
        return set()
    return {
        str(value.get("source_id") or "")
        for value in files.values()
        if isinstance(value, dict) and value.get("source_id")
    }


def _saved_state_source_ids_for_reset() -> set[str]:
    """Inspect reset scope without applying state-version migration gates."""

    path = _state_path()
    if not path.exists():
        return set()
    try:
        state = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    return _state_source_ids(state) if isinstance(state, dict) else set()


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
            "status": item.get("status"),
            "retryable": bool(item.get("retryable")),
            "diagnostic": item.get("diagnostic") or {},
            "updated_at": item.get("updated_at"),
        }
        for item in state.get("files", {}).values()
        if item.get("status") != "indexed"
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
    batch_size_files: int | None = None,
    privacy_safe_root: bool = False,
    pipeline_fingerprint: str = "",
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
    if pipeline_fingerprint:
        expected["pipeline_fingerprint"] = pipeline_fingerprint
    keys = ["resolved_root", "source_id", "scan_subdir"]
    if "batch_size_files" in saved and batch_size_files is not None:
        keys.append("batch_size_files")
    if pipeline_fingerprint:
        keys.append("pipeline_fingerprint")
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
