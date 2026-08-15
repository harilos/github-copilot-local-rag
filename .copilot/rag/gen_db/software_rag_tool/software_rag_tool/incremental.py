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
from .progress import emit_event, write_progress
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
                f"{summary['input_error_files']:,}ä»¶ã®ãƒ•ã‚¡ã‚¤ãƒ«ã‚’èª­ã¿å–ã‚Œã¾ã›ã‚“ã§ã—ãŸã€‚"
                "èª­ã‚ãŸãƒ•ã‚¡ã‚¤ãƒ«ã¯åæ˜ æ¸ˆã¿ã§ã€å¤±æ•—ãƒ•ã‚¡ã‚¤ãƒ«ã¯æ¬¡å›žè‡ªå‹•å†è©¦è¡Œã—ã¾ã™ã€‚"
            )
        _write_errors_report(state)
        write_progress(
            status=summary["result_status"],
            phase="completed",
            files_done=_files_done(summary),
            error_files=summary["error_files"],
            input_error_files=summary["input_error_files"],
            extract_error_files=summary["extã^µ¶‰žËkºwµç@€€€€€€€‰Í…¹}ÍÕ‰‘¥Èˆè¥Ñ•´¹•Ð ‰Í…¹}ÍÕ‰‘¥Èˆ¤½È€ˆ¸ˆ°4(€€€€€€€€€€€€‰É•Í½±Ù•‘}É½½Ðˆè¥Ñ•´¹•Ð ‰É•Í½±Ù•‘}É½½Ðˆ¤½È€ˆˆ°4(€€€€€€€€€€€€‰½¹Ñ•¹Ñ}¡…Í ˆè¥Ñ•µl‰½¹Ñ•¹Ñ}¡…Í ‰t°4(€€€€€€€€€€€€‰¡Õ¹­•É}½¹™¥œˆè¥Ñ•´¹•Ð ‰¡Õ¹­•É}½¹™¥œˆ¤½Èíô°4(€€€€€€€€€€€€‰É•½É‘}¥‘ÌˆèÉ•½É‘}¥‘Ì°4(€€€€€€€€€€€€‰É•½É‘}½Õ¹Ðˆè±•¸¡É•½É‘}¥‘Ì¤°4(€€€€€€€€€€€€‰É•½É‘Í}Á…Ñ ˆèÉ•½É‘}Á…Ñ ¹É•±…Ñ¥Ù•}Ñ¼¡±•…¹}‘¥È ¤¤¹…Í}Á½Í¥à ¤°4(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰¥¹‘•á•ˆ°4(€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ðˆè‘…Ñ•Ñ¥µ”¹¹½Ü¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹¥Í½™½Éµ…Ð ¤°4(€€€€€€€ô4(4(4)‘•˜}É•½É‘}•ÉÉ½È¡ÍÑ…Ñ”è‘¥ÑmÍÑÈ°¹åt°¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´ø9½¹”è4(€€€ÁÉ•Ù¥½ÕÌ€ô¥Ñ•´¹•Ð ‰ÁÉ•Ù¥½ÕÍ}•¹ÑÉäˆ¤4(€€€Ù…±Õ”€ô‘¥Ð¡ÁÉ•Ù¥½ÕÌ¤¥˜¥Í¥¹ÍÑ…¹”¡ÁÉ•Ù¥½ÕÌ°‘¥Ð¤•±Í”íô4(€€€Ù…±Õ”¹ÕÁ‘…Ñ”¡ì4(€€€€€€€€‰Í½ÕÉ•}¥ˆè¥Ñ•µl‰Í½ÕÉ•}¥‰t°4(€€€€€€€€‰Á…Ñ ˆè¥Ñ•µl‰É•°‰t°4(€€€€€€€€‰ÍÑ½É•‘}Á…Ñ ˆè¥Ñ•µl‰É•°‰t°4(€€€€€€€€‰Í…¹}ÍÕ‰‘¥Èˆè¥Ñ•´¹•Ð ‰Í…¹}ÍÕ‰‘¥Èˆ¤½È€ˆ¸ˆ°4(€€€€€€€€‰É•Í½±Ù•‘}É½½Ðˆè¥Ñ•´¹•Ð ‰É•Í½±Ù•‘}É½½Ðˆ¤½È€ˆˆ°4(€€€€€€€€‰½¹Ñ•¹Ñ}¡…Í ˆè¥Ñ•µl‰½¹Ñ•¹Ñ}¡…Í ‰t°4(€€€€€€€€‰™…¥±•‘}½¹Ñ•¹Ñ}¡…Í ˆè¥Ñ•´¹•Ð ‰™…¥±•‘}½¹Ñ•¹Ñ}¡…Í ˆ¤½È€ˆˆ°4(€€€€€€€€‰¡Õ¹­•É}½¹™¥œˆè€ 4(€€€€€€€€€€€¥Ñ•´¹•Ð ‰ÁÉ•Ù¥½ÕÍ}¡Õ¹­•É}½¹™¥œˆ¤4(€€€€€€€€€€€¥˜¥Ñ•´¹•Ð ‰¡…‘}ÁÉ•Ù¥½ÕÍ}•¹ÑÉäˆ¤4(€€€€€€€€€€€•±Í”¥Ñ•´¹•Ð ‰¡Õ¹­•É}½¹™¥œˆ¤4(€€€€€€€€¤4(€€€€€€€½Èíô°4(€€€€€€€€‰™…¥±•‘}¡Õ¹­•É}½¹™¥œˆè¥Ñ•´¹•Ð ‰¡Õ¹­•É}½¹™¥œˆ¤½Èíô°4(€€€€€€€€‰É•½É‘}¥‘Ìˆè¥Ñ•µl‰ÁÉ•Ù¥½ÕÍ}É•½É‘}¥‘Ì‰t°4(€€€€€€€€‰É•½É‘}½Õ¹Ðˆè±•¸¡¥Ñ•µl‰ÁÉ•Ù¥½ÕÍ}É•½É‘}¥‘Ì‰t¤°4(€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰•ÉÉ½Èˆ°4(€€€€€€€€‰•ÉÉ½Èˆè¥Ñ•µl‰•ÉÉ½È‰t°4(€€€€€€€€‰•ÉÉ½É}­¥¹ˆè¥Ñ•´¹•Ð ‰•ÉÉ½É}­¥¹ˆ¤½È€‰•áÑÉ…Ðˆ°4(€€€€€€€€‰É•ÑÉå…‰±”ˆè‰½½°¡¥Ñ•´¹•Ð ‰É•ÑÉå…‰±”ˆ¤¤°4(€€€€€€€€‰‘¥…¹½ÍÑ¥Œˆè‘¥Ð¡¥Ñ•´¹•Ð ‰‘¥…¹½ÍÑ¥Œˆ¤½Èíô¤°4(€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ðˆè‘…Ñ•Ñ¥µ”¹¹½Ü¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹¥Í½™½Éµ…Ð ¤°4(€€€ô¤4(€€€ÍÑ…Ñ•l‰™¥±•Ì‰um¥Ñ•µl‰­•ä‰ut€ôÙ…±Õ”4(4(4)‘•˜}¥¹¥Ñ¥…±}ÍÑ…Ñ” ¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€É•ÑÕÉ¸ì‰Ù•ÉÍ¥½¸ˆè€È°€‰™¥±•Ìˆèíô°€‰¥¹•ÍÑ¥½¸ˆèíõô4(4(4)‘•˜}±½…‘}ÍÑ…Ñ” ¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€Á…Ñ €ô}ÍÑ…Ñ•}Á…Ñ  ¤4(€€€¥˜¹½ÐÁ…Ñ ¹•á¥ÍÑÌ ¤è4(€€€€€€€É•ÑÕÉ¸}¥¹¥Ñ¥…±}ÍÑ…Ñ” ¤4(€€€‘…Ñ„€ô©Í½¸¹±½…‘Ì¡Á…Ñ ¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ°•ÉÉ½ÉÌô‰É•Á±…”ˆ¤¤4(€€€Ù•ÉÍ¥½¸€ô¥¹Ð¡‘…Ñ„¹•Ð ‰Ù•ÉÍ¥½¸ˆ¤½È€Ä¤4(€€€¥˜€‰™¥±•Ìˆ¹½Ð¥¸‘…Ñ„½È¹½Ð¥Í¥¹ÍÑ…¹”¡‘…Ñ…l‰™¥±•Ì‰t°‘¥Ð¤è4(€€€€€€€‘…Ñ…l‰™¥±•Ì‰t€ôíô4(€€€¥˜Ù•ÉÍ¥½¸€ð€È…¹‘…Ñ…l‰™¥±•Ì‰tè4(€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È 4(€€€€€€€€€€€€‰á¥ÍÑ¥¹œ¥¹‘•àÍÑ…Ñ”ÕÍ•ÌÁÉ”µÉ½½ÐµÁÉ•™¥á•‘½Õµ•¹ÐÁ…Ñ¡Ì¸€ˆ4(€€€€€€€€€€€€‰I•‰Õ¥±Ñ¡”‘…Ñ…‰…Í”½¹”Ý¥Ñ €´µ™½É”µÉ•‰Õ¥±¸ˆ4(€€€€€€€€¤4(€€€‘…Ñ…l‰Ù•ÉÍ¥½¸‰t€ô€È4(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡‘…Ñ„¹•Ð ‰¥¹•ÍÑ¥½¸ˆ¤°‘¥Ð¤è4(€€€€€€€‘…Ñ…l‰¥¹•ÍÑ¥½¸‰t€ôíô4(€€€É•ÑÕÉ¸‘…Ñ„4(4(4)‘•˜}•™™•Ñ¥Ù•}‰…Ñ¡}Í¥é•}™¥±•Ì 4(€€€ÍÑ…Ñ”è‘¥ÑmÍÑÈ°¹åt°4(€€€€¨°4(€€€É•ÅÕ•ÍÑ•è¥¹Ðð9½¹”°4(€€€É•ÍÕµ”è‰½½°°4(¤€´ø¥¹Ðè4(€€€¥˜É•ÅÕ•ÍÑ•¥Ì¹½Ð9½¹”…¹É•ÅÕ•ÍÑ•€ðô€Àè4(€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰‰…Ñ¡}Í¥é•}™¥±•ÌµÕÍÐ‰”Á½Í¥Ñ¥Ù”ˆ¤4(€€€Í…Ù•‘}¥¹•ÍÑ¥½¸€ôÍÑ…Ñ”¹•Ð ‰¥¹•ÍÑ¥½¸ˆ¤4(€€€Í…Ù•€ô€ 4(€€€€€€€Í…Ù•‘}¥¹•ÍÑ¥½¸¹•Ð ‰‰…Ñ¡}Í¥é•}™¥±•Ìˆ¤4(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡Í…Ù•‘}¥¹•ÍÑ¥½¸°‘¥Ð¤4(€€€€€€€•±Í”9½¹”4(€€€€¤4(€€€¥˜É•ÍÕµ”…¹Í…Ù•¥Ì¹½Ð9½¹”è4(€€€€€€€ÑÉäè4(€€€€€€€€€€€Í…Ù•‘}Ù…±Õ”€ô¥¹Ð¡Í…Ù•¤4(€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤…Ì•áŒè4(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È 4(€€€€€€€€€€€€€€€€‰Í…Ù•‰…Ñ¡}Í¥é•}™¥±•Ì¥Ì¥¹Ù…±¥ˆ4(€€€€€€€€€€€€¤™É½´•áŒ4(€€€€€€€¥˜Í…Ù•‘}Ù…±Õ”€ðô€Àè4(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰Í…Ù•‰…Ñ¡}Í¥é•}™¥±•ÌµÕÍÐ‰”Á½Í¥Ñ¥Ù”ˆ¤4(€€€€€€€¥˜É•ÅÕ•ÍÑ•¥Ì¹½Ð9½¹”…¹É•ÅÕ•ÍÑ•€„ôÍ…Ù•‘}Ù…±Õ”è4(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È 4(€€€€€€€€€€€€€€€€‰É•ÍÕµ”Í•ÑÑ¥¹Ì‘¼¹½Ðµ…Ñ Í…Ù•¥¹‘•àÍÑ…Ñ”è€ˆ4(€€€€€€€€€€€€€€€€‰‰…Ñ¡}Í¥é•}™¥±•Ìˆ4(€€€€€€€€€€€€¤4(€€€€€€€É•ÑÕÉ¸Í…Ù•‘}Ù…±Õ”4(€€€É•ÑÕÉ¸É•ÅÕ•ÍÑ•½ÈU1Q}%9MQ%=9}	Q!}M%i}%1L4(4(4)‘•˜}Í…Ù•}ÍÑ…Ñ”¡ÍÑ…Ñ”è‘¥ÑmÍÑÈ°¹åt¤€´ø9½¹”è4(€€€Á…Ñ €ô}ÍÑ…Ñ•}Á…Ñ  ¤4(€€€ÍÑ…Ñ•l‰ÕÁ‘…Ñ•‘}…Ð‰t€ô‘…Ñ•Ñ¥µ”¹¹½Ü¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹¥Í½™½Éµ…Ð ¤4(€€€…Ñ½µ¥}ÝÉ¥Ñ•}©Í½¸¡Á…Ñ °ÍÑ…Ñ”¤4(4(4)‘•˜}ÍÑ…Ñ•}Á…Ñ  ¤€´øA…Ñ è4(€€€É•ÑÕÉ¸±½Í}‘¥È ¤€¼€‰¥¹‘•á}ÍÑ…Ñ”¹©Í½¸ˆ4(4(4)‘•˜}ÝÉ¥Ñ•}•ÉÉ½ÉÍ}É•Á½ÉÐ¡ÍÑ…Ñ”è‘¥ÑmÍÑÈ°¹åt¤€´ø9½¹”è4(€€€•ÉÉ½ÉÌ€ôl4(€€€€€€€ì4(€€€€€€€€€€€€‰Í½ÕÉ•}¥ˆè¥Ñ•´¹•Ð ‰Í½ÕÉ•}¥ˆ¤°4(€€€€€€€€€€€€‰Á…Ñ ˆè¥Ñ•´¹•Ð ‰Á…Ñ ˆ¤°4(€€€€€€€€€€€€‰•ÉÉ½Èˆè¥Ñ•´¹•Ð ‰•ÉÉ½Èˆ¤°4(€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ðˆè¥Ñ•´¹•Ð ‰ÕÁ‘…Ñ•‘}…Ðˆ¤°4(€€€€€€€ô4(€€€€€€€™½È¥Ñ•´¥¸ÍÑ…Ñ”¹•Ð ‰™¥±•Ìˆ°íô¤¹Ù…±Õ•Ì ¤4(€€€€€€€¥˜¥Ñ•´¹•Ð ‰ÍÑ…ÑÕÌˆ¤€ôô€‰•ÉÉ½Èˆ4(€€€t4(€€€Á…Ñ €ô±½Í}‘¥È ¤€¼€‰ÁÉ•Á…É•}•ÉÉ½ÉÌ¹©Í½¸ˆ4(€€€…Ñ½µ¥}ÝÉ¥Ñ•}©Í½¸¡Á…Ñ °•ÉÉ½ÉÌ°Í½ÉÑ}­•åÌõ…±Í”¤4(4(4)‘•˜}É•½É‘}©Í½¹±}Á…Ñ ¡Í½ÕÉ•}¥èÍÑÈ°É•°èÍÑÈ¤€´øA…Ñ è4(€€€Í…™•}Í½ÕÉ”€ôÉ”¹ÍÕˆ¡È‰myµi„µèÀ´å|¸µt¬ˆ°€‰|ˆ°Í½ÕÉ•}¥¤¹ÍÑÉ¥À ˆ¹|´ˆ¤½È€‰±½…°ˆ4(€€€¹…µ”€ôÍ¡„ÈÔÙ}Ñ•áÐ¡˜‰íÍ½ÕÉ•}¥‘ôéíÉ•±ôˆ¥lèÈÑt€¬€ˆ¹©Í½¹°ˆ4(€€€É•ÑÕÉ¸±•…¹}‘¥È ¤€¼€‰É•½É‘Ìˆ€¼Í…™•}Í½ÕÉ”€¼¹…µ”4(4(4)‘•˜}ÍÑ…Ñ•}­•ä¡Í½ÕÉ•}¥èÍÑÈ°É•°èÍÑÈ¤€´øÍÑÈè4(€€€É•ÑÕÉ¸˜‰íÍ½ÕÉ•}¥‘ôéíÉ•±ôˆ4(4(4)‘•˜}Ù…±¥‘…Ñ•}É•ÍÕµ•}ÍÑ…Ñ” 4(€€€ÍÑ…Ñ”è‘¥ÑmÍÑÈ°¹åt°4(€€€Í½Á”è%¹•ÍÑ¥½¹M½Á”°4(€€€Í½ÕÉ•}¥èÍÑÈ°4(€€€‰…Ñ¡}Í¥é•}™¥±•Ìè¥¹Ðð9½¹”€ô9½¹”°4(€€€ÁÉ¥Ù…å}Í…™•}É½½Ðè‰½½°€ô…±Í”°4(¤€´ø9½¹”è4(€€€Í…Ù•€ôÍÑ…Ñ”¹•Ð ‰¥¹•ÍÑ¥½¸ˆ¤4(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡Í…Ù•°‘¥Ð¤½È¹½ÐÍ…Ù•è4(€€€€€€€É•ÑÕÉ¸4(€€€•áÁ•Ñ•€ô}Á•ÉÍ¥ÍÑ•¹Ñ}Í½Á•}™¥•±‘Ì 4(€€€€€€€Í½Á”°4(€€€€€€€Í½ÕÉ•}¥õÍ½ÕÉ•}¥°4(€€€€€€€ÁÉ¥Ù…å}Í…™•}É½½ÐõÁÉ¥Ù…å}Í…™•}É½½Ð°4(€€€€¤4(€€€¥˜‰…Ñ¡}Í¥é•}™¥±•Ì¥Ì¹½Ð9½¹”è4(€€€€€€€•áÁ•Ñ•‘l‰‰…Ñ¡}Í¥é•}™¥±•Ì‰t€ô‰…Ñ¡}Í¥é•}™¥±•Ì4(€€€­•åÌ€ôl‰É•Í½±Ù•‘}É½½Ðˆ°€‰Í½ÕÉ•}¥ˆ°€‰Í…¹}ÍÕ‰‘¥È‰t4(€€€¥˜€‰‰…Ñ¡}Í¥é•}™¥±•Ìˆ¥¸Í…Ù•…¹‰…Ñ¡}Í¥é•}™¥±•Ì¥Ì¹½Ð9½¹”è4(€€€€€€€­•åÌ¹…ÁÁ•¹ ‰‰…Ñ¡}Í¥é•}™¥±•Ìˆ¤4(€€€µ¥Íµ…Ñ¡•Ì€ôl4(€€€€€€€­•ä4(€€€€€€€™½È­•ä¥¸­•åÌ4(€€€€€€€¥˜ÍÑÈ¡Í…Ù•¹•Ð¡­•ä¤½È€ˆˆ¤€„ôÍÑÈ¡•áÁ•Ñ•¹•Ð¡­•ä¤½È€ˆˆ¤4(€€€t4(€€€¥˜µ¥Íµ…Ñ¡•Ìè4(€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È 4(€€€€€€€€€€€€‰É•ÍÕµ”Í•ÑÑ¥¹Ì‘¼¹½Ðµ…Ñ Í…Ù•¥¹‘•àÍÑ…Ñ”è€ˆ4(€€€€€€€€€€€€¬€ˆ°€ˆ¹©½¥¸¡µ¥Íµ…Ñ¡•Ì¤4(€€€€€€€€¤4(4(4)‘•˜}É•½¹¥±•}µ¥ÍÍ¥¹}™¥±•Ì 4(€€€ÍÑ…Ñ”è‘¥ÑmÍÑÈ°¹åt°4(€€€€¨°4(€€€Í½Á”è%¹•ÍÑ¥½¹M½Á”°4(€€€Í½ÕÉ•}¥èÍÑÈ°4(€€€‘¥Í½Ù•É•‘}­•åÌèÍ•ÑmÍÑÉt°4(€€€Á•ÉÍ¥ÍÑ•¹Ñ}É½½Ñ}¥‘•¹Ñ¥ÑäèÍÑÈð9½¹”€ô9½¹”°4(¤€´ø‘¥ÑmÍÑÈ°¥¹Ñtè4(€€€µ¥ÍÍ¥¹}­•åÌè±¥ÍÑmÍÑÉt€ômt4(€€€É•½É‘}¥‘Ìè±¥ÍÑmÍÑÉt€ômt4(€€€É•½É‘}Á…Ñ¡Ìè±¥ÍÑmA…Ñ¡t€ômt4(€€€™½È­•ä°¥Ñ•´¥¸±¥ÍÐ¡ÍÑ…Ñ”¹•Ð ‰™¥±•Ìˆ°íô¤¹¥Ñ•µÌ ¤¤è4(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€ÍÑ½É•‘}Á…Ñ €ôÍÑÈ 4(€€€€€€€€€€€¥Ñ•´¹•Ð ‰ÍÑ½É•‘}Á…Ñ ˆ¤½È¥Ñ•´¹•Ð ‰Á…Ñ ˆ¤½È€ˆˆ4(€€€€€€€€¤4(€€€€€€€¥˜€ 4(€€€€€€€€€€€ÍÑÈ¡¥Ñ•´¹•Ð ‰Í½ÕÉ•}¥ˆ¤½È€ˆˆ¤€„ôÍ½ÕÉ•}¥4(€€€€€€€€€€€½ÈÍÑÈ¡¥Ñ•´¹•Ð ‰É•Í½±Ù•‘}É½½Ðˆ¤½È€ˆˆ¤4(€€€€€€€€€€€€„ôÍÑÈ¡Á•ÉÍ¥ÍÑ•¹Ñ}É½½Ñ}¥‘•¹Ñ¥Ñä½ÈÍ½Á”¹É•Í½±Ù•‘}É½½Ð¤4(€€€€€€€€€€€½È¹½ÐÍ½Á”¹½¹Ñ…¥¹Í}ÍÑ½É•‘}Á…Ñ ¡ÍÑ½É•‘}Á…Ñ ¤4(€€€€€€€€€€€½È­•ä¥¸‘¥Í½Ù•É•‘}­•åÌ4(€€€€€€€€¤è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€µ¥ÍÍ¥¹}­•åÌ¹…ÁÁ•¹¡­•ä¤4(€€€€€€€É•½É‘}¥‘Ì¹•áÑ•¹ 4(€€€€€€€€€€€ÍÑÈ¡Ù…±Õ”¤™½ÈÙ…±Õ”¥¸¥Ñ•´¹•Ð ‰É•½É‘}¥‘Ìˆ¤½Èmt4(€€€€€€€€¤4(€€€€€€€É•½É‘}Á…Ñ¡Ì¹…ÁÁ•¹¡}É•½É‘}©Í½¹±}Á…Ñ ¡Í½ÕÉ•}¥°ÍÑ½É•‘}Á…Ñ ¤¤4(4(€€€¥˜¹½Ðµ¥ÍÍ¥¹}­•åÌè4(€€€€€€€É•ÑÕÉ¸ì‰‘•±•Ñ•‘}™¥±•Ìˆè€À°€‰‘•±•Ñ•‘}É•½É‘Ìˆè€Áô4(€€€‘•±•Ñ•€ô‘•±•Ñ•}¥‘Ì¡É•½É‘}¥‘Ì¤4(€€€‘•±•Ñ•}…Ñ…±½}¡Õ¹­Ì¡É•½É‘}¥‘Ì¤4(€€€±•…¹}É½½Ð€ô±•…¹}‘¥È ¤¹É•Í½±Ù” ¤4(€€€™½ÈÉ•½É‘}Á…Ñ ¥¸É•½É‘}Á…Ñ¡Ìè4(€€€€€€€ÑÉäè4(€€€€€€€€€€€É•Í½±Ù•€ôÉ•½É‘}Á…Ñ ¹•áÁ…¹‘ÕÍ•È ¤¹É•Í½±Ù” ¤4(€€€€€€€€€€€É•Í½±Ù•¹É•±…Ñ¥Ù•}Ñ¼¡±•…¹}É½½Ð¤4(€€€€€€€•á•ÁÐ€¡=MÉÉ½È°Y…±Õ•ÉÉ½È¤è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€É•Í½±Ù•¹Õ¹±¥¹¬¡µ¥ÍÍ¥¹}½¬õQÉÕ”¤4(€€€™½È­•ä¥¸µ¥ÍÍ¥¹}­•åÌè4(€€€€€€€ÍÑ…Ñ•l‰™¥±•Ì‰t¹Á½À¡­•ä°9½¹”¤4(€€€•µ¥Ñ}•Ù•¹Ð 4(€€€€€€€€‰Í½Á•}É•½¹¥±•ˆ°4(€€€€€€€Í½ÕÉ•}¥õÍ½ÕÉ•}¥°4(€€€€€€€Í…¹}ÍÕ‰‘¥ÈõÍ½Á”¹Í…¹}ÍÕ‰‘¥È°4(€€€€€€€‘•±•Ñ•‘}™¥±•Ìõ±•¸¡µ¥ÍÍ¥¹}­•åÌ¤°4(€€€€€€€‘•±•Ñ•‘}É•½É‘Ìõ‘•±•Ñ•°4(€€€€¤4(€€€É•ÑÕÉ¸ì4(€€€€€€€€‰‘•±•Ñ•‘}™¥±•Ìˆè±•¸¡µ¥ÍÍ¥¹}­•åÌ¤°4(€€€€€€€€‰‘•±•Ñ•‘}É•½É‘Ìˆè‘•±•Ñ•°4(€€€ô4(4(4)‘•˜}É•Í•Ñ}±•…¹}‘¥È ¤€´ø9½¹”è4(€€€‘¥É•Ñ½Éä€ô±•…¹}‘¥È ¤4(€€€¥˜‘¥É•Ñ½Éä¹•á¥ÍÑÌ ¤è4(€€€€€€€Í¡ÕÑ¥°¹ÉµÑÉ•”¡‘¥É•Ñ½Éä¤4(€€€‘¥É•Ñ½Éä¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”°•á¥ÍÑ}½¬õQÉÕ”¤4(€€€É•Í•Ñ}…Ñ…±½œ ¤4(4(4)‘•˜}ÁÉ½É•ÍÍ}±¥¹”¡ÍÕµµ…Éäè‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè4(€€€É•ÑÕÉ¸€ 4(€€€€€€€€‰AI=IML€ˆ4(€€€€€€€˜‰¥¹‘•á•‘}™¥±•ÌõíÍÕµµ…Éål¥¹‘•á•‘}™¥±•Ìuô€ˆ4(€€€€€€€˜‰Í­¥ÁÁ•‘}™¥±•ÌõíÍÕµµ…ÉålÍ­¥ÁÁ•‘}™¥±•Ìuô€ˆ4(€€€€€€€˜‰•ÉÉ½É}™¥±•ÌõíÍÕµµ…Éål•ÉÉ½É}™¥±•Ìuô€ˆ4(€€€€€€€˜‰ÕÁÍ•ÉÑ•‘}É•½É‘ÌõíÍÕµµ…ÉålÕÁÍ•ÉÑ•‘}É•½É‘Ìuô€ˆ4(€€€€€€€˜‰‘•±•Ñ•‘}É•½É‘ÌõíÍÕµµ…Éål‘•±•Ñ•‘}É•½É‘Ìuôˆ4(€€€€¤4(4(4)‘•˜}™¥±•Í}‘½¹”¡ÍÕµµ…Éäè‘¥ÑmÍÑÈ°¹åt¤€´ø¥¹Ðè4(€€€É•ÑÕÉ¸¥¹Ð¡ÍÕµµ…Éål‰¥¹‘•á•‘}™¥±•Ì‰t¤€¬¥¹Ð¡ÍÕµµ…Éål‰Í­¥ÁÁ•‘}™¥±•Ì‰t¤€¬¥¹Ð¡ÍÕµµ…Éål‰•ÉÉ½É}™¥±•Ì‰t¤4(4(4)‘•˜}É•ÍÕ±Ñ}ÍÑ…ÑÕÌ¡ÍÕµµ…Éäè‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè4(€€€•ÉÉ½ÉÌ€ô¥¹Ð¡ÍÕµµ…Éä¹•Ð ‰•ÉÉ½É}™¥±•Ìˆ¤½È€À¤4(€€€¥˜•ÉÉ½ÉÌ€ôô€Àè4(€€€€€€€É•ÑÕÉ¸€‰ÍÕ•ÍÌˆ4(€€€½µÁ±•Ñ•€ô¥¹Ð¡ÍÕµµ…Éä¹•Ð ‰¥¹‘•á•‘}™¥±•Ìˆ¤½È€À¤€¬¥¹Ð 4(€€€€€€€ÍÕµµ…Éä¹•Ð ‰Í­¥ÁÁ•‘}™¥±•Ìˆ¤½È€À4(€€€€¤4(€€€¥˜¥¹Ð¡ÍÕµµ…Éä¹•Ð ‰•áÑÉ…Ñ}•ÉÉ½É}™¥±•Ìˆ¤½È€À¤€ø€À½È½µÁ±•Ñ•€ôô€Àè4(€€€€€€€É•ÑÕÉ¸€‰™…¥±ÕÉ”ˆ4(€€€É•ÑÕÉ¸€‰Á…ÉÑ¥…°ˆ4(4(4)‘•˜}Í…™•}ÍÑ½É•‘}Á…Ñ ¡Í½Á”è%¹•ÍÑ¥½¹M½Á”°Á…Ñ èA…Ñ ¤€´øÍÑÈè4(€€€€ˆˆ‰	Õ¥±Ñ¡”¹½Éµ…°Á½ÉÑ…‰±”Á…Ñ Ý¥Ñ¡½ÕÐ½Á•¹¥¹œ½ÈÉ•Í½±Ù¥¹œÑ¡”™¥±”¸ˆˆˆ4(4(€€€…¹‘¥‘…Ñ”€ôA…Ñ ¡½Ì¹Á…Ñ ¹…‰ÍÁ…Ñ ¡A…Ñ ¡Á…Ñ ¤¤¤4(€€€ÑÉäè4(€€€€€€€É•±…Ñ¥Ù”€ô…¹‘¥‘…Ñ”¹É•±…Ñ¥Ù•}Ñ¼¡Í½Á”¹É•Í½±Ù•‘}É½½Ð¤4(€€€•á•ÁÐY…±Õ•ÉÉ½Èè4(€€€€€€€É•±…Ñ¥Ù”€ôA…Ñ ¡…¹‘¥‘…Ñ”¹¹…µ”¤4(€€€É•ÑÕÉ¸AÕÉ•A½Í¥áA…Ñ ¡Í½Á”¹É½½Ñ}‘¥ÍÁ±…å}¹…µ”°€©É•±…Ñ¥Ù”¹Á…ÉÑÌ¤¹…Í}Á½Í¥à ¤4(4(4)‘•˜}Í…™•}™¥±•}‘¥…¹½ÍÑ¥Œ 4(€€€€¨°4(€€€É•°èÍÑÈ°4(€€€•áŒè	…Í•á•ÁÑ¥½¸°4(€€€ÍÑ…”èÍÑÈ°4(€€€É•ÑÉå…‰±”è‰½½°°4(¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€‘¥…¹½ÍÑ¥Œè‘¥ÑmÍÑÈ°¹åt€ôì4(€€€€€€€€‰Á…Ñ ˆèÍÑÈ¡É•°¥lèÉ|ÀÐát°4(€€€€€€€€‰ÍÑ…”ˆèÍÑÈ¡ÍÑ…”¥lèàÁt°4(€€€€€€€€‰•ÉÉ½É}ÑåÁ”ˆèÑåÁ”¡•áŒ¤¹}}¹…µ•}}lèÄÈÁt°4(€€€€€€€€‰É•ÑÉå…‰±”ˆè‰½½°¡É•ÑÉå…‰±”¤°4(€€€ô4(€€€•ÉÉ½É}¹Õµ‰•È€ô•Ñ…ÑÑÈ¡•áŒ°€‰•ÉÉ¹¼ˆ°9½¹”¤4(€€€¥˜¥Í¥¹ÍÑ…¹”¡•ÉÉ½É}¹Õµ‰•È°¥¹Ð¤è4(€€€€€€€‘¥…¹½ÍÑ¥l‰•ÉÉ¹¼‰t€ô•ÉÉ½É}¹Õµ‰•È4(€€€Ý¥¹‘½ÝÍ}•ÉÉ½È€ô•Ñ…ÑÑÈ¡•áŒ°€‰Ý¥¹•ÉÉ½Èˆ°9½¹”¤4(€€€¥˜¥Í¥¹ÍÑ…¹”¡Ý¥¹‘½ÝÍ}•ÉÉ½È°¥¹Ð¤è4(€€€€€€€‘¥…¹½ÍÑ¥l‰Ý¥¹•ÉÉ½È‰t€ôÝ¥¹‘½ÝÍ}•ÉÉ½È4(€€€É•ÑÕÉ¸‘¥…¹½ÍÑ¥Œ4(4(4)‘•˜}¥Í}¥¹ÁÕÑ}É•…‘}½Í•ÉÉ½È¡•áŒè=MÉÉ½È°Í½ÕÉ•}Á…Ñ èA…Ñ ¤€´ø‰½½°è4(€€€€ˆˆ‰¥ÍÑ¥¹Õ¥Í Í½ÕÉ”µ½Á•¸™…¥±ÕÉ•Ì™É½´•áÑÉ…Ñ½ÈÝ½É­ÍÁ…”™…¥±ÕÉ•Ì¸ˆˆˆ4(4(€€€™¥±•¹…µ•Ì€ôl4(€€€€€€€Ù…±Õ”4(€€€€€€€™½ÈÙ…±Õ”¥¸€ 4(€€€€€€€€€€€•Ñ…ÑÑÈ¡•áŒ°€‰™¥±•¹…µ”ˆ°9½¹”¤°4(€€€€€€€€€€€•Ñ…ÑÑÈ¡•áŒ°€‰™¥±•¹…µ”Èˆ°9½¹”¤°4(€€€€€€€€¤4(€€€€€€€¥˜Ù…±Õ”4(€€€t4(€€€¥˜¹½Ð™¥±•¹…µ•Ìè4(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡•áŒ°¥±•9½Ñ½Õ¹‘ÉÉ½È¤è4(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”4(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡•áŒ°A•Éµ¥ÍÍ¥½¹ÉÉ½È¤…¹•Ñ…ÑÑÈ¡•áŒ°€‰•ÉÉ¹¼ˆ°9½¹”¤¥¸ì4(€€€€€€€€€€€•ÉÉ¹¼¹L°4(€€€€€€€€€€€•ÉÉ¹¼¹AI4°4(€€€€€€€ôè4(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”4(€€€€€€€É•ÑÕÉ¸•Ñ…ÑÑÈ¡•áŒ°€‰Ý¥¹•ÉÉ½Èˆ°9½¹”¤¥¸}IQIe	1}%9AUQ}]%9II=IL4(€€€•áÁ•Ñ•€ô½Ì¹Á…Ñ ¹¹½Éµ…Í”¡½Ì¹Á…Ñ ¹…‰ÍÁ…Ñ ¡½Ì¹™ÍÁ…Ñ ¡Í½ÕÉ•}Á…Ñ ¤¤¤4(€€€™½ÈÙ…±Õ”¥¸™¥±•¹…µ•Ìè4(€€€€€€€ÑÉäè4(€€€€€€€€€€€…¹‘¥‘…Ñ”€ô½Ì¹Á…Ñ ¹¹½Éµ…Í”¡½Ì¹Á…Ñ ¹…‰ÍÁ…Ñ ¡½Ì¹™Í‘•½‘”¡Ù…±Õ”¤¤¤4(€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€¥˜…¹‘¥‘…Ñ”€ôô•áÁ•Ñ•è4(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”4(€€€É•ÑÕÉ¸…±Í”4(4(4)‘•˜}‘¥…¹½ÍÑ¥}Ñ•áÐ¡‘¥…¹½ÍÑ¥Œè‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè4(€€€Ù…±Õ•Ì€ôl4(€€€€€€€ÍÑÈ¡‘¥…¹½ÍÑ¥Œ¹•Ð ‰•ÉÉ½É}ÑåÁ”ˆ¤½È€‰=MÉÉ½Èˆ¤°4(€€€€€€€˜‰ÍÑ…”õí‘¥…¹½ÍÑ¥Œ¹•Ð ÍÑ…”œ¤½È€¥¹ÁÕÐµÉ•…ôˆ°4(€€€€€€€˜‰Á…Ñ õí‘¥…¹½ÍÑ¥Œ¹•Ð Á…Ñ œ¤½È€œôˆ°4(€€€t4(€€€¥˜€‰•ÉÉ¹¼ˆ¥¸‘¥…¹½ÍÑ¥Œè4(€€€€€€€Ù…±Õ•Ì¹…ÁÁ•¹¡˜‰•ÉÉ¹¼õí‘¥…¹½ÍÑ¥l•ÉÉ¹¼uôˆ¤4(€€€¥˜€‰Ý¥¹•ÉÉ½Èˆ¥¸‘¥…¹½ÍÑ¥Œè4(€€€€€€€Ù…±Õ•Ì¹…ÁÁ•¹¡˜‰Ý¥¹•ÉÉ½Èõí‘¥…¹½ÍÑ¥lÝ¥¹•ÉÉ½Èuôˆ¤4(€€€É•ÑÕÉ¸€ˆ€ˆ¹©½¥¸¡Ù…±Õ•Ì¤4(4(4)‘•˜}ÁÉ¥Ù…Ñ•}É½½Ñ}¥‘•¹Ñ¥Ñä¡Í½Á”è%¹•ÍÑ¥½¹M½Á”¤€´øÍÑÈè4(€€€¹½Éµ…±¥é•€ô½Ì¹Á…Ñ ¹¹½Éµ…Í”¡ÍÑÈ¡Í½Á”¹É•Í½±Ù•‘}É½½Ð¤¤4(€€€É•ÑÕÉ¸€‰Í¡„ÈÔØèˆ€¬Í¡„ÈÔÙ}Ñ•áÐ¡¹½Éµ…±¥é•¤4(4(4)}1=U}%1M}]%9II=IL€ô™É½é•¹Í•Ð 4(€€€ì4(€€€€€€€€ÌÔà°4(€€€€€€€€ÌØÈ°4(€€€€€€€€ÌØÌ°4(€€€€€€€€ÌØÐ°4(€€€€€€€€ÌØÔ°4(€€€€€€€€ÌØØ°4(€€€€€€€€ÌØä°4(€€€€€€€€ÌÜÀ°4(€€€€€€€€ÌÜÄ°4(€€€€€€€€ÌÜÈ°4(€€€€€€€€ÌÜÐ°4(€€€€€€€€ÌÜÔ°4(€€€€€€€€ÌÜØ°4(€€€€€€€€ÌÜÜ°4(€€€€€€€€ÌÜà°4(€€€€€€€€ÌÜä°4(€€€€€€€€ÌàÀ°4(€€€€€€€€ÌàÄ°4(€€€€€€€€ÌàÈ°4(€€€€€€€€ÌàÌ°4(€€€€€€€€ÌàÔ°4(€€€€€€€€ÌàØ°4(€€€€€€€€ÌàÜ°4(€€€€€€€€Ìàà°4(€€€€€€€€Ìàä°4(€€€€€€€€ÌäÀ°4(€€€€€€€€ÌäÄ°4(€€€€€€€€ÌäÈ°4(€€€€€€€€ÌäÌ°4(€€€€€€€€ÌäÐ°4(€€€€€€€€ÌäÔ°4(€€€€€€€€ÌäØ°4(€€€€€€€€ÌäÜ°4(€€€€€€€€Ìäà°4(€€€ô4(¤4)}IQIe	1}%9AUQ}]%9II=IL€ô™É½é•¹Í•Ð¡ìÈ°€Ì°€Ô°€ÌÈ°€ÌÍô¤ð€ 4(€€€}1=U}%1M}]%9II=IL4(¤4(4(4)‘•˜}ÉÕ¹}™…¥±ÕÉ•}Ñ•áÐ 4(€€€•áŒè	…Í•á•ÁÑ¥½¸°4(€€€€¨°4(€€€Í½Á”è%¹•ÍÑ¥½¹M½Á”°4(€€€ÁÉ¥Ù…å}Í…™•}É½½Ðè‰½½°°4(¤€´øÍÑÈè4(€€€Ñ•áÐ€ô˜‰íÑåÁ”¡•áŒ¤¹}}¹…µ•}}ôèí•áôˆ4(€€€¥˜¹½ÐÁÉ¥Ù…å}Í…™•}É½½Ðè4(€€€€€€€É•ÑÕÉ¸Ñ•áÐ4(€€€Ù…±Õ•Ì€ômÑåÁ”¡•áŒ¤¹}}¹…µ•}|°€‰ÍÑ…”õÉÕ¸‰t4(€€€•ÉÉ½É}¹Õµ‰•È€ô•Ñ…ÑÑÈ¡•áŒ°€‰•ÉÉ¹¼ˆ°9½¹”¤4(€€€¥˜¥Í¥¹ÍÑ…¹”¡•ÉÉ½É}¹Õµ‰•È°¥¹Ð¤è4(€€€€€€€Ù…±Õ•Ì¹…ÁÁ•¹¡˜‰•ÉÉ¹¼õí•ÉÉ½É}¹Õµ‰•Éôˆ¤4(€€€Ý¥¹‘½ÝÍ}•ÉÉ½È€ô•Ñ…ÑÑÈ¡•áŒ°€‰Ý¥¹•ÉÉ½Èˆ°9½¹”¤4(€€€¥˜¥Í¥¹ÍÑ…¹”¡Ý¥¹‘½ÝÍ}•ÉÉ½È°¥¹Ð¤è4(€€€€€€€Ù…±Õ•Ì¹…ÁÁ•¹¡˜‰Ý¥¹•ÉÉ½ÈõíÝ¥¹‘½ÝÍ}•ÉÉ½Éôˆ¤4(€€€É•ÑÕÉ¸€ˆ€ˆ¹©½¥¸¡Ù…±Õ•Ì¤4(4(4)‘•˜}É•‘…Ñ}ÁÉ¥Ù…Ñ•}É½½Ñ}Ñ•áÐ¡Ù…±Õ”è¹ä°Í½Á”è%¹•ÍÑ¥½¹M½Á”¤€´øÍÑÈè4(€€€Ñ•áÐ€ôÍÑÈ¡Ù…±Õ”½È€ˆˆ¤4(€€€É½½ÑÌ€ôíÍÑÈ¡Í½Á”¹±½¥…±}É½½Ð¤°ÍÑÈ¡Í½Á”¹É•Í½±Ù•‘}É½½Ð¥ô4(€€€™½ÈÉ½½Ð¥¸Í½ÉÑ•¡É½½ÑÌ°­•äõ±•¸°É•Ù•ÉÍ”õQÉÕ”¤è4(€€€€€€€Á…ÉÑÌ€ômÁ…ÉÐ™½ÈÁ…ÉÐ¥¸É”¹ÍÁ±¥Ð¡È‰mqp½t¬ˆ°É½½Ð¤¥˜Á…ÉÑt4(€€€€€€€¥˜¹½ÐÁ…ÉÑÌè4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€Á…ÑÑ•É¸€ôÈ‰mqp½t¬ˆ¹©½¥¸¡É”¹•Í…Á”¡Á…ÉÐ¤™½ÈÁ…ÉÐ¥¸Á…ÉÑÌ¤4(€€€€€€€Ñ•áÐ€ôÉ”¹ÍÕˆ 4(€€€€€€€€€€€Á…ÑÑ•É¸°4(€€€€€€€€€€€€ˆñaQI91}M=UI}I==Pøˆ°4(€€€€€€€€€€€Ñ•áÐ°4(€€€€€€€€€€€™±…ÌõÉ”¹%9=IM°4(€€€€€€€€¤4(€€€É•ÑÕÉ¸Ñ•áÐ4(4(4)‘•˜}Á•ÉÍ¥ÍÑ•¹Ñ}Í½Á•}™¥•±‘Ì 4(€€€Í½Á”è%¹•ÍÑ¥½¹M½Á”°4(€€€€¨°4(€€€Í½ÕÉ•}¥èÍÑÈ°4(€€€ÁÉ¥Ù…å}Í…™•}É½½Ðè‰½½°°4(¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€¥˜¹½ÐÁÉ¥Ù…å}Í…™•}É½½Ðè4(€€€€€€€É•ÑÕÉ¸Í½Á”¹ÍÑ…Ñ•}™¥•±‘Ì¡Í½ÕÉ•}¥õÍ½ÕÉ•}¥¤4(€€€¥‘•¹Ñ¥Ñä€ô}ÁÉ¥Ù…Ñ•}É½½Ñ}¥‘•¹Ñ¥Ñä¡Í½Á”¤4(€€€É•ÑÕÉ¸ì4(€€€€€€€€‰É½½Ðˆè€ˆñaQI91}M=UI}I==Pøˆ°4(€€€€€€€€‰É•Í½±Ù•‘}É½½Ðˆè¥‘•¹Ñ¥Ñä°4(€€€€€€€€‰É½½Ñ}‘¥ÍÁ±…å}¹…µ”ˆèÍ½Á”¹É½½Ñ}‘¥ÍÁ±…å}¹…µ”°4(€€€€€€€€‰Í…¹}ÍÕ‰‘¥ÈˆèÍ½Á”¹Í…¹}ÍÕ‰‘¥È°4(€€€€€€€€‰Í…¹}É½½ÐˆèÍ½Á”¹Í…¹}ÍÕ‰‘¥È°4(€€€€€€€€‰ÍÑ½É•‘}Á…Ñ¡}ÁÉ•™¥àˆèÍ½Á”¹ÍÑ½É•‘}Á…Ñ¡}ÁÉ•™¥à°4(€€€€€€€€‰¥¹±Õ‘•}É½½Ñ}¹…µ•}¥¹}Á…Ñ ˆèQÉÕ”°4(€€€€€€€€‰Í½ÕÉ•}¥ˆèÍ½ÕÉ•}¥°4(€€€€€€€€‰ÁÉ¥Ù…å}Í…™•}É½½ÐˆèQÉÕ”°4(€€€ô4(4(4)‘•˜}µ¥É…Ñ•}ÁÉ¥Ù…Ñ•}Í½Á•}Á…Ñ¡Ì 4(€€€ÍÑ…Ñ”è‘¥ÑmÍÑÈ°¹åt°4(€€€€¨°4(€€€Í½Á”è%¹•ÍÑ¥½¹M½Á”°4(€€€Í½ÕÉ•}¥èÍÑÈ°4(€€€Á•ÉÍ¥ÍÑ•¹Ñ}É½½Ñ}¥‘•¹Ñ¥ÑäèÍÑÈ°4(¤€´ø9½¹”è4(€€€€ˆˆ‰I•µ½Ù”Ñ¡”ÕÉÉ•¹Ð•áÑ•É¹…°É½½Ð™É½´ÍÑ…Ñ”Ý¡¥±”É•Ñ…¥¹¥¹œ¥‘•¹Ñ¥Ñä¸ˆˆˆ4(4(€€€É…Ý}É½½Ð€ôÍÑÈ¡Í½Á”¹É•Í½±Ù•‘}É½½Ð¤4(€€€¥¹•ÍÑ¥½¸€ôÍÑ…Ñ”¹•Ð ‰¥¹•ÍÑ¥½¸ˆ¤4(€€€¥˜¥Í¥¹ÍÑ…¹”¡¥¹•ÍÑ¥½¸°‘¥Ð¤…¹ÍÑÈ 4(€€€€€€€¥¹•ÍÑ¥½¸¹•Ð ‰Í½ÕÉ•}¥ˆ¤½È€ˆˆ4(€€€€¤€ôôÍ½ÕÉ•}¥è4(€€€€€€€¥¹•ÍÑ¥½¸¹ÕÁ‘…Ñ” 4(€€€€€€€€€€€}Á•ÉÍ¥ÍÑ•¹Ñ}Í½Á•}™¥•±‘Ì 4(€€€€€€€€€€€€€€€Í½Á”°4(€€€€€€€€€€€€€€€Í½ÕÉ•}¥õÍ½ÕÉ•}¥°4(€€€€€€€€€€€€€€€ÁÉ¥Ù…å}Í…™•}É½½ÐõQÉÕ”°4(€€€€€€€€€€€€¤4(€€€€€€€€¤4(€€€™½È¥Ñ•´¥¸ÍÑ…Ñ”¹•Ð ‰™¥±•Ìˆ°íô¤¹Ù…±Õ•Ì ¤è4(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€¥˜ÍÑÈ¡¥Ñ•´¹•Ð ‰Í½ÕÉ•}¥ˆ¤½È€ˆˆ¤€„ôÍ½ÕÉ•}¥è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€¥˜ÍÑÈ¡¥Ñ•´¹•Ð ‰É•Í½±Ù•‘}É½½Ðˆ¤½È€ˆˆ¤¥¸ì4(€€€€€€€€€€€É…Ý}É½½Ð°4(€€€€€€€€€€€Á•ÉÍ¥ÍÑ•¹Ñ}É½½Ñ}¥‘•¹Ñ¥Ñä°4(€€€€€€€ôè4(€€€€€€€€€€€¥Ñ•µl‰É•Í½±Ù•‘}É½½Ð‰t€ôÁ•ÉÍ¥ÍÑ•¹Ñ}É½½Ñ}¥‘•¹Ñ¥Ñä4(