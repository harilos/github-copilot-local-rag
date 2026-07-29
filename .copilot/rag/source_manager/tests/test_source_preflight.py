from __future__ import annotations

import types
import unittest
from dataclasses import dataclass
from typing import Any

from source_manager.source_preflight import (
    _confirm_and_store,
    _install_manager_confirmation,
    estimate_minutes_range,
)


@dataclass
class _Stored:
    payload: dict[str, Any]
    revision: int = 1
    etag: str = "etag-1"


class _Store:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def save_state(
        self,
        key: str,
        value: dict[str, Any],
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> _Stored:
        self.saved = dict(value)
        return _Stored(dict(value), expected_revision + 1, "etag-2")

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


if __name__ == "__main__":
    unittest.main()
