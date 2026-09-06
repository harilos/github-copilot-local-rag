from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from source_manager import artifact_reset, runner
from source_manager.errors import SourceManagerError
from source_manager.operation_lock import database_operation_lock


TOOL_ROOT = Path(__file__).resolve().parents[2] / "gen_db" / "software_rag_tool"
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
from software_rag_tool.data_lifecycle import (  # noqa: E402
    ATTENTION_REQUIRED,
    DataLifecycleError,
    RESETTING,
    capture_ready_epoch,
    read_lifecycle,
)


class ArtifactResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="data-reset-")
        self.db = Path(self.temporary.name) / "fixture-rag"
        self.db.mkdir()
        self._write("db.json", {"db_name": "fixture-rag", "collection": "fixture"})
        (self.db / "DB_PROFILE.md").write_bytes(
            b"\xef\xbb\xbf# User title\r\n\r\n## User notes\r\n\r\nkeep me\r\n"
        )
        self._write("VERSION.json", {"content_version": "fixture"})
        self._write("source-links.json", {"links": ["keep"]})
        self._source("src_git-0123456789ab", "github", {"repository_url": "https://example.invalid/r.git"})
        self._source("src_other-abcdef012345", "other", {"url": "https://example.invalid/one"})
        for relative in (
            "catalog.sqlite",
            "catalog.sqlite-wal",
            "data/raw/old.txt",
            "data/clean/record.json",
            "index/chroma/segment.bin",
            "index/manifest.json",
            "logs/index_state.json",
            "logs/progress.json",
            "logs/events.jsonl",
            "rag-wrapper.json",
        ):
            path = self.db / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative: str, payload: object) -> None:
        path = self.db / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _source(self, key: str, source_type: str, fetch: dict[str, object]) -> None:
        directory = self.db / "sources" / key
        self._write(
            f"sources/{key}/source.json",
            {
                "schema_version": "local-rag-source-manager-v1",
                "local_source_key": key,
                "source_id": f"source-{key}",
                "source_type": source_type,
                "display_name": key,
                "fetch": fetch,
                "ingest": {
                    "work_directory": f"sources/{key}/work/ingest/{key}",
                    "logical_root_name": key,
                },
                "metadata_sync_pending": False,
                "revision": 1,
                "updated_at": "2026-09-06T00:00:00Z",
            },
        )
        (directory / "work" / "ingest" / key).mkdir(parents=True)
        (directory / "work" / "ingest" / key / "document.md").write_text(
            "old document", encoding="utf-8"
        )
        (directory / "state.json").write_text("old state", encoding="utf-8")
        (directory / "events.jsonl").write_text("old event\n", encoding="utf-8")

    def test_reset_detaches_all_old_read_paths_and_protects_one_shot_original(self) -> None:
        preserved = {
            path: path.read_bytes()
            for path in (
                self.db / "db.json",
                self.db / "VERSION.json",
                self.db / "DB_PROFILE.md",
                self.db / "source-links.json",
                self.db / "sources/src_git-0123456789ab/source.json",
                self.db / "sources/src_other-abcdef012345/source.json",
            )
        }
        first = artifact_reset.reset_data(self.db, daemon_status="not_running")
        second = artifact_reset.reset_data(self.db, daemon_status="stopped")
        self.assertEqual(ATTENTION_REQUIRED, first["status"])
        self.assertEqual(first["epoch"], second["epoch"])
        self.assertEqual(preserved, {path: path.read_bytes() for path in preserved})
        for relative in (
            "catalog.sqlite",
            "catalog.sqlite-wal",
            "data",
            "index",
            "logs",
            "rag-wrapper.json",
            "sources/src_git-0123456789ab/state.json",
            "sources/src_git-0123456789ab/events.jsonl",
            "sources/src_git-0123456789ab/work",
            "sources/src_other-abcdef012345/state.json",
            "sources/src_other-abcdef012345/events.jsonl",
            "sources/src_other-abcdef012345/work",
        ):
            self.assertFalse((self.db / relative).exists(), relative)
        protected = self.db / ".protected-originals/src_other-abcdef012345"
        sealed = list(protected.glob("*/SEALED.json"))
        self.assertEqual(1, len(sealed))
        self.assertEqual(
            "old document",
            next(protected.glob("*/content/ingest/*/document.md")).read_text(
                encoding="utf-8"
            ),
        )
        with self.assertRaises(DataLifecycleError):
            capture_ready_epoch(self.db)

    def test_reset_writes_nonready_marker_before_detaching_and_retry_converges(self) -> None:
        real_replace = os.replace

        def fail_index(source: object, destination: object) -> None:
            if Path(source) == self.db / "index":
                raise PermissionError("fixture busy")
            real_replace(source, destination)

        with mock.patch.object(artifact_reset.os, "replace", side_effect=fail_index):
            with self.assertRaises(PermissionError):
                artifact_reset.reset_data(self.db, daemon_status="stopped")
        lifecycle = read_lifecycle(self.db, allow_missing=False)
        self.assertIsNotNone(lifecycle)
        self.assertEqual(RESETTING, lifecycle.status)
        with self.assertRaises(DataLifecycleError):
            capture_ready_epoch(self.db)
        result = artifact_reset.reset_data(self.db, daemon_status="not_running")
        self.assertEqual(ATTENTION_REQUIRED, result["status"])
        self.assertFalse((self.db / "index").exists())

    def test_retry_repairs_one_shot_seal_after_atomic_move(self) -> None:
        real_manifest = artifact_reset._protected_manifest
        attempts = 0

        def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("fixture seal publication failed")
            return real_manifest(*args, **kwargs)

        with mock.patch.object(
            artifact_reset, "_protected_manifest", side_effect=fail_once
        ):
            with self.assertRaises(PermissionError):
                artifact_reset.reset_data(self.db, daemon_status="stopped")
            result = artifact_reset.reset_data(self.db, daemon_status="stopped")
        self.assertEqual(ATTENTION_REQUIRED, result["status"])
        protected = self.db / ".protected-originals/src_other-abcdef012345"
        self.assertEqual(1, len(list(protected.glob("*/manifest.json"))))
        self.assertEqual(1, len(list(protected.glob("*/SEALED.json"))))

    def test_rerun_after_partial_refresh_uses_a_new_reset_generation(self) -> None:
        first = artifact_reset.reset_data(self.db, daemon_status="stopped")
        (self.db / "catalog.sqlite").write_text("new partial catalog", encoding="utf-8")
        state = self.db / "sources/src_git-0123456789ab/state.json"
        state.write_text("new partial state", encoding="utf-8")
        second = artifact_reset.reset_data(self.db, daemon_status="stopped")
        self.assertNotEqual(first["epoch"], second["epoch"])
        retired = self.db / ".retired-data" / second["reset_run_id"]
        self.assertEqual(
            "new partial catalog",
            (retired / "active/catalog.sqlite").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "new partial state",
            (
                retired
                / "sources/src_git-0123456789ab/state.json"
            ).read_text(encoding="utf-8"),
        )

    def test_unfinished_reset_refuses_changed_source_configuration(self) -> None:
        real_replace = os.replace

        def fail_index(source: object, destination: object) -> None:
            if Path(source) == self.db / "index":
                raise PermissionError("fixture busy")
            real_replace(source, destination)

        with mock.patch.object(artifact_reset.os, "replace", side_effect=fail_index):
            with self.assertRaises(PermissionError):
                artifact_reset.reset_data(self.db, daemon_status="stopped")
        config = self.db / "sources/src_git-0123456789ab/source.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["fetch"]["repository_url"] = "https://example.invalid/new.git"
        config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(SourceManagerError, "configuration changed"):
            artifact_reset.reset_data(self.db, daemon_status="not_running")

    def test_preflight_does_not_mutate_and_excludes_generated_page_inventory(self) -> None:
        config = self.db / "sources/src_git-0123456789ab/source.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["fetch"]["page_urls"] = ["https://generated.invalid/1"]
        config.write_text(json.dumps(payload), encoding="utf-8")
        before = self._snapshot()
        first = artifact_reset.plan_data_reset(self.db)
        self.assertEqual(before, self._snapshot())
        payload["fetch"]["page_urls"] = ["https://generated.invalid/2"]
        config.write_text(json.dumps(payload), encoding="utf-8")
        second = artifact_reset.plan_data_reset(self.db)
        self.assertEqual(first["source_config_digest"], second["source_config_digest"])
        payload["fetch"]["repository_url"] = "https://example.invalid/changed.git"
        config.write_text(json.dumps(payload), encoding="utf-8")
        third = artifact_reset.plan_data_reset(self.db)
        self.assertNotEqual(second["source_config_digest"], third["source_config_digest"])

    def test_daemon_failure_changes_nothing(self) -> None:
        before = self._snapshot()
        with self.assertRaisesRegex(SourceManagerError, "daemon stop"):
            artifact_reset.reset_data(self.db, daemon_status="draining")
        self.assertEqual(before, self._snapshot())

    def test_management_lock_is_reentrant_but_excludes_another_thread(self) -> None:
        failures: list[BaseException] = []

        def contend() -> None:
            try:
                with database_operation_lock(self.db):
                    pass
            except BaseException as exc:
                failures.append(exc)

        with database_operation_lock(self.db):
            with database_operation_lock(self.db):
                thread = threading.Thread(target=contend)
                thread.start()
                thread.join(timeout=2)
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], SourceManagerError)

    def test_automatic_source_refresh_promotes_same_epoch_to_ready(self) -> None:
        other = self.db / "sources/src_other-abcdef012345"
        shutil.rmtree(other)
        reset = artifact_reset.reset_data(self.db, daemon_status="not_running")
        self.assertEqual("refetch_required", reset["status"])
        items = [
            {
                "local_source_key": "src_git-0123456789ab",
                "source_type": "github",
            }
        ]
        with (
            mock.patch.object(runner, "list_sources", return_value=items),
            mock.patch.object(
                runner,
                "_search_artifacts_ready_for_all_sources",
                return_value=True,
            ),
            mock.patch.object(
                runner,
                "_update_source_for_run",
                return_value={"status": "updated"},
            ),
        ):
            result = runner.update_all_sources(self.db)
        self.assertTrue(result["snapshot_marker_eligible"])
        self.assertEqual("ready", result["lifecycle_status"])
        lifecycle = read_lifecycle(self.db, allow_missing=False)
        self.assertEqual(reset["epoch"], lifecycle.epoch)
        self.assertEqual("ready", lifecycle.status)

    def test_nonready_refresh_does_not_replay_old_metadata_only_checkpoint(self) -> None:
        config = self.db / "sources/src_git-0123456789ab/source.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["metadata_sync_pending"] = True
        config.write_text(json.dumps(payload), encoding="utf-8")
        artifact_reset.reset_data(self.db, daemon_status="stopped")
        with mock.patch.object(
            runner,
            "_resume_metadata_sync",
            side_effect=AssertionError("old metadata-only resume must not run"),
        ):
            result = runner.update_source(
                self.db,
                "src_git-0123456789ab",
                executor=lambda *_args: {"status": "ok", "default_branch": "main"},
            )
        self.assertEqual("fetched", result["status"])

    def test_database_without_sources_is_explicit_attention_required(self) -> None:
        shutil.rmtree(self.db / "sources")
        plan = artifact_reset.plan_data_reset(self.db)
        self.assertEqual(0, plan["source_count"])
        self.assertEqual(
            ["no_registered_sources"],
            [item["reason"] for item in plan["exceptions"]],
        )
        result = artifact_reset.reset_data(self.db, daemon_status="not_running")
        self.assertEqual("attention_required", result["status"])
        self.assertEqual("no_registered_sources", result["exceptions"][0]["reason"])

    @unittest.skipUnless(os.name == "nt", "Windows junction contract")
    def test_windows_junction_is_rejected_without_touching_external_content(self) -> None:
        shutil.rmtree(self.db / "data")
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        junction = self.db / "data"
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("junction creation is unavailable")
        try:
            with self.assertRaisesRegex(SourceManagerError, "contains a link"):
                artifact_reset.reset_data(self.db, daemon_status="stopped")
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
        finally:
            if junction.exists():
                os.rmdir(junction)

    def _snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.db).as_posix(): path.read_bytes()
            for path in self.db.rglob("*")
            if path.is_file()
        }


