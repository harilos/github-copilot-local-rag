from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from source_manager.checkpoints import complete_run, new_run_state
from source_manager.runner import (
    _previous_success_matches_plan,
    register_source,
    update_all_sources,
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
            self._create_search_artifacts(
                db_root,
                source_id="src_fixture-0123456789ab",
                document_count=3,
            )
            progress: list[dict] = []

            def unchanged_executor(_plan, _work, _state):
                return {
                    "status": "ok",
                    "documents": 3,
                    "revision": "abc123",
                    "no_change": True,
                }

            result = update_source(
                db_root,
                key,
                executor=unchanged_executor,
                progress_callback=lambda event: progress.append(dict(event)),
            )
            update_all_result = update_all_sources(
                db_root,
                executor=unchanged_executor,
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
        self.assertEqual(registered["revision"], result["revision"])
        self.assertEqual(registered["etag"], result["etag"])
        self.assertEqual("complete", final["status"])
        self.assertEqual(3, final["fetched_count"])
        self.assertEqual(3, final["indexed_confirmed_count"])
        self.assertIn("fetch.no_change_skipped", {event["event"] for event in events})
        self.assertTrue(any(event.get("status") == "skipped" for event in progress))
        self.assertEqual(1, update_all_result["completed_source_count"])
        self.assertTrue(update_all_result["snapshot_marker_eligible"])

    def test_missing_or_inconsistent_index_artifacts_force_reflection(self) -> None:
        for damage in ("catalog_missing", "source_count_mismatch"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temporary:
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
                store.save_state(
                    key,
                    complete_run(state),
                    expected_revision=0,
                    expected_etag=MISSING_ETAG,
                )
                self._create_search_artifacts(
                    db_root,
                    source_id="src_fixture-0123456789ab",
                    document_count=2 if damage == "source_count_mismatch" else 3,
                )
                if damage == "catalog_missing":
                    (db_root / "catalog.sqlite").unlink()

                result = update_source(
                    db_root,
                    key,
                    executor=lambda *_: {
                        "status": "ok",
                        "documents": 3,
                        "revision": "abc123",
                        "no_change": True,
                    },
                )

                self.assertEqual("fetched", result["status"])
                self.assertNotIn("skip_reason", result)

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

    @staticmethod
    def _create_search_artifacts(
        db_root: Path,
        *,
        source_id: str,
        document_count: int,
    ) -> None:
        collection = "example-rag"
        (db_root / "db.json").write_text(
            json.dumps({"collection": collection}),
            encoding="utf-8",
        )
        index = db_root / "index"
        chroma = index / "chroma"
        chroma.mkdir(parents=True)
        (index / "manifest.json").write_text(
            json.dumps({"record_count": document_count}),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(db_root / "catalog.sqlite")) as connection:
            connection.execute(
                "CREATE TABLE document ("
                "doc_pk TEXT, source_id TEXT, visible_until TEXT)"
            )
            connection.execute("CREATE TABLE chunk (chunk_pk TEXT)")
            connection.executemany(
                "INSERT INTO document VALUES (?, ?, NULL)",
                [
                    (f"doc-{index}", source_id)
                    for index in range(document_count)
                ],
            )
            connection.executemany(
                "INSERT INTO chunk VALUES (?)",
                [(f"chunk-{index}",) for index in range(document_count)],
            )
            connection.commit()
        with closing(sqlite3.connect(chroma / "chroma.sqlite3")) as connection:
            connection.execute("CREATE TABLE collections (id TEXT, name TEXT)")
            connection.execute("CREATE TABLE segments (id TEXT, collection TEXT)")
            connection.execute("CREATE TABLE embeddings (id TEXT, segment_id TEXT)")
            connection.execute(
                "INSERT INTO collections VALUES (?, ?)",
                ("collection-id", collection),
            )
            connection.execute(
                "INSERT INTO segments VALUES (?, ?)",
                ("segment-id", "collection-id"),
            )
            connection.executemany(
                "INSERT INTO embeddings VALUES (?, ?)",
                [
                    (f"embedding-{index}", "segment-id")
                    for index in range(document_count)
                ],
            )
            connection.commit()


if __name__ == "__main__":
    unittest.main()
