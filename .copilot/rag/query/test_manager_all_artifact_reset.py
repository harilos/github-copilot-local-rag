from __future__ import annotations

import importlib.util
import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


MANAGER_PATH = Path(__file__).resolve().parents[1] / "manage.py"
SPEC = importlib.util.spec_from_file_location("local_rag_manage_data_reset", MANAGER_PATH)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)
from source_manager import artifact_reset, daemon_control, runner  # noqa: E402


class ManagerAllDataResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="manager-data-reset-")
        self.base = Path(self.temporary.name)
        self.rag_root = self.base / "rag"
        self.dbs_root = self.rag_root / "dbs"
        self.dbs_root.mkdir(parents=True)
        self.output: list[str] = []
        self.command_runner = mock.Mock(side_effect=AssertionError("unexpected subprocess"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(self, answers: list[Any] | None = None) -> Any:
        values = iter(answers or [])

        def answer(_prompt: str) -> str:
            value = next(values, EOFError())
            if isinstance(value, BaseException):
                raise value
            return value

        return manage.LocalRagManager(
            rag_root=self.rag_root,
            dbs_root=self.dbs_root,
            input_fn=answer,
            output_fn=self.output.append,
            runner=self.command_runner,
            color=False,
        )

    def make_database(self, name: str) -> Path:
        root = self.dbs_root / name
        root.mkdir()
        self._write(root, "db.json", {"db_name": name, "collection": name})
        self._write(root, "VERSION.json", {"content_version": "fixture"})
        (root / "DB_PROFILE.md").write_text("# User profile\n", encoding="utf-8")
        self._write(root, "source-links.json", {"keep": True})
        for key, provider in (
            ("src_git-0123456789ab", "github"),
            ("src_other-abcdef012345", "other"),
        ):
            self._write(
                root,
                f"sources/{key}/source.json",
                {
                    "local_source_key": key,
                    "source_type": provider,
                    "display_name": key,
                    "fetch": {"url": "https://example.invalid/source"},
                },
            )
            for relative in (
                f"sources/{key}/state.json",
                f"sources/{key}/events.jsonl",
                f"sources/{key}/work/ingest/{key}/old.md",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old", encoding="utf-8")
        for relative in (
            "catalog.sqlite",
            "data/raw/old.md",
            "data/clean/old.json",
            "index/chroma/old.bin",
            "logs/index_state.json",
            "logs/progress.json",
            "rag-wrapper.json",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old", encoding="utf-8")
        return root

    @staticmethod
    def _write(root: Path, relative: str, payload: object) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_top_menu_replaces_old_reset_entry_and_routes_option_seven(self) -> None:
        manager = self.manager(["7", "0"])
        with mock.patch.object(manager, "_reset_all_derived_artifacts") as reset:
            self.assertEqual(0, manager.run())
        reset.assert_called_once_with()
        self.assertEqual(
            "全DBの過去データを切り離して現在設定から取り直す【危険】",
            dict(manage.TOP_MENU)["7"],
        )

    def test_no_database_or_decline_has_no_side_effect(self) -> None:
        manager = self.manager()
        with (
            mock.patch.object(manager, "_database_summaries", return_value=[]),
            mock.patch.object(daemon_control, "stop_search_daemon") as stop,
        ):
            manager._reset_all_derived_artifacts()
        stop.assert_not_called()
        root = self.make_database("one-rag")
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        manager = self.manager(["n"])
        with (
            mock.patch.object(manager, "_database_summaries", return_value=[{"name": "one-rag"}]),
            mock.patch.object(daemon_control, "stop_search_daemon") as stop,
        ):
            manager._reset_all_derived_artifacts()
        stop.assert_not_called()
        self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_daemon_stop_is_required_before_first_mutation(self) -> None:
        root = self.make_database("one-rag")
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        manager = self.manager(["y"])
        with (
            mock.patch.object(manager, "_database_summaries", return_value=[{"name": "one-rag"}]),
            mock.patch.object(daemon_control, "stop_search_daemon", return_value={"status": "draining"}),
            mock.patch.object(artifact_reset, "reset_data") as reset,
        ):
            manager._reset_all_derived_artifacts()
        reset.assert_not_called()
        self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_all_database_reset_uses_one_confirmation_and_never_starts_fetch(self) -> None:
        roots = [self.make_database(name) for name in ("one-rag", "two-rag")]
        preserved = {
            path: path.read_bytes()
            for root in roots
            for path in (
                root / "db.json",
                root / "VERSION.json",
                root / "DB_PROFILE.md",
                root / "source-links.json",
                root / "sources/src_git-0123456789ab/source.json",
                root / "sources/src_other-abcdef012345/source.json",
            )
        }
        manager = self.manager(["y"])
        with (
            mock.patch.object(manager, "_database_summaries", return_value=[{"name": root.name} for root in roots]),
            mock.patch.object(manager, "_confirm", wraps=manager._confirm) as confirm,
            mock.patch.object(daemon_control, "stop_search_daemon", return_value={"status": "stopped"}),
            mock.patch.object(artifact_reset, "reset_data", wraps=artifact_reset.reset_data) as reset,
            mock.patch.object(runner, "update_all_sources") as update_all,
            mock.patch.object(runner, "update_source") as update_one,
        ):
            manager._reset_all_derived_artifacts()
        confirm.assert_called_once()
        self.assertEqual(2, reset.call_count)
        update_all.assert_not_called()
        update_one.assert_not_called()
        self.assertEqual(preserved, {path: path.read_bytes() for path in preserved})
        for root in roots:
            self.assertFalse((root / "catalog.sqlite").exists())
            self.assertFalse((root / "data").exists())
            self.assertFalse((root / "index").exists())
            self.assertFalse((root / "logs").exists())
            self.assertTrue((root / "data-lifecycle.json").is_file())
            self.assertEqual(1, len(list((root / ".protected-originals").glob("*/*/SEALED.json"))))
        rendered = "\n".join(self.output)
        self.assertIn("再取得待ち 0 DB / 要対応 2 DB / 失敗 0 DB", rendered)
        self.assertIn("要対応のDB: one-rag, two-rag", rendered)
        self.assertIn("ネットワーク取得・変換・embedding・再構築を開始しません", rendered)

    def test_preflight_reports_invalid_database_and_does_not_pass_it_to_reset(self) -> None:
        valid = self.make_database("valid-rag")
        manager = self.manager(["y"])
        with (
            mock.patch.object(
                manager,
                "_database_summaries",
                return_value=[{"name": "../outside-rag"}, {"name": "valid-rag"}],
            ),
            mock.patch.object(daemon_control, "stop_search_daemon", return_value={"status": "not_running"}),
            mock.patch.object(artifact_reset, "reset_data", wraps=artifact_reset.reset_data) as reset,
        ):
            manager._reset_all_derived_artifacts()
        reset.assert_called_once_with(valid, daemon_status="not_running")
        self.assertIn("未完了のDB: ../outside-rag", "\n".join(self.output))

    def test_cli_plan_only_uses_same_preflight_without_mutation(self) -> None:
        root = self.make_database("one-rag")
        before = {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        manager = self.manager()
        args = argparse.Namespace(
            all=False,
            db="one-rag",
            plan_only=True,
            yes=False,
        )
        output = io.StringIO()
        with (
            mock.patch.object(manage, "LocalRagManager", return_value=manager),
            mock.patch.object(daemon_control, "stop_search_daemon") as stop,
            contextlib.redirect_stdout(output),
        ):
            code = manage._run_reset_data_cli(args, mock.Mock())
        self.assertEqual(0, code)
        stop.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual("attention_required", payload["status"])
        self.assertEqual("one-rag", payload["plans"][0]["db"])
        self.assertEqual(before, {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        })


if __name__ == "__main__":
    unittest.main()
