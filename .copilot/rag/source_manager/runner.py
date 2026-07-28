from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .checkpoints import complete_run, new_run_state
from .errors import SourceManagerError
from .execution import execute_fetch_plan, validate_managed_work_tree
from .metadata import publish_source_metadata
from .networking import resolve_source_network_route
from .providers import validate_provider_config
from .store import MISSING_ETAG, SourceStore, StoredJson


FetchExecutor = Callable[[dict[str, Any], Path, dict[str, Any]], Mapping[str, Any]]
CommandRunner = Callable[[list[str]], Any]
MetadataPublisher = Callable[[Path, Mapping[str, Any], Path], None]


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
) -> dict[str, Any]:
    """Register one provisional Source without assigning indexed identity."""
    values = dict(fetch)
    runtime_path = runtime_input or values.pop("runtime_path", None)
    runtime = _validate_registration_runtime(source_type, runtime_path)
    store = SourceStore(db_root)
    stored = store.create_source(
        source_type=source_type,
        display_name=display_name,
        fetch=values,
        local_source_key=local_source_key,
        source_id=source_id,
        link=link,
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
        )
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
) -> dict[str, Any]:
    """Prepare or execute one fetch; network access exists only in executor."""
    store = SourceStore(db_root)
    source = store.read_source(local_source_key)
    if not source.payload:
        raise SourceManagerError("Source does not exist")
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
        in {"github", "svn", "redmine"}
        and command_runner is None
        and http_get is None
        and rag_root is not None
    ):
        route = resolve_source_network_route(
            Path(rag_root),
            environment=environment,
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
    outcome = dict(
        effective_executor(plan.to_dict(), work, runtime_state)
    )
    if outcome.get("status") not in {"ok", "complete"}:
        runtime_state["status"] = "failed"
        runtime_state["phase"] = "fetch"
        runtime_state["can_resume"] = True
        runtime_state["last_error"] = str(
            outcome.get("error") or "fetch_failed"
        )[:200]
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
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in list_sources(db_root):
        key = str(item.get("local_source_key") or "")
        if not key or item.get("status") == "invalid":
            results.append(item)
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
            continue
        if item.get("source_type") == "sharepoint" and not _is_windows():
            results.append(
                {
                    **item,
                    "status": "skipped",
                    "skip_reason": "sharepoint_update_requires_windows",
                }
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
                )
            )
        except SourceManagerError as exc:
            results.append(
                {
                    "local_source_key": key,
                    "status": "failed",
                    "error": type(exc).__name__,
                }
            )
    failed = [item for item in results if item.get("status") == "failed"]
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
            if not failed
            else "partial"
        ),
        "source_count": len(results),
        "updateable_source_count": len(updateable),
        "completed_source_count": completed_source_count,
        "snapshot_marker_eligible": (
            not failed
            and not blocking_skips
            and completed_source_count == len(updateable)
        ),
        "results": results,
    }


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
    store.append_event(
        local_source_key,
        "add.completed",
        {"source_id": confirmed.payload["source_id"]},
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
) -> dict[str, Any]:
    key = str(source["local_source_key"])
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
    ]
    runner = command_runner or _run_command
    completed = runner(arguments)
    if int(getattr(completed, "returncode", 1)) != 0:
        raise SourceManagerError("ADD failed")
    try:
        summary = json.loads(str(getattr(completed, "stdout", "") or ""))
    except json.JSONDecodeError as exc:
        raise SourceManagerError("ADD did not return trusted JSON") from exc
    if not isinstance(summary, dict):
        raise SourceManagerError("ADD did not return trusted JSON")
    reported_source_id = summary.get("source_id")
    if not isinstance(reported_source_id, str) or reported_source_id != key:
        raise SourceManagerError(
            "ADD did not return the requested trusted source_id"
        )
    return {"source_id": reported_source_id, "summary": summary}


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
) -> dict[str, Any]:
    """Fetch Redmine serially and reflect each stable batch of five."""
    plan = store.plan(source.payload)
    if not state.payload or state.payload.get("status") == "complete":
        initial = new_run_state(plan)
        current_state = store.save_state(
            source.payload["local_source_key"],
            initial,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
    else:
        resumed = copy.deepcopy(state.payload)
        resumed["plan_etag"] = plan.plan_etag
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
        )

    state_holder = [current_state]
    source_holder = [current_source]

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

    def item_checkpoint(completed_count: int, issue_id: int) -> None:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        confirmed = int(value.get("indexed_confirmed_count") or 0)
        value.update(
            {
                "status": "running",
                "phase": "fetch",
                "fetched_count": int(completed_count),
                "pending_count": int(completed_count) - confirmed,
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
        )
    except Exception as exc:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        value.update(
            {
                "status": "interrupted",
                "can_resume": True,
                "last_error": type(exc).__name__,
            }
        )
        try:
            state_holder[0] = store.save_state(
                source.payload["local_source_key"],
                value,
                expected_revision=stored.revision,
                expected_etag=stored.etag,
            )
        except SourceManagerError:
            pass
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

    sync_result = _synchronize_metadata(
        store,
        source_holder[0],
        rag_root=rag_root,
        metadata_publisher=metadata_publisher,
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


def _redmine_reflect_batch(
    store: SourceStore,
    source: StoredJson,
    state: StoredJson,
    *,
    python_executable: Path,
    rag_root: Path,
    command_runner: CommandRunner | None,
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
        )
    except Exception as exc:
        interrupted = copy.deepcopy(state.payload)
        interrupted.update(
            {
                "status": "interrupted",
                "phase": "reflect",
                "can_resume": True,
                "last_error": type(exc).__name__,
            }
        )
        store.save_state(
            source.payload["local_source_key"],
            interrupted,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
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
) -> dict[str, Any]:
    work = Path(add_root)
    validate_managed_work_tree(work)
    try:
        add_result = _execute_add(
            db_root=store.db_root,
            source=source.payload,
            work=work,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
        )
    except Exception as exc:
        interrupted = copy.deepcopy(state.payload)
        interrupted.update(
            {
                "status": "interrupted",
                "phase": "reflect",
                "can_resume": True,
                "last_error": type(exc).__name__,
                "metadata_sync_pending": False,
            }
        )
        store.save_state(
            source.payload["local_source_key"],
            interrupted,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
        store.append_event(
            source.payload["local_source_key"],
            "add.interrupted",
            {"error": type(exc).__name__},
        )
        raise

    confirmed = confirm_add_success(
        store.db_root,
        source.payload["local_source_key"],
        source_id=str(add_result["source_id"]),
    )
    confirmed_source = store.read_source(source.payload["local_source_key"])
    sync_result = _synchronize_metadata(
        store,
        confirmed_source,
        rag_root=rag_root,
        metadata_publisher=metadata_publisher,
    )
    reflected = copy.deepcopy(state.payload)
    fetched_count = int(reflected.get("fetched_count") or 0)
    reflected.update(
        {
            "indexed_confirmed_count": fetched_count,
            "pending_count": 0,
            "last_completed_item": fetched_count or None,
            "metadata_sync_pending": bool(
                sync_result.get("metadata_sync_pending")
            ),
            "last_error": sync_result.get("metadata_error"),
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
    else:
        reflected = complete_run(reflected)
    final_state = store.save_state(
        source.payload["local_source_key"],
        reflected,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )
    return {
        **confirmed,
        **sync_result,
        "state_revision": final_state.revision,
        "add_summary": add_result["summary"],
    }


def _run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
        check=False,
    )


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
        store.append_event(
            source.payload["local_source_key"],
            "metadata.synchronization_failed",
            {"error": type(exc).__name__},
        )
        return {
            "status": "metadata_sync_pending",
            "metadata_sync_pending": True,
            "metadata_error": type(exc).__name__,
        }
