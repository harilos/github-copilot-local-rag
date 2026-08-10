from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from source_manager import artifact_reset, runner
from source_manager.checkpoints import complete_run, new_run_state
from source_manager.errors import SourceManagerError
from source_manager.store import MISSING_ETAG, SourceStore


class ArtifactResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Path(self.temporary.name) / "fixture-rag"
        self.db.mkdir()
        (self.db / "db.json").write_text('{"collection":"fixture"}', encoding="utf-8")
        (self.db / "DB_PROFILE.md").write_text("description", encoding="utf-8")
        (self.db / "source-links.json").write_text("links", encoding="utf-8")
        (self.db / "source-links.json.bak").write_text("backup", encoding="utf-8")
        source = self.db / "sources" / "src_fixture-0123456789ab"
        work = source / "work" / "ingest" / source.name
        work.mkdir(parents=True)
        (source / "source.json").write_text('{"source_type":"other"}', encoding="utf-8")
        (source / "state.json").write_text("state", encoding="utf-8")
        (source / "events.jsonl").write_text("event\n", encoding="utf-8")
        (work / "document.md").write_text("document", encoding="utf-8")
        for relative in ("data/clean", "index", "logs"):
            (self.db / relative).mkdir(parents=True, exist_ok=True)
        for relative in (
            "catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm",
            "data/clean/record.json", "index/manifest.json",
            "logs/index_state.json", "logs/progress.json", "logs/prepare_errors.json",
        ):
            (self.db / relative).write_text(relative, encoding="utf-8")
        self.preserved = (
            self.db / "db.json", self.db / "DB_PROFILE.md",
            self.db / "source-links.json", self.db / "source-links.json.bak",
            source / "source.json", source / "events.jsonl", work / "document.md",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reset_preserves_sources_and_work_and_is_idempotent(self) -> None:
        before = {path: path.read_bytes() for path in self.preserved}
        first = artifact_reset.reset_derived_artifacts(
            self.db, daemon_status="not_running"
        )
        second = artifact_reset.reset_derived_artifacts(
            self.db, daemon_status="stopped"
        )
        self.assertTrue(first["removed"])
        self.assertEqual([], second["removed"])
        self.assertEqual(before, {path: path.read_bytes() for path in self.preserved})
        for relative in (
            "catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm",
            "data/clean", "index", "logs/index_state.json",
            "logs/progress.json", "logs/prepare_errors.json",
            "sources/src_fixture-0123456789ab/state.json",
        ):
            self.assertFalse((self.db / relative).exists(), relative)

    def test_daemon_failure_and_anchor_failure_change_nothing(self) -> None:
        before = self._snapshot()
        with self.assertRaisesRegex(SourceManagerError, "daemon stop"):
            artifact_reset.reset_derived_artifacts(self.db, daemon_status="draining")
        self.assertEqual(before, self._snapshot())
        with mock.patch.object(Path, "unlink", side_effect=OSError("busy")), \
                self.assertRaisesRegex(SourceManagerError, "catalog.sqlite"):
            artifact_reset.reset_derived_artifacts(self.db, daemon_status="stopped")
        self.assertEqual(before, self._snapshot())

    def test_failure_after_anchor_is_not_ready_and_retry_finishes(self) -> None:
        original = artifact_reset.shutil.rmtree
        def remove(path):
            if path.name == "index":
                raise OSError("busy")
            return original(path)
        with mock.patch.object(artifact_reset.shutil, "rmtree", side_effect=remove), \
                self.assertRaisesRegex(SourceManagerError, "index"):
            artifact_reset.reset_derived_artifacts(self.db, daemon_status="stopped")
        self.assertFalse((self.db / "catalog.sqlite").exists())
        self.assertTrue((self.db / "index").exists())
        artifact_reset.reset_derived_artifacts(self.db, daemon_status="not_running")
        self.assertFalse((self.db / "index").exists())

    def _snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.db).as_posix(): path.read_bytes()
            for path in self.db.rglob("*") if path.is_file()
        }


class ForceRunScopeTests(unittest.TestCase):
    def test_artifact_health_is_sampled_once_for_the_whole_source_run(self) -> None:
        items = [
            {"local_source_key": "src_one-0123456789ab", "source_type": "github"},
            {"local_source_key": "src_two-abcdef012345", "source_type": "svn"},
            {
                "local_source_key": "src_other-fedcba987654",
                "source_type": "other", "source_id": "src_other",
            },
        ]
        seen: list[bool] = []
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary)
            def update(_root, _key, force, **_kwargs):
                seen.append(force)
                (db / "catalog.sqlite").touch()
                return {"status": "updated"}
            with mock.patch.object(runner, "list_sources", return_value=items), mock.patch.object(
                runner, "_search_artifacts_ready_for_all_sources", return_value=False
            ), mock.patch.object(runner, "_update_source_for_run", side_effect=update):
                runner.update_all_sources(db)
            self.assertEqual([True, True, True], seen)
            seen.clear()
            with mock.patch.object(runner, "list_sources", return_value=items), mock.patch.object(
                runner, "_search_artifacts_ready_for_all_sources", return_value=True
            ), mock.patch.object(runner, "_update_source_for_run", side_effect=update):
                runner.update_all_sources(db)
            self.assertEqual([False, False], seen)

    def test_force_disables_git_and_svn_revision_no_change(self) -> None:
        cases = {
            "github": {
                "repository_url": "https://example.invalid/repository.git",
                "include_paths": [], "updated_within_days": None,
            },
            "svn": {
                "repository_url": "https://example.invalid/svn/repository",
                "recursive": True, "updated_within_days": None,
            },
        }
        for provider, fetch in cases.items():
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                db = Path(temporary)
                registered = runner.register_source(
                    db, source_type=provider, display_name=provider,
                    fetch=fetch, source_id=f"src_{provider}-0123456789ab",
                )
                key = registered["local_source_key"]
                store = SourceStore(db)
                state = new_run_state(store.plan(store.read_source(key).payload))
                state.update({"fetched_count": 1, "indexed_confirmed_count": 1})
                store.save_state(
                    key, complete_run(state), expected_revision=0,
                    expected_etag=MISSING_ETAG,
                )
                observed: list[bool] = []
                def execute(*_args, **kwargs):
                    observed.append(bool(kwargs["previous_run_complete"]))
                    return {"status": "ok", "documents": 1}
                with mock.patch.object(runner, "execute_fetch_plan", side_effect=execute), \
                        mock.patch.object(runner, "_reflect_and_sync", return_value={"status": "updated"}):
                    runner._update_source_for_run(
                        db, key, True, python_executable="python",
                        rag_root=db, command_runner=lambda _args: None,
                    )
                self.assertEqual([False], observed)


if __name__ == "__main__":
    unittest.main()
