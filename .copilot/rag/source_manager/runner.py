from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .checkpoints import complete_run, new_run_state
from .errors import (
    SourceManagerError,
    exception_summary,
    sanitize_diagnostic,
)
from .execution import execute_fetch_plan, validate_managed_work_tree
from .diagnostics import exception_diagnostic, process_diagnostic
from .gitlab_issues import (
    GITLAB_ISSUE_IDS_STATE_KEY,
    GITLAB_ISSUES_CUTOFF_STATE_KEY,
    GITLAB_PROJECT_ID_STATE_KEY,
    generated_gitlab_issues_link,
    gitlab_issues_updated_after,
    repair_generated_gitlab_issues_link,
)
from .github_content import (
    generated_github_issues_link,
    generated_github_wiki_link,
)
from .metadata import publish_source_metadata
from .networking import resolve_source_network_route
from .providers import validate_provider_config
from .redmine import (
    REDMINE_CUTOFF_STATE_KEY,
    redmine_updated_on_cutoff,
    repair_generated_redmine_link,
)
from .store import MISSING_ETAG, SourceStore, StoredJson
from .subprocess_stream import (
    ProgressCallback,
    ResultExtractionError,
    extract_json_result,
    run_streaming_process,
)


FetchExecutor = Callable[[dict[str, Any], Path, dict[str, Any]], Mapping[str, Any]]
CommandRunner = Callable[[list[str]], Any]
MetadataPublisher = Callable[[Path, Mapping[str, Any], Path], None]


def _emit_progress(
    callback: ProgressCallback | None,
    event: Mapping[str, Any],
) -> None:
    if callback is None:
        return
    try:
        callback(dict(event))
    except Exception:
        return