class ForceRunScopeTests(unittest.TestCase):
    def test_nonready_lifecycle_forces_fresh_fetch_and_blocks_one_shot_reuse(self) -> None:
        items = [
            {"local_source_key": "src_one-0123456789ab", "source_type": "github"},
            {
                "local_source_key": "src_other-fedcba987654",
                "source_type": "other",
                "source_id": "one-shot",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "fixture-rag"
            db.mkdir()
            (db / "db.json").write_text("{}", encoding="utf-8")
            lifecycle = artifact_reset._runtime()
            del lifecycle
            from software_rag_tool.data_lifecycle import (  # noqa: PLC0415
                new_reset_lifecycle,
                write_lifecycle,
            )

            marker = new_reset_lifecycle(
                db,
                source_config_digest=artifact_reset.plan_data_reset(db)[
                    "source_config_digest"
                ],
            )
            write_lifecycle(db, marker)
            seen: list[bool] = []

            def update(_root: Path, _key: str, force: bool, **_kwargs: object) -> dict[str, str]:
                seen.append(force)
                return {"status": "updated"}

            with (
                mock.patch.object(runner, "list_sources", return_value=items),
                mock.patch.object(runner, "_update_source_for_run", side_effect=update),
            ):
                result = runner.update_all_sources(db)
            self.assertEqual([True], seen)
            self.assertFalse(result["snapshot_marker_eligible"])
            self.assertEqual(
                "one_shot_source_requires_reimport",
                result["results"][1]["skip_reason"],
            )

    def test_explicitly_reimported_one_shot_is_not_automatically_run_again(self) -> None:
        items = [
            {
                "local_source_key": "src_other-fedcba987654",
                "source_type": "other",
                "source_id": "one-shot",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "fixture-rag"
            db.mkdir()
            (db / "db.json").write_text("{}", encoding="utf-8")
            from software_rag_tool.data_lifecycle import (  # noqa: PLC0415
                new_reset_lifecycle,
                write_lifecycle,
            )

            marker = new_reset_lifecycle(
                db,
                source_config_digest=artifact_reset.plan_data_reset(db)[
                    "source_config_digest"
                ],
            )
            write_lifecycle(db, marker)
            with (
                mock.patch.object(runner, "list_sources", return_value=items),
                mock.patch.object(runner, "_one_shot_reimport_ready", return_value=True),
                mock.patch.object(
                    runner, "_search_artifacts_ready_for_all_sources", return_value=True
                ),
                mock.patch.object(runner, "_complete_data_refresh", return_value="ready"),
                mock.patch.object(runner, "_update_source_for_run") as update,
            ):
                result = runner.update_all_sources(db)
            update.assert_not_called()
            self.assertTrue(result["snapshot_marker_eligible"])
            self.assertEqual(
                "one_shot_source_reimported", result["results"][0]["skip_reason"]
            )


if __name__ == "__main__":
    unittest.main()
