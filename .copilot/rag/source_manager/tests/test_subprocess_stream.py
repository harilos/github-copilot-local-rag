from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from source_manager.subprocess_stream import (
    CAPTURE_HEAD_BYTES,
    CAPTURE_TAIL_BYTES,
    PROGRESS_FRAME,
    RESULT_FRAME,
    ResultExtractionError,
    extract_json_result,
    run_streaming_process,
)
from source_manager.runner import _execute_add


class StreamingProcessTests(unittest.TestCase):
    def test_drains_both_pipes_and_bounds_verbose_diagnostics(self) -> None:
        script = (
            "import os,sys,threading\n"
            "def write(fd, byte):\n"
            "  [os.write(fd, byte * 8192) for _ in range(40)]\n"
            "a=threading.Thread(target=write,args=(1,b'o'))\n"
            "b=threading.Thread(target=write,args=(2,b'e'))\n"
            "a.start(); b.start(); a.join(); b.join()\n"
            f"print('\\n{RESULT_FRAME}' + "
            "'''{\"status\":\"ok\",\"source_id\":\"src_a\"}''')\n"
        )
        result = run_streaming_process(
            [sys.executable, "-c", script],
            heartbeat_interval=0,
        )
        self.assertEqual(0, result.returncode)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        maximum = (
            CAPTURE_HEAD_BYTES
            + CAPTURE_TAIL_BYTES
            + len("\n...[bounded diagnostic omitted]...\n")
        )
        self.assertLessEqual(len(result.stdout.encode("utf-8")), maximum)
        self.assertLessEqual(len(result.stderr.encode("utf-8")), maximum)
        self.assertEqual(1, len(result.result_frames))
        self.assertEqual(
            "src_a",
            extract_json_result(result)["source_id"],
        )

    def test_result_frame_survives_when_middle_stdout_is_omitted(self) -> None:
        script = (
            "import sys\n"
            "sys.stdout.write('a' * 200000 + '\\n')\n"
            f"print('{RESULT_FRAME}' + "
            "'''{\"status\":\"ok\",\"source_id\":\"middle\"}''')\n"
            "sys.stdout.write('z' * 200000 + '\\n')\n"
        )
        result = run_streaming_process(
            [sys.executable, "-c", script],
            heartbeat_interval=0,
        )
        self.assertTrue(result.stdout_truncated)
        self.assertNotIn(RESULT_FRAME, result.stdout)
        self.assertEqual(
            "middle",
            extract_json_result(result)["source_id"],
        )

    def test_cr_progress_and_callback_failure_do_not_fail_process(self) -> None:
        observed: list[dict] = []

        def callback(event):
            observed.append(dict(event))
            raise RuntimeError("UI failure")

        script = (
            "import sys\n"
            f"sys.stderr.write('{PROGRESS_FRAME}' + "
            "'''{\"current\":2,\"total\":3}''' + '\\r')\n"
            "sys.stderr.flush()\n"
            "print('{\"status\":\"ok\"}')\n"
        )
        result = run_streaming_process(
            [sys.executable, "-c", script],
            progress_callback=callback,
            heartbeat_interval=0,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual(1, len(observed))
        self.assertEqual("progress", observed[0]["event"])
        self.assertEqual(2, observed[0]["payload"]["current"])

    def test_heartbeat_is_observational(self) -> None:
        observed: list[dict] = []
        result = run_streaming_process(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(.09); print('{\"status\":\"ok\"}')",
            ],
            progress_callback=lambda event: observed.append(dict(event)),
            heartbeat_interval=0.02,
        )
        self.assertEqual(0, result.returncode)
        self.assertTrue(
            any(item.get("event") == "heartbeat" for item in observed)
        )

    def test_timeout_keeps_bounded_diagnostics(self) -> None:
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as captured:
            run_streaming_process(
                [
                    sys.executable,
                    "-c",
                    "import sys,time; print('started'); "
                    "sys.stdout.flush(); time.sleep(5)",
                ],
                timeout=0.05,
                heartbeat_interval=0,
            )
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("started", str(captured.exception.output))
        diagnostic = captured.exception.process_diagnostic
        self.assertEqual(
            [sys.executable, "-c"],
            diagnostic["command"][:2],
        )
        self.assertTrue(diagnostic["cwd"])
        self.assertIsNotNone(diagnostic["returncode"])
        self.assertGreaterEqual(diagnostic["elapsed_seconds"], 0.05)
        self.assertIn("started", diagnostic["stdout"]["text"])


