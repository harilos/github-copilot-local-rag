from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
sys.path.insert(0, str(QUERY_ROOT))

import agent003_answer_packet
import result_bundle
import skill_runner


def _payload(database: str = "project-rag") -> dict:
    return {
        "schema": "local-rag.search.v1",
        "status": "ok",
        "answerability": "full",
        "selected_db": database,
        "query": "承認値は何ですか？",
        "evidence": [{
            "id": "R1", "text": "承認値は7%です。",
            "matched_excerpt": "承認値は7%です。",
            "context_before": "提案値は12%でした。", "context_after": "Issueはclosedです。",
            "context_reason": "same_section_neighbor", "source_ranges": [],
            "source": {"path": "docs/issue.md", "title": "issue.md", "revision": "sha256:test"},
            "location": {"section": "Decision"}, "signals": ["semantic"],
        }],
        "background_context": [], "related_context": [], "document_results": [],
        "warnings": [], "coverage": {"returned_distinct_documents": 1},
    }


class SkillRunnerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="skill-runner-")
        self.root = Path(self.temporary.name)
        self.spool = self.root / "results"
        self.registry = self.root / "bindings"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = skill_runner.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def _parser_error(self, arguments: list[str]) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            skill_runner.main(arguments)
        self.assertEqual(2, raised.exception.code)

    def _publish(self, database: str = "project-rag") -> dict:
        return result_bundle.publish_result_bundle(
            _payload(database), spool_root=self.spool,
            now=datetime.now(timezone.utc),
        )

    def test_commands_are_fixed_and_question_is_child_stdin(self) -> None:
        parser = skill_runner._parser()
        args = parser.parse_args(skill_runner._protect_option_values([
            "search", "--db", "project-rag", "--question", "--先頭 ' 引用",
            "--answer-goal", "comparison", "--entity", "A2L",
        ]))
        command = skill_runner._search_command(args)
        self.assertEqual(
            [sys.executable, "-I", "-X", "utf8", "-B", str(RAG_ROOT / "search.py")],
            command[:6],
        )
        self.assertIn("--result-delivery", command)
        self.assertIn("file", command)
        self.assertIn("--stdin", command)
        self.assertNotIn(args.question, command)
        self.assertEqual(str(RAG_ROOT / "dbs"), skill_runner._child_environment()["RAG_DBS_ROOT"])
        self.assertNotIn("PYTHONPATH", skill_runner._child_environment())

    def test_search_reads_fixed_bundle_and_emits_one_path_free_packet(self) -> None:
        pointer = self._publish()
        pointer["summary_file"] = "C:" + "\\Users\\victim\\secret.txt"
        with (
            mock.patch.object(skill_runner, "SPOOL_ROOT", self.spool),
            mock.patch.object(skill_runner, "REGISTRY_ROOT", self.registry),
            mock.patch.object(skill_runner, "_run_public", return_value=(pointer, 0)),
        ):
            code, stdout, stderr = self._main([
                "search", "--db", "project-rag", "--question", "承認値は何ですか？",
            ])
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(1, len(stdout.splitlines()))
        packet = json.loads(stdout)
        self.assertEqual("ok", packet["status"])
        self.assertRegex(packet["result_token"], r"^lrt_[A-Za-z0-9_-]{32}$")
        rendered = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn(pointer["result_set_id"], rendered)
        self.assertNotIn("summary_file", rendered)
        self.assertNotIn("C:\\Users", rendered)

    def test_detail_uses_disk_token_in_a_later_invocation(self) -> None:
        pointer = self._publish()
        with (
            mock.patch.object(skill_runner, "SPOOL_ROOT", self.spool),
            mock.patch.object(skill_runner, "REGISTRY_ROOT", self.registry),
            mock.patch.object(skill_runner, "_run_public", return_value=(pointer, 0)),
        ):
            code, stdout, _stderr = self._main([
                "search", "--db", "project-rag", "--question", "承認値は何ですか？",
            ])
        token = json.loads(stdout)["result_token"]
        with (
            mock.patch.object(skill_runner, "SPOOL_ROOT", self.spool),
            mock.patch.object(skill_runner, "REGISTRY_ROOT", self.registry),
        ):
            code, stdout, stderr = self._main([
                "detail", "--result-token", token, "--item-id", "E1",
                "--detail-level", "expanded",
            ])
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        detail = json.loads(stdout)
        self.assertEqual(agent003_answer_packet.DETAIL_SCHEMA_VERSION, detail["schema_version"])
        self.assertEqual("project-rag", detail["database"])
        self.assertEqual(["E1"], detail["requested_evidence_ids"])

    def test_wrong_database_and_tamper_fail_closed_without_locator(self) -> None:
        pointer = self._publish()
        with (
            mock.patch.object(skill_runner, "SPOOL_ROOT", self.spool),
            mock.patch.object(skill_runner, "REGISTRY_ROOT", self.registry),
            mock.patch.object(skill_runner, "_run_public", return_value=(pointer, 0)),
        ):
            code, stdout, stderr = self._main([
                "search", "--db", "wrong-rag", "--question", "question",
            ])
        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        packet = json.loads(stdout)
        self.assertEqual("error", packet["status"])
        self.assertNotIn(pointer["result_set_id"], stdout)

    def test_list_projects_only_bounded_routing_metadata(self) -> None:
        raw = {
            "schema": "local-rag.database-list.v2", "status": "ok",
            "databases": [{
                "name": "project-rag", "title": "Project", "query_hint": "設計資料",
                "content_summary": "10 documents", "source_count": 1,
                "unattributed_document_count": 0, "source_types": [],
                "sources": [{"name": "Main", "type": "github", "label": "GitHub", "document_count": 10}],
                "additional_source_count": 0, "content_summary_status": "complete",
            }],
        }
        with mock.patch.object(skill_runner, "_run_public", return_value=(raw, 0)):
            code, stdout, stderr = self._main(["list"])
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        packet = json.loads(stdout)
        self.assertEqual({"name", "title", "query_hint", "content_summary", "sources"}, set(packet["databases"][0]))
        self.assertNotIn("document_count", stdout)

    def test_setup_projects_sanitized_status_only(self) -> None:
        raw = {
            "status": "error", "setup_complete": False,
            "lookup_ready": False,
            "runtime": {}, "network": {},
            "databases": {"healthy": [], "unhealthy": []},
            "warnings": [], "next_action": "Run setup again.",
            "failed_check": "model_prepare", "error_kind": "dependency_failed",
            "error": "C:" + "\\Users\\victim\\secret.txt",
        }
        with mock.patch.object(skill_runner, "_run_public", return_value=(raw, 1)):
            code, stdout, stderr = self._main(["setup"])
        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertEqual({
            "schema_version", "status", "payload_complete", "setup_complete",
            "phase", "retry_required", "error_code",
        }, set(json.loads(stdout)))
        self.assertNotIn("C:\\Users", stdout)

    def test_setup_rejects_unvalidated_json_object(self) -> None:
        with mock.patch.object(
            skill_runner,
            "_run_public",
            return_value=({"setup_complete": True}, 0),
        ):
            code, stdout, stderr = self._main(["setup"])
        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertEqual("invalid_setup_result", json.loads(stdout)["error_code"])

    def test_search_and_detail_packet_limits_replace_whole_payload(self) -> None:
        token = "lrt_" + "A" * 32
        packet = agent003_answer_packet.build_search_packet({
            "status": "ok", "selected_db": "project-rag",
            "evidence": [{"id": "E1", "text": "x" * 10_000, "source_title": "Title"}],
        }, result_token=token, inspectable_evidence_ids=["E1"])
        output, oversized = skill_runner._render(packet, limit=512, command="search")
        self.assertTrue(oversized)
        replacement = json.loads(output)
        self.assertEqual("response_too_large", replacement["status"])
        self.assertNotIn("x" * 100, output)
        self.assertLessEqual(len(output.encode("utf-8")), skill_runner.MAX_ERROR_PACKET_BYTES)

    def test_invalid_arguments_never_accept_result_id_or_unbounded_ids(self) -> None:
        self._parser_error(["detail", "--result-set-id", "00000000-0000-0000-0000-000000000001", "--item-id", "E1"])
        self._parser_error(["detail", "--result-token", "lrt_" + "A" * 32, "--item-id", "../E1"])
        self._parser_error(["detail", "--result-token", "lrt_" + "A" * 32, "--item-id", "E1", "--item-id", "E1"])
        self._parser_error(["search", "--db", "../project-rag", "--question", "question"])
        self._parser_error(["search", "--db", "project-rag", "--question", "bad\x00text"])

    def test_strict_utf8_json_and_capture_limit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runner-child-") as temporary:
            helper = Path(temporary) / "helper.py"
            cases = (
                ("import sys;sys.stdout.buffer.write(b'\\xff')", "invalid_command_utf8"),
                ("print('[]')", "invalid_test_output"),
                (f"import sys,time;sys.stdout.buffer.write(b'x'*{skill_runner.MAX_COMMAND_OUTPUT_BYTES + 1});sys.stdout.flush();time.sleep(2)", "public_command_output_too_large"),
            )
            for source, expected in cases:
                with self.subTest(expected=expected):
                    helper.write_text(source, encoding="utf-8")
                    with mock.patch.object(skill_runner, "_validate_fixed_command"):
                        with self.assertRaises(skill_runner.RunnerError) as raised:
                            skill_runner._run_public(
                                [sys.executable, "-I", "-B", str(helper)],
                                input_bytes=None, timeout=5, invalid_code="invalid_test_output",
                            )
                    self.assertEqual(expected, raised.exception.code)

    def test_windows_cp932_stdout_pipe_emits_one_utf8_packet(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runner-stdout-") as temporary:
            helper = Path(temporary) / "helper.py"
            marker = "—😀𠮷"
            helper.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(QUERY_ROOT)!r})\n"
                "import skill_runner\n"
                "sys.stdout.reconfigure(encoding='cp932', errors='strict')\n"
                f"marker = {marker!r}\n"
                "skill_runner._run_list = lambda: {"
                "'schema_version':'local-rag-catalog-v1','status':'ok',"
                "'payload_complete':True,'databases':[{'name':'probe-rag',"
                "'title':marker,'query_hint':marker,'content_summary':marker,'sources':[]}]}\n"
                "raise SystemExit(skill_runner.main(['list']))\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", "-B", str(helper)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(b"", completed.stderr)
        decoded = completed.stdout.decode("utf-8", errors="strict")
        self.assertEqual(1, len(decoded.splitlines()))
        packet = json.loads(decoded)
        self.assertEqual(marker, packet["databases"][0]["title"])
        self.assertEqual(marker, packet["databases"][0]["query_hint"])
        self.assertEqual(marker, packet["databases"][0]["content_summary"])

    def test_timeout_kills_the_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runner-tree-") as temporary:
            root = Path(temporary)
            helper, pid_file = root / "parent.py", root / "child.pid"
            helper.write_text(
                "import pathlib,subprocess,sys,time\n"
                "c=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'])\n"
                "pathlib.Path(sys.argv[1]).write_text(str(c.pid),encoding='ascii')\n"
                "print('{}',flush=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            with mock.patch.object(skill_runner, "_validate_fixed_command"):
                with self.assertRaises(skill_runner.RunnerError) as raised:
                    skill_runner._run_public(
                        [sys.executable, "-I", "-B", str(helper), str(pid_file)],
                        input_bytes=None, timeout=0.5, invalid_code="invalid_test_output",
                    )
            self.assertEqual("public_command_timeout", raised.exception.code)
            child_pid = int(pid_file.read_text(encoding="ascii"))
            deadline = time.monotonic() + 5
            while _process_running(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_process_running(child_pid), f"orphan process remains: {child_pid}")


def _process_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
