from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


QUERY_ROOT = Path(__file__).resolve().parent
RAGD_SPEC = importlib.util.spec_from_file_location("rag_daemon", QUERY_ROOT / "ragd.py")
assert RAGD_SPEC and RAGD_SPEC.loader
RAGD = importlib.util.module_from_spec(RAGD_SPEC)
RAGD_SPEC.loader.exec_module(RAGD)
import rag_manager as RAG_MANAGER
from rag_manager import PersistentWorkerManager


class DaemonOwnershipTests(unittest.TestCase):
    def test_health_lifecycle_distinguishes_starting_ready_and_busy(self) -> None:
        health = {
            "lifecycle_state": "STARTING",
            "active_requests": 0,
            "queue_depth": 0,
            "handled_request_count": 0,
            "model_load_count": 0,
            "open_database_count": 0,
            "worker_pid": None,
            "worker_generation": None,
            "worker_job_object_active": False,
            "manager_rss_bytes": None,
            "worker_rss_bytes": None,
            "manager_handle_count": None,
            "worker_handle_count": None,
            "manager_thread_count": 1,
            "worker_thread_count": None,
        }
        server = SimpleNamespace(
            state_lock=threading.Lock(),
            active_requests=0,
            request_sequence=0,
            runtime_ready=False,
            dense_ready=False,
            generation="generation",
            started_at="now",
            started_monotonic=time.monotonic(),
            code_fingerprint="fingerprint",
            idle_timeout=600,
            worker_manager=SimpleNamespace(
                health=lambda: dict(health),
            ),
        )
        self.assertEqual("STARTING", RAGD._server_health_payload(server)["lifecycle_state"])
        self.assertFalse(RAGD._server_health_payload(server)["dense_ready"])
        health["lifecycle_state"] = "READY"
        self.assertEqual("READY", RAGD._server_health_payload(server)["lifecycle_state"])
        health["model_load_count"] = 1
        self.assertTrue(RAGD._server_health_payload(server)["dense_ready"])
        health["lifecycle_state"] = "BUSY"
        health["active_requests"] = 1
        self.assertEqual("BUSY", RAGD._server_health_payload(server)["lifecycle_state"])

    def test_current_generation_owns_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ragd.json"
            path.write_text(
                json.dumps({"generation": "current", "pid": 123}),
                encoding="utf-8",
            )
            self.assertFalse(
                RAGD._state_is_superseded(path, generation="current", pid=123)
            )

    def test_replaced_generation_is_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ragd.json"
            path.write_text(
                json.dumps({"generation": "new", "pid": 456}),
                encoding="utf-8",
            )
            self.assertTrue(
                RAGD._state_is_superseded(path, generation="old", pid=123)
            )

    def test_unpublished_daemon_is_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            self.assertTrue(
                RAGD._state_is_superseded(path, generation="old", pid=123)
            )


class PersistentManagerTests(unittest.TestCase):
    def test_release_lease_blocks_search_until_matching_resume(self) -> None:
        manager = PersistentWorkerManager(
            rag_root=QUERY_ROOT.parent,
            dbs_root=QUERY_ROOT.parent / "dbs",
            manager_generation="manager-lease-test",
        )
        release = manager.release_db("ac-rag", lease_id="lease-a")
        self.assertEqual("db_released", release["status"])
        blocked = manager.submit_search(
            {
                "request_id": "blocked",
                "client_id": "client",
                "db": "ac-rag",
                "question": "q",
                "remaining_deadline_ms": 500,
            }
        )
        self.assertEqual("db_release_in_progress", blocked["error_kind"])
        mismatch = manager.resume_db("ac-rag", lease_id="lease-b")
        self.assertEqual("release_lease_mismatch", mismatch["status"])
        resumed = manager.resume_db("ac-rag", lease_id="lease-a")
        self.assertEqual("db_resumed", resumed["status"])
        manager.shutdown()

    def test_windows_release_blocks_all_dbs_until_manager_restart(self) -> None:
        manager = PersistentWorkerManager(
            rag_root=QUERY_ROOT.parent,
            dbs_root=QUERY_ROOT.parent / "dbs",
            manager_generation="manager-windows-release-test",
        )
        with patch.object(RAG_MANAGER.os, "name", "nt"):
            release = manager.release_db("ac-rag", lease_id="lease-a")
            self.assertEqual("db_released", release["status"])
            other_db = manager.submit_search(
                {
                    "request_id": "other-db",
                    "client_id": "client",
                    "db": "incident-rag",
                    "question": "q",
                    "remaining_deadline_ms": 500,
                }
            )
            self.assertEqual(
                "daemon_restarting_for_maintenance",
                other_db["error_kind"],
            )
            resumed = manager.resume_db("ac-rag", lease_id="lease-a")
            self.assertTrue(resumed["manager_restart_required"])
            same_db = manager.submit_search(
                {
                    "request_id": "same-db",
                    "client_id": "client",
                    "db": "ac-rag",
                    "question": "q",
                    "remaining_deadline_ms": 500,
                }
            )
            self.assertEqual("daemon_draining", same_db["error_kind"])
        manager.shutdown()

    def test_manager_serializes_concurrent_clients(self) -> None:
        manager = PersistentWorkerManager(
            rag_root=QUERY_ROOT.parent,
            dbs_root=QUERY_ROOT.parent / "dbs",
            manager_generation="manager-test",
        )
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def execute(item):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {
                "status": "ok",
                "db": item.db_name,
                "request_id": item.request_id,
            }

        manager._execute_item = execute  # type: ignore[method-assign]
        results: list[dict] = []

        def submit(index: int) -> None:
            results.append(
                manager.submit_search(
                    {
                        "request_id": f"request-{index}",
                        "client_id": f"client-{index}",
                        "db": "ac-rag",
                        "question": "q",
                        "remaining_deadline_ms": 2_000,
                    }
                )
            )

        threads = [
            threading.Thread(target=submit, args=(index,))
            for index in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        manager.shutdown()
        self.assertEqual(1, maximum_active)
        self.assertEqual(4, len(results))
        self.assertEqual(
            {f"request-{index}" for index in range(4)},
            {result["request_id"] for result in results},
        )

    def test_manager_import_does_not_load_retrieval_runtime(self) -> None:
        script = f"""
import importlib.util, json, sys
from pathlib import Path
root = Path({str(QUERY_ROOT)!r})
sys.path.insert(0, str(root))
before = set(sys.modules)
spec = importlib.util.spec_from_file_location('rag_manager_probe', root / 'rag_manager.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
loaded = set(sys.modules) - before
forbidden = [name for name in loaded if name.split('.')[0] in {{
    'chromadb', 'onnxruntime', 'transformers', 'sudachipy'
}}]
if forbidden or 'software_rag_tool.search_api' in loaded:
    raise SystemExit(json.dumps({{'forbidden': forbidden}}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