def _persistable_http_diagnostic(
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove secret-bearing header entries before durable event storage.

    The live UI may show the header name with a ``<REDACTED>`` value, while
    the Source event contract intentionally rejects credential-like keys as
    well as values.  Persist non-secret headers and only a count for redacted
    entries.
    """
    value = copy.deepcopy(dict(event))
    for field in ("request_headers", "response_headers"):
        headers = value.get(field)
        if not isinstance(headers, Mapping):
            continue
        visible: dict[str, Any] = {}
        redacted_count = 0
        for name, header_value in headers.items():
            if str(header_value) == "<REDACTED>":
                redacted_count += 1
            else:
                visible[str(name)] = header_value
        value[field] = visible
        if redacted_count:
            value[f"{field}_redacted_count"] = redacted_count
    return value


def register_source(
    db_root: Path,
    *,
    source_type: str,
    display_name: str,
    fetch: Mapping[str, Any],
    local_source_key: str | None = None,
    source_id: str | None = None,
    link: Mapping[str, Any] | None = None,
    runtime_input: str | Path | None = None,
    start: bool = False,
    python_executable: str | Path | None = None,
    rag_root: str | Path | None = None,
    executor: FetchExecutor | None = None,
    command_runner: CommandRunner | None = None,
    http_get: Callable[..., Any] | None = None,
    environment: Mapping[str, str] | None = None,
    metadata_publisher: MetadataPublisher | None = None,
    clock: Callable[[], datetime] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Register one provisional Source without assigning indexed identity."""
    values = dict(fetch)
    runtime_path = runtime_input or values.pop("runtime_path", None)
    runtime = _validate_registration_runtime(source_type, runtime_path)
    store = SourceStore(db_root)
    effective_link = link
    if str(source_type).strip().lower() == "redmine":
        effective_link = repair_generated_redmine_link(
            values.get("project_url"),
            link,
        )
    elif str(source_type).strip().lower() == "gitlab_issues":
        effective_link = (
            generated_gitlab_issues_link(
                values.get("project_url"),
                values.get("gitlab_url"),
            )
            if link is None
            else repair_generated_gitlab_issues_link(
                values.get("project_url"),
                values.get("gitlab_url"),
                link,
            )
        )
    elif str(source_type).strip().lower() == "github_issues":
        effective_link = (
            generated_github_issues_link(values.get("repository_url"))
            if link is None
            else link
        )
    elif str(source_type).strip().lower() == "github_wiki":
        effective_link = (
            generated_github_wiki_link(values.get("repository_url"))
            if link is None
            else link
        )
    stored = store.create_source(
        source_type=source_type,
        display_name=display_name,
        fetch=values,
        local_source_key=local_source_key,
        source_id=source_id,
        link=effective_link,
    )
    if runtime is not None:
        plan = store.plan(stored.payload)
        state = new_run_state(plan)
        state["runtime"] = {"input_path": str(runtime)}
        store.save_state(
            stored.payload["local_source_key"],
            state,
            expected_revision=0,
            expected_etag=MISSING_ETAG,
        )
    store.append_event(
        stored.payload["local_source_key"],
        "source.registered",
        {"source_type": source_type},
    )
    if start:
        try:
            return update_source(
                db_root,
                stored.payload["local_source_key"],
                executor=executor,
                command_runner=command_runner,
                http_get=http_get,
                environment=environment,
                python_executable=python_executable,
                rag_root=rag_root,
                metadata_publisher=metadata_publisher,
                clock=clock,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            _attach_registration_failure(
                exc,
                local_source_key=stored.payload["local_source_key"],
                events_jsonl=store.paths(
                    stored.payload["local_source_key"]
                ).events_jsonl,
            )
            raise
    return _source_dto(store, stored)


def list_sources(db_root: Path) -> list[dict[str, Any]]:
    store = SourceStore(db_root)
    values: list[dict[str, Any]] = []
    for key in store.list_keys():
        try:
            values.append(_source_dto(store, store.read_source(key)))
        except SourceManagerError:
            values.append(
                {
                    "local_source_key": key,
                    "status": "invalid",
                }
            )
    return values


def update_source(
    db_root: Path,
    local_source_key: str,
    *,
    executor: FetchExecutor | None = None,
    python_executable: str | Path | None = None,
    rag_root: str | Path | None = None,
    command_runner: CommandRunner | None = None,
    http_get: Callable[..., Any] | None = None,
    environment: Mapping[str, str] | None = None,
    metadata_publisher: MetadataPublisher | None = None,
    runtime_input: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Prepare or execute one fetch; network access exists only in executor."""
    store = SourceStore(db_root)
    source = store.read_source(local_source_key)
    if not source.payload:
        raise SourceManagerError("Source does not exist")
    if source.payload.get("source_type") == "redmine":
        source = _normalize_redmine_source(store, source)
    elif source.payload.get("source_type") == "gitlab_issues":
        source = _normalize_gitlab_issues_source(store, source)
    runtime = _validate_registration_runtime(
        str(source.payload["source_type"]),
        runtime_input,
    )
    provider_command_runner = command_runner
    provider_http_get = http_get
    provider_environment = environment
    existing_state = store.read_state(local_source_key)
    if (
        source.payload.get("source_id")
        and source.payload.get("metadata_sync_pending")
        and (
            not existing_state.payload
            or existing_state.payload.get("phase")
            in {"metadata", "complete"}
        )
    ):
        return _resume_metadata_sync(
            store,
            source,
            rag_root=rag_root,
            metadata_publisher=metadata_publisher,
        )
    if (
        executor is None
        and source.payload.get("source_type")
        in {
            "github",
            "github_issues",
            "github_wiki",
            "svn",
            "redmine",
            "gitlab_issues",
        }
        and command_runner is None
        and http_get is None
        and rag_root is not None
    ):
        route = resolve_source_network_route(
            Path(rag_root),
            environment=environment,
            progress_callback=progress_callback,
        )
        provider_command_runner = route.command_runner
        provider_http_get = route.http_get
        provider_environment = route.environment
    if (
        source.payload.get("source_type") == "redmine"
        and executor is None
        and python_executable is not None
        and rag_root is not None
    ):
        return _update_redmine_source(
            store,
            source,
            existing_state,
            python_executable=Path(python_executable),
            rag_root=Path(rag_root),
            command_runner=command_runner,
            http_get=provider_http_get,
            environment=provider_environment,
            metadata_publisher=metadata_publisher,
            clock=clock,
            progress_callback=progress_callback,
        )
    if (
        source.payload.get("source_type") == "gitlab_issues"
        and executor is None
        and python_executable is not None
        and rag_root is not None
    ):
        return _update_gitlab_issues_source(
            store,
            source,
            existing_state,
            python_executable=Path(python_executable),
            rag_root=Path(rag_root),
            command_runner=command_runner,
            http_get=provider_http_get,
            environment=provider_environment,
            metadata_publisher=metadata_publisher,
            clock=clock,
            progress_callback=progress_callback,
        )
    plan = store.plan(source.payload)
    state_stored = existing_state
    if (
        state_stored.payload
        and state_stored.payload.get("phase") == "reflect"
        and int(state_stored.payload.get("pending_count") or 0) > 0
        and source.payload.get("source_type") != "sharepoint"
    ):
        return _resume_add_only(
            store,
            source,
            state_stored,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            metadata_publisher=metadata_publisher,
            progress_callback=progress_callback,
        )
    if (
        state_stored.payload
        and state_stored.payload.get("status") != "complete"
    ):
        state = copy.deepcopy(state_stored.payload)
        state["plan_etag"] = plan.plan_etag
        state["status"] = "planned"
    else:
        state = new_run_state(plan)
    if runtime is not None:
        state["runtime"] = {"input_path": str(runtime)}
    saved_state = store.save_state(
        local_source_key,
        state,
        expected_revision=state_stored.revision,
        expected_etag=state_stored.etag,
    )
    work = store.ensure_work_directory(local_source_key)
    _emit_progress(
        progress_callback,
        {
            "phase": f"{source.payload['source_type']}.fetch",
            "label_ja": "取得開始",
            "provider": source.payload["source_type"],
            "total_kind": "unknown",
            "status": "running",
        },
    )
    effective_executor = executor
    if (
        effective_executor is None
        and python_executable is not None
        and rag_root is not None
    ):
        effective_executor = lambda plan_value, work_value, state_value: (
            execute_fetch_plan(
                plan_value,
                work_value,
                state_value,
                command_runner=provider_command_runner,
                http_get=provider_http_get,
                environment=provider_environment,
                clock=clock,
                progress_callback=progress_callback,
            )
        )
    if effective_executor is None:
        result = _source_dto(store, source)
        result.update(
            {
                "status": "planned",
                "fetch_plan": plan.to_dict(),
                "add_request": _add_request(source.payload, work),
                "execution_context": {
                    "python_executable": (
                        str(python_executable) if python_executable else None
                    ),
                    "rag_root": str(rag_root) if rag_root else None,
                },
            }
        )
        return result

    runtime_state = copy.deepcopy(saved_state.payload)
    try:
        outcome = dict(
            effective_executor(plan.to_dict(), work, runtime_state)
        )
    except Exception as exc:
        runtime_state.update(
            {
                "status": "interrupted",
                "phase": "fetch",
                "can_resume": True,
                "last_error": exception_summary(exc),
            }
        )
        try:
            store.save_state(
                local_source_key,
                runtime_state,
                expected_revision=saved_state.revision,
                expected_etag=saved_state.etag,
            )
            store.append_event(
                local_source_key,
                "fetch.interrupted",
                {"error": exception_summary(exc)},
            )
        except Exception:
            # Recording recovery state is best effort. A Windows sharing or
            # access-denied error here must not replace the original fetch
            # exception that explains why the operation stopped.
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", f"fetch.{source.payload['source_type']}")
        raise
    if outcome.get("status") not in {"ok", "complete"}:
        failure_detail = sanitize_diagnostic(
            outcome.get("error") or "fetch_failed",
            max_chars=4_000,
        )
        runtime_state["status"] = "failed"
        runtime_state["phase"] = "fetch"
        runtime_state["can_resume"] = True
        runtime_state["last_error"] = failure_detail
        failed = store.save_state(
            local_source_key,
            runtime_state,
            expected_revision=saved_state.revision,
            expected_etag=saved_state.etag,
        )
        store.append_event(
            local_source_key,
            "fetch.failed",
            {"error": runtime_state["last_error"]},
        )
        return {
            **_source_dto(store, source),
            "status": "failed",
            "state_revision": failed.revision,
            "failure_stage": "fetch",
            "error": failure_detail,
            "error_type": str(
                outcome.get("error_type") or "ProviderResultError"
            ),
            "events_jsonl": store.paths(
                local_source_key
            ).events_jsonl,
        }

    source, link_pending = _apply_fetch_metadata(store, source, outcome)
    if source.payload["source_type"] == "other":
        runtime_state["runtime"] = {
            "input_path": "<REDACTED_AFTER_IMPORT>"
        }
    fetched_count = int(outcome.get("documents") or 0)
    runtime_state.update(
        {
            "status": "fetched",
            "phase": "reflect",
            "fetched_count": fetched_count,
            "pending_count": max(
                0,
                fetched_count
                - int(runtime_state.get("indexed_confirmed_count") or 0),
            ),
            "can_resume": True,
            "last_error": None,
        }
    )
    if link_pending:
        runtime_state["link_configuration_pending"] = True
    else:
        runtime_state.pop("link_configuration_pending", None)
    fetched = store.save_state(
        local_source_key,
        runtime_state,
        expected_revision=saved_state.revision,
        expected_etag=saved_state.etag,
    )
    store.append_event(
        local_source_key,
        "fetch.completed",
        {
            "provider": source.payload["source_type"],
            "documents": int(outcome.get("documents") or 0),
        },
    )
    _emit_progress(
        progress_callback,
        {
            "phase": f"{source.payload['source_type']}.fetch",
            "label_ja": "取得",
            "provider": source.payload["source_type"],
            "completed": fetched_count,
            "unit": "件",
            "total_kind": "unknown",
            "status": "completed",
            "checkpoint_saved": True,
        },
    )
    result = {
        **_source_dto(store, source),
        "status": "fetched",
        "state_revision": fetched.revision,
        "add_request": _add_request(source.payload, work),
    }
    if python_executable is not None and rag_root is not None:
        reflection_root = (
            Path(str(outcome["external_add_root"]))
            if outcome.get("external_add_root")
            else work
        )
        result = _reflect_and_sync(
            store,
            source,
            fetched,
            add_root=reflection_root,
            python_executable=Path(python_executable),
            rag_root=Path(rag_root),
            command_runner=command_runner,
            metadata_publisher=metadata_publisher,
            progress_callback=progress_callback,
        )
    return result


def update_all_sources(
    db_root: Path,
    *,
    executor: FetchExecutor | None = None,
    python_executable: str | Path | None = None,
    rag_root: str | Path | None = None,
    command_runner: CommandRunner | None = None,
    http_get: Callable[..., Any] | None = None,
    environment: Mapping[str, str] | None = None,
    metadata_publisher: MetadataPublisher | None = None,
    clock: Callable[[], datetime] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    source_items = list_sources(db_root)
    for source_index, item in enumerate(source_items, start=1):
        key = str(item.get("local_source_key") or "")
        if not key or item.get("status") == "invalid":
            results.append(item)
            _emit_source_count_progress(
                progress_callback,
                item,
                source_index,
                len(source_items),
            )
            continue
        if (
            item.get("source_type") == "other"
            and item.get("source_id")
            and not item.get("metadata_sync_pending")
        ):
            results.append(
                {
                    **item,
                    "status": "skipped",
                    "skip_reason": "one_shot_source_complete",
                }
            )
            _emit_source_count_progress(
                progress_callback,
                item,
                source_index,
                len(source_items),
            )
            continue
        if item.get("source_type") == "sharepoint" and not _is_windows():
            results.append(
                {
                    **item,
                    "status": "skipped",
                    "skip_reason": "sharepoint_update_requires_windows",
                }
            )
            _emit_source_count_progress(
                progress_callback,
                item,
                source_index,
                len(source_items),
            )
            continue
        try:
            results.append(
                update_source(
                    db_root,
                    key,
                    executor=executor,
                    python_executable=python_executable,
                    rag_root=rag_root,
                    command_runner=command_runner,
                    http_get=http_get,
                    environment=environment,
                    metadata_publisher=metadata_publisher,
                    clock=clock,
                    progress_callback=progress_callback,
                )
            )
        except SourceManagerError as exc:
            paths = SourceStore(db_root).paths(key)
            failure_diagnostic = exception_diagnostic(
                exc,
                operation="Source更新・再開",
                stage=str(getattr(exc, "stage", None) or "source.update"),
                db_name=Path(db_root).name,
                source_name=str(item.get("display_name") or ""),
                source_key=key,
                provider=str(item.get("source_type") or ""),
                can_resume=True,
                events_jsonl=str(paths.events_jsonl),
            )
            results.append(
                {
                    "display_name": item.get("display_name"),
                    "source_type": item.get("source_type"),
                    "local_source_key": key,
                    "status": "failed",
                    "failure_stage": getattr(exc, "stage", None),
                    "error_type": type(exc).__name__,
                    "error": exception_summary(exc),
                    "events_jsonl": paths.events_jsonl,
                    "failure_diagnostic": failure_diagnostic,
                    "process_diagnostic": (
                        getattr(exc, "process_diagnostic")
                        if isinstance(
                            getattr(exc, "process_diagnostic", None),
                            dict,
                        )
                        else None
                    ),
                }
            )
        finally:
            _emit_source_count_progress(
                progress_callback,
                item,
                source_index,
                len(source_items),
            )
    failed = [item for item in results if item.get("status") == "failed"]
    partial = [item for item in results if item.get("status") == "partial"]
    blocking_skips = [
        item
        for item in results
        if item.get("skip_reason") == "sharepoint_update_requires_windows"
    ]
    updateable = [
        item
        for item in results
        if item.get("skip_reason") != "one_shot_source_complete"
    ]
    successful_statuses = {"updated", "complete", "success"}
    completed_source_count = sum(
        1
        for item in updateable
        if item.get("status") in successful_statuses
    )
    return {
        "status": (
            "ok"
            if not failed and not partial
            else "partial"
        ),
        "source_count": len(results),
        "updateable_source_count": len(updateable),
        "completed_source_count": completed_source_count,
        "snapshot_marker_eligible": (
            not failed
            and not partial
            and not blocking_skips
            and completed_source_count == len(updateable)
        ),
        "results": results,
    }


def _emit_source_count_progress(
    progress_callback: ProgressCallback | None,
    item: Mapping[str, Any],
    completed: int,
    total: int,
) -> None:
    _emit_progress(
        progress_callback,
        {
            "phase": "sources",
            "label_ja": "Source単位の進捗",
            "provider": item.get("source_type"),
            "completed": completed,
            "total": total,
            "unit": " Source",
            "total_kind": "exact",
            "current_item": str(
                item.get("display_name")
                or item.get("local_source_key")
                or ""
            ),
            "status": "completed" if completed == total else "running",
            "checkpoint_saved": True,
        },
    )


def _is_windows() -> bool:
    return os.name == "nt"


_UNSET = object()


def update_source_configuration(
    db_root: Path,
    local_source_key: str,
    *,
    fetch: Mapping[str, Any],
    display_name: str | None = None,
    pending_link: Mapping[str, Any] | None | object = _UNSET,
) -> dict[str, Any]:
    """CAS-update editable Source settings without changing identity/paths."""
    store = SourceStore(db_root)
    source = store.read_source(local_source_key)
    if not source.payload:
        raise SourceManagerError("Source does not exist")
    state = store.read_state(local_source_key)
    if state.payload and state.payload.get("status") not in {
        "complete",
        "planned",
    }:
        raise SourceManagerError(
            "Source operation must be resumed before configuration changes"
        )
    normalized_fetch = validate_provider_config(
        str(source.payload["source_type"]),
        fetch,
    )
    if (
        source.payload.get("source_id")
        and source.payload.get("source_type") == "sharepoint"
        and normalized_fetch != source.payload.get("fetch")
    ):
        raise SourceManagerError(
            "sharepoint_ingestion_root_is_immutable_add_new_source"
        )
    if (
        source.payload.get("source_id")
        and source.payload.get("source_type") == "gitlab_issues"
    ):
        current_fetch = validate_provider_config(
            "gitlab_issues",
            source.payload.get("fetch") or {},
        )
        if any(
            normalized_fetch.get(key) != current_fetch.get(key)
            for key in ("gitlab_url", "project_url")
        ):
            raise SourceManagerError(
                "gitlab_issue_project_is_immutable_add_new_source"
            )
    payload = copy.deepcopy(source.payload)
    payload["fetch"] = normalized_fetch
    if display_name is not None:
        payload["display_name"] = str(display_name)
    if pending_link is not _UNSET:
        if pending_link is None:
            payload.pop("pending_metadata", None)
        else:
            payload["pending_metadata"] = {
                "source_type": payload["source_type"],
                "link": copy.deepcopy(dict(pending_link)),
            }
    saved = store.save_source(
        payload,
        expected_revision=source.revision,
        expected_etag=source.etag,
    )
    store.append_event(local_source_key, "source.configuration_updated")
    return _source_dto(store, saved)


def confirm_add_success(
    db_root: Path,
    local_source_key: str,
    *,
    source_id: str,
    result_status: str = "success",
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    store = SourceStore(db_root)
    loaded = store.read_source(local_source_key)
    if not loaded.payload:
        raise SourceManagerError("Source does not exist")
    confirmed = store.confirm_source_id(
        local_source_key,
        source_id,
        expected_revision=loaded.revision,
        expected_etag=loaded.etag,
    )
    status = str(result_status or "success")
    event_payload: dict[str, Any] = {
        "source_id": confirmed.payload["source_id"]
    }
    if status == "partial" and isinstance(summary, Mapping):
        event_payload.update(
            {
                "result_status": "partial",
                "indexed_files": int(summary.get("indexed_files") or 0),
                "skipped_files": int(summary.get("skipped_files") or 0),
                "input_error_files": int(
                    summary.get("input_error_files") or 0
                ),
                "error_details": list(summary.get("error_details") or [])[:100],
            }
        )
    store.append_event(
        local_source_key,
        "add.partial" if status == "partial" else "add.completed",
        event_payload,
    )
    return _source_dto(store, confirmed)


def _apply_fetch_metadata(
    store: SourceStore,
    source: StoredJson,
    outcome: Mapping[str, Any],
) -> tuple[StoredJson, bool]:
    payload = copy.deepcopy(source.payload)
    if payload["source_type"] != "github":
        return source, False
    # Once ADD established Source identity, the canonical sidecar is the sole
    # active Link truth. Refreshing repository content must not overwrite a
    # human-edited ref or permalink with a newly observed default branch.
    if payload.get("source_id"):
        return source, False
    branch = str(outcome.get("default_branch") or "").strip()
    browser = _github_browser_root(
        str(payload["fetch"].get("repository_url") or "")
    )
    if branch and browser:
        payload["pending_metadata"] = {
            "source_type": "github",
            "link": {
                "enabled": True,
                "strategy": "github-blob",
                "settings": {
                    "repository_url": browser,
                    "ref": branch,
                    "permalink_enabled": False,
                },
            },
        }
    else:
        return source, True
    saved = store.save_source(
        payload,
        expected_revision=source.revision,
        expected_etag=source.etag,
    )
    return saved, False


def _normalize_redmine_source(
    store: SourceStore,
    source: StoredJson,
) -> StoredJson:
    """Refresh legacy derived fetch fields and known generated Link defaults."""
    payload = copy.deepcopy(source.payload)
    normalized_fetch = validate_provider_config(
        "redmine",
        payload.get("fetch") or {},
    )
    changed = normalized_fetch != payload.get("fetch")
    payload["fetch"] = normalized_fetch
    pending = payload.get("pending_metadata")
    if isinstance(pending, dict) and isinstance(pending.get("link"), Mapping):
        repaired = repair_generated_redmine_link(
            normalized_fetch["project_url"],
            pending["link"],
        )
        if repaired is not None and repaired != pending["link"]:
            pending = copy.deepcopy(pending)
            pending["link"] = repaired
            payload["pending_metadata"] = pending
            changed = True
    if not changed:
        return source
    return store.save_source(
        payload,
        expected_revision=source.revision,
        expected_etag=source.etag,
    )


def _normalize_gitlab_issues_source(
    store: SourceStore,
    source: StoredJson,
) -> StoredJson:
    """Normalize GitLab settings and keep a generated Issue Link current."""

    payload = copy.deepcopy(source.payload)
    normalized_fetch = validate_provider_config(
        "gitlab_issues",
        payload.get("fetch") or {},
    )
    changed = normalized_fetch != payload.get("fetch")
    payload["fetch"] = normalized_fetch
    pending = payload.get("pending_metadata")
    if isinstance(pending, dict) and isinstance(pending.get("link"), Mapping):
        repaired = repair_generated_gitlab_issues_link(
            normalized_fetch["project_url"],
            normalized_fetch["gitlab_url"],
            pending["link"],
        )
        if repaired is not None and repaired != pending["link"]:
            pending = copy.deepcopy(pending)
            pending["link"] = repaired
            payload["pending_metadata"] = pending
            changed = True
    if not changed:
        return source
    return store.save_source(
        payload,
        expected_revision=source.revision,
        expected_etag=source.etag,
    )


def _github_browser_root(fetch_url: str) -> str | None:
    split = urlsplit(fetch_url)
    if (
        split.scheme.casefold() != "https"
        or not split.netloc
        or str(split.hostname or "").casefold() != "github.com"
    ):
        return None
    if split.username is not None or split.password is not None:
        return None
    path = split.path.rstrip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    components = [value for value in path.split("/") if value]
    if len(components) < 2:
        return None
    return urlunsplit(("https", split.netloc, path, "", "")).rstrip("/")


def _add_request(source: Mapping[str, Any], work: Path) -> dict[str, Any]:
    pending_source_id = str(
        source.get("source_id") or source["local_source_key"]
    )
    arguments = [
        "--root",
        str(work),
        "--source-id",
        pending_source_id,
        "--include-root-name-in-path",
    ]
    return {
        "root": str(work),
        "source_id": pending_source_id,
        "arguments": arguments,
    }


def _source_dto(store: SourceStore, stored: StoredJson) -> dict[str, Any]:
    value = stored.payload
    paths = store.paths(value["local_source_key"])
    return {
        "local_source_key": value["local_source_key"],
        "source_id": value.get("source_id"),
        "source_type": value["source_type"],
        "display_name": value["display_name"],
        "status": "registered",
        "metadata_sync_pending": bool(
            value.get("metadata_sync_pending")
        ),
        "link_configured": isinstance(
            (value.get("pending_metadata") or {}).get("link")
            if isinstance(value.get("pending_metadata"), dict)
            else None,
            dict,
        ),
        "pending_metadata": copy.deepcopy(
            value.get("pending_metadata")
        ),
        "revision": stored.revision,
        "etag": stored.etag,
        "paths": {
            "source_json": paths.source_json,
            "state_json": paths.state_json,
            "events_jsonl": paths.events_jsonl,
            "work_directory": paths.work_directory,
            "logical_root_name": paths.logical_root_name,
        },
    }


def _execute_add(
    *,
    db_root: Path,
    source: Mapping[str, Any],
    work: Path,
    python_executable: Path,
    rag_root: Path,
    command_runner: CommandRunner | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    key = str(source["local_source_key"])
    privacy_safe_root = (
        str(source.get("source_type") or "").strip().lower()
        == "sharepoint"
    )
    arguments = [
        str(python_executable),
        str(rag_root / "gen_db" / "add_data.py"),
        "--db",
        db_root.name,
        "--root",
        str(work),
        "--source-id",
        key,
        "--include-root-name-in-path",
        "--retry-errors",
        "--manager-protocol-v1",
    ]
    if privacy_safe_root:
        arguments.append("--privacy-safe-root")
    started = time.monotonic()
    completed = (
        command_runner(arguments)
        if command_runner is not None
        else run_streaming_process(
            arguments,
            progress_callback=progress_callback,
        )
    )
    elapsed = time.monotonic() - started
    returncode = int(getattr(completed, "returncode", 1))
    if returncode != 0:
        raw_stderr = _redact_external_root(
            getattr(completed, "stderr", ""),
            work,
        ) if privacy_safe_root else str(getattr(completed, "stderr", "") or "")
        raw_stdout = _redact_external_root(
            getattr(completed, "stdout", ""),
            work,
        ) if privacy_safe_root else str(getattr(completed, "stdout", "") or "")
        stderr = sanitize_diagnostic(
            raw_stderr,
            max_chars=65_536,
        )
        stdout = sanitize_diagnostic(
            raw_stdout,
            max_chars=65_536,
        )
        details: list[str] = []
        if stderr:
            details.append(f"標準エラー:\n{stderr}")
        if stdout:
            details.append(f"標準出力:\n{stdout}")
        suffix = "\n" + "\n".join(details) if details else ""
        error = SourceManagerError(
            "ADD failed: 検索への反映に失敗しました"
            f"（終了コード: {returncode}）。{suffix}",
            stage="reflect.add",
        )
        safe_arguments = list(arguments)
        if privacy_safe_root:
            try:
                root_index = safe_arguments.index("--root") + 1
                safe_arguments[root_index] = "<EXTERNAL_SOURCE_ROOT>"
            except (ValueError, IndexError):
                pass
        error.process_diagnostic = process_diagnostic(
            arguments=safe_arguments,
            cwd=rag_root,
            returncode=returncode,
            elapsed_seconds=elapsed,
            stdout=raw_stdout,
            stderr=raw_stderr,
        )
        raise error

    def validate_add_result(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if value.get("source_id") != key:
            return False
        # This boundary invokes add_data.py without --operation=build.  A
        # build-shaped summary must not be accepted as proof that this ADD
        # reflected the requested Source.
        if value.get("operation") != "add":
            return False
        for field in (
            "file_count",
            "indexed_files",
            "skipped_files",
            "error_files",
            "upserted_records",
            "deleted_records",
        ):
            count = value.get(field)
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                return False
        for field in ("input_error_files", "extract_error_files"):
            if field not in value:
                continue
            count = value.get(field)
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                return False
        if "result_status" in value and value.get("result_status") not in {
            "success",
            "partial",
            "failure",
        }:
            return False
        return True

    try:
        summary = extract_json_result(
            completed,
            validator=validate_add_result,
        )
    except ResultExtractionError as exc:
        diagnostic_codes = sorted(
            {
                str(item.get("error"))
                for item in exc.diagnostics
                if item.get("error")
            }
        )
        suffix = (
            f" ({', '.join(diagnostic_codes)})"
            if diagnostic_codes
            else ""
        )
        raise SourceManagerError(
            "ADD did not return exactly one trusted JSON result "
            "with trusted source_id and schema"
            + suffix
            + "\nJSON候補診断:\n"
            + sanitize_diagnostic(
                json.dumps(
                    list(exc.diagnostics),
                    ensure_ascii=False,
                    indent=2,
                ),
                max_chars=65_536,
            ),
            stage="reflect.add",
        ) from exc
    reported_source_id = summary.get("source_id")
    if not isinstance(reported_source_id, str) or reported_source_id != key:
        raise SourceManagerError(
            "ADD did not return the requested trusted source_id",
            stage="reflect.add",
        )
    error_files = int(summary.get("error_files") or 0)
    input_error_files = int(summary.get("input_error_files") or 0)
    extract_error_files = int(summary.get("extract_error_files") or 0)
    result_status = str(summary.get("result_status") or "")
    if not result_status:
        result_status = "failure" if error_files else "success"
    completed_files = int(summary.get("indexed_files") or 0) + int(
        summary.get("skipped_files") or 0
    )
    valid_partial = (
        result_status == "partial"
        and error_files > 0
        and input_error_files == error_files
        and extract_error_files == 0
        and completed_files > 0
    )
    if (
        result_status == "failure"
        or (result_status == "partial" and not valid_partial)
        or (error_files > 0 and not valid_partial)
    ):
        error = SourceManagerError(
            "ADD failed: "
            f"{error_files:,}件のファイル処理に失敗しました。"
            "失敗ファイルは次回の再開時に再試行します。",
            stage="reflect.add",
        )
        error.diagnostic = {
            "result_status": "failure",
            "error_files": error_files,
            "input_error_files": input_error_files,
            "extract_error_files": extract_error_files,
            "error_details": list(summary.get("error_details") or [])[:100],
        }
        if not privacy_safe_root:
            error.process_diagnostic = process_diagnostic(
                arguments=arguments,
                cwd=rag_root,
                returncode=returncode,
                elapsed_seconds=elapsed,
                stdout=getattr(completed, "stdout", ""),
                stderr=getattr(completed, "stderr", ""),
            )
        raise error
    return {
        "source_id": reported_source_id,
        "status": result_status,
        "summary": summary,
    }


def _redact_external_root(value: Any, root: Path) -> str:
    text = str(value or "")
    candidates = {
        str(root),
        str(Path(os.path.abspath(root))),
        str(root).replace("\\", "/"),
        str(root).replace("/", "\\"),
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            text = re.sub(
                re.escape(candidate),
                "<EXTERNAL_SOURCE_ROOT>",
                text,
                flags=re.IGNORECASE,
            )
    return text


def _update_redmine_source(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
    *,
    python_executable: Path,
    rag_root: Path,
    command_runner: CommandRunner | None,
    http_get: Callable[..., Any] | None,
    environment: Mapping[str, str] | None,
    metadata_publisher: MetadataPublisher | None,
    clock: Callable[[], datetime] | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    """Fetch Redmine serially and reflect each stable ADD batch."""
    plan = store.plan(source.payload)
    if not state.payload or state.payload.get("status") == "complete":
        initial = new_run_state(plan)
        cutoff = redmine_updated_on_cutoff(
            source.payload["fetch"].get("updated_within_days"),
            {},
            clock=clock,
        )
        if cutoff is not None:
            initial[REDMINE_CUTOFF_STATE_KEY] = cutoff
        initial["initial_database_reflection"] = (
            _is_initial_database_reflection(store, source)
        )
        current_state = store.save_state(
            source.payload["local_source_key"],
            initial,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
    else:
        resumed = copy.deepcopy(state.payload)
        resumed["plan_etag"] = plan.plan_etag
        cutoff = redmine_updated_on_cutoff(
            source.payload["fetch"].get("updated_within_days"),
            resumed,
            clock=clock,
        )
        if cutoff is not None:
            resumed[REDMINE_CUTOFF_STATE_KEY] = cutoff
        else:
            resumed.pop(REDMINE_CUTOFF_STATE_KEY, None)
        current_state = store.save_state(
            source.payload["local_source_key"],
            resumed,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
    current_source = source
    if (
        current_state.payload.get("phase") == "reflect"
        and int(current_state.payload.get("pending_count") or 0) > 0
    ):
        current_source, current_state, _summary = _redmine_reflect_batch(
            store,
            current_source,
            current_state,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
        )

    state_holder = [current_state]
    source_holder = [current_source]
    progress_total_holder = [
        len(current_state.payload.get("redmine_issue_ids") or [])
        or None
    ]
    detail_progress_high_water = [
        int(current_state.payload.get("fetched_count") or 0)
    ]

    def inventory_checkpoint(issue_ids: list[int]) -> None:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        value["redmine_issue_ids"] = [int(item) for item in issue_ids]
        value["updated_at"] = value.get("updated_at")
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        progress_total_holder[0] = len(issue_ids)
        _emit_progress(
            progress_callback,
            {
                "phase": "redmine.inventory",
                "label_ja": "Redmine Issue一覧取得",
                "provider": "redmine",
                "completed": len(issue_ids),
                "total": len(issue_ids),
                "unit": "件",
                "total_kind": "exact",
                "status": "completed",
                "checkpoint_saved": True,
            },
        )

    def item_checkpoint(completed_count: int, issue_id: int) -> None:
        stored = state_holder[0]
        completed = int(completed_count)
        is_new_progress = completed > detail_progress_high_water[0]
        value = copy.deepcopy(stored.payload)
        confirmed = int(value.get("indexed_confirmed_count") or 0)
        value.update(
            {
                "status": "running",
                "phase": "fetch",
                "fetched_count": completed,
                "pending_count": completed - confirmed,
                "last_completed_item": int(issue_id),
                "can_resume": True,
                "last_error": None,
            }
        )
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        total = progress_total_holder[0]
        if is_new_progress:
            detail_progress_high_water[0] = completed
        _emit_progress(
            progress_callback,
            {
                "phase": "redmine.detail",
                "label_ja": "Redmine Issue詳細取得",
                "provider": "redmine",
                "completed": completed,
                "total": total,
                "unit": "件",
                "total_kind": "exact" if total is not None else "unknown",
                "current_item": f"Issue #{int(issue_id)}",
                "status": "running" if is_new_progress else "replayed",
                "checkpoint_saved": True,
            },
        )

    def reflect_batch(completed_count: int, issue_id: int) -> None:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        value.update(
            {
                "status": "running",
                "phase": "reflect",
                "fetched_count": int(completed_count),
                "pending_count": (
                    int(completed_count)
                    - int(value.get("indexed_confirmed_count") or 0)
                ),
                "last_completed_item": int(issue_id),
                "can_resume": True,
            }
        )
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        _emit_progress(
            progress_callback,
            {
                "phase": "redmine.reflect",
                "label_ja": "検索反映",
                "provider": "redmine",
                "completed": int(
                    value.get("indexed_confirmed_count") or 0
                ),
                "total": progress_total_holder[0],
                "unit": "件",
                "total_kind": (
                    "exact"
                    if progress_total_holder[0] is not None
                    else "unknown"
                ),
                "current_item": f"Issue #{int(issue_id)}",
                "status": "running",
            },
        )
        (
            source_holder[0],
            state_holder[0],
            _summary,
        ) = _redmine_reflect_batch(
            store,
            source_holder[0],
            state_holder[0],
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
        )
        _emit_progress(
            progress_callback,
            {
                "phase": "redmine.reflect",
                "label_ja": "検索反映",
                "provider": "redmine",
                "completed": int(
                    state_holder[0].payload.get(
                        "indexed_confirmed_count"
                    )
                    or 0
                ),
                "total": progress_total_holder[0],
                "unit": "件",
                "total_kind": (
                    "exact"
                    if progress_total_holder[0] is not None
                    else "unknown"
                ),
                "status": "completed",
                "checkpoint_saved": True,
            },
        )

    resume_count = int(
        current_state.payload.get("indexed_confirmed_count") or 0
    )
    stable_ids_value = current_state.payload.get("redmine_issue_ids")
    stable_issue_ids = (
        [int(value) for value in stable_ids_value]
        if isinstance(stable_ids_value, list)
        else None
    )

    def redmine_progress(event: Mapping[str, Any]) -> None:
        if event.get("event") == "redmine.http_attempt":
            try:
                store.append_event(
                    source.payload["local_source_key"],
                    "redmine.http_attempt",
                    _persistable_http_diagnostic(event),
                )
            except Exception:
                pass
        _emit_progress(progress_callback, event)

    try:
        outcome = execute_fetch_plan(
            plan.to_dict(),
            store.ensure_work_directory(source.payload["local_source_key"]),
            current_state.payload,
            command_runner=command_runner,
            http_get=http_get,
            environment=environment,
            item_callback=item_checkpoint,
            batch_callback=reflect_batch,
            resume_count=resume_count,
            stable_issue_ids=stable_issue_ids,
            inventory_callback=inventory_checkpoint,
            clock=clock,
            progress_callback=redmine_progress,
        )
    except Exception as exc:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        error_detail = exception_summary(exc)
        value.update(
            {
                "status": "interrupted",
                "can_resume": True,
                "last_error": error_detail,
            }
        )
        try:
            state_holder[0] = store.save_state(
                source.payload["local_source_key"],
                value,
                expected_revision=stored.revision,
                expected_etag=stored.etag,
            )
        except Exception:
            # Preserve the provider failure even when Windows temporarily
            # denies access to the recovery state file.
            pass
        details: dict[str, Any] = {"error": error_detail}
        if isinstance(getattr(exc, "process_diagnostic", None), dict):
            details["process"] = getattr(exc, "process_diagnostic")
        if isinstance(getattr(exc, "diagnostic", None), dict):
            details["diagnostic"] = _persistable_http_diagnostic(
                getattr(exc, "diagnostic")
            )
        try:
            store.append_event(
                source.payload["local_source_key"],
                "redmine.fetch.interrupted",
                details,
            )
        except Exception:
            # The primary Redmine exception remains authoritative; diagnostics
            # persistence must not mask it.
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", "fetch.redmine")
        raise

    if not source_holder[0].payload.get("source_id"):
        completed = complete_run(state_holder[0].payload)
        store.save_state(
            source.payload["local_source_key"],
            completed,
            expected_revision=state_holder[0].revision,
            expected_etag=state_holder[0].etag,
        )
        return {
            **_source_dto(store, source_holder[0]),
            "status": "no_documents",
            "fetched_count": 0,
        }

    if bool(
        state_holder[0].payload.get("initial_database_reflection")
    ):
        _write_initial_snapshot_marker(store.db_root)
    sync_result = _synchronize_metadata(
        store,
        source_holder[0],
        rag_root=rag_root,
        metadata_publisher=metadata_publisher,
    )
    _emit_progress(
        progress_callback,
        {
            "phase": "metadata",
            "label_ja": "Source Metadata反映",
            "provider": "redmine",
            "completed": 1 if not sync_result.get("metadata_sync_pending") else 0,
            "total": 1,
            "unit": "件",
            "total_kind": "exact",
            "status": (
                "completed"
                if not sync_result.get("metadata_sync_pending")
                else "failed"
            ),
            "checkpoint_saved": not sync_result.get("metadata_sync_pending"),
        },
    )
    stored = state_holder[0]
    final = copy.deepcopy(stored.payload)
    final.update(
        {
            "fetched_count": int(outcome.get("documents") or 0),
            "pending_count": 0,
            "metadata_sync_pending": bool(
                sync_result.get("metadata_sync_pending")
            ),
            "last_error": sync_result.get("metadata_error"),
        }
    )
    if sync_result.get("metadata_sync_pending"):
        final.update(
            {
                "status": "interrupted",
                "phase": "metadata",
                "can_resume": True,
            }
        )
    else:
        final = complete_run(final)
    final_state = store.save_state(
        source.payload["local_source_key"],
        final,
        expected_revision=stored.revision,
        expected_etag=stored.etag,
    )
    return {
        **_source_dto(store, store.read_source(source.payload["local_source_key"])),
        **sync_result,
        "status": (
            "metadata_sync_pending"
            if sync_result.get("metadata_sync_pending")
            else "updated"
        ),
        "fetched_count": int(outcome.get("documents") or 0),
        "indexed_confirmed_count": int(
            final_state.payload.get("indexed_confirmed_count") or 0
        ),
        "state_revision": final_state.revision,
    }


def _update_gitlab_issues_source(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
    *,
    python_executable: Path,
    rag_root: Path,
    command_runner: CommandRunner | None,
    http_get: Callable[..., Any] | None,
    environment: Mapping[str, str] | None,
    metadata_publisher: MetadataPublisher | None,
    clock: Callable[[], datetime] | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    """Fetch GitLab Issues serially and reflect stable batches of five."""

    plan = store.plan(source.payload)
    if not state.payload or state.payload.get("status") == "complete":
        initial = new_run_state(plan)
        cutoff = gitlab_issues_updated_after(
            source.payload["fetch"].get("updated_within_days"),
            {},
            clock=clock,
        )
        if cutoff is not None:
            initial[GITLAB_ISSUES_CUTOFF_STATE_KEY] = cutoff
        initial["initial_database_reflection"] = (
            _is_initial_database_reflection(store, source)
        )
        current_state = store.save_state(
            source.payload["local_source_key"],
            initial,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
    else:
        resumed = copy.deepcopy(state.payload)
        resumed["plan_etag"] = plan.plan_etag
        cutoff = gitlab_issues_updated_after(
            source.payload["fetch"].get("updated_within_days"),
            resumed,
            clock=clock,
        )
        if cutoff is not None:
            resumed[GITLAB_ISSUES_CUTOFF_STATE_KEY] = cutoff
        else:
            resumed.pop(GITLAB_ISSUES_CUTOFF_STATE_KEY, None)
        current_state = store.save_state(
            source.payload["local_source_key"],
            resumed,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )

    current_source = source
    if (
        current_state.payload.get("phase") == "reflect"
        and int(current_state.payload.get("pending_count") or 0) > 0
    ):
        (
            current_source,
            current_state,
            _summary,
        ) = _gitlab_issues_reflect_batch(
            store,
            current_source,
            current_state,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
        )

    state_holder = [current_state]
    source_holder = [current_source]
    progress_total_holder = [
        len(current_state.payload.get(GITLAB_ISSUE_IDS_STATE_KEY) or [])
        or None
    ]

    def inventory_snapshot(
        project_id: int,
        issue_iids: list[int],
    ) -> None:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        value[GITLAB_PROJECT_ID_STATE_KEY] = int(project_id)
        value[GITLAB_ISSUE_IDS_STATE_KEY] = [
            int(item) for item in issue_iids
        ]
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        progress_total_holder[0] = len(issue_iids)
        _emit_progress(
            progress_callback,
            {
                "phase": "gitlab_issues.inventory",
                "label_ja": "GitLab Issue更新対象確定",
                "provider": "gitlab_issues",
                "completed": len(issue_iids),
                "total": len(issue_iids),
                "unit": "件",
                "total_kind": "exact",
                "status": "completed",
                "checkpoint_saved": True,
            },
        )

    def item_checkpoint(completed_count: int, issue_iid: int) -> None:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        confirmed = int(value.get("indexed_confirmed_count") or 0)
        value.update(
            {
                "status": "running",
                "phase": "fetch",
                "fetched_count": int(completed_count),
                "pending_count": int(completed_count) - confirmed,
                "last_completed_item": int(issue_iid),
                "can_resume": True,
                "last_error": None,
            }
        )
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        total = progress_total_holder[0]
        _emit_progress(
            progress_callback,
            {
                "phase": "gitlab_issues.detail",
                "label_ja": "GitLab Issue詳細・コメント取得",
                "provider": "gitlab_issues",
                "completed": int(completed_count),
                "total": total,
                "unit": "件",
                "total_kind": "exact" if total is not None else "unknown",
                "current_item": f"Issue #{int(issue_iid)}",
                "status": "running",
                "checkpoint_saved": True,
            },
        )

    def no_change_checkpoint(
        completed_count: int,
        issue_iid: int,
    ) -> None:
        """Advance the frozen queue without ADD when no file changed."""

        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        value.update(
            {
                "status": "running",
                "phase": "fetch",
                "fetched_count": int(completed_count),
                "indexed_confirmed_count": int(completed_count),
                "pending_count": 0,
                "last_completed_item": int(issue_iid),
                "can_resume": True,
                "last_error": None,
            }
        )
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )

    def reflect_batch(completed_count: int, issue_iid: int) -> None:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        value.update(
            {
                "status": "running",
                "phase": "reflect",
                "fetched_count": int(completed_count),
                "pending_count": (
                    int(completed_count)
                    - int(value.get("indexed_confirmed_count") or 0)
                ),
                "last_completed_item": int(issue_iid),
                "can_resume": True,
            }
        )
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        _emit_progress(
            progress_callback,
            {
                "phase": "gitlab_issues.reflect",
                "label_ja": "検索反映",
                "provider": "gitlab_issues",
                "completed": int(
                    value.get("indexed_confirmed_count") or 0
                ),
                "total": progress_total_holder[0],
                "unit": "件",
                "total_kind": (
                    "exact"
                    if progress_total_holder[0] is not None
                    else "unknown"
                ),
                "current_item": f"Issue #{int(issue_iid)}",
                "status": "running",
            },
        )
        (
            source_holder[0],
            state_holder[0],
            _summary,
        ) = _gitlab_issues_reflect_batch(
            store,
            source_holder[0],
            state_holder[0],
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
        )
        _emit_progress(
            progress_callback,
            {
                "phase": "gitlab_issues.reflect",
                "label_ja": "検索反映",
                "provider": "gitlab_issues",
                "completed": int(
                    state_holder[0].payload.get(
                        "indexed_confirmed_count"
                    )
                    or 0
                ),
                "total": progress_total_holder[0],
                "unit": "件",
                "total_kind": (
                    "exact"
                    if progress_total_holder[0] is not None
                    else "unknown"
                ),
                "status": "completed",
                "checkpoint_saved": True,
            },
        )

    resume_count = int(
        current_state.payload.get("indexed_confirmed_count") or 0
    )
    stable_ids_value = current_state.payload.get(
        GITLAB_ISSUE_IDS_STATE_KEY
    )
    stable_issue_ids = (
        [int(value) for value in stable_ids_value]
        if isinstance(stable_ids_value, list)
        else None
    )
    stable_project_value = current_state.payload.get(
        GITLAB_PROJECT_ID_STATE_KEY
    )
    stable_project_id = (
        int(stable_project_value)
        if stable_project_value is not None
        else None
    )

    def gitlab_progress(event: Mapping[str, Any]) -> None:
        if event.get("event") == "gitlab_issues.http_attempt":
            try:
                store.append_event(
                    source.payload["local_source_key"],
                    "gitlab_issues.http_attempt",
                    _persistable_http_diagnostic(event),
                )
            except Exception:
                pass
        _emit_progress(progress_callback, event)

    try:
        outcome = execute_fetch_plan(
            plan.to_dict(),
            store.ensure_work_directory(
                source.payload["local_source_key"]
            ),
            current_state.payload,
            command_runner=command_runner,
            http_get=http_get,
            environment=environment,
            item_callback=item_checkpoint,
            batch_callback=reflect_batch,
            no_change_callback=no_change_checkpoint,
            resume_count=resume_count,
            stable_issue_ids=stable_issue_ids,
            stable_project_id=stable_project_id,
            inventory_snapshot_callback=inventory_snapshot,
            clock=clock,
            progress_callback=gitlab_progress,
        )
    except Exception as exc:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        error_detail = exception_summary(exc)
        value.update(
            {
                "status": "interrupted",
                "can_resume": True,
                "last_error": error_detail,
            }
        )
        try:
            state_holder[0] = store.save_state(
                source.payload["local_source_key"],
                value,
                expected_revision=stored.revision,
                expected_etag=stored.etag,
            )
        except Exception:
            pass
        details: dict[str, Any] = {"error": error_detail}
        if isinstance(getattr(exc, "process_diagnostic", None), dict):
            details["process"] = getattr(exc, "process_diagnostic")
        if isinstance(getattr(exc, "diagnostic", None), dict):
            details["diagnostic"] = _persistable_http_diagnostic(
                getattr(exc, "diagnostic")
            )
        try:
            store.append_event(
                source.payload["local_source_key"],
                "gitlab_issues.fetch.interrupted",
                details,
            )
        except Exception:
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", "fetch.gitlab_issues")
        raise

    if not source_holder[0].payload.get("source_id"):
        completed = complete_run(state_holder[0].payload)
        completed.pop(GITLAB_PROJECT_ID_STATE_KEY, None)
        store.save_state(
            source.payload["local_source_key"],
            completed,
            expected_revision=state_holder[0].revision,
            expected_etag=state_holder[0].etag,
        )
        return {
            **_source_dto(store, source_holder[0]),
            "status": "no_documents",
            "fetched_count": 0,
        }

    if bool(
        state_holder[0].payload.get("initial_database_reflection")
    ):
        _write_initial_snapshot_marker(store.db_root)
    sync_result = _synchronize_metadata(
        store,
        source_holder[0],
        rag_root=rag_root,
        metadata_publisher=metadata_publisher,
    )
    _emit_progress(
        progress_callback,
        {
            "phase": "metadata",
            "label_ja": "Source Metadata反映",
            "provider": "gitlab_issues",
            "completed": (
                1 if not sync_result.get("metadata_sync_pending") else 0
            ),
            "total": 1,
            "unit": "件",
            "total_kind": "exact",
            "status": (
                "completed"
                if not sync_result.get("metadata_sync_pending")
                else "failed"
            ),
            "checkpoint_saved": not sync_result.get(
                "metadata_sync_pending"
            ),
        },
    )
    stored = state_holder[0]
    final = copy.deepcopy(stored.payload)
    final.update(
        {
            "fetched_count": int(outcome.get("documents") or 0),
            "pending_count": 0,
            "metadata_sync_pending": bool(
                sync_result.get("metadata_sync_pending")
            ),
            "last_error": sync_result.get("metadata_error"),
        }
    )
    if sync_result.get("metadata_sync_pending"):
        final.update(
            {
                "status": "interrupted",
                "phase": "metadata",
                "can_resume": True,
            }
        )
    else:
        final = complete_run(final)
        final.pop(GITLAB_PROJECT_ID_STATE_KEY, None)
    final_state = store.save_state(
        source.payload["local_source_key"],
        final,
        expected_revision=stored.revision,
        expected_etag=stored.etag,
    )
    return {
        **_source_dto(
            store,
            store.read_source(source.payload["local_source_key"]),
        ),
        **sync_result,
        "status": (
            "metadata_sync_pending"
            if sync_result.get("metadata_sync_pending")
            else "updated"
        ),
        "fetched_count": int(outcome.get("documents") or 0),
        "indexed_confirmed_count": int(
            final_state.payload.get("indexed_confirmed_count") or 0
        ),
        "state_revision": final_state.revision,
    }


def _gitlab_issues_reflect_batch(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
    *,
    python_executable: Path,
    rag_root: Path,
    command_runner: CommandRunner | None,
    progress_callback: ProgressCallback | None,
) -> tuple[StoredJson, StoredJson, dict[str, Any]]:
    work = store.ensure_work_directory(source.payload["local_source_key"])
    validate_managed_work_tree(work)
    try:
        add_result = _execute_add(
            db_root=store.db_root,
            source=source.payload,
            work=work,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        interrupted = copy.deepcopy(state.payload)
        error_detail = exception_summary(exc)
        interrupted.update(
            {
                "status": "interrupted",
                "phase": "reflect",
                "can_resume": True,
                "last_error": error_detail,
            }
        )
        try:
            store.save_state(
                source.payload["local_source_key"],
                interrupted,
                expected_revision=state.revision,
                expected_etag=state.etag,
            )
        except Exception:
            pass
        try:
            store.append_event(
                source.payload["local_source_key"],
                "gitlab_issues.reflect.interrupted",
                {
                    "error": error_detail,
                    **(
                        {"process": getattr(exc, "process_diagnostic")}
                        if isinstance(
                            getattr(exc, "process_diagnostic", None),
                            dict,
                        )
                        else {}
                    ),
                },
            )
        except Exception:
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", "reflect.gitlab_issues_batch")
        raise
    if source.payload.get("source_id"):
        current_source = source
    else:
        confirm_add_success(
            store.db_root,
            source.payload["local_source_key"],
            source_id=str(add_result["source_id"]),
        )
        current_source = store.read_source(
            source.payload["local_source_key"]
        )
    reflected = copy.deepcopy(state.payload)
    confirmed_count = int(reflected.get("fetched_count") or 0)
    reflected.update(
        {
            "status": "running",
            "phase": "fetch",
            "indexed_confirmed_count": confirmed_count,
            "pending_count": 0,
            "can_resume": True,
            "last_error": None,
        }
    )
    current_state = store.save_state(
        source.payload["local_source_key"],
        reflected,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )
    return current_source, current_state, add_result["summary"]


def _redmine_reflect_batch(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
    *,
    python_executable: Path,
    rag_root: Path,
    command_runner: CommandRunner | None,
    progress_callback: ProgressCallback | None,
) -> tuple[StoredJson, StoredJson, dict[str, Any]]:
    work = store.ensure_work_directory(source.payload["local_source_key"])
    validate_managed_work_tree(work)
    fetched_count = int(state.payload.get("fetched_count") or 0)
    indexed_count = int(state.payload.get("indexed_confirmed_count") or 0)
    batch_count = fetched_count - indexed_count
    if batch_count <= 0:
        raise SourceManagerError(
            "Redmine ADD batch has no pending Issues",
            stage="reflect.redmine_batch",
        )
    issue_ids = state.payload.get("redmine_issue_ids")
    total_count = len(issue_ids) if isinstance(issue_ids, list) else None
    _emit_progress(
        progress_callback,
        {
            "event": "redmine.add_batch",
            "phase": "redmine.reflect",
            "label_ja": "検索DB反映",
            "provider": "redmine",
            "completed": indexed_count,
            "current_index": fetched_count,
            "total": total_count,
            "unit": "件",
            "total_kind": "exact" if total_count is not None else "unknown",
            "current_item": state.payload.get("last_completed_item"),
            "status": "started",
        },
    )

    try:
        add_result = _execute_add(
            db_root=store.db_root,
            source=source.payload,
            work=work,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        interrupted = copy.deepcopy(state.payload)
        error_detail = exception_summary(exc)
        interrupted.update(
            {
                "status": "interrupted",
                "phase": "reflect",
                "can_resume": True,
                "last_error": error_detail,
            }
        )
        try:
            store.save_state(
                source.payload["local_source_key"],
                interrupted,
                expected_revision=state.revision,
                expected_etag=state.etag,
            )
        except Exception:
            pass
        try:
            store.append_event(
                source.payload["local_source_key"],
                "redmine.reflect.interrupted",
                {
                    "error": error_detail,
                    **(
                        {"process": getattr(exc, "process_diagnostic")}
                        if isinstance(
                            getattr(exc, "process_diagnostic", None),
                            dict,
                        )
                        else {}
                    ),
                },
            )
        except Exception:
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", "reflect.redmine_batch")
        raise
    if source.payload.get("source_id"):
        current_source = source
    else:
        confirm_add_success(
            store.db_root,
            source.payload["local_source_key"],
            source_id=str(add_result["source_id"]),
        )
        current_source = store.read_source(source.payload["local_source_key"])
    reflected = copy.deepcopy(state.payload)
    confirmed_count = int(reflected.get("fetched_count") or 0)
    reflected.update(
        {
            "status": "running",
            "phase": "fetch",
            "indexed_confirmed_count": confirmed_count,
            "pending_count": 0,
            "can_resume": True,
            "last_error": None,
        }
    )
    current_state = store.save_state(
        source.payload["local_source_key"],
        reflected,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )
    _emit_progress(
        progress_callback,
        {
            "event": "redmine.add_batch",
            "phase": "redmine.reflect",
            "label_ja": "検索DB反映",
            "provider": "redmine",
            "completed": confirmed_count,
            "current_index": confirmed_count,
            "total": total_count,
            "unit": "件",
            "total_kind": "exact" if total_count is not None else "unknown",
            "documents": batch_count,
            "status": "success",
            "checkpoint_saved": True,
        },
    )

    return current_source, current_state, add_result["summary"]


def _resume_add_only(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
    *,
    python_executable: str | Path | None,
    rag_root: str | Path | None,
    command_runner: CommandRunner | None,
    metadata_publisher: MetadataPublisher | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    if python_executable is None or rag_root is None:
        return {
            **_source_dto(store, source),
            "status": "reflection_pending",
            "resumed_operation": "add",
        }
    result = _reflect_and_sync(
        store,
        source,
        state,
        add_root=store.ensure_work_directory(
            source.payload["local_source_key"]
        ),
        python_executable=Path(python_executable),
        rag_root=Path(rag_root),
        command_runner=command_runner,
        metadata_publisher=metadata_publisher,
        progress_callback=progress_callback,
    )
    result["resumed_operation"] = "add"
    return result


def _reflect_and_sync(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
    *,
    add_root: Path,
    python_executable: Path,
    rag_root: Path,
    command_runner: CommandRunner | None,
    metadata_publisher: MetadataPublisher | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    work = Path(add_root)
    validate_managed_work_tree(work)
    state, initial_database_reflection = _record_initial_snapshot_candidate(
        store,
        source,
        state,
    )
    _emit_progress(
        progress_callback,
        {
            "phase": "reflect",
            "label_ja": "検索反映",
            "provider": source.payload.get("source_type"),
            "completed": int(
                state.payload.get("indexed_confirmed_count") or 0
            ),
            "total": int(state.payload.get("fetched_count") or 0),
            "unit": "件",
            "total_kind": "exact",
            "status": "running",
        },
    )
    try:
        add_result = _execute_add(
            db_root=store.db_root,
            source=source.payload,
            work=work,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        interrupted = copy.deepcopy(state.payload)
        error_detail = exception_summary(exc)
        interrupted.update(
            {
                "status": "interrupted",
                "phase": "reflect",
                "can_resume": True,
                "last_error": error_detail,
                "metadata_sync_pending": False,
            }
        )
        try:
            store.save_state(
                source.payload["local_source_key"],
                interrupted,
                expected_revision=state.revision,
                expected_etag=state.etag,
            )
        except Exception:
            pass
        try:
            store.append_event(
                source.payload["local_source_key"],
                "add.interrupted",
                {
                    "error": error_detail,
                    **(
                        {"process": getattr(exc, "process_diagnostic")}
                        if isinstance(
                            getattr(exc, "process_diagnostic", None),
                            dict,
                        )
                        else {}
                    ),
                    **(
                        {"diagnostic": getattr(exc, "diagnostic")}
                        if isinstance(getattr(exc, "diagnostic", None), dict)
                        else {}
                    ),
                },
            )
        except Exception:
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", "reflect.add")
        raise

    add_summary = dict(add_result["summary"])
    partial = str(add_result.get("status") or "") == "partial"
    confirmed = confirm_add_success(
        store.db_root,
        source.payload["local_source_key"],
        source_id=str(add_result["source_id"]),
        result_status="partial" if partial else "success",
        summary=add_summary,
    )
    _emit_progress(
        progress_callback,
        {
            "phase": "reflect",
            "label_ja": "検索反映",
            "provider": source.payload.get("source_type"),
            "completed": int(state.payload.get("fetched_count") or 0),
            "total": int(state.payload.get("fetched_count") or 0),
            "unit": "件",
            "total_kind": "exact",
            "status": "partial" if partial else "completed",
            "checkpoint_saved": True,
        },
    )
    if initial_database_reflection and not partial:
        _write_initial_snapshot_marker(store.db_root)
    confirmed_source = store.read_source(source.payload["local_source_key"])
    sync_result = _synchronize_metadata(
        store,
        confirmed_source,
        rag_root=rag_root,
        metadata_publisher=metadata_publisher,
    )
    _emit_progress(
        progress_callback,
        {
            "phase": "metadata",
            "label_ja": "Source Metadata反映",
            "provider": source.payload.get("source_type"),
            "completed": 1 if not sync_result.get("metadata_sync_pending") else 0,
            "total": 1,
            "unit": "件",
            "total_kind": "exact",
            "status": (
                ("partial" if partial else "completed")
                if not sync_result.get("metadata_sync_pending")
                else "failed"
            ),
            "checkpoint_saved": not sync_result.get("metadata_sync_pending"),
        },
    )
    reflected = copy.deepcopy(state.payload)
    fetched_count = int(reflected.get("fetched_count") or 0)
    input_error_files = int(add_summary.get("input_error_files") or 0)
    confirmed_count = int(add_summary.get("indexed_files") or 0) + int(
        add_summary.get("skipped_files") or 0
    )
    reflected.update(
        {
            "indexed_confirmed_count": (
                confirmed_count if partial else fetched_count
            ),
            "pending_count": input_error_files if partial else 0,
            "last_completed_item": fetched_count or None,
            "metadata_sync_pending": bool(
                sync_result.get("metadata_sync_pending")
            ),
            "last_error": (
                add_summary.get("warning_ja")
                if partial
                else sync_result.get("metadata_error")
            ),
        }
    )
    if sync_result.get("metadata_sync_pending"):
        reflected.update(
            {
                "status": "interrupted",
                "phase": "metadata",
                "can_resume": True,
            }
        )
    elif partial:
        reflected.update(
            {
                "status": "partial",
                "phase": "reflect",
                "can_resume": True,
                "input_error_files": input_error_files,
                "input_error_details": list(
                    add_summary.get("error_details") or []
                )[:100],
            }
        )
    else:
        reflected = complete_run(reflected)
    final_state = store.save_state(
        source.payload["local_source_key"],
        reflected,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )
    result = {
        **confirmed,
        **sync_result,
        "state_revision": final_state.revision,
        "add_summary": add_summary,
    }
    if partial and not sync_result.get("metadata_sync_pending"):
        result.update(
            {
                "status": "partial",
                "message": str(
                    add_summary.get("warning_ja")
                    or "一部のファイルを読み取れませんでした。次回自動再試行します。"
                ),
                "can_resume": True,
            }
        )
    elif sync_result.get("metadata_sync_pending"):
        result["status"] = "metadata_sync_pending"
    return result


def _record_initial_snapshot_candidate(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
) -> tuple[StoredJson, bool]:
    recorded = state.payload.get("initial_database_reflection")
    if isinstance(recorded, bool):
        return state, recorded
    candidate = _is_initial_database_reflection(store, source)
    value = copy.deepcopy(state.payload)
    value["initial_database_reflection"] = candidate
    updated = store.save_state(
        source.payload["local_source_key"],
        value,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )
    return updated, candidate


def _is_initial_database_reflection(
    store: SourceStore,
    source: StoredJson,
) -> bool:
    if source.payload.get("source_id"):
        return False
    marker = store.db_root / "rag-wrapper.json"
    if marker.exists() or marker.is_symlink():
        return False
    catalog = store.db_root / "catalog.sqlite"
    if catalog.is_symlink():
        return False
    if not catalog.exists():
        return True
    try:
        resolved = catalog.resolve(strict=True)
        connection = sqlite3.connect(
            resolved.as_uri() + "?mode=ro",
            uri=True,
        )
        try:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(document)"
                )
            }
            if "doc_pk" not in columns:
                return False
            visibility = (
                " WHERE visible_until IS NULL"
                if "visible_until" in columns
                else ""
            )
            return (
                connection.execute(
                    f"SELECT 1 FROM document{visibility} LIMIT 1"
                ).fetchone()
                is None
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return False


def _write_initial_snapshot_marker(db_root: Path) -> None:
    path = db_root / "rag-wrapper.json"
    if path.exists() or path.is_symlink():
        return
    current = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "schema_version": "local-rag.wrapper.v1",
        "content_snapshot_at": current.isoformat().replace("+00:00", "Z"),
        "reason": "initial_database_reflection",
    }
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _attach_registration_failure(
    exc: BaseException,
    *,
    local_source_key: str,
    events_jsonl: str,
) -> None:
    """Attach non-secret recovery context for the human Manager."""
    if getattr(exc, "stage", None) is None:
        setattr(exc, "stage", "registration.initial_processing")
    setattr(exc, "source_saved", True)
    setattr(exc, "local_source_key", str(local_source_key))
    setattr(exc, "events_jsonl", str(events_jsonl))


def _validate_registration_runtime(
    source_type: str,
    runtime_path: str | Path | None,
) -> Path | None:
    """Validate transient registration input before creating Source files."""
    if runtime_path is None:
        return None
    if str(source_type or "").strip().lower() != "other":
        raise SourceManagerError(
            "runtime_path is supported only for Other Sources"
        )
    runtime = Path(str(runtime_path)).expanduser()
    if not runtime.is_absolute():
        raise SourceManagerError("Other runtime_path must be absolute")
    return runtime


def _resume_metadata_sync(
    store: SourceStore,
    source: StoredJson,
    *,
    rag_root: str | Path | None,
    metadata_publisher: MetadataPublisher | None,
) -> dict[str, Any]:
    """Retry sidecar publication only; never repeat fetch or ADD."""
    result = _synchronize_metadata(
        store,
        source,
        rag_root=rag_root,
        metadata_publisher=metadata_publisher,
    )
    state = store.read_state(source.payload["local_source_key"])
    if state.payload:
        updated = copy.deepcopy(state.payload)
        updated["metadata_sync_pending"] = bool(
            result.get("metadata_sync_pending")
        )
        updated["last_error"] = result.get("metadata_error")
        if result.get("metadata_sync_pending"):
            updated.update(
                {
                    "status": "interrupted",
                    "phase": "metadata",
                    "can_resume": True,
                }
            )
        else:
            updated = complete_run(updated)
            if source.payload.get("source_type") == "gitlab_issues":
                updated.pop(GITLAB_PROJECT_ID_STATE_KEY, None)
        store.save_state(
            source.payload["local_source_key"],
            updated,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
    return {
        **_source_dto(store, store.read_source(source.payload["local_source_key"])),
        **result,
        "resumed_operation": "metadata_sync",
    }


def _synchronize_metadata(
    store: SourceStore,
    source: StoredJson,
    *,
    rag_root: str | Path | None,
    metadata_publisher: MetadataPublisher | None,
) -> dict[str, Any]:
    publisher = metadata_publisher or publish_source_metadata
    if rag_root is None:
        return {
            "status": "metadata_sync_pending",
            "metadata_sync_pending": True,
        }
    try:
        publisher(
            store.db_root,
            copy.deepcopy(source.payload),
            Path(rag_root),
        )
        synced = store.mark_metadata_synced(
            source.payload["local_source_key"],
            expected_revision=source.revision,
            expected_etag=source.etag,
        )
        store.append_event(
            source.payload["local_source_key"],
            "metadata.synchronized",
        )
        return {
            **_source_dto(store, synced),
            "status": "updated",
            "metadata_sync_pending": False,
        }
    except Exception as exc:
        error_detail = exception_summary(exc)
        store.append_event(
            source.payload["local_source_key"],
            "metadata.synchronization_failed",
            {"error": error_detail},
        )
        return {
            "status": "metadata_sync_pending",
            "metadata_sync_pending": True,
            "metadata_error": error_detail,
        }