class ResultExtractionTests(unittest.TestCase):
    @staticmethod
    def _validator(value):
        if value.get("status") != "ok" or not value.get("source_id"):
            raise ValueError("status/source_id contract")

    def test_prefers_framed_result_over_other_output(self) -> None:
        completed = SimpleNamespace(
            stdout=(
                '{"log":"ordinary JSON"}\n'
                f'{RESULT_FRAME}{{"status":"ok","source_id":"src_a"}}\n'
            ),
            result_frames=(
                '{"status":"ok","source_id":"src_a"}',
            ),
        )
        self.assertEqual(
            "src_a",
            extract_json_result(
                completed,
                validator=self._validator,
            )["source_id"],
        )

    def test_whole_document_preserves_existing_cli_contract(self) -> None:
        completed = SimpleNamespace(
            stdout=json.dumps({"status": "ok", "source_id": "legacy"})
        )
        self.assertEqual(
            "legacy",
            extract_json_result(
                completed,
                validator=self._validator,
            )["source_id"],
        )

    def test_raw_decode_accepts_one_valid_candidate_among_logs(self) -> None:
        completed = SimpleNamespace(
            stdout=(
                'log {"status":"ignored"}\r'
                'result {"status":"ok","source_id":"src_b"} finished'
            )
        )
        self.assertEqual(
            "src_b",
            extract_json_result(
                completed,
                validator=self._validator,
            )["source_id"],
        )

    def test_multiple_valid_candidates_are_rejected_as_ambiguous(self) -> None:
        completed = SimpleNamespace(
            stdout=(
                '{"status":"ok","source_id":"one"}\n'
                '{"status":"ok","source_id":"two"}'
            )
        )
        with self.assertRaises(ResultExtractionError) as captured:
            extract_json_result(
                completed,
                validator=self._validator,
            )
        self.assertIn("multiple", str(captured.exception))
        self.assertTrue(
            any(
                item.get("error") == "ambiguous_results"
                for item in captured.exception.diagnostics
            )
        )

    def test_zero_valid_candidates_has_decode_and_schema_diagnostics(self) -> None:
        completed = SimpleNamespace(
            stdout='prefix {"status": nope}\n{"status":"error"}'
        )
        with self.assertRaises(ResultExtractionError) as captured:
            extract_json_result(
                completed,
                validator=self._validator,
            )
        errors = {
            item.get("error")
            for item in captured.exception.diagnostics
        }
        self.assertIn("json_decode_error", errors)
        self.assertIn("schema_rejected", errors)
        self.assertIn("no_valid_result", errors)
        rendered = str(captured.exception.diagnostics)
        self.assertIn("excerpt", rendered)
        self.assertIn("caret", rendered)

    def test_multiple_framed_results_are_rejected(self) -> None:
        completed = SimpleNamespace(
            stdout="",
            result_frames=(
                '{"status":"ok","source_id":"one"}',
                '{"status":"ok","source_id":"two"}',
            ),
        )
        with self.assertRaisesRegex(
            ResultExtractionError,
            "multiple schema-valid framed",
        ):
            extract_json_result(
                completed,
                validator=self._validator,
            )


class AddIntegrationTests(unittest.TestCase):
    def test_real_add_cli_has_manager_only_frame_protocol(self) -> None:
        rag_root = Path(__file__).resolve().parents[2]
        cli = (rag_root / "gen_db" / "add_data.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("--manager-protocol-v1", cli)
        self.assertIn("@@LOCAL_RAG_PROGRESS_V1@@", cli)
        self.assertIn("@@LOCAL_RAG_RESULT_V1@@", cli)

    def test_default_add_route_uses_frames_and_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rag_root = root / "rag"
            script = rag_root / "gen_db" / "add_data.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "import json,sys\n"
                "key=sys.argv[sys.argv.index('--source-id')+1]\n"
                f"sys.stderr.write('{PROGRESS_FRAME}' + "
                "json.dumps({'phase':'reflect','current':1}) + '\\r')\n"
                "sys.stderr.flush()\n"
                "print('ordinary human output')\n"
                f"print('{RESULT_FRAME}' + "
                "json.dumps({'operation':'add','source_id':key,"
                "'file_count':1,'indexed_files':1,'skipped_files':0,"
                "'error_files':0,'upserted_records':1,"
                "'deleted_records':0}))\n",
                encoding="utf-8",
            )
            work = root / "db-rag" / "sources" / "src_key" / "work"
            work.mkdir(parents=True)
            observed: list[dict] = []
            result = _execute_add(
                db_root=root / "db-rag",
                source={"local_source_key": "src_key"},
                work=work,
                python_executable=Path(sys.executable),
                rag_root=rag_root,
                command_runner=None,
                progress_callback=lambda event: observed.append(
                    dict(event)
                ),
            )
        self.assertEqual("src_key", result["source_id"])
        self.assertEqual("add", result["summary"]["operation"])
        self.assertEqual(1, len(observed))
        self.assertEqual("reflect", observed[0]["payload"]["phase"])

    def test_add_boundary_rejects_build_shaped_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_id = "src_key"
            completed = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "operation": "build",
                        "source_id": source_id,
                        "file_count": 1,
                        "indexed_files": 1,
                        "skipped_files": 0,
                        "error_files": 0,
                        "upserted_records": 1,
                        "deleted_records": 0,
                    }
                ),
                stderr="",
            )
            with self.assertRaisesRegex(
                ValueError,
                "trusted JSON result",
            ):
                _execute_add(
                    db_root=root / "fixture-rag",
                    source={"local_source_key": source_id},
                    work=root / "work",
                    python_executable=Path(sys.executable),
                    rag_root=root / "rag",
                    command_runner=lambda _arguments: completed,
                    progress_callback=None,
                )


if __name__ == "__main__":
    unittest.main()
