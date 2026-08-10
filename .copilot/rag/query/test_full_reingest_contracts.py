from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manage = load("full_reingest_manage", RAG_ROOT / "manage.py")
database_list = load(
    "full_reingest_database_list", RAG_ROOT / "wrapper" / "database_list.py"
)
from wrapper import search_command  # noqa: E402
status_module = load("full_reingest_status", RAG_ROOT / "gen_db" / "status.py")
rebuild_module = load(
    "full_reingest_rebuild", RAG_ROOT / "gen_db" / "rebuild_component.py"
)


class FullReingestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="full-reingest-contract-")
        self.base = Path(self.temporary.name)
        self.dbs = self.base / "dbs"
        self.root = self.dbs / "example-rag"
        self.root.mkdir(parents=True)
        (self.root / "db.json").write_text(
            json.dumps({"db_name": "example-rag", "collection": "example"}),
            encoding="utf-8",
        )
        (self.root / "DB_PROFILE.md").write_text("# Example\n", encoding="utf-8")
        (self.root / "full-reingest-required.json").write_text(
            json.dumps({"schema_version": "local-rag.full-reingest-required.v1"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_search_stops_before_lower_process_and_reports_required_state(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(search_command, "_database_root", return_value=self.root),
            mock.patch.object(search_command.subprocess, "run") as run,
            contextlib.redirect_stdout(output),
        ):
            code = search_command.main(["question", "--db", "example-rag"])
        self.assertEqual(2, code)
        self.assertEqual("full_reingest_required", json.loads(output.getvalue())["status"])
        run.assert_not_called()

    def test_auto_selected_database_also_reports_required_state(self) -> None:
        completed = SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"status": "error", "db": "example-rag"}).encode(),
            stderr=b"",
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                search_command,
                "_database_root",
                side_effect=lambda _root, name: self.root if name else None,
            ),
            mock.patch.object(search_command.subprocess, "run", return_value=completed),
            contextlib.redirect_stdout(output),
        ):
            code = search_command.main(["question", "--auto"])
        self.assertEqual(2, code)
        self.assertEqual("full_reingest_required", json.loads(output.getvalue())["status"])

    def test_database_list_and_status_expose_required_state(self) -> None:
        public = database_list._public_database(
            {"name": "example-rag", "title": "Example"}, self.dbs
        )
        self.assertTrue(public["full_reingest_required"])
        self.assertIn("全件取り直し", public["content_summary"])

        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs)}),
            mock.patch.object(
                status_module.sys,
                "argv",
                ["status.py", "--db", "example-rag", "--json"],
            ),
            contextlib.redirect_stdout(output),
        ):
            status_module.main()
        payload = json.loads(output.getvalue())
        self.assertEqual("full_reingest_required", payload["status"])
        self.assertFalse(payload["can_resume"])

    def test_manager_confirmation_shows_scope_and_only_requests_reset(self) -> None:
        answers = iter(["y"])
        output: list[str] = []
        manager = manage.LocalRagManager(
            rag_root=self.base,
            dbs_root=self.dbs,
            runtime_python=self.base / "python",
            input_fn=lambda _prompt: next(answers),
            output_fn=output.append,
            runner=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ),
        )
        result = {"status": "required", "deleted": ["index"]}
        with (
            mock.patch(
                "source_manager.daemon_control.stop_search_daemon",
                return_value={"status": "not_running"},
            ) as stop,
            mock.patch(
                "source_manager.full_reingest.request_full_reingest",
                return_value=result,
            ) as request,
        ):
            manager._request_full_reingest("example-rag")
        stop.assert_called_once()
        request.assert_called_once_with(self.root.resolve())
        rendered = "\n".join(output)
        for required in ("DB名", "保持", "削除", "再取得", "全Source"):
            self.assertIn(required, rendered)

    def test_rebuild_publishes_active_state_before_mutation(self) -> None:
        with (
            mock.patch.object(
                rebuild_module.sys,
                "argv",
                ["rebuild_component.py", "--db", "example-rag", "--component", "all"],
            ),
            mock.patch.object(rebuild_module, "ensure_db_layout", return_value=self.root),
            mock.patch.object(rebuild_module, "write_progress") as progress,
            mock.patch.object(rebuild_module, "_rebuild") as rebuild,
        ):
            calls = mock.Mock()
            calls.attach_mock(progress, "progress")
            calls.attach_mock(rebuild, "rebuild")
            rebuild_module.main()
        self.assertEqual("running", progress.call_args_list[0].kwargs["status"])
        self.assertEqual("completed", progress.call_args_list[-1].kwargs["status"])
        self.assertEqual("progress", calls.mock_calls[0][0])
        self.assertEqual("rebuild", calls.mock_calls[1][0])
        rebuild.assert_called_once()


if __name__ == "__main__":
    unittest.main()
