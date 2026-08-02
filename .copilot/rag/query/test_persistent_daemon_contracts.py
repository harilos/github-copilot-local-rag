from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(QUERY_ROOT))
from rag_worker import (  # noqa: E402
    _execute_search_payload,
    _final_dense_loaded,
    _wrapper_handoff_environment,
)
from rag_manager import PersistentWorkerManager  # noqa: E402
from ragd import _drain_worker_then_stop_listener  # noqa: E402
import search as search_module  # noqa: E402


class PersistentDaemonContracts(unittest.TestCase):
    def test_public_stop_reports_when_daemon_is_not_running(self) -> None:
        with mock.patch.object(search_module, "_read_state", return_value=None):
            result = search_module.stop_persistent_daemon()
        self.assertEqual("not_running", result["status"])
        self.assertTrue(result["stopped"])

    def test_public_stop_authenticates_and_waits_for_retirement(self) -> None:
        state = {
            "pid": 123,
            "generation": "generation",
            "token": "token",
            "code_fingerprint": "fingerprint",
        }
        identity = {
            "status": "ok",
            "pid": 123,
            "generation": "generation",
            "code_fingerprint": "fingerprint",
        }
        with (
            mock.patch.object(
                search_module,
                "_read_state",
                return_value=state,
            ),
            mock.patch.object(
                search_module,
                "_daemon_health_payload",
                return_value=identity,
            ),
            mock.patch.object(
                search_module,
                "_request_daemon_shutdown",
                return_value=True,
            ) as shutdown,
            mock.patch.object(
                search_module,
                "_wait_for_daemon_retirement",
                return_value=True,
            ) as wait,
            mock.patch.object(
                search_module,
                "_process_is_alive",
                return_value=False,
            ),
            mock.patch.object(
                search_module,
                "_discard_published_state",
            ),
        ):
            result = search_module.stop_persistent_daemon(
                timeout_seconds=3.0,
            )
        self.assertEqual("stopped", result["status"])
        self.assertTrue(result["stopped"])
        shutdown.assert_called_once()
        wait.assert_called_once()

    def test_public_stop_never_kills_an_unauthenticated_pid(self) -> None:
        state = {
            "pid": 123,
            "generation": "generation",
            "token": "token",
            "code_fingerprint": "fingerprint",
        }
        with (
            mock.patch.object(
                search_module,
                "_read_state",
                return_value=state,
            ),
            mock.patch.object(
                search_module,
                "_daemon_health_payload",
                return_value=None,
            ),
            mock.patch.object(
                search_module,
                "_process_is_alive",
                return_value=True,
            ),
            mock.patch.object(
                search_module,
                "_request_daemon_shutdown",
            ) as shutdown,
            mock.patch.object(
                search_module,
                "_terminate_daemon_process",
            ) as terminate,
        ):
            result = search_module.stop_persistent_daemon()
        self.assertEqual("unreachable", result["status"])
        self.assertFalse(result["stopped"])
        shutdown.assert_not_called()
        terminate.assert_not_called()

    def test_public_stop_reports_a_concurrent_replacement_generation(
        self,
    ) -> None:
        old_state = {
            "pid": 123,
            "generation": "old-generation",
            "token": "old-token",
            "code_fingerprint": "fingerprint",
        }
        new_state = {
            "pid": 456,
            "generation": "new-generation",
            "token": "new-token",
            "code_fingerprint": "fingerprint",
        }

        def identity(state: dict, *, timeout: float) -> dict:
            del timeout
            return {
                "status": "ok",
                "pid": state["pid"],
                "generation": state["generation"],
                "code_fingerprint": state["code_fingerprint"],
            }

        with (
            mock.patch.object(
                search_module,
                "_read_state",
                side_effect=[old_state, new_state],
            ),
            mock.patch.object(
                search_module,
                "_daemon_health_payload",
                side_effect=identity,
            ),
            mock.patch.object(
                search_module,
                "_request_daemon_shutdown",
                return_value=True,
            ),
            mock.patch.object(
                search_module,
                "_wait_for_daemon_retirement",
                return_value=True,
            ),
            mock.patch.object(
                search_module,
                "_process_is_alive",
                return_value=False,
            ),
        ):
            result = search_module.stop_persistent_daemon()

        self.assertEqual("restarted", result["status"])
        self.assertFalse(result["stopped"])
        self.assertEqual(456, result["pid"])

    def test_state_discard_rechecks_generation_under_start_lock(
        self,
    ) -> None:
        old_state = {
            "pid": 123,
            "generation": "old-generation",
            "token": "old-token",
        }
        new_state = {
            "pid": 456,
            "generation": "new-generation",
            "token": "new-token",
        }
        with tempfile.TemporaryDirectory(
            prefix="rag-daemon-discard-",
        ) as temporary:
            state_file = Path(temporary) / "ragd.json"
            state_file.write_text("replacement", encoding="utf-8")
            with (
                mock.patch.object(
                    search_module,
                    "STATE_FILE",
                    state_file,
                ),
                mock.patch.object(
                    search_module,
                    "_secure_runtime_directory",
                ),
                mock.patch.object(
                    search_module,
                    "_acquire_start_lock",
                    return_value=1234,
                ),
                mock.patch.object(
                    search_module,
                    "_release_start_lock",
                ) as release,
                mock.patch.object(
                    search_module,
                    "_read_state",
                    return_value=new_state,
                ),
            ):
                search_module._discard_published_state(old_state)

            self.assertTrue(state_file.exists())
            release.assert_called_once_with(1234)

    def test_private_wrapper_handoff_environment_is_request_scoped(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {"LOCAL_RAG_WRAPPER_INTERNAL": "previous"},
        ):
            with _wrapper_handoff_environment(True):
                self.assertEqual(
                    "1",
                    os.environ["LOCAL_RAG_WRAPPER_INTERNAL"],
                )
            self.assertEqual(
                "previous",
                os.environ["LOCAL_RAG_WRAPPER_INTERNAL"],
            )
        with mock.patch.dict(
            os.environ,
            {"LOCAL_RAG_WRAPPER_INTERNAL": "1"},
        ):
            with _wrapper_handoff_environment(False):
                self.assertNotIn(
                    "LOCAL_RAG_WRAPPER_INTERNAL",
                    os.environ,
                )
            self.assertEqual(
                "1",
                os.environ["LOCAL_RAG_WRAPPER_INTERNAL"],
            )

    def test_search_response_cannot_regress_completed_warmup(self) -> None:
        ready = threading.Event()
        self.assertFalse(_final_dense_loaded(False, ready))
        # Simulate warmup completing after the request-start readiness check
        # and immediately before response state is serialized.
        ready.set()
        self.assertTrue(_final_dense_loaded(False, ready))

    def test_warmup_event_is_published_before_ready_snapshot(self) -> None:
        source = (QUERY_ROOT / "rag_worker.py").read_text(encoding="utf-8")
        success_branch = source.split("        else:", 1)[1].split(
            "        finally:",
            1,
        )[0]
        self.assertLess(
            success_branch.index("dense_ready.set()"),
            success_branch.index('dense_warmup_state="ready"'),
        )

    def test_cold_worker_never_enters_dense_evidence_path(self) -> None:
        calls: list[str] = []

        def adaptive(**_kwargs):
            calls.append("adaptive")
            raise AssertionError("cold worker must not enter adaptive Dense")

        def standard(**kwargs):
            calls.append(str(kwargs["retrieval_mode"]))
            return {
                "status": "partial",
                "warnings": [],
                "evidence": [],
                "document_results": [],
            }

        result = _execute_search_payload(
            {
                "db": "ac-rag",
                "question": "general semantic question",
                "retrieval_mode": "hybrid",
                "adaptive_hybrid": True,
            },
            run_adaptive_search_payload=adaptive,
            run_search_payload=standard,
            deadline_monotonic=None,
            dense_runtime_ready=False,
        )
        self.assertEqual(["lexical"], calls)
        self.assertFalse(result["dense_used"])
        self.assertEqual(
            "background_dense_warmup_incomplete",
            result["dense_skipped_reason"],
        )
        self.assertEqual(
            {
                "attempted": False,
                "succeeded": False,
                "error": None,
                "skipped_reason": "background_dense_warmup_incomplete",
            },
            result["lane_health"]["dense"],
        )

    def test_cold_worker_preserves_catalog_lane_failure_health(self) -> None:
        def standard(**_kwargs):
            return {
                "status": "error",
                "warnings": ["lexical_search_unavailable:LexicalSearchError"],
                "evidence": [],
                "document_results": [],
                "lane_health": {
                    "lexical": {
                        "attempted": True,
                        "succeeded": False,
                        "error": "LexicalSearchError",
                    }
                },
            }

        result = _execute_search_payload(
            {
                "db": "ac-rag",
                "question": "general lexical question",
                "retrieval_mode": "hybrid",
            },
            run_adaptive_search_payload=lambda **_kwargs: {},
            run_search_payload=standard,
            deadline_monotonic=None,
            dense_runtime_ready=False,
        )
        self.assertEqual(
            "LexicalSearchError",
            result["lane_health"]["lexical"]["error"],
        )
        self.assertEqual(
            "background_dense_warmup_incomplete",
            result["lane_health"]["dense"]["skipped_reason"],
        )
        self.assertIn(
            "lexical_search_unavailable:LexicalSearchError",
            result["warnings"],
        )

    def test_client_and_manager_imports_are_native_runtime_free(self) -> None:
        script = f"""
import importlib.util, json, sys
from pathlib import Path
root = Path({str(QUERY_ROOT)!r})
sys.path.insert(0, str(root))
for name in ('search', 'ragd'):
    before = set(sys.modules)
    spec = importlib.util.spec_from_file_location(name + '_probe', root / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loaded = set(sys.modules) - before
    forbidden = [
        value for value in loaded
        if value.split('.')[0] in {{
            'chromadb', 'onnxruntime', 'transformers',
            'sudachipy', 'sentence_transformers'
        }}
    ]
    if forbidden or 'software_rag_tool.search_api' in loaded:
        raise SystemExit(json.dumps({{'name': name, 'forbidden': forbidden}}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_runtime_fingerprint_includes_manager_and_worker(self) -> None:
        search_source = (QUERY_ROOT / "search.py").read_text(
            encoding="utf-8"
        )
        daemon_source = (QUERY_ROOT / "ragd.py").read_text(
            encoding="utf-8"
        )
        for filename in ("rag_manager.py", "rag_worker.py"):
            self.assertIn(filename, search_source)
            self.assertIn(filename, daemon_source)

    def test_warmup_telemetry_uses_a_dedicated_one_way_pipe(self) -> None:
        worker_source = (QUERY_ROOT / "rag_worker.py").read_text(
            encoding="utf-8"
        )
        manager_source = (QUERY_ROOT / "rag_manager.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(1, worker_source.count("status_connection.send("))
        self.assertIn("context.Pipe(duplex=False)", manager_source)
        self.assertIn('"op": "worker_status"', worker_source)

    def test_health_stays_responsive_while_worker_is_reaped(self) -> None:
        manager = object.__new__(PersistentWorkerManager)
        manager._worker_lifecycle_lock = threading.RLock()
        manager._process = None
        manager._connection = None
        manager._worker_state = {}
        manager._worker_pid = 123
        manager._worker_generation = "worker-generation"
        manager._job = object()
        manager._last_worker_start_error = None
        manager._condition = threading.Condition()
        manager._active = None
        manager._pending_total = 0
        manager._closed = False
        manager._handled_request_count = 0
        manager.manager_generation = "manager-generation"
        manager.started_monotonic = time.monotonic()
        acquired = threading.Event()
        release = threading.Event()

        def hold_lifecycle_lock() -> None:
            with manager._worker_lifecycle_lock:
                acquired.set()
                release.wait(timeout=2)

        thread = threading.Thread(target=hold_lifecycle_lock)
        thread.start()
        self.assertTrue(acquired.wait(timeout=1))
        started = time.monotonic()
        try:
            health = manager.health()
        finally:
            release.set()
            thread.join(timeout=1)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(health["worker_transitioning"])
        self.assertEqual("STARTING", health["lifecycle_state"])

    def test_closed_worker_pipe_is_never_ready(self) -> None:
        class AliveProcess:
            def is_alive(self) -> bool:
                return True

        class ClosedConnection:
            closed = True

        manager = object.__new__(PersistentWorkerManager)
        manager._worker_lifecycle_lock = threading.RLock()
        manager._process = AliveProcess()
        manager._connection = ClosedConnection()
        manager._worker_state = {}
        manager._worker_pid = 123
        manager._worker_generation = "worker-generation"
        manager._job = object()
        manager._last_worker_start_error = None
        manager._condition = threading.Condition()
        manager._active = None
        manager._pending_total = 0
        manager._closed = False
        manager._handled_request_count = 0
        manager.manager_generation = "manager-generation"
        manager.started_monotonic = time.monotonic()

        health = manager.health()

        self.assertTrue(health["worker_alive"])
        self.assertFalse(health["worker_usable"])
        self.assertEqual("STARTING", health["lifecycle_state"])

    def test_shutdown_reaps_worker_before_stopping_listener(self) -> None:
        calls: list[str] = []

        class WorkerManager:
            def shutdown(self) -> bool:
                calls.append("worker")
                return True

        class Server:
            worker_manager = WorkerManager()

            def shutdown(self) -> None:
                calls.append("listener")

        _drain_worker_then_stop_listener(Server())
        self.assertEqual(["worker", "listener"], calls)

    def test_shutdown_keeps_listener_until_worker_is_reaped(self) -> None:
        calls: list[str] = []

        class WorkerManager:
            outcomes = iter((False, False, True))

            def shutdown(self) -> bool:
                calls.append("worker")
                return next(self.outcomes)

        class Server:
            worker_manager = WorkerManager()

            def shutdown(self) -> None:
                calls.append("listener")

        with mock.patch("ragd.time.sleep"):
            _drain_worker_then_stop_listener(Server())
        self.assertEqual(
            ["worker", "worker", "worker", "listener"],
            calls,
        )

    def test_unreaped_worker_never_spawns_a_second_worker(self) -> None:
        class AliveProcess:
            def is_alive(self) -> bool:
                return True

        class ClosedConnection:
            closed = True

        manager = object.__new__(PersistentWorkerManager)
        manager._process = AliveProcess()
        manager._connection = ClosedConnection()
        manager._job = None
        manager._last_worker_start_error = None
        manager._stop_worker_locked = mock.Mock(return_value=False)
        with mock.patch("rag_manager.multiprocessing.get_context") as context:
            ready = manager._ensure_worker_locked(timeout_seconds=1.0)
        self.assertFalse(ready)
        self.assertEqual(
            "previous_worker_not_reaped",
            manager._last_worker_start_error,
        )
        context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
