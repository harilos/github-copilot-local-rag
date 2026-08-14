from __future__ import annotations

import copy
import functools
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError
from .source_exclusion import (
    FILE_BASED_SOURCE_TYPES,
    SourcePreview,
    discard_prepared_work,
    exclusion_signature,
    normalize_exclusion_paths,
    parse_exclusion_input,
    preview_and_prepare_work,
)


_RUNTIME_PATCH_MARKER = "_local_rag_source_preflight_runtime_installed"
_MANAGER_PATCH_MARKER = "_local_rag_source_preflight_manager_installed"
_REDMINE_REQUIRED_ATTR = "_local_rag_preflight_required"
_RESULT_ATTR = "_local_rag_preflight_result"
_CONFIRM_METHOD = "confirm_source_estimate"
_PREVIEW_METHOD = "confirm_source_preview"
_PREVIEW_ATTR = "_local_rag_source_preview"


class SourceEstimateDeclined(RuntimeError):
    def __init__(self, documents: int) -> None:
        self.documents = max(0, int(documents))
        super().__init__("source_document_estimate_declined")


def estimate_minutes_range(documents: int) -> tuple[int, int]:
    count = max(0, int(documents))
    return count, count * 5


def install_source_preflight_runtime() -> None:
    """Install initial-Source estimate confirmation without changing CLI APIs."""

    from . import execution, manager_connections, runner

    if bool(getattr(runner, _RUNTIME_PATCH_MARKER, False)):
        return

    _install_manager_hook(manager_connections)
    _install_normal_source_preflight(runner)
    _install_redmine_preflight(execution, runner)
    _install_gitlab_issues_preflight(execution, runner)
    setattr(runner, _RUNTIME_PATCH_MARKER, True)


def _install_manager_hook(manager_connections: Any) -> None:
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_confirmation(manager_class)

    manager_connections.install_manager_connection_ui = install_manager_connection_ui


def _install_manager_confirmation(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _MANAGER_PATCH_MARKER, False)):
        return
    original = manager_class._progress_callback

    for method_name in (
        "_prompt_new_svn_source",
        "_prompt_new_other_source",
    ):
        _install_exclusion_prompt(manager_class, method_name)

    @functools.wraps(original)
    def progress_callback(
        self: Any,
        operation: str,
        *,
        provider: str | None = None,
    ) -> Any:
        renderer = original(self, operation, provider=provider)

        def confirm_source_estimate(documents: int) -> bool:
            count = max(0, int(documents))
            minimum, maximum = estimate_minutes_range(count)
            self.output("")
            self._print_info(f"追加対象の概算: 約{count:,}件")
            self.output(
                f"目安時間: 約{minimum:,}～{maximum:,}分"
                "（1文書あたり1～5分の幅で算出）"
            )
            return bool(
                self._confirm(
                    f"約{count:,}件追加します。よろしいですか？"
                )
            )

        def confirm_source_preview(preview: Mapping[str, Any]) -> bool:
            included = max(0, int(preview.get("included_count") or 0))
            included_bytes = max(0, int(preview.get("included_bytes") or 0))
            excluded = max(0, int(preview.get("excluded_count") or 0))
            excluded_bytes = max(0, int(preview.get("excluded_bytes") or 0))
            minimum, maximum = estimate_minutes_range(included)
            self.output("")
            self._print_info(
                f"除外後の追加対象: {included:,}件 / {included_bytes:,} bytes"
            )
            self.output(
                f"除外: {excluded:,}件 / {excluded_bytes:,} bytes"
            )
            self.output(
                f"目安時間: 約{minimum:,}～{maximum:,}分"
                "（1文書あたり1～5分の幅で算出）"
            )
            return bool(
                self._confirm(
                    f"除外後の{included:,}件を追加します。よろしいですか？"
                )
            )

        setattr(renderer, _CONFIRM_METHOD, confirm_source_estimate)
        setattr(renderer, _PREVIEW_METHOD, confirm_source_preview)
        return renderer

    manager_class._progress_callback = progress_callback
    setattr(manager_class, _MANAGER_PATCH_MARKER, True)


