from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from unittest import mock

TOOL_ROOT = Path(__file__).resolve().parents[1]
RAG_ROOT = TOOL_ROOT.parents[1]
sys.path[:0] = [str(TOOL_ROOT / "tests"), str(TOOL_ROOT), str(RAG_ROOT)]
from software_rag_tool import progress, source_inventory
from software_rag_tool.atomic_io import atomic_write_json


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


add_data = load_script("observation_add", RAG_ROOT / "gen_db" / "add_data.py")
status_cli = load_script("observation_status", RAG_ROOT / "gen_db" / "status.py")


class OptionalSinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_disabled_progress_warns_once_and_snapshot_still_advances(self):
        calls = []

        @progress.observability_run
        def run():
            for index in range(20):
                calls.append(progress.write_progress(files_done=index))
            return {"result_status": "success"}

        warnings = io.StringIO()
        with mock.patch.object(progress, "atomic_write_json", side_effect=OSError("private path")) as write, redirect_stderr(warnings):
            result = run()
        self.assertEqual(write.call_count, 1)
        self.assertEqual(calls[-1]["files_done"], 19)
        self.assertEqual(result["result_status"], "success")
        self.assertTrue(result["observability_degraded"])
        self.assertEqual(result["observability_failed_sinks"], ["progress"])
        self.assertEqual(warnings.getvalue().count("WARNING"), 1)
        self.assertNotIn("private path", warnings.getvalue())
        self.assertIsNone(progress._RUN.get())

    def test_each_run_retries_sink_and_state_never_leaks(self):
        @progress.observability_run
        def run():
            progress.write_progress(status="completed")
            return {}

        with mock.patch.object(progress, "atomic_write_json", side_effect=OSError("injected")) as write, redirect_stderr(io.StringIO()):
            run()
            run()
        self.assertEqual(write.call_count, 2)
        self.assertNotIn("observability_degraded", run())

    def test_events_disable_independently_and_only_once(self):
        class FailedOpen:
            parent = mock.Mock()
            open = mock.Mock(side_effect=OSError("private path"))

        @progress.observability_run
        def run():
            for index in range(20):
                progress.emit_event("test", index=index)
                progress.write_progress(files_done=index)
            return {}

        warnings = io.StringIO()
        with mock.patch.object(progress, "events_path", return_value=FailedOpen), redirect_stderr(warnings):
            result = run()
        self.assertEqual(FailedOpen.open.call_count, 1)
        self.assertEqual(result["observability_failed_sinks"], ["events"])
        self.assertEqual(progress.read_progress()["files_done"], 19)
        self.assertEqual(warnings.getvalue().count("WARNING"), 1)
        self.assertNotIn("private path", warnings.getvalue())

    def test_unscoped_persistence_failures_remain_strict(self):
        with mock.patch.object(progress, "atomic_write_json", side_effect=OSError("canonical")):
            with self.assertRaises(OSError):
                progress.write_progress(status="completed")
        with mock.patch.object(progress, "events_path", side_effect=OSError("canonical")):
            with self.assertRaises(OSError):
                progress.emit_event("test")

    def test_invalid_payload_is_not_hidden_even_after_disabling(self):
        for sink in ("progress", "events"):
            @progress.observability_run
            def run():
                if sink == "progress":
                    progress.write_progress(status="running")
                    progress.write_progress(invalid=object())
                else:
                    progress.emit_event("start")
                    progress.emit_event("invalid", invalid=object())
                return {}

            with mock.patch.object(progress, "atomic_write_json", side_effect=OSError()), mock.patch.object(progress, "events_path", side_effect=OSError()), redirect_stderr(io.StringIO()):
                with self.subTest(sink=sink), self.assertRaises(TypeError):
                    run()
            self.assertIsNone(progress._RUN.get())

    def test_programming_error_from_writer_is_not_hidden(self):
        @progress.observability_run
        def run():
            progress.write_progress(status="running")
            return {}

        with mock.patch.object(progress, "atomic_write_json", side_effect=TypeError("bug")):
            with self.assertRaisesRegex(TypeError, "bug"):
                run()

    def test_failed_progress_cannot_replace_original_body_exception(self):
        original = RuntimeError("original body failure")

        @progress.observability_run
        def run():
            try:
                raise original
            except Exception:
                progress.write_progress(status="failed")
                progress.emit_event("failed")
                raise

        with mock.patch.object(progress, "atomic_write_json", side_effect=OSError()), mock.patch.object(progress, "events_path", side_effect=OSError()), redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                run()
        self.assertIs(raised.exception, original)
        self.assertIsNone(progress._RUN.get())

    def test_read_failure_disables_progress_before_repeated_attempts(self):
        @progress.observability_run
        def run():
            progress.write_progress(files_done=1)
            progress.write_progress(files_done=2)
            return {}

        with mock.patch.object(progress, "read_progress", side_effect=OSError()) as read, mock.patch.object(progress, "atomic_write_json") as write, redirect_stderr(io.StringIO()):
            result = run()
        self.assertEqual(read.call_count, 1)
        write.assert_not_called()
        self.assertEqual(result["observability_failed_sinks"], ["progress"])

    def test_successful_events_are_single_utf8_jsonl_records(self):
        @progress.observability_run
        def run():
            progress.emit_event("日本語—😀", line="one\ntwo")
            return {}

        self.assertEqual(run(), {})
        lines = progress.events_path().read_bytes().decode("utf-8", errors="strict").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["event"], "日本語—😀")


