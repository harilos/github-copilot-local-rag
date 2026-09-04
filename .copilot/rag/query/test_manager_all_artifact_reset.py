from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


MANAGER_PATH = Path(__file__).resolve().parents[1] / "manage.py"
SPEC = importlib.util.spec_from_file_location("local_rag_manage_all_reset", MANAGER_PATH)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)
from source_manager import artifact_reset, daemon_control, runner  # noqa: E402


class ManagerAllArtifactResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-manager-all-reset-")
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

    def make_database(self, name: str) -> tuple[Path, list[Path], list[Path]]:
        root = self.dbs_root / name
        root.mkdir()
        preserved: list[Path] = []
        removed: list[Path] = []

        def write(relative: str, content: str, *, keep: bool) -> None:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            (preserved if keep else removed).append(path)

        for relative, content in (
            ("db.json", json.dumps({"db_name": name, "collection": name})),
            ("DB_PROFILE.md", "DB description"),
            ("VERSION.json", '{"content_version":"fixture"}'),
            ("source-links.json", "source links"),
            ("source-links.json.bak", "source link backup"),
            ("data/raw/keep.txt", "raw input"),
            ("logs/events.jsonl", "database event\n"),
        ):
            write(relative, content, keep=True)
        for key, provider in (
            ("src_one-0123456789ab", "github"),
            ("src_two-abcdef012345", "other"),
        ):
            prefix = f"sources/{key}"
            write(f"{prefix}/source.json", json.dumps({"source_type": provider}), keep=True)
            write(f"{prefix}/events.jsonl", "source event\n", keep=True)
            write(f"{prefix}/work/ingest/{key}/document.md", "取得済み文書", keep=True)
            write(f"{prefix}/state.json", '{"status":"complete"}', keep=False)
        for relative in (
            "catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm",
            "data/clean/record.json", "index/chroma/segment.bin",
            "index/manifest.json", "logs/index_state.json",
            "logs/progress.json", "logs/prepare_errors.json",
        ):
            write(relative, relative, keep=False)
        return root, preserved, removed

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.base).as_posix(): path.read_bytes()
            for path in self.base.rglob("*") if path.is_file()
        }

    def test_top_menu_routes_option_seven_to_all_database_reset(self) -> None:
        manager = self.manager(["7", "0"])
        with mock.patch.object(manager, "_reset_all_derived_artifacts") as reset:
            self.assertEqual(0, manager.run())
        reset.assert_called_once_with()
        self.assertEqual(
            "全DBの全Sourceを全件取り直しが必要な状態にする【危険】",
            dict(manage.TOP_MENU)["7"],
        )
        self.command_runner.assert_not_called()

    def test_no_databases_does_not_confirm_stop_daemon_or_reset(self) -> None:
        manager = self.manager()
        with (
            mock.patch.object(manager, "_database_summaries", return_value=[]),
            mock.patch.object(manager, "_confirm") as confirm,
            mock.patch.object(daemon_control, "stop_search_daemon") as stop,
            mock.patch.object(artifact_reset, "reset_derived_artifacts") as reset,
        ):
            manager._reset_all_derived_artifacts()
        confirm.assert_not_called()
        stop.assert_not_called()
        reset.assert_not_called()
        self.assertIn("リセットできるDBがありません", "\n".join(self.output))

    def test_declined_default_eof_and_interrupt_leave_everything_unchanged(self) -> None:
        self.make_database("one-rag")
        before = self.snapshot()
        for answer in ("n", "", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__ if not isinstance(answer, str) else answer):
                manager = self.manager([answer])
                with (
                    mock.patch.object(manager, "_database_summaries", return_value=[{"name": "one-rag"}]),
                    mock.patch.object(daemon_control, "stop_search_daemon") as stop,
                    mock.patch.object(artifact_reset, "reset_derived_artifacts") as reset,
                ):
                    manager._reset_all_derived_artifacts()
                stop.assert_not_called()
                reset.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def test_daemon_stop_must_be_confirmed_before_any_database_reset(self) -> None:
        self.make_database("one-rag")
        before = self.snapshot()
        for result in (
            {"status": "draining"}, {"status": "restarted"},
            {"status": "unknown"}, {}, RuntimeError("stop failed"),
        ):
            with self.subTest(result=result):
                manager = self.manager(["y"])
                stop_kwargs = (
                    {"side_effect": result}
                    if isinstance(result, Exception)
                    else {"return_value": result}
                )
                with (
                    mock.patch.object(manager, "_database_summaries", return_value=[{"name": "one-rag"}]),
                    mock.patch.object(daemon_control, "stop_search_daemon", **stop_kwargs) as stop,
                    mock.patch.object(artifact_reset, "reset_derived_artifacts") as reset,
                ):
                    manager._reset_all_derived_artifacts()
                stop.assert_called_once_with(self.rag_root, timeout_seconds=10.0)
                reset.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def test_two_databases_reset_every_source_preserve_inputs_and_are_idempotent(self) -> None:
        fixtures = [self.make_database(name) for name in ("one-rag", "two-rag")]
        preserved = {path: path.read_bytes() for _, paths, _ in fixtures for path in paths}
        manager = self.manager(["y", "yes"])
        summaries = [{"name": "one-rag"}, {"name": "two-rag"}, {"name": "one-rag"}]
        with (
            mock.patch.object(manager, "_database_summaries", return_value=summaries),
            mock.patch.object(manager, "_confirm", wraps=manager._confirm) as confirm,
            mock.patch.object(daemon_control, "stop_search_daemon", return_value={"status": "stopped"}) as stop,
            mock.patch.object(artifact_reset, "reset_derived_artifacts", wraps=artifact_reset.reset_derived_artifacts) as reset,
            mock.patch.object(runner, "update_all_sources") as update_all,
            mock.patch.object(runner, "update_source") as update_one,
            mock.patch.object(manager, "_repair_search_automatically") as rebuild,
            mock.patch.object(manager, "_invoke") as invoke,
        ):
            manager._reset_all_derived_artifacts()
            confirm.assert_called_once()
            stop.assert_called_once_with(self.rag_root, timeout_seconds=10.0)
            self.assertEqual(
                [mock.call(root, daemon_status="stopped") for root, _, _ in fixtures],
                reset.call_args_list,
            )
            self.assertEqual(preserved, {path: path.read_bytes() for path in preserved})
            for root, _, removed in fixtures:
                for path in removed + [root / "data/clean", root / "index"]:
                    self.assertFalse(path.exists(), str(path))
            first_snapshot = self.snapshot()
            stop.reset_mock()
            stop.return_value = {"status": "not_running"}
            reset.reset_mock()
            manager._reset_all_derived_artifacts()
            stop.assert_called_once_with(self.rag_root, timeout_seconds=10.0)
            self.assertEqual(
                [mock.call(root, daemon_status="not_running") for root, _, _ in fixtures],
                reset.call_args_list,
            )
            self.assertEqual(first_snapshot, self.snapshot())
            update_all.assert_not_called()
            update_one.assert_not_called()
            rebuild.assert_not_called()
            invoke.assert_not_called()
        self.command_runner.assert_not_called()
        rendered = "\n".join(self.output)
        self.assertEqual(2, rendered.count("成功 2 DB / 失敗 0 DB"))
        self.assertEqual(2, rendered.count("削除: 0件"))
        self.assertIn("対象: 2 DB", rendered)
        self.assertIn("再取得・反映が完了するまで、対象DBは検索できなくなります", rendered)
        self.assertIn("この操作だけでは取得・ADD・embedding・再構築・再試行を開始しません", rendered)

    def test_invalid_database_targets_are_reported_and_never_passed_to_reset(self) -> None:
        valid, _, _ = self.make_database("valid-rag")
        outside = self.rag_root / "outside-rag"
        outside.mkdir()
        sentinel = outside / "catalog.sqlite"
        sentinel.write_bytes(b"must remain")
        manager = self.manager(["y"])
        names = ["../outside-rag", "missing-rag", "valid-rag"]
        with (
            mock.patch.object(manager, "_database_summaries", return_value=[{"name": name} for name in names]),
            mock.patch.object(daemon_control, "stop_search_daemon", return_value={"status": "not_running"}),
            mock.patch.object(artifact_reset, "reset_derived_artifacts", return_value={"removed": []}) as reset,
        ):
            manager._reset_all_derived_artifacts()
        reset.assert_called_once_with(valid, daemon_status="not_running")
        self.assertEqual(b"must remain", sentinel.read_bytes())
        rendered = "\n".join(self.output)
        self.assertIn("成功 1 DB / 失敗 2 DB", rendered)
        self.assertIn("未完了のDB: ../outside-rag, missing-rag", rendered)

    def test_middle_database_partial_failure_continues_and_retry_finishes(self) -> None:
        fixtures = [self.make_database(name) for name in ("one-rag", "two-rag", "three-rag")]
        preserved = {path: path.read_bytes() for _, paths, _ in fixtures for path in paths}
        manager = self.manager(["y", "y"])
        original_remove = artifact_reset.shutil.rmtree

        def remove(path: Path) -> None:
            if path == self.dbs_root / "two-rag" / "index":
                raise OSError("fixture index is busy")
            original_remove(path)

        with (
            mock.patch.object(manager, "_database_summaries", return_value=[{"name": root.name} for root, _, _ in fixtures]),
            mock.patch.object(daemon_control, "stop_search_daemon", return_value={"status": "not_running"}) as stop,
            mock.patch.object(artifact_reset.shutil, "rmtree", side_effect=remove),
        ):
            manager._reset_all_derived_artifacts()
        stop.assert_called_once_with(self.rag_root, timeout_seconds=10.0)
        self.assertEqual(preserved, {path: path.read_bytes() for path in preserved})
        for root, _, removed in fixtures:
            self.assertFalse((root / "catalog.sqlite").exists())
            if root.name == "two-rag":
                self.assertTrue((root / "index").is_dir())
                self.assertTrue((root / "sources/src_one-0123456789ab/state.json").exists())
            else:
                for path in removed:
                    self.assertFalse(path.exists(), str(path))
        rendered = "\n".join(self.output)
        self.assertIn("成功 2 DB / 失敗 1 DB", rendered)
        self.assertIn("未完了のDB: two-rag", rendered)
        self.assertIn("失敗DBは一部削除済みの場合があります", rendered)
        with (
            mock.patch.object(manager, "_database_summaries", return_value=[{"name": root.name} for root, _, _ in fixtures]),
            mock.patch.object(daemon_control, "stop_search_daemon", return_value={"status": "not_running"}),
        ):
            manager._reset_all_derived_artifacts()
        for _, _, removed in fixtures:
            for path in removed:
                self.assertFalse(path.exists(), str(path))
        self.assertEqual(preserved, {path: path.read_bytes() for path in preserved})
        self.assertIn("成功 3 DB / 失敗 0 DB", "\n".join(self.output))
        self.command_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