def _install_normal_source_preflight(runner: Any) -> None:
    original = runner._reflect_and_sync

    @functools.wraps(original)
    def reflect_and_sync(
        store: Any,
        source: Any,
        state: Any,
        *,
        add_root: Any,
        python_executable: Any,
        rag_root: Any,
        command_runner: Any,
        metadata_publisher: Any,
        progress_callback: Any,
    ) -> dict[str, Any]:
        reflection_root = Path(add_root)
        try:
            session = (
                runner._database_writer_session(
                    store.db_root, stage="reflect.preflight"
                )
                if hasattr(runner, "_database_writer_session")
                and hasattr(store, "db_root")
                else nullcontext()
            )
            with session:
                state, reflection_root = _prepare_file_source_preview(
                    store,
                    source,
                    state,
                    Path(add_root),
                )
                if not source.payload.get("source_id"):
                    state, confirmed, documents = _confirm_and_store(
                        store,
                        source,
                        state,
                        progress_callback,
                    )
                    if not confirmed:
                        result = runner._source_dto(store, source)
                        minimum, maximum = estimate_minutes_range(documents)
                        result.update(
                            {
                                "status": "confirmation_declined",
                                "message": "概算確認で追加を開始しませんでした。",
                                "estimated_documents": documents,
                                "estimated_minutes_min": minimum,
                                "estimated_minutes_max": maximum,
                                "state_revision": state.revision,
                            }
                        )
                        return result
            return original(
                store,
                source,
                state,
                add_root=reflection_root,
                python_executable=python_executable,
                rag_root=rag_root,
                command_runner=command_runner,
                metadata_publisher=metadata_publisher,
                progress_callback=progress_callback,
                persistent_root_identity=(
                    Path(add_root)
                    if reflection_root != Path(add_root)
                    else None
                ),
            )
        finally:
            discard_prepared_work(reflection_root, Path(add_root))

    runner._reflect_and_sync = reflect_and_sync


def _confirm_and_store(
    store: Any,
    source: Any,
    state: Any,
    progress_callback: Any,
) -> tuple[Any, bool, int]:
    preview = _preview_from_state(state.payload)
    documents = (
        preview.included_count
        if preview is not None
        else max(0, int(state.payload.get("fetched_count") or 0))
    )
    if bool(state.payload.get("preflight_confirmed")):
        return state, True, documents

    previous_preview = _set_optional_attribute(
        progress_callback,
        _PREVIEW_ATTR,
        preview,
    )
    try:
        confirmed = _request_confirmation(progress_callback, documents)
    finally:
        _restore_optional_attribute(
            progress_callback,
            _PREVIEW_ATTR,
            previous_preview,
        )
    value = copy.deepcopy(state.payload)
    value.update(
        {
            "preflight_estimated_documents": documents,
            "preflight_confirmed": bool(confirmed),
            "preflight_confirmation": (
                "confirmed" if confirmed else "declined"
            ),
        }
    )
    if not confirmed:
        value.update(
            {
                "status": "interrupted",
                "phase": "reflect",
                "can_resume": True,
                "last_error": None,
            }
        )
    saved = store.save_state(
        source.payload["local_source_key"],
        value,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )
    minimum, maximum = estimate_minutes_range(documents)
    event_details: dict[str, Any] = {
        "estimated_documents": documents,
        "estimated_minutes_min": minimum,
        "estimated_minutes_max": maximum,
    }
    if preview is not None:
        event_details.update(preview.to_dict())
    store.append_event(
        source.payload["local_source_key"],
        (
            "source.preflight.confirmed"
            if confirmed
            else "source.preflight.declined"
        ),
        event_details,
    )
    return saved, bool(confirmed), documents


