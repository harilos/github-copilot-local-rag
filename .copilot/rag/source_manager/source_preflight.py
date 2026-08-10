from __future__ import annotations

import copy
import functools
from typing import Any, Mapping


_RUNTIME_PATCH_MARKER = "_local_rag_source_preflight_runtime_installed"
_MANAGER_PATCH_MARKER = "_local_rag_source_preflight_manager_installed"
_REDMINE_REQUIRED_ATTR = "_local_rag_preflight_required"
_RESULT_ATTR = "_local_rag_preflight_result"
_CONFIRM_METHOD = "confirm_source_estimate"


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

        setattr(renderer, _CONFIRM_METHOD, confirm_source_estimate)
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
            add_root=add_root,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            metadata_publisher=metadata_publisher,
            progress_callback=progress_callback,
        )

    runner._reflect_and_sync = reflect_and_sync


def _confirm_and_store(
    store: Any,
    source: Any,
    state: Any,
    progress_callback: Any,
) -> tuple[Any, bool, int]:
    documents = max(0, int(state.payload.get("fetched_count") or 0))
    if bool(state.payload.get("preflight_confirmed")):
        return state, True, documents

    confirmed = _request_confirmation(progress_callback, documents)
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
    store.append_event(
        source.payload["local_source_key"],
        (
            "source.preflight.confirmed"
            if confirmed
            else "source.preflight.declined"
        ),
        {
            "estimated_documents": documents,
            "estimated_minutes_min": minimum,
            "estimated_minutes_max": maximum,
        },
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


def _request_confirmation(progress_callback: Any, documents: int) -> bool:
    callback = getattr(progress_callback, _CONFIRM_METHOD, None)
    if not callable(callback):
        return True
    return bool(callback(max(0, int(documents))))


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
