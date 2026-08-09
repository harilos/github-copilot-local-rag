from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from source_manager.checkpoints import complete_run, new_run_state
from source_manager.runner import (
    _previous_success_matches_plan,
    register_source,
    update_source,
)
from source_manager.store import MISSING_ETAG, SourceStore


class SourceNoChangeSkipTests(unittest.TestCase):
    def test_completed_matching_source_skips_reflection_and_preserves_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary) / "example-rag"
            db_root.mkdir()
            registered = register_source(
                db_root,
                source_type="github",
                display_name="fixture",
                fetch={
                    "repository_url": "https://example.invalid/repository.git",
                    "include_paths": [],
                    "updated_within_days": None,
                },
                source_id="src_fixture-0123456789ab",
            )
            key = registered["local_source_key"]
            store = SourceStore(db_root)
            source = store.read_source(key)
            state = new_run_state(store.plan(source.payload))
            state["fetched_count"] = 3
            state["indexed_confirmed_count"] = 3
            state = complete_run(state)
            store.save_state(
                key,
                state,
                expected_revision=0,
                expected_etag=MISSING_ETAG,
            )
            progress: list[dict] = []

            result = update_source(
                db_root,
                key,
                executor=lambda _plan, _work, _state: {
                    "status": "ok",
                    "documents": 3,
                    "revision": "abc123",
                    "no_change": True,
                },
                progress_callback=lambda event: progress.append(dict(event)),
            )

            final = store.read_state(key).payload
            events = [
                json.loads(line)
                for line in (db_root / store.paths(key).events_jsonl).read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
        self.assertEqual("skipped", result["status"])
        self.assertEqual("repository_revision_unchanged", result["skip_reason"])
        self.assertEqual("complete", final["status"])
        self.assertEqual(3, final["fetched_count"])
        self.assertEqual(3, final["indexed_confirmed_count"])
        self.assertIn("fetch.no_change_skipped", {event["event"] for event in events})
        self.assertTrue(any(event.get("status") == "skipped" for event in progress))

    def test_failed_or_changed_plan_cannot_authorize_skip(self) -> None:
        source = {"source_id": "src_fixture-0123456789ab"}
        complete = {
            "status": "complete",
            "phase": "complete",
            "plan_etag": "same",
            "metadata_sync_pending": False,
            "pending_count": 0,
        }
        self.assertTrue(
            _previous_success_matches_plan(source, complete, "same")
        )
        for changed in (
            {**complete, "status": "interrupted"},
            {**complete, "phase": "reflect"},
            {**complete, "plan_etag": "old-settings"},
            {**complete, "pending_count": 1},
            {**complete, "metadata_sync_pending": True},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(
                    _previous_success_matches_plan(source, changed, "same")
                )
        self.assertFalse(
            _previous_success_matches_plan({}, complete, "same")
        )


if __name__ == "__main__":
    unittest.main()