class SnapshotWatcherTests(unittest.TestCase):
    def test_watcher_opens_no_files_and_owns_detached_snapshot(self):
        watcher = add_data._AddProgressWatcher(enabled=True)
        snapshot = {"phase": "extract", "files_total": 2, "current_batch_files": ["first"]}
        output = io.StringIO()
        with mock.patch.object(Path, "open", side_effect=AssertionError("watcher opened file")), mock.patch.object(Path, "stat", side_effect=AssertionError("watcher inspected file")), redirect_stderr(output):
            watcher.offer(snapshot)
            snapshot["current_batch_files"].append("second")
            watcher.start()
            watcher.stop()
        self.assertEqual(watcher._read_snapshot()["current_batch_files"], ["first"])
        self.assertIn(add_data._PROGRESS_FRAME, output.getvalue())

    def test_patch_restored_on_success_and_exception(self):
        watcher = add_data._AddProgressWatcher(enabled=True)
        for raises in (False, True):
            with mock.patch.object(add_data.incremental_module, "write_progress", side_effect=lambda **kw: kw) as original:
                try:
                    with add_data._install_exact_file_index_progress(watcher):
                        add_data.incremental_module.write_progress(phase="extract", current_file="one")
                        self.assertEqual(watcher._read_snapshot()["current_file_index"], 1)
                        if raises:
                            raise RuntimeError("body")
                except RuntimeError:
                    pass
                self.assertIs(add_data.incremental_module.write_progress, original)

    def test_protocol_disabled_creates_no_thread_or_snapshot(self):
        watcher = add_data._AddProgressWatcher(enabled=False)
        with mock.patch.object(threading, "Thread", side_effect=AssertionError("unexpected thread")):
            watcher.start()
            watcher.offer({"phase": "extract"})
            watcher.stop()
        self.assertIsNone(watcher._read_snapshot())

    def test_timer_heartbeat_updates_eta_without_new_snapshot(self):
        watcher = add_data._AddProgressWatcher(enabled=True)
        watcher.offer({"phase": "extract", "files_total": 10, "files_done": 2})
        frames = []
        ready = threading.Event()

        def capture(frame):
            frames.append(frame)
            if len(frames) >= 2:
                ready.set()

        with mock.patch.object(watcher, "_emit", side_effect=capture):
            watcher.start()
            try:
                self.assertTrue(ready.wait(2))
            finally:
                watcher.stop()
        self.assertGreater(frames[1]["eta_seconds"], frames[0]["eta_seconds"])

    def test_thousand_updates_with_real_status_and_inventory_readers(self):
        from test_progress_actual_store_resilience import ActualStoreProgressResilienceTests
        fixture = ActualStoreProgressResilienceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = fixture._make_db("target-rag", "target_custom")
        with ExitStack() as stack:
            stack.enter_context(fixture.actual_runtime())
            stack.enter_context(mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(root)}))
            fixture._run_incremental()
            fixture.change_input("stress-update")
            progress.write_progress(serial=-1)
            watcher = add_data._AddProgressWatcher(enabled=True)
            stop = threading.Event()
            failures = []
            reads = [0, 0]

            def reader(index):
                try:
                    while not stop.is_set():
                        if index == 0:
                            snapshot = status_cli._load_json(root / "logs" / "progress.json")
                        else:
                            inventory = source_inventory.build_source_inventory(root)
                            self.assertNotIn("progress_invalid", [item.code for item in inventory.diagnostics])
                            state = source_inventory._supplemental_state(root)
                            self.assertEqual(state["diagnostics"], [])
                            snapshot = state["progress"]
                        self.assertIsInstance(snapshot, dict)
                        self.assertGreaterEqual(snapshot["serial"], -1)
                        reads[index] += 1
                        time.sleep(0.0005)
                except BaseException as exc:
                    # Do not put synthetic absolute paths in test evidence.
                    failures.append((type(exc).__name__, getattr(exc, "errno", None), getattr(exc, "winerror", None)))

            @progress.observability_run
            def run():
                with add_data._install_exact_file_index_progress(watcher):
                    for index in range(1000):
                        add_data.incremental_module.write_progress(serial=index, phase="extract", files_done=index, files_total=1000, current_file="synthetic.txt")
                    return fixture._run_incremental()

            threads = [threading.Thread(target=reader, args=(index,)) for index in (0, 1)]
            output = io.StringIO()
            with redirect_stderr(output):
                watcher.start()
                for thread in threads:
                    thread.start()
                try:
                    result = run()
                finally:
                    stop.set()
                    for thread in threads:
                        thread.join(3)
                    watcher.stop()
            self.assertEqual(failures, [])
            self.assertTrue(all(count > 0 for count in reads))
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(result["result_status"], "success")
            self.assertNotIn("observability_degraded", result)
            fixture.assert_converged(root)
            self.assertEqual(progress.read_progress()["serial"], 999)
            self.assertEqual(list((root / "logs").glob("*.tmp")), [])
            frames = [json.loads(line[len(add_data._PROGRESS_FRAME):]) for line in output.getvalue().splitlines()]
            self.assertGreater(len(frames), 0)
            self.assertEqual(frames[-1]["completed"], 1)
            self.assertEqual(watcher._read_snapshot()["serial"], 999)
            self.assertNotIn("WARNING", output.getvalue())


if __name__ == "__main__":
    unittest.main()
