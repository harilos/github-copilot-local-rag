from __future__ import annotations

import types
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from source_manager.source_preflight import (
    _confirm_and_store,
    _install_gitlab_issues_preflight,
    _install_manager_confirmation,
    _install_normal_source_preflight,
    _install_redmine_preflight,
    estimate_minutes_range,
)
from source_manager.source_exclusion import SourcePreview


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

    def test_manager_preview_shows_included_and_excluded_stats_semantically(self) -> None:
        class Renderer:
            pass

        class Manager:
            def __init__(self) -> None:
                self.lines: list[str] = []
                self.prompts: list[str] = []

            def _progress_callback(self, operation: str, *, provider=None):
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
        renderer = manager._progress_callback("Source追加", provider="svn")

        self.assertTrue(
            renderer.confirm_source_preview(
                SourcePreview(3, 1024, 2, 512).to_dict()
            )
        )
        rendered = "\n".join(manager.lines)
        self.assertIn("追加対象", rendered)
        self.assertIn("3件", rendered)
        self.assertIn("1,024", rendered)
        self.assertIn("除外", rendered)
        self.assertIn("2件", rendered)
        self.assertIn("512", rendered)
        self.assertEqual(1, len(manager.prompts))

    def test_file_source_add_uses_filtered_view_and_keeps_acquired_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            acquired = work_root / "ingest" / "source-key"
            (acquired / "build").mkdir(parents=True)
            (acquired / "keep.md").write_text("keep", encoding="utf-8")
            (acquired / "build" / "skip.md").write_text(
                "skip", encoding="utf-8"
            )
            source = types.SimpleNamespace(
                payload={
                    "local_source_key": "src_example-0123456789ab",
                    "source_id": "src_example-0123456789ab",
                    "source_type": "github",
                    "fetch": {"exclude_paths": ["build"]},
                }
            )
            state = _Stored(
                {
                    "schema_version": "local-rag-source-state-v1",
                    "local_source_key": "src_example-0123456789ab",
                    "status": "fetched",
                    "phase": "reflect",
                    "fetched_count": 2,
                    "indexed_confirmed_count": 2,
                    "pending_count": 2,
                }
            )
            store = _Store(state)
            observed: list[tuple[str, list[str]]] = []
            runner = types.SimpleNamespace()

            def reflect(_store, _source, current_state, *, add_root, **_kwargs):
                observed.append(
                    (
                        Path(add_root).name,
                        sorted(
                            path.relative_to(add_root).as_posix()
                            for path in Path(add_root).rglob("*")
                            if path.is_file()
                        ),
                    )
                )
                return {"status": "updated", "state": current_state.payload}

            runner._reflect_and_sync = reflect
            _install_normal_source_preflight(runner)

            result = runner._reflect_and_sync(
                store,
                source,
                state,
                add_root=acquired,
                python_executable=Path("python"),
                rag_root=Path("rag"),
                command_runner=None,
                metadata_publisher=None,
                progress_callback=None,
            )

            self.assertEqual("updated", result["status"])
            self.assertEqual([("source-key", ["keep.md"])], observed)
            self.assertTrue((acquired / "build" / "skip.md").is_file())
            self.assertFalse((work_root / "filtered" / "source-key").exists())
            assert store.current is not None
            self.assertEqual(1, store.current.payload["fetched_count"])
            self.assertEqual(
                1, store.current.payload["indexed_confirmed_count"]
            )
            self.assertEqual(1, store.current.payload["preflight_excluded_count"])

    def test_preview_is_recomputed_when_empty_unset_work_grows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            acquired = work_root / "ingest" / "source-key"
            acquired.mkdir(parents=True)
            source = types.SimpleNamespace(
                payload={
                    "local_source_key": "src_example-0123456789ab",
                    "source_id": "src_example-0123456789ab",
                    "source_type": "other",
                    "fetch": {},
                }
            )
            state = _Stored(
                {
                    "schema_version": "local-rag-source-state-v1",
                    "local_source_key": "src_example-0123456789ab",
                    "status": "fetched",
                    "phase": "reflect",
                    "fetched_count": 0,
                    "indexed_confirmed_count": 0,
                    "pending_count": 0,
                }
            )
            store = _Store(state)
            observed: list[tuple[int, list[str]]] = []
            runner = types.SimpleNamespace()

            def reflect(_store, _source, current_state, *, add_root, **_kwargs):
                observed.append(
                    (
                        current_state.payload["preflight_included_count"],
                        sorted(
                            path.relative_to(add_root).as_posix()
                            for path in Path(add_root).rglob("*")
                            if path.is_file()
                        ),
                    )
                )
                return {"status": "updated"}

            runner._reflect_and_sync = reflect
            _install_normal_source_preflight(runner)
            arguments = {
                "add_root": acquired,
                "python_executable": Path("python"),
                "rag_root": Path("rag"),
                "command_runner": None,
                "metadata_publisher": None,
                "progress_callback": None,
            }

            runner._reflect_and_sync(store, source, state, **arguments)
            (acquired / "later.md").write_text("later", encoding="utf-8")
            assert store.current is not None
            runner._reflect_and_sync(store, source, store.current, **arguments)

            self.assertEqual([(0, []), (1, ["later.md"])], observed)
            assert store.current is not None
            self.assertEqual(1, store.current.payload["fetched_count"])

    def test_filtered_preview_is_rebuilt_when_acquired_work_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            acquired = work_root / "ingest" / "source-key"
            (acquired / "build").mkdir(parents=True)
            (acquired / "keep.md").write_text("keep", encoding="utf-8")
            skipped = acquired / "build" / "skip.md"
            skipped.write_text("skip", encoding="utf-8")
            source = types.SimpleNamespace(
                payload={
                    "local_source_key": "src_example-0123456789ab",
                    "source_id": "src_example-0123456789ab",
                    "source_type": "github",
                    "fetch": {"exclude_paths": ["build"]},
                }
            )
            state = _Stored(
                {
                    "schema_version": "local-rag-source-state-v1",
                    "local_source_key": "src_example-0123456789ab",
                    "status": "fetched",
                    "phase": "reflect",
                    "fetched_count": 2,
                    "indexed_confirmed_count": 0,
                    "pending_count": 2,
                }
            )
            store = _Store(state)
            observed: list[tuple[int, int, list[str]]] = []
            runner = types.SimpleNamespace()

            def reflect(_store, _source, current_state, *, add_root, **_kwargs):
                observed.append(
                    (
                        current_state.payload["preflight_included_count"],
                        current_state.payload["preflight_excluded_count"],
                        sorted(
                            path.relative_to(add_root).as_posix()
                            for path in Path(add_root).rglob("*")
                            if path.is_file()
                        ),
                    )
                )
                return {"status": "updated"}

            runner._reflect_and_sync = reflect
            _install_normal_source_preflight(runner)
            arguments = {
                "add_root": acquired,
                "python_executable": Path("python"),
                "rag_root": Path("rag"),
                "command_runner": None,
                "metadata_publisher": None,
                "progress_callback": None,
            }

            runner._reflect_and_sync(store, source, state, **arguments)
            skipped.unlink()
            (acquired / "later.md").write_text("later", encoding="utf-8")
            assert store.current is not None
            runner._reflect_and_sync(store, source, store.current, **arguments)

            self.assertEqual(
                [
                    (1, 1, ["keep.md"]),
                    (2, 0, ["keep.md", "later.md"]),
                ],
                observed,
            )

    def test_filtered_view_is_discarded_on_decline_and_retry_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            acquired = work_root / "ingest" / "source-key"
            (acquired / "build").mkdir(parents=True)
            (acquired / "keep.md").write_text("keep", encoding="utf-8")
            (acquired / "build" / "skip.md").write_text(
                "skip", encoding="utf-8"
            )
            source = types.SimpleNamespace(
                payload={
                    "local_source_key": "src_example-0123456789ab",
                    "source_id": None,
                    "source_type": "github",
                    "fetch": {"exclude_paths": ["build"]},
                }
            )
            state = _Stored(
                {
                    "schema_version": "local-rag-source-state-v1",
                    "local_source_key": "src_example-0123456789ab",
                    "status": "fetched",
                    "phase": "reflect",
                    "fetched_count": 2,
                    "pending_count": 2,
                }
            )
            store = _Store(state)
            observed: list[list[str]] = []
            runner = types.SimpleNamespace()

            def reflect(_store, _source, _state, *, add_root, **_kwargs):
                observed.append(
                    sorted(
                        path.relative_to(add_root).as_posix()
                        for path in Path(add_root).rglob("*")
                        if path.is_file()
                    )
                )
                return {"status": "updated"}

            runner._reflect_and_sync = reflect
            runner._source_dto = lambda _store, _source: {}
            _install_normal_source_preflight(runner)

            declined = runner._reflect_and_sync(
                store,
                source,
                state,
                add_root=acquired,
                python_executable=Path("python"),
                rag_root=Path("rag"),
                command_runner=None,
                metadata_publisher=None,
                progress_callback=_Callback(False),
            )

            filtered = work_root / "filtered" / "source-key"
            self.assertEqual("confirmation_declined", declined["status"])
            self.assertFalse(filtered.exists())
            self.assertEqual([], observed)
            assert store.current is not None

            retried = runner._reflect_and_sync(
                store,
                source,
                store.current,
                add_root=acquired,
                python_executable=Path("python"),
                rag_root=Path("rag"),
                command_runner=None,
                metadata_publisher=None,
                progress_callback=_Callback(True),
            )

            self.assertEqual("updated", retried["status"])
            self.assertEqual([["keep.md"]], observed)
            self.assertFalse(filtered.exists())

    def test_filtered_view_is_discarded_on_add_abort_and_retry_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            acquired = work_root / "ingest" / "source-key"
            (acquired / "build").mkdir(parents=True)
            (acquired / "keep.md").write_text("keep", encoding="utf-8")
            (acquired / "build" / "skip.md").write_text(
                "skip", encoding="utf-8"
            )
            source = types.SimpleNamespace(
                payload={
                    "local_source_key": "src_example-0123456789ab",
                    "source_id": "src_example-0123456789ab",
                    "source_type": "github",
                    "fetch": {"exclude_paths": ["build"]},
                }
            )
            state = _Stored(
                {
                    "schema_version": "local-rag-source-state-v1",
                    "local_source_key": "src_example-0123456789ab",
                    "status": "fetched",
                    "phase": "reflect",
                    "fetched_count": 2,
                    "pending_count": 2,
                }
            )
            store = _Store(state)
            observed: list[list[str]] = []
            abort = [True, False]
            runner = types.SimpleNamespace()

            def reflect(_store, _source, _state, *, add_root, **_kwargs):
                observed.append(
                    sorted(
                        path.relative_to(add_root).as_posix()
                        for path in Path(add_root).rglob("*")
                        if path.is_file()
                    )
                )
                if abort.pop(0):
                    raise KeyboardInterrupt("simulated ADD abort")
                return {"status": "updated"}

            runner._reflect_and_sync = reflect
            _install_normal_source_preflight(runner)
            arguments = {
                "add_root": acquired,
                "python_executable": Path("python"),
                "rag_root": Path("rag"),
                "command_runner": None,
                "metadata_publisher": None,
                "progress_callback": None,
            }

            with self.assertRaises(KeyboardInterrupt):
                runner._reflect_and_sync(store, source, state, **arguments)

            filtered = work_root / "filtered" / "source-key"
            self.assertFalse(filtered.exists())
            assert store.current is not None

            result = runner._reflect_and_sync(
                store,
                source,
                store.current,
                **arguments,
            )

            self.assertEqual("updated", result["status"])
            self.assertEqual([["keep.md"], ["keep.md"]], observed)
            self.assertFalse(filtered.exists())

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
