from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from source_manager import runner
from source_manager.errors import SourceManagerError
from source_manager.lifecycle_bridge import require_ready_database
from source_manager.store import SourceStore
from software_rag_tool.data_lifecycle import (
    DataLifecycleError, assert_ready_epoch, capture_ready_epoch,
    empty_config_digest, new_reset_lifecycle, read_lifecycle,
    transition_lifecycle, write_lifecycle,
)
from wrapper.search_command import _add_ingestion_notice
from software_rag_tool.search_api import _finalize_search_payload


class PartialSearchRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "fixture-rag"
        self.db.mkdir()
        (self.db / "db.json").write_text(json.dumps({"collection": "fixture"}))
        self.store = SourceStore(self.db)
        self.keys = [f"src_{name}-0123456789ab" for name in ("a", "b", "c")]
        for key in self.keys:
            source = self.store.create_source(
                source_type="github", display_name=key,
                fetch={"repository_url": "https://example.invalid/r.git"},
                local_source_key=key,
            )
            self.store.confirm_source_id(
                key, key, expected_revision=source.revision, expected_etag=source.etag,
            )
        self.marker = new_reset_lifecycle(self.db, source_config_digest=empty_config_digest())
        write_lifecycle(self.db, self.marker)
        transition_lifecycle(self.db, self.marker, status="refetch_required")

    def state(self, key: str, *, count: int, status: str = "complete", empty: int = 0) -> None:
        source = self.store.read_source(key)
        current = self.store.read_state(key)
        state = runner.new_run_state(self.store.plan(source.payload))
        state.update({
            "status": status, "phase": "complete" if status == "complete" else "reflect",
            "indexed_confirmed_count": count, "fetched_count": count + empty,
            "empty_files": empty, "can_resume": status != "complete",
        })
        self.store.save_state(key, state, expected_revision=current.revision, expected_etag=current.etag)

    def artifacts(self, *, consistent: bool = True) -> None:
        (self.db / "index/chroma").mkdir(parents=True)
        (self.db / "index/manifest.json").write_text(json.dumps({"record_count": 1}))
        with sqlite3.connect(self.db / "catalog.sqlite") as db:
            db.executescript("CREATE TABLE document(doc_pk INTEGER, source_id TEXT); CREATE TABLE chunk(chunk_pk INTEGER);")
            db.execute("INSERT INTO document VALUES (1, ?)", (self.keys[0],))
            db.execute("INSERT INTO chunk VALUES (1)")
        with sqlite3.connect(self.db / "index/chroma/chroma.sqlite3") as db:
            db.executescript("CREATE TABLE collections(id TEXT, name TEXT); CREATE TABLE segments(id TEXT, collection TEXT); CREATE TABLE embeddings(segment_id TEXT);")
            db.execute("INSERT INTO collections VALUES ('c', 'fixture')")
            db.execute("INSERT INTO segments VALUES ('s', 'c')")
            if consistent:
                db.execute("INSERT INTO embeddings VALUES ('s')")

    def test_search_opens_before_failed_sibling_and_empty_source_finish(self) -> None:
        visited = []

        def update(_root, key, _force, **_kwargs):
            visited.append(key)
            if key == self.keys[0]:
                self.artifacts()
                self.state(key, count=1, status="partial")
                return {"status": "partial", "display_name": key}
            self.assertEqual(self.marker.epoch, capture_ready_epoch(self.db))
            if key == self.keys[1]:
                raise SourceManagerError("fixture source failed")
            self.state(key, count=0, empty=1)
            return {"status": "updated", "warning_files": 1}

        with mock.patch.object(runner, "_update_source_for_run", side_effect=update):
            result = runner.update_all_sources(self.db)
        self.assertEqual(self.keys, visited)
        self.assertEqual("searchable_partial", result["lifecycle_status"])
        self.assertFalse(result["snapshot_marker_eligible"])
        self.assertEqual(["partial", "failed", "updated"], [item["status"] for item in result["results"]])
        payload = {"status": "ok", "evidence": [{"id": "E1"}]}
        _add_ingestion_notice(payload, self.db)
        self.assertEqual("ok", payload["status"])
        self.assertEqual([{"id": "E1"}], payload["evidence"])
        self.assertIn("未完了", payload["warnings"][0])
        self.assertIn("本文なし", payload["warnings"][1])
        lower = _finalize_search_payload(
            {"status": "ok", "warnings": [], "evidence": []},
            store=SimpleNamespace(context=SimpleNamespace(root=self.db)),
            db_name=self.db.name, explain=False,
        )
        self.assertEqual("ok", lower["status"])
        self.assertIn("未完了", lower["warnings"][0])
        with self.assertRaises(DataLifecycleError):
            require_ready_database(self.db)
        next_reset = new_reset_lifecycle(self.db, source_config_digest=empty_config_digest())
        write_lifecycle(self.db, next_reset)
        transition_lifecycle(self.db, next_reset, status="searchable_partial")
        with self.assertRaises(DataLifecycleError):
            assert_ready_epoch(self.db, self.marker.epoch)

    def test_count_mismatch_does_not_open_search(self) -> None:
        self.artifacts(consistent=False)
        self.state(self.keys[0], count=1)
        status = runner._complete_data_refresh(self.db, self.marker, runner.list_sources(self.db), allow_partial=True)
        self.assertEqual("refetch_required", status)
        with self.assertRaises(DataLifecycleError):
            capture_ready_epoch(self.db)

    def test_empty_only_checkpoint_does_not_claim_searchable_documents(self) -> None:
        self.state(self.keys[0], count=0, empty=1)
        self.assertFalse(runner._search_artifacts_ready_for_any_source(self.db, runner.list_sources(self.db)))

    def test_completed_siblings_promote_to_ready_without_changing_epoch(self) -> None:
        self.artifacts()
        for index, key in enumerate(self.keys):
            self.state(key, count=int(index == 0), empty=int(index != 0))
        items = runner.list_sources(self.db)
        runner._complete_data_refresh(self.db, self.marker, items, allow_partial=True)
        self.assertEqual("ready", runner._complete_data_refresh(self.db, self.marker, items))
        self.assertEqual(self.marker.epoch, require_ready_database(self.db))
        self.assertEqual("ready", runner._complete_data_refresh(self.db, self.marker, items, allow_partial=True))


if __name__ == "__main__":
    unittest.main()
