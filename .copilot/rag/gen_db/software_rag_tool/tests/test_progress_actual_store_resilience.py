from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = TEST_ROOT.parent
RAG_ROOT = TOOL_ROOT.parents[1]
for directory in (TEST_ROOT, TOOL_ROOT, RAG_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_atomic_io_windows_retry import WINDOWS_READER, windows_error
import test_db_write_integrity as integrity
from software_rag_tool import incremental, manifest, progress
from software_rag_tool.embeddings import DocumentTokenBudget
from software_rag_tool.writer_runtime import database_writer_session


BOUNDARIES = {
    "delete": "delete_ids",
    "upsert": "upsert_records",
    "catalog": "upsert_catalog_records",
    "clean": "write_jsonl",
    "state": "_save_state",
}


@contextmanager
def boundary_fault(stage: str, *, authoritative: bool):
    control = {"armed": False, "calls": 0}
    original = getattr(incremental, BOUNDARIES[stage])
    original_snapshot = progress.atomic_write_json
    original_open = Path.open
    primary = windows_error(112)

    def snapshot(*args, **kwargs):
        if control["armed"]:
            raise windows_error(5)
        return original_snapshot(*args, **kwargs)

    def open_file(path, *args, **kwargs):
        if control["armed"] and path.name == "events.jsonl" and args and args[0] == "a":
            raise OSError("injected optional event append-open failure")
        return original_open(path, *args, **kwargs)

    def boundary(*args, **kwargs):
        result = original(*args, **kwargs)
        control["calls"] += 1
        # Initial state precedes ingestion. Select its next checkpoint instead.
        selected = stage != "state" or control["calls"] == 2
        if selected and not control["armed"]:
            control["armed"] = True
            if authoritative:
                raise primary
            # Exercise real optional persistence code exactly at this boundary.
            progress.write_progress(phase="fixture_boundary")
            progress.emit_event("fixture_boundary")
        return result

    with (
        mock.patch.object(incremental, BOUNDARIES[stage], side_effect=boundary),
        mock.patch.object(progress, "atomic_write_json", side_effect=snapshot),
        mock.patch.object(Path, "open", new=open_file),
    ):
        yield control, primary


class ActualStoreProgressResilienceTests(unittest.TestCase):
    setUp = integrity.IncrementalIntegrityTests.setUp
    tearDown = integrity.IncrementalIntegrityTests.tearDown
    _install_actual_runtime = integrity.IncrementalIntegrityTests._install_actual_runtime
    _close_actual_clients = staticmethod(integrity.IncrementalIntegrityTests._close_actual_clients)
    _actual_id_sets = integrity.IncrementalIntegrityTests._actual_id_sets
    _run_incremental = integrity.IncrementalIntegrityTests._run_incremental

    def _make_db(self, name, collection):
        target = integrity.IncrementalIntegrityTests._make_db(self, name, collection)
        with (
            mock.patch.dict(os.environ, {"LOCAL_RAG_LEXICAL_TOKENIZER": "fallback"}),
            database_writer_session(self.dbs_root, name),
        ):
            manifest.write_manifest(0)
        return target

    @contextmanager
    def actual_runtime(self):
        clients = {}
        try:
            with ExitStack() as stack:
                clients = self._install_actual_runtime(stack)
                stack.enter_context(redirect_stdout(io.StringIO()))
                yield
        finally:
            self._close_actual_clients(clients)

    def assert_converged(self, target):
        id_sets = self._actual_id_sets(target)
        self.assertTrue(id_sets[0])
        self.assertTrue(all(values == id_sets[0] for values in id_sets[1:]))
        self.assertEqual([], list(target.rglob(".*.tmp")))

    def change_input(self, label):
        (self.input_root / "document.txt").write_text("synthetic fixture " + label, encoding="utf-8")

    def child(self, mode, stage="none", *, powershell=False):
        arguments = [
            sys.executable, str(Path(__file__).resolve()), "--ingest-child",
            mode, str(self.dbs_root), str(self.input_root), stage,
        ]
        if powershell:
            quoted = " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
            arguments = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$PSVersionTable.PSVersion.ToString(); & " + quoted + "; exit $LASTEXITCODE",
            ]
        return subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)

    @contextmanager
    def held_reader(self, path, *, block_append=False, seconds=3.0):
        # FILE_SHARE_READ alone denies append; READ|WRITE denies only replace.
        code = WINDOWS_READER.replace("0x1 | 0x2", "0x1") if block_append else WINDOWS_READER
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(path), str(seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
            yield
        finally:
            try:
                _, stderr = child.communicate(timeout=seconds + 2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                self.fail("fixture reader failed to close")
            self.assertEqual(0, child.returncode, stderr)

    def test_optional_failures_at_five_store_boundaries_still_converge(self):
        target = self._make_db("target-rag", "target_custom")
        with self.actual_runtime():
            self._run_incremental()
            for stage in BOUNDARIES:
                with self.subTest(stage=stage):
                    self.change_input(stage)
                    warnings = io.StringIO()
                    with boundary_fault(stage, authoritative=False) as (control, _), redirect_stderr(warnings):
                        result = self._run_incremental()
                    self.assertTrue(control["armed"])
                    self.assertEqual("success", result["result_status"])
                    self.assertTrue(result["observability_degraded"])
                    self.assertEqual(["events", "progress"], result["observability_failed_sinks"])
                    self.assertEqual(2, warnings.getvalue().count("observability_degraded"))
                    self.assertNotIn(str(self.root), warnings.getvalue())
                    self.assert_converged(target)

    def test_authoritative_failures_exit_nonzero_preserve_primary_and_resume_converges(self):
        target = self._make_db("target-rag", "target_custom")
        with self.actual_runtime():
            self._run_incremental()
        for stage in BOUNDARIES:
            with self.subTest(stage=stage):
                self.change_input(stage)
                failed = self.child("fail", stage)
                self.assertEqual(1, failed.returncode, "authoritative child must fail")
                failure = json.loads(failed.stdout.strip())
                self.assertTrue(failure["primary_preserved"])
                self.assertEqual(112, failure["winerror"])
                self.assertIn("observability_degraded", failed.stderr)
                self.assertNotIn(str(self.root), failed.stderr)
                recovered = self.child("resume")
                self.assertEqual(0, recovered.returncode, recovered.stderr)
                self.assertEqual("success", json.loads(recovered.stdout.strip())["result_status"])
                with self.actual_runtime():
                    self.assert_converged(target)

    @unittest.skipUnless(os.name == "nt", "requires Windows file sharing")
    def test_sustained_real_progress_and_event_handles_degrade_but_body_completes(self):
        target = self._make_db("target-rag", "target_custom")
        with self.actual_runtime():
            self._run_incremental()
            for sink, filename in (("progress", "progress.json"), ("events", "events.jsonl")):
                with self.subTest(sink=sink):
                    path = target / "logs" / filename
                    previous = path.read_bytes()
                    self.change_input(sink)
                    warnings = io.StringIO()
                    with self.held_reader(path, block_append=sink == "events"), redirect_stderr(warnings):
                        started = time.monotonic()
                        result = self._run_incremental()
                        elapsed = time.monotonic() - started
                    # CPython append-open can surface errno-only PermissionError
                    # (no winerror); that is immediately degraded, not retried.
                    if sink == "progress":
                        self.assertGreaterEqual(elapsed, 1.8)
                    self.assertEqual("success", result["result_status"])
                    self.assertEqual([sink], result["observability_failed_sinks"])
                    self.assertEqual(previous, path.read_bytes())
                    self.assertEqual(1, warnings.getvalue().count("observability_degraded"))
                    self.assert_converged(target)

    @unittest.skipUnless(os.name == "nt", "requires Windows file sharing")
    def test_real_held_canonical_state_is_fatal_old_intact_then_cli_resume_recovers(self):
        target = self._make_db("target-rag", "target_custom")
        with self.actual_runtime():
            self._run_incremental()
        state = target / "logs/index_state.json"
        previous = state.read_bytes()
        self.change_input("canonical-state")
        with self.held_reader(state, seconds=6.0):
            failed = self.child("run")
            self.assertEqual(1, failed.returncode)
            self.assertIn(json.loads(failed.stdout.strip())["winerror"], {5, 32, 33})
            self.assertEqual(previous, state.read_bytes())
            self.assertEqual([], list(target.rglob(".*.tmp")))
        recovered = self.child("resume")
        self.assertEqual(0, recovered.returncode, recovered.stderr)
        with self.actual_runtime():
            self.assert_converged(target)

    @unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
    def test_powershell_51_portable_python_update_resume_and_status_smoke(self):
        target = self._make_db("target-rag", "target_custom")
        for operation in ("run", "resume"):
            result = self.child(operation, powershell=True)
            self.assertEqual(0, result.returncode, result.stderr)
            lines = result.stdout.strip().splitlines()
            self.assertTrue(lines[0].startswith("5.1."))
            self.assertEqual("success", json.loads(lines[-1])["result_status"])
        status = self.child("status", powershell=True)
        self.assertEqual(0, status.returncode, status.stderr)
        summary = json.loads(status.stdout.strip().splitlines()[-1])
        self.assertEqual("success", summary["status"])
        self.assertEqual(1, summary["collection_count"])
        with self.actual_runtime():
            self.assert_converged(target)

    def test_registered_filesystem_source_updates_real_add_and_commits_source_state(self):
        from source_manager import runner as source_runner
        from source_manager.store import SourceStore

        target = self._make_db("target-rag", "target_custom")
        registered = source_runner.register_source(
            target,
            source_type="other",
            display_name="Synthetic filesystem fixture",
            fetch={"one_shot": True},
            runtime_input=self.input_root,
        )
        source_key = registered["local_source_key"]
        completed_processes = []

        def command_runner(arguments):
            self.assertEqual(Path(arguments[1]), RAG_ROOT / "gen_db/add_data.py")
            # Forward the real Source runner's CLI arguments unchanged; only
            # bootstrap its portable-Python fixture embedder/tokenizer.
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--source-add-child", str(self.dbs_root), *arguments[2:]],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
            )
            completed_processes.append(completed)
            return completed

        with mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs_root)}):
            result = source_runner.update_source(
                target,
                source_key,
                runtime_input=self.input_root,
                python_executable=Path(sys.executable),
                rag_root=RAG_ROOT,
                command_runner=command_runner,
            )
        self.assertEqual("updated", result["status"])
        self.assertTrue(result["observability_degraded"])
        self.assertEqual(["progress"], result["observability_failed_sinks"])
        self.assertEqual(1, len(completed_processes))
        self.assertEqual(0, completed_processes[0].returncode)
        self.assertEqual(1, completed_processes[0].stderr.count("observability_degraded"))
        source_store = SourceStore(target)
        committed = source_store.read_state(source_key).payload
        self.assertEqual("complete", committed["status"])
        self.assertEqual("complete", committed["phase"])
        self.assertEqual(1, committed["indexed_confirmed_count"])
        self.assertEqual(0, committed["pending_count"])
        self.assertFalse(committed["can_resume"])
        self.assertEqual(source_key, source_store.read_source(source_key).payload["source_id"])
        self.assertFalse(result["metadata_sync_pending"])
        with self.actual_runtime():
            self.assert_converged(target)

    def test_native_process_kill_after_vector_upsert_then_resume_converges(self):
        target = self._make_db("target-rag", "target_custom")
        with self.actual_runtime():
            self._run_incremental()
        self.change_input("native-child-termination")
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--ingest-child", "kill", str(self.dbs_root), str(self.input_root), "upsert"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            self.assertEqual("READY", child.stdout.readline().strip())
            self.assertIsNone(child.poll())
            child.terminate()  # This PID is exclusively the synthetic writer.
            child.communicate(timeout=10)
            self.assertNotEqual(0, child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
        with self.actual_runtime():
            interrupted = self._actual_id_sets(target)
        self.assertNotEqual(interrupted[0], interrupted[1])
        recovered = self.child("resume")
        self.assertEqual(0, recovered.returncode, recovered.stderr)
        self.assertEqual("success", json.loads(recovered.stdout.strip())["result_status"])
        with self.actual_runtime():
            self.assert_converged(target)


def ingestion_child(arguments):
    mode, dbs_root, input_root, stage = arguments
    fixture = ActualStoreProgressResilienceTests()
    fixture.token_budget = DocumentTokenBudget(
        tokenizer=integrity.CharacterTokenizer(), target_tokens=320, max_tokens=384,
        tokenizer_name="character-integrity-test", document_prefix="document: ",
    )
    entrypoint = "status.py" if mode == "status" else "add_data.py"
    module = integrity.WriterEntrypointTests._load("actual_progress_child", RAG_ROOT / "gen_db" / entrypoint)
    argv = [entrypoint, "--db", "target-rag", "--root", input_root, "--source-id", "src-a", "--batch-size-files", "1"]
    if mode == "status":
        argv = [entrypoint, "--db", "target-rag", "--json"]
    if mode == "resume":
        argv.append("--resume")
    output = io.StringIO()
    clients = {}
    primary = None
    try:
        with ExitStack() as stack:
            clients = fixture._install_actual_runtime(stack)
            stack.enter_context(mock.patch.dict(os.environ, {"RAG_DBS_ROOT": dbs_root}))
            stack.enter_context(mock.patch.object(module, "load_env"))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            if mode == "fail":
                _, primary = stack.enter_context(boundary_fault(stage, authoritative=True))
            if mode == "kill":
                original_upsert = incremental.upsert_records

                def wait_after_upsert(*args, **kwargs):
                    result = original_upsert(*args, **kwargs)
                    print("READY", file=sys.__stdout__, flush=True)
                    sys.stdin.buffer.read(1)
                    return result

                stack.enter_context(mock.patch.object(incremental, "upsert_records", side_effect=wait_after_upsert))
            with redirect_stdout(output):
                code = module.main()
            # Keep process output restricted to the authoritative result summary.
            parsed = json.loads(output.getvalue()[output.getvalue().find("{"):])
            if mode == "status":
                print(json.dumps({"status": parsed["status"], "collection_count": parsed["collection_count"]}))
            else:
                print(json.dumps({"result_status": parsed["result_status"]}))
            return code
    except Exception as exc:
        print(json.dumps({"primary_preserved": exc is primary, "error_type": type(exc).__name__, "winerror": getattr(exc, "winerror", None)}))
        return 1
    finally:
        fixture._close_actual_clients(clients)


def source_add_child(arguments):
    dbs_root, *forwarded = arguments
    fixture = ActualStoreProgressResilienceTests()
    fixture.token_budget = DocumentTokenBudget(
        tokenizer=integrity.CharacterTokenizer(), target_tokens=320, max_tokens=384,
        tokenizer_name="character-integrity-test", document_prefix="document: ",
    )
    module = integrity.WriterEntrypointTests._load("source_progress_child", RAG_ROOT / "gen_db/add_data.py")
    clients = {}
    try:
        with ExitStack() as stack:
            clients = fixture._install_actual_runtime(stack)
            stack.enter_context(mock.patch.dict(os.environ, {"RAG_DBS_ROOT": dbs_root}))
            stack.enter_context(mock.patch.object(module, "load_env"))
            stack.enter_context(mock.patch.object(sys, "argv", ["add_data.py", *forwarded]))
            stack.enter_context(mock.patch.object(progress, "atomic_write_json", side_effect=windows_error(5)))
            return module.main()
    finally:
        fixture._close_actual_clients(clients)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--source-add-child":
        raise SystemExit(source_add_child(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--ingest-child":
        raise SystemExit(ingestion_child(sys.argv[2:]))
    unittest.main()
