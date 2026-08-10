from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from source_manager import full_reingest as full_reingest_module
from source_manager.checkpoints import complete_run, new_run_state
from source_manager.errors import SourceManagerError
from source_manager.full_reingest import (
    MARKER_NAME,
    finish_full_reingest,
    full_reingest_required,
    request_full_reingest,
)
from source_manager.runner import update_all_sources, update_source
from source_manager.store import SourceStore


class FullReingestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="full-reingest-")
        self.root = Path(self.temporary.name) / "example-rag"
        self.root.mkdir()
        (self.root / "db.json").write_text(
            json.dumps({"db_name": "example-rag", "collection": "example"}),
            encoding="utf-8",
        )
        (self.root / "DB_PROFILE.md").write_text("# Example\n", encoding="utf-8")
        self.store = SourceStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def source(self, kind: str = "github") -> str:
        fetch = (
            {"repository_url": "https://example.invalid/repo.git"}
            if kind == "github"
            else {"one_shot": True}
        )
        stored = self.store.create_source(
            source_type=kind,
            display_name=f"{kind} fixture",
            fetch=fetch,
            source_id=f"source-{kind}",
        )
        key = str(stored.payload["local_source_key"])
        state = complete_run(new_run_state(self.store.plan(stored.payload)))
        state["fetched_count"] = 1
        state["indexed_confirmed_count"] = 1
        self.store.save_state(
            key,
            state,
            expected_revision=0,
            expected_etag="missing",
        )
        self.store.append_event(key, "fixture.ready")
        work = self.store.ensure_work_directory(key)
        (work / "document.txt").write_text("preserved", encoding="utf-8")
        return key

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def add_generated(self) -> None:
        for relative in ("data/clean", "index/chroma", "logs"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        (self.root / "data/clean/records.jsonl").write_text("old", encoding="utf-8")
        (self.root / "index/chroma/chroma.sqlite3").write_text("old", encoding="utf-8")
        (self.root / "index/manifest.json").write_text("{}", encoding="utf-8")
        (self.root / "catalog.sqlite").write_text("old", encoding="utf-8")
        (self.root / "catalog.sqlite-wal").write_text("old", encoding="utf-8")
        (self.root / "logs/index_state.json").write_text("{}", encoding="utf-8")
        (self.root / "logs/progress.json").write_text(
            json.dumps({"status": "completed"}), encoding="utf-8"
        )

    def test_reset_preserves_identity_links_events_and_work(self) -> None:
        key = self.source("github")
        links = {"schema_version": "fixture", "sources": []}
        for name in ("source-links.json", "source-links.json.bak"):
            (self.root / name).write_text(json.dumps(links), encoding="utf-8")
        self.add_generated()
        preserved = [
            self.root / "db.json",
            self.root / "DB_PROFILE.md",
            self.root / "source-links.json",
            self.root / "source-links.json.bak",
            Path(self.store.paths(key).absolute(self.root, self.store.paths(key).source_json)),
            Path(self.store.paths(key).absolute(self.root, self.store.paths(key).events_jsonl)),
            self.store.ensure_work_directory(key) / "document.txt",
        ]
        before = {str(path): self.digest(path) for path in preserved}

        result = request_full_reingest(self.root)

        self.assertEqual(before, {str(path): self.digest(path) for path in preserved})
        self.assertEqual("required", result["status"])
        self.assertTrue(full_reingest_required(self.root))
        for relative in (
            "data/clean", "index", "catalog.sqlite", "catalog.sqlite-wal",
            "logs/index_state.json", "logs/progress.json",
            self.store.paths(key).state_json,
        ):
            self.assertFalse((self.root / relative).exists(), relative)

    def test_active_source_rejects_before_marker_or_deletion(self) -> None:
        key = self.source("github")
        self.add_generated()
        state = self.store.read_state(key)
        payload = dict(state.payload)
        payload.update({"status": "planned", "phase": "fetch"})
        self.store.save_state(
            key, payload,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
        with self.assertRaises(SourceManagerError):
            request_full_reingest(self.root)
        self.assertTrue((self.root / "catalog.sqlite").exists())

    def test_broken_and_unsafe_paths_reject_before_deletion(self) -> None:
        key = self.source("github")
        self.add_generated()
        unsafe = self.store.ensure_work_directory(key) / "document.txt"
        real_lstat = os.lstat

        def linked(path: object, *args: object, **kwargs: object):
            if Path(path) == unsafe:
                return SimpleNamespace(
                    st_mode=stat.S_IFREG,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(full_reingest_module.os, "lstat", side_effect=linked):
            with self.assertRaises(SourceManagerError):
                request_full_reingest(self.root)
        self.assertFalse((self.root / MARKER_NAME).exists())
        self.assertTrue((self.root / "catalog.sqlite").exists())

        (self.root / "sources" / "broken").mkdir()
        with self.assertRaises(SourceManagerError):
            request_full_reingest(self.root)
        self.assertTrue((self.root / "catalog.sqlite").exists())

    def test_marker_blocks_repository_no_change_and_other_one_shot_skip(self) -> None:
        github = self.source("github")
        other = self.source("other")
        request_full_reingest(self.root)
        with self.assertRaisesRegex(SourceManagerError, "no-change"):
            update_source(
                self.root,
                github,
                executor=lambda *_: {"status": "ok", "no_change": True},
            )
        self.assertEqual("fetched", update_source(self.root, other)["status"])
        with mock.patch(
            "source_manager.runner.update_source",
            return_value={"local_source_key": other, "status": "failed"},
        ) as called:
            result = update_all_sources(self.root)
        self.assertGreaterEqual(called.call_count, 2)
        self.assertNotIn(
            "one_shot_source_complete",
            {row.get("skip_reason") for row in result["results"]},
        )
        self.assertTrue(full_reingest_required(self.root))

    def test_marker_is_removed_only_for_exact_success_and_complete_artifacts(self) -> None:
        key = self.source("github")
        request_full_reingest(self.root)
        success = {"results": [{"local_source_key": key, "status": "updated"}]}
        self.assertFalse(
            finish_full_reingest(self.root, success, artifacts_complete=False)
        )
        self.assertTrue(full_reingest_required(self.root))
        self.assertFalse(
            finish_full_reingest(
                self.root,
                {"results": [{"local_source_key": "stale", "status": "updated"}]},
                artifacts_complete=True,
            )
        )
        self.assertTrue(finish_full_reingest(self.root, success, artifacts_complete=True))
        self.assertFalse(full_reingest_required(self.root))

    def test_reset_performs_no_fetch_add_embed_or_rebuild(self) -> None:
        self.source("github")
        self.add_generated()
        with mock.patch("source_manager.runner.update_source") as update:
            request_full_reingest(self.root)
        update.assert_not_called()

    def test_partial_invalidation_keeps_diagnostic_and_rerun_converges(self) -> None:
        self.source("github")
        self.add_generated()
        real_remove = full_reingest_module._remove_generated_target
        calls = 0

        def fail_second(root: Path, target: Path) -> bool:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("fixture lock")
            return real_remove(root, target)

        with mock.patch.object(
            full_reingest_module, "_remove_generated_target", side_effect=fail_second
        ):
            with self.assertRaises(SourceManagerError):
                request_full_reingest(self.root)
        marker = json.loads((self.root / MARKER_NAME).read_text(encoding="utf-8"))
        self.assertEqual("invalidation_failed", marker["status"])
        self.assertEqual("PermissionError", marker["failure"])
        self.assertTrue(marker["deleted"])
        self.assertEqual("required", request_full_reingest(self.root)["status"])

    def test_dead_rebuild_progress_does_not_block_reset_forever(self) -> None:
        self.source("github")
        self.add_generated()
        (self.root / "logs/progress.json").write_text(
            json.dumps({"status": "running", "operation_pid": 12345}),
            encoding="utf-8",
        )
        with mock.patch.object(full_reingest_module, "_process_exists", return_value=True):
            with self.assertRaises(SourceManagerError):
                request_full_reingest(self.root)
        self.assertTrue((self.root / "catalog.sqlite").exists())
        with mock.patch.object(full_reingest_module, "_process_exists", return_value=False):
            result = request_full_reingest(self.root)
        self.assertEqual("required", result["status"])

    def test_windows_process_probe_is_read_only(self) -> None:
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 99
        kernel.GetExitCodeProcess.side_effect = (
            lambda _handle, code: setattr(code._obj, "value", 259) or True
        )
        api = SimpleNamespace(kernel32=kernel)
        with (
            mock.patch.object(full_reingest_module.os, "name", "nt"),
            mock.patch.object(full_reingest_module.ctypes, "windll", api),
            mock.patch.object(full_reingest_module.os, "kill") as kill,
        ):
            self.assertTrue(full_reingest_module._process_exists(12345))
        kill.assert_not_called()
        self.assertIs(kernel.OpenProcess.restype, full_reingest_module.ctypes.c_void_p)
        self.assertEqual(3, len(kernel.OpenProcess.argtypes))
        self.assertEqual(2, len(kernel.GetExitCodeProcess.argtypes))
        kernel.CloseHandle.assert_called_once_with(99)


if __name__ == "__main__":
    unittest.main()
