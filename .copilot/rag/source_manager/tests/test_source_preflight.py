from __future__ import annotations

import types
import unittest
from dataclasses import dataclass
from typing import Any

from source_manager.source_preflight import (
    _confirm_and_store,
    _install_gitlab_issues_preflight,
    _install_manager_confirmation,
    _install_redmine_preflight,
    estimate_minutes_range,
)


@dataclass
class _Stored:
    payload: dict[str, Any]
    revision: int = 1
    etag: str = "etag-1"


class _Store:
    def __init__(self, state: _Stored | None = None) -> None:
        self.saved: dict[str, Any] | None = None
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.current = state
        self.issue_ids: list[int] = []

    def read_state(self, key: str) -> _Stored:
        assert self.current is not None
        return self.current

    def save_state(
        self,
        key: str,
        value: dict[str, Any],
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> _Stored:
        self.saved = dict(value)
        self.current = _Stored(
            dict(value),
            expected_revision + 1,
            f"etag-{expected_revision + 1}",
        )
        return self.current

    def append_event(
        self,
        key: str,
        event: str,
        details: dict[str, Any],
    ) -> None:
        self.events.append((key, event, dict(details)))


class _Callback:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.counts: list[int] = []

    def confirm_source_estimate(self, documents: int) -> bool:
        self.counts.append(documents)
        return self.answer


class SourcePreflightTests(unittest.TestCase):
    def test_estimate_uses_one_to_five_minutes_per_document(self) -> None:
        self.assertEqual((0, 0), estimate_minutes_range(0))
        self.assertEqual((12, 60), estimate_minutes_range(12))

    def test_decline_is_saved_as_resumable_before_add(self) -> None:
        store = _Store()
        source = types.SimpleNamespace(
            payload={"local_source_key": "src_example-0123456789ab"}
        )
        state = _Stored(
            {
                "schema_version": "local-rag-source-state-v1",
                "local_source_key": "src_example-0123456789ab",
                "status": "fetched",
                "phase": "reflect",
                "fetched_count": 8,
                "pending_count": 8,
            }
        )
        callback = _Callback(False)

        saved, confirmed, documents = _confirm_and_store(
            store,
            source,
            state,
            callback,
        )

        self.assertFalse(confirmed)
        self.assertEqual(8, documents)
        self.assertEqual([8], callback.counts)
        self.assertEqual("interrupted", saved.payload["status"])
        self.assertEqual("reflect", saved.payload["phase"])
        self.assertTrue(saved.payload["can_resume"])
        self.assertFalse(saved.payload["preflight_confirmed"])
        self.assertEqual("declined", saved.payload["preflight_confirmation"])
        self.assertEqual("source.preflight.declined", store.events[0][1])
        self.assertEqual(40, store.events[0][2]["estimated_minutes_max"])

    def test_confirmation_is_not_repeated_after_it_was_saved(self) -> None:
        store = _Store()
        source = types.SimpleNamespace(
            payload={"local_source_key": "src_example-0123456789ab"}
        )
        state = _Stored(
            {
                "schema_version": "local-rag-source-state-v1",
                "local_source_key": "src_example-0123456789ab",
                "status": "fetched",
                "phase": "reflect",
                "fetched_count": 3,
                "pending_count": 3,
                "preflight_confirmed": True,
            }
        )
        callback = _Callback(False)

        saved, confirmed, documents = _confirm_and_store(
            store,
            source,
            state,
            callback,
        )

        self.assertIs(saved, state)
        self.assertTrue(confirmed)
        self.assertEqual(3, documents)
        self.assertEqual([], callback.counts)
        self.assertIsNone(store.saved)

    def test_manager_prompt_shows_count_and_time_range_without_hiding_answer(self) -> None:
        class Renderer:
            pass

        class Manager:
            def __init__(self) -> None:
                self.lines: list[str] = []
                self.prompts: list[str] = []

            def _progress_callback(
                self,
                operation: str,
                *,
                provider: str | None = None,
            ) -> Renderer:
                return Renderer()

            def output(self, value: str) -> None:
                self.lines.append(value)

            def _print_info(self, value: str) -> None:
                self.lines.append(value)

            def _confirm(self, value: str) -> bool:
                self.prompts.append(value)
                return True

        _install_manager_confirmation(Manager)
        manager = Manager()
        renderer = manager._progress_callback("Source追加", provider="github")

        self.assertTrue(renderer.confirm_source_estimate(7))
        rendered = "\n".join(manager.lines)
        self.assertIn("約7件", rendered)
        self.assertIn("約7～35分", rendered)
        self.assertEqual(
            ["約7件追加します。よろしいですか？"],
            manager.prompts,
        )

    def test_redmine_decline_happens_after_inventory_but_before_detail_fetch(self) -> None:
        detail_started: list[bool] = []
        execution = types.SimpleNamespace()

        def base_redmine(
            settings: dict[str, Any],
            work: Any,
            getter: Any,
            environment: dict[str, str],
            *,
            item_callback: Any,
            batch_callback: Any,
            resume_count: int,
            stable_issue_ids: list[int] | None,
            inventory_callback: Any,
            updated_on_cutoff: str | None,
            progress_callback: Any,
        ) -> dict[str, Any]:
            if stable_issue_ids is None:
                inventory_callback([101, 102, 103])
            detail_started.append(True)
            return {"status": "ok", "documents": 3}

        execution._redmine = base_redmine
        source = types.SimpleNamespace(
            payload={
                "local_source_key": "src_redmine-0123456789ab",
                "source_id": None,
            }
        )
        state = _Stored(
            {
                "schema_version": "local-rag-source-state-v1",
                "local_source_key": "src_redmine-0123456789ab",
                "status": "planned",
                "phase": "fetch",
            }
        )
        store = _Store(state)
        runner = types.SimpleNamespace()

        def base_update(
            store_value: _Store,
            source_value: Any,
            state_value: _Stored,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return execution._redmine(
                {},
                None,
                None,
                {},
                item_callback=None,
                batch_callback=None,
                resume_count=0,
                stable_issue_ids=None,
                inventory_callback=lambda ids: store_value.issue_ids.extend(ids),
                updated_on_cutoff=None,
                progress_callback=kwargs.get("progress_callback"),
            )

        runner._update_redmine_source = base_update
        runner._source_dto = lambda store_value, source_value: {
            "local_source_key": source_value.payload["local_source_key"],
            "source_id": source_value.payload.get("source_id"),
        }
        _install_redmine_preflight(execution, runner)
        callback = _Callback(False)

        result = runner._update_redmine_source(
            store,
            source,
            state,
            progress_callback=callback,
        )

        self.assertEqual("confirmation_declined", result["status"])
        self.assertEqual([3], callback.counts)
        self.assertEqual([101, 102, 103], store.issue_ids)
        self.assertEqual([], detail_started)
        assert store.current is not None
        self.assertTrue(store.current.payload["can_resume"])
        self.assertFalse(store.current.payload["preflight_confirmed"])

    def test_redmine_confirmation_is_persisted_for_resume(self) -> None:
        execution = types.SimpleNamespace()

        def base_redmine(
            settings: dict[str, Any],
            work: Any,
            getter: Any,
            environment: dict[str, str],
            *,
            item_callback: Any,
            batch_callback: Any,
            resume_count: int,
            stable_issue_ids: list[int] | None,
            inventory_callback: Any,
            updated_on_cutoff: str | None,
            progress_callback: Any,
        ) -> dict[str, Any]:
            inventory_callback([1, 2])
            return {"status": "ok", "documents": 2}

        execution._redmine = base_redmine
        source = types.SimpleNamespace(
            payload={
                "local_source_key": "src_redmine-abcdef012345",
                "source_id": None,
            }
        )
        state = _Stored(
            {
                "schema_version": "local-rag-source-state-v1",
                "local_source_key": "src_redmine-abcdef012345",
                "status": "planned",
                "phase": "fetch",
            }
        )
        store = _Store(state)
        runner = types.SimpleNamespace()

        def base_update(
            store_value: _Store,
            source_value: Any,
            state_value: _Stored,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return execution._redmine(
                {},
                None,
                None,
                {},
                item_callback=None,
                batch_callback=None,
                resume_count=0,
                stable_issue_ids=None,
                inventory_callback=lambda ids: store_value.issue_ids.extend(ids),
                updated_on_cutoff=None,
                progress_callback=kwargs.get("progress_callback"),
            )

        runner._update_redmine_source = base_update
        runner._source_dto = lambda store_value, source_value: {}
        _install_redmine_preflight(execution, runner)
        callback = _Callback(True)

        result = runner._update_redmine_source(
            store,
            source,
            state,
            progress_callback=callback,
        )

        self.assertEqual("ok", result["status"])
        assert store.current is not None
        self.assertTrue(store.current.payload["preflight_confirmed"])
        self.assertEqual(2, store.current.payload["preflight_estimated_documents"])
        self.assertEqual("source.preflight.confirmed", store.events[-1][1])

    def test_gitlab_decline_freezes_project_inventory_before_details(
        self,
    ) -> None:
        detail_started: list[bool] = []
        snapshots: list[tuple[int, list[int]]] = []
        execution = types.SimpleNamespace()

        def base_gitlab(
            settings: dict[str, Any],
            work: Any,
            request: Any,
            environment: dict[str, str],
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
        ) -> dict[str, Any]:
            if stable_issue_ids is None:
                inventory_snapshot_callback(101, [11, 12, 13])
            detail_started.append(True)
            return {"status": "ok", "documents": 3}

        execution.fetch_gitlab_issues = base_gitlab
        source = types.SimpleNamespace(
            payload={
                "local_source_key": "src_gitlab_issues-0123456789ab",
                "source_id": None,
            }
        )
        state = _Stored(
            {
                "schema_version": "local-rag-source-state-v1",
                "local_source_key": "src_gitlab_issues-0123456789ab",
                "status": "planned",
                "phase": "fetch",
            }
        )
        store = _Store(state)
        runner = types.SimpleNamespace()

        def base_update(
            store_value: _Store,
            source_value: Any,
            state_value: _Stored,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return execution.fetch_gitlab_issues(
                {},
                None,
                None,
                {},
                item_callback=None,
                batch_callback=None,
                resume_count=0,
                stable_issue_ids=None,
                stable_project_id=None,
                inventory_snapshot_callback=(
                    lambda project_id, issue_ids: snapshots.append(
                        (project_id, list(issue_ids))
                    )
                ),
                updated_after=None,
                progress_callback=kwargs.get("progress_callback"),
            )

        runner._update_gitlab_issues_source = base_update
        runner._source_dto = lambda store_value, source_value: {
            "local_source_key": source_value.payload["local_source_key"],
            "source_id": source_value.payload.get("source_id"),
        }
        _install_gitlab_issues_preflight(execution, runner)
        callback = _Callback(False)

        result = runner._update_gitlab_issues_source(
            store,
            source,
            state,
            progress_callback=callback,
        )

        self.assertEqual("confirmation_declined", result["status"])
        self.assertEqual([3], callback.counts)
        self.assertEqual([(101, [11, 12, 13])], snapshots)
        self.assertEqual([], detail_started)
        assert store.current is not None
        self.assertTrue(store.current.payload["can_resume"])
        self.assertFalse(store.current.payload["preflight_confirmed"])

    def test_gitlab_confirmation_is_persisted_for_resume(self) -> None:
        execution = types.SimpleNamespace()

        def base_gitlab(
            settings: dict[str, Any],
            work: Any,
            request: Any,
            environment: dict[str, str],
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
        ) -> dict[str, Any]:
            inventory_snapshot_callback(101, [1, 2])
            return {"status": "ok", "documents": 2}

        execution.fetch_gitlab_issues = base_gitlab
        source = types.SimpleNamespace(
            payload={
                "local_source_key": "src_gitlab_issues-abcdef012345",
                "source_id": None,
            }
        )
        state = _Stored(
            {
                "schema_version": "local-rag-source-state-v1",
                "local_source_key": "src_gitlab_issues-abcdef012345",
                "status": "planned",
                "phase": "fetch",
            }
        )
        store = _Store(state)
        runner = types.SimpleNamespace()

        def base_update(
            store_value: _Store,
            source_value: Any,
            state_value: _Stored,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return execution.fetch_gitlab_issues(
                {},
                None,
                None,
                {},
                item_callback=None,
                batch_callback=None,
                resume_count=0,
                stable_issue_ids=None,
                stable_project_id=None,
                inventory_snapshot_callback=lambda *_args: None,
                updated_after=None,
                progress_callback=kwargs.get("progress_callback"),
            )

        runner._update_gitlab_issues_source = base_update
        runner._source_dto = lambda store_value, source_value: {}
        _install_gitlab_issues_preflight(execution, runner)
        callback = _Callback(True)

        result = runner._update_gitlab_issues_source(
            store,
            source,
            state,
            progress_callback=callback,
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual([2], callback.counts)
        assert store.current is not None
        self.assertTrue(store.current.payload["preflight_confirmed"])
        self.assertEqual(
            2,
            store.current.payload["preflight_estimated_documents"],
        )
        self.assertEqual("source.preflight.confirmed", store.events[-1][1])


if __name__ == "__main__":
    unittest.main()