def _install_redmine_preflight(execution: Any, runner: Any) -> None:
    original_redmine = execution._redmine

    @functools.wraps(original_redmine)
    def redmine(
        settings: dict[str, Any],
        work: Any,
        getter: Any,
        environment: Mapping[str, str],
        *,
        item_callback: Any,
        batch_callback: Any,
        resume_count: int,
        stable_issue_ids: list[int] | None,
        inventory_callback: Any,
        updated_on_cutoff: str | None,
        progress_callback: Any,
        _force_full_materialization: bool = False,
    ) -> dict[str, Any]:
        required = bool(
            getattr(progress_callback, _REDMINE_REQUIRED_ATTR, False)
        )
        if not required:
            return original_redmine(
                settings,
                work,
                getter,
                environment,
                item_callback=item_callback,
                batch_callback=batch_callback,
                resume_count=resume_count,
                stable_issue_ids=stable_issue_ids,
                inventory_callback=inventory_callback,
                updated_on_cutoff=updated_on_cutoff,
                progress_callback=progress_callback,
                **({"_force_full_materialization": True} if _force_full_materialization else {}),
            )

        # A resumed first import already has a stable issue inventory. Confirm
        # before any issue detail request or ADD batch.
        if stable_issue_ids is not None:
            documents = len(stable_issue_ids)
            confirmed = _request_confirmation(progress_callback, documents)
            _record_callback_result(progress_callback, documents, confirmed)
            if not confirmed:
                raise SourceEstimateDeclined(documents)
            return original_redmine(
                settings,
                work,
                getter,
                environment,
                item_callback=item_callback,
                batch_callback=batch_callback,
                resume_count=resume_count,
                stable_issue_ids=stable_issue_ids,
                inventory_callback=inventory_callback,
                updated_on_cutoff=updated_on_cutoff,
                progress_callback=progress_callback,
                **({"_force_full_materialization": True} if _force_full_materialization else {}),
            )

        # A new first import learns the approximate count from the inventory.
        # The original inventory callback is still invoked so the stable IDs are
        # checkpointed even when the human declines.
        def confirmed_inventory(issue_ids: list[int]) -> None:
            documents = len(issue_ids)
            confirmed = _request_confirmation(progress_callback, documents)
            _record_callback_result(progress_callback, documents, confirmed)
            if inventory_callback is not None:
                inventory_callback(issue_ids)
            if not confirmed:
                raise SourceEstimateDeclined(documents)

        return original_redmine(
            settings,
            work,
            getter,
            environment,
            item_callback=item_callback,
            batch_callback=batch_callback,
            resume_count=resume_count,
            stable_issue_ids=stable_issue_ids,
            inventory_callback=confirmed_inventory,
            updated_on_cutoff=updated_on_cutoff,
            progress_callback=progress_callback,
            **({"_force_full_materialization": True} if _force_full_materialization else {}),
        )

    execution._redmine = redmine

    original_update = runner._update_redmine_source

    @functools.wraps(original_update)
    def update_redmine_source(
        store: Any,
        source: Any,
        state: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        progress_callback = kwargs.get("progress_callback")
        required = not bool(source.payload.get("source_id")) and not bool(
            state.payload.get("preflight_confirmed")
        )
        previous_required = _set_optional_attribute(
            progress_callback,
            _REDMINE_REQUIRED_ATTR,
            required,
        )
        previous_result = _set_optional_attribute(
            progress_callback,
            _RESULT_ATTR,
            None,
        )
        try:
            return original_update(store, source, state, **kwargs)
        except SourceEstimateDeclined as exc:
            return _record_redmine_decline(store, source, runner, exc.documents)
        finally:
            result = getattr(progress_callback, _RESULT_ATTR, None)
            if isinstance(result, Mapping) and bool(result.get("confirmed")):
                _persist_redmine_confirmation(
                    store,
                    source,
                    int(result.get("documents") or 0),
                )
            _restore_optional_attribute(
                progress_callback,
                _RESULT_ATTR,
                previous_result,
            )
            _restore_optional_attribute(
                progress_callback,
                _REDMINE_REQUIRED_ATTR,
                previous_required,
            )

    runner._update_redmine_source = update_redmine_source


def _install_gitlab_issues_preflight(
    execution: Any,
    runner: Any,
) -> None:
    original_gitlab = execution.fetch_gitlab_issues

    @functools.wraps(original_gitlab)
    def gitlab_issues(
        settings: Mapping[str, Any],
        work: Any,
        request: Any,
        environment: Mapping[str, str],
        *,
        item_callback: Any,
        batch_callback: Any,
        resume_count: int,
        stable_issue_ids: list[int] | None,
        stable_project_id: int | None,
        inventory_snapshot_callback: Any,
        updated_after: str | None,
        progress_callback: Any,
        no_change_callback: Any = None,
        _force_full_materialization: bool = False,
    ) -> dict[str, Any]:
        required = bool(
            getattr(progress_callback, _REDMINE_REQUIRED_ATTR, False)
        )
        if not required:
            return original_gitlab(
                settings,
                work,
                request,
                environment,
                item_callback=item_callback,
                batch_callback=batch_callback,
                resume_count=resume_count,
                stable_issue_ids=stable_issue_ids,
                stable_project_id=stable_project_id,
                inventory_snapshot_callback=inventory_snapshot_callback,
                updated_after=updated_after,
                progress_callback=progress_callback,
                no_change_callback=no_change_callback,
                **({"_force_full_materialization": True} if _force_full_materialization else {}),
            )

        if stable_issue_ids is not None:
            documents = len(stable_issue_ids)
            confirmed = _request_confirmation(
                progress_callback,
                documents,
            )
            _record_callback_result(
                progress_callback,
                documents,
                confirmed,
            )
            if not confirmed:
                raise SourceEstimateDeclined(documents)
            return original_gitlab(
                settings,
                work,
                request,
                environment,
                item_callback=item_callback,
                batch_callback=batch_callback,
                resume_count=resume_count,
                stable_issue_ids=stable_issue_ids,
                stable_project_id=stable_project_id,
                inventory_snapshot_callback=inventory_snapshot_callback,
                updated_after=updated_after,
                progress_callback=progress_callback,
                no_change_callback=no_change_callback,
                **({"_force_full_materialization": True} if _force_full_materialization else {}),
            )

        def confirmed_inventory(
            project_id: int,
            issue_iids: list[int],
        ) -> None:
            documents = len(issue_iids)
            confirmed = _request_confirmation(
                progress_callback,
                documents,
            )
            _record_callback_result(
                progress_callback,
                documents,
                confirmed,
            )
            if inventory_snapshot_callback is not None:
                inventory_snapshot_callback(project_id, issue_iids)
            if not confirmed:
                raise SourceEstimateDeclined(documents)

        return original_gitlab(
            settings,
            work,
            request,
            environment,
            item_callback=item_callback,
            batch_callback=batch_callback,
            resume_count=resume_count,
            stable_issue_ids=stable_issue_ids,
            stable_project_id=stable_project_id,
            inventory_snapshot_callback=confirmed_inventory,
            updated_after=updated_after,
            progress_callback=progress_callback,
            no_change_callback=no_change_callback,
            **({"_force_full_materialization": True} if _force_full_materialization else {}),
        )

    execution.fetch_gitlab_issues = gitlab_issues

    original_update = runner._update_gitlab_issues_source

    @functools.wraps(original_update)
    def update_gitlab_issues_source(
        store: Any,
        source: Any,
        state: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        progress_callback = kwargs.get("progress_callback")
        required = not bool(source.payload.get("source_id")) and not bool(
            state.payload.get("preflight_confirmed")
        )
        previous_required = _set_optional_attribute(
            progress_callback,
            _REDMINE_REQUIRED_ATTR,
            required,
        )
        previous_result = _set_optional_attribute(
            progress_callback,
            _RESULT_ATTR,
            None,
        )
        try:
            return original_update(store, source, state, **kwargs)
        except SourceEstimateDeclined as exc:
            return _record_redmine_decline(
                store,
                source,
                runner,
                exc.documents,
            )
        finally:
            result = getattr(progress_callback, _RESULT_ATTR, None)
            if isinstance(result, Mapping) and bool(result.get("confirmed")):
                _persist_redmine_confirmation(
                    store,
                    source,
                    int(result.get("documents") or 0),
                )
            _restore_optional_attribute(
                progress_callback,
                _RESULT_ATTR,
                previous_result,
            )
            _restore_optional_attribute(
                progress_callback,
                _REDMINE_REQUIRED_ATTR,
                previous_required,
            )

    runner._update_gitlab_issues_source = update_gitlab_issues_source


def _record_redmine_decline(
    store: Any,
    source: Any,
    runner: Any,
    documents: int,
) -> dict[str, Any]:
    current = store.read_state(source.payload["local_source_key"])
    value = copy.deepcopy(current.payload)
    value.update(
        {
            "status": "interrupted",
            "phase": "reflect",
            "can_resume": True,
            "last_error": None,
            "preflight_estimated_documents": documents,
            "preflight_confirmed": False,
            "preflight_confirmation": "declined",
        }
    )
    saved = store.save_state(
        source.payload["local_source_key"],
        value,
        expected_revision=current.revision,
        expected_etag=current.etag,
    )
    minimum, maximum = estimate_minutes_range(documents)
    store.append_event(
        source.payload["local_source_key"],
        "source.preflight.declined",
        {
            "estimated_documents": documents,
            "estimated_minutes_min": minimum,
            "estimated_minutes_max": maximum,
        },
    )
    result = runner._source_dto(store, source)
    result.update(
        {
            "status": "confirmation_declined",
            "message": "概算確認で追加を開始しませんでした。",
            "estimated_documents": documents,
            "estimated_minutes_min": minimum,
            "estimated_minutes_max": maximum,
            "state_revision": saved.revision,
        }
    )
    return result


def _persist_redmine_confirmation(
    store: Any,
    source: Any,
    documents: int,
) -> None:
    try:
        current = store.read_state(source.payload["local_source_key"])
        if not current.payload or bool(current.payload.get("preflight_confirmed")):
            return
        value = copy.deepcopy(current.payload)
        value.update(
            {
                "preflight_estimated_documents": max(0, int(documents)),
                "preflight_confirmed": True,
                "preflight_confirmation": "confirmed",
            }
        )
        store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=current.revision,
            expected_etag=current.etag,
        )
        minimum, maximum = estimate_minutes_range(documents)
        store.append_event(
            source.payload["local_source_key"],
            "source.preflight.confirmed",
            {
                "estimated_documents": max(0, int(documents)),
                "estimated_minutes_min": minimum,
                "estimated_minutes_max": maximum,
            },
        )
    except Exception:
        # The confirmation has already been given. Failure to persist this
        # convenience flag must not turn a successful or resumable import into
        # a failed Source operation.
        return


def _request_confirmation(
    progress_callback: Any,
    documents: int,
    *,
    preview: SourcePreview | None = None,
) -> bool:
    if preview is None:
        candidate = getattr(progress_callback, _PREVIEW_ATTR, None)
        if isinstance(candidate, SourcePreview):
            preview = candidate
    if preview is not None:
        preview_callback = getattr(progress_callback, _PREVIEW_METHOD, None)
        if callable(preview_callback):
            return bool(preview_callback(preview.to_dict()))
    callback = getattr(progress_callback, _CONFIRM_METHOD, None)
    if not callable(callback):
        return True
    return bool(callback(max(0, int(documents))))


def _prepare_file_source_preview(
    store: Any,
    source: Any,
    state: Any,
    add_root: Path,
) -> tuple[Any, Path]:
    source_type = str(source.payload.get("source_type") or "").strip().lower()
    if source_type not in FILE_BASED_SOURCE_TYPES:
        return state, add_root
    fetch = source.payload.get("fetch")
    settings = dict(fetch) if isinstance(fetch, Mapping) else {}
    paths = normalize_exclusion_paths(settings.get("exclude_paths"))
    signature = exclusion_signature(paths)
    filtered_root = (
        add_root.parent.parent / "filtered" / add_root.name
    )
    prepared = preview_and_prepare_work(
        add_root,
        paths,
        filtered_root=filtered_root,
    )
    preview = prepared.preview
    value = copy.deepcopy(state.payload)
    effective_fetched_count = (
        preview.included_count
        if paths or preview.acquired_count > 0
        else max(0, int(value.get("fetched_count") or 0))
    )
    confirmed_count = min(
        effective_fetched_count,
        max(0, int(value.get("indexed_confirmed_count") or 0)),
    )
    value.update(
        {
            "fetched_count": effective_fetched_count,
            "indexed_confirmed_count": confirmed_count,
            "pending_count": max(
                0,
                effective_fetched_count
                - confirmed_count,
            ),
            "preflight_acquired_count": preview.acquired_count,
            "preflight_acquired_bytes": preview.acquired_bytes,
            "preflight_included_count": preview.included_count,
            "preflight_included_bytes": preview.included_bytes,
            "preflight_excluded_count": preview.excluded_count,
            "preflight_excluded_bytes": preview.excluded_bytes,
            "preflight_exclusion_hash": signature,
            "preflight_filter_applied": True,
        }
    )
    try:
        saved = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
        store.append_event(
            source.payload["local_source_key"],
            "source.preflight.preview_prepared",
            preview.to_dict(),
        )
        return saved, prepared.add_root
    except BaseException:
        discard_prepared_work(prepared.add_root, add_root)
        raise


def _preview_from_state(value: Mapping[str, Any]) -> SourcePreview | None:
    fields = (
        "preflight_included_count",
        "preflight_included_bytes",
        "preflight_excluded_count",
        "preflight_excluded_bytes",
    )
    if any(field not in value for field in fields):
        return None
    return SourcePreview(
        included_count=max(0, int(value[fields[0]] or 0)),
        included_bytes=max(0, int(value[fields[1]] or 0)),
        excluded_count=max(0, int(value[fields[2]] or 0)),
        excluded_bytes=max(0, int(value[fields[3]] or 0)),
    )


def _install_exclusion_prompt(manager_class: type[Any], method_name: str) -> None:
    original = getattr(manager_class, method_name, None)
    if not callable(original):
        return

    @functools.wraps(original)
    def prompt(self: Any) -> dict[str, Any] | None:
        proposal = original(self)
        if proposal is None:
            return None
        raw = self._prompt_preserving_value(
            "除外パス／glob（カンマ区切り）",
            "",
            required=False,
            description=(
                "Source rootからの相対POSIX path/globです。"
                "空欄は除外なしです。例: build, **/*.tmp"
            ),
            empty_help="除外なし",
        )
        if raw is None:
            return None
        try:
            exclude_paths = parse_exclusion_input(raw)
        except SourceManagerError as exc:
            self._print_error(str(exc))
            return None
        updated = copy.deepcopy(proposal)
        fetch = dict(updated.get("fetch") or {})
        fetch["exclude_paths"] = exclude_paths
        updated["fetch"] = fetch
        summary = list(updated.get("summary") or ())
        summary.append(
            (
                "除外パス",
                ", ".join(exclude_paths) if exclude_paths else "なし",
            )
        )
        updated["summary"] = tuple(summary)
        return updated

    setattr(manager_class, method_name, prompt)


def _record_callback_result(
    progress_callback: Any,
    documents: int,
    confirmed: bool,
) -> None:
    if progress_callback is None:
        return
    try:
        setattr(
            progress_callback,
            _RESULT_ATTR,
            {
                "documents": max(0, int(documents)),
                "confirmed": bool(confirmed),
            },
        )
    except Exception:
        return


def _set_optional_attribute(target: Any, name: str, value: Any) -> tuple[bool, Any]:
    if target is None:
        return False, None
    existed = hasattr(target, name)
    previous = getattr(target, name, None)
    try:
        setattr(target, name, value)
    except Exception:
        return False, None
    return existed, previous


def _restore_optional_attribute(
    target: Any,
    name: str,
    previous: tuple[bool, Any],
) -> None:
    if target is None:
        return
    existed, value = previous
    try:
        if existed:
            setattr(target, name, value)
        else:
            delattr(target, name)
    except Exception:
        return
