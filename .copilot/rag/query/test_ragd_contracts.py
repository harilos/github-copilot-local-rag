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
from software_rag_tool import db_maintenance


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
    def test_idle_health_merges_generation_scoped_worker_telemetry(self) -> None:
        manager = PersistentWorkerManager(
            rag_root=QUERY_ROOT.parent,
            dbs_root=QUERY_ROOT.parent / "dbs",
            manager_generation="manager-telemetry-test",
        )
        receive, send = RAG_MANAGER.multiprocessing.get_context(
            "spawn"
        ).Pipe(duplex=False)
        with manager._worker_lifecycle_lock:
            manager._status_connection = receive
            manager._worker_generation = "worker-current"
            manager._worker_pid = 321
            manager._worker_state_revision = 1
            manager._worker_state = {
                "worker_pid": 321,
                "worker_generation": "worker-current",
                "state_revision": 1,
                "dense_warmup_state": "starting",
                "model_load_count": 0,
            }
        send.send(
            {
                "op": "worker_status",
                "worker_generation": "worker-current",
                "state_revision": 2,
                "worker_state": {
                    "worker_pid": 321,
                    "worker_generation": "worker-current",
                    "state_revision": 2,
                    "dense_warmup_state": "ready",
                    "model_load_count": 1,
                },
            }
        )
        ready = manager.health()
        self.assertEqual("ready", ready["dense_warmup_state"])
        self.assertEqual(1, ready["model_load_count"])

        # Neither an older snapshot nor another generation may regress the
        # terminal readiness state.
        for generation, revision in (
            ("worker-current", 1),
            ("worker-replacement", 99),
        ):
            send.send(
                {
                    "op": "worker_status",
                    "worker_generation": generation,
                    "state_revision": revision,
                    "worker_state": {
                        "worker_pid": 999,
                        "worker_generation": generation,
                        "state_revision": revision,
                        "dense_warmup_state": "starting",
                        "model_load_count": 0,
                    },
                }
            )
        still_ready = manager.health()
        self.assertEqual("ready", still_ready["dense_warmup_state"])
        self.assertEqual(1, still_ready["model_load_count"])
        send.close()
        manager.shutdown()

    def test_release_lease_blocks_search_until_matching_resume(self) -> None:
        manager = PersistentWorkerManager(
            rag_root=QUERY_ROOT.parent,
            dbs_root=QUERY_ROOT.parent / "dbs",
            manager_generation="manager-lease-test",
        )
        release = manager.release_db(
            "ac-rag",
            lease_id="lease-a",
            operation="add",
        )
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
        self.assertEqual(
            {
                "schema": "local-rag.search.v1",
                "status": "busy",
                "error": "db_maintenance_in_progress",
                "db": "ac-rag",
                "operation": "add",
            },
            blocked,
        )
        mismatch = manager.resume_db("ac-rag", lease_id="lease-b")
        self.assertEqual("release_lease_mismatch", mismatch["status"])
        resumed = manager.resume_db("ac-rag", lease_id="lease-a")
        self.assertEqual("db_resumed", resumed["status"])
        self.assertFalse(resumed["manager_restart_required"])
        manager.shutdown()

    def test_windows_release_blocks_only_target_db(self) -> None:
        manager = PersistentWorkerManager(
            rag_root=QUERY_ROOT.parent,
            dbs_root=QUERY_ROOT.parent / "dbs",
            manager_generation="manager-windows-release-test",
        )
        manager._execute_item = lambda item: {  # type: ignore[method-assign]
            "status": "ok",
            "db": item.db_name,
            "request_id": item.request_id,
        }
        with patch.object(RAG_MANAGER.os, "name", "nt"):
            release = manager.release_db(
                "ac-rag",
                lease_id="lease-a",
                operation="build",
            )
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
            self.assertEqual("ok", other_db["status"])
            self.assertEqual("incident-rag", other_db["db"])
            same_db = manager.submit_search(
                {
                    "request_id": "same-db",
                    "client_id": "client",
                    "db": "ac-rag",
                    "question": "q",
                    "remaining_deadline_ms": 500,
                }
            )
            self.assertEqual("busy", same_db["status"])
            self.assertEqual(
                "db_maintenance_in_progress",
                same_db["error"],
            )
            resumed = manager.resume_db("ac-rag", lease_id="lease-a")
            self.assertFalse(resumed["manager_restart_required"])
        manager.shutdown()

    def test_finished_persistent_lease_recovers_without_resume_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rag_root = Path(directory) / "rag"
            manager = PersistentWorkerManager(
                rag_root=rag_root,
                dbs_root=rag_root / "dbs",
                manager_generation="manager-stale-release-test",
            )
            lease = db_maintenance.acquire_maintenance_lease(
                "target-rag",
                operation="add",
                rag_root=rag_root,
            )
            release = manager.release_db(
                "target-rag",
                lease_id=lease.lease_id,
                operation="add",
            )
            self.assertEqual("db_released", release["status"])
            db_maintenance.finish_maintenance(lease)
            manager._execute_item = lambda item: {  # type: ignore[method-assign]
                "status": "ok",
                "db": item.db_name,
            }
            result = manager.submit_search(
                {
                    "request_id": "after-stale",
                    "client_id": "client",
                    "db": "target-rag",
                    "question": "q",
                    "remaining_deadline_ms": 500,
                }
            )
            self.assertEqual("ok", result["status"])
            self.assertNotIn("target-rag", manager._releasing_dbs)
            manager.shutdown()

    def test_replacement_manager_accepts_matching_durable_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rag_root = Path(directory) / "rag"
            lease = db_maintenance.acquire_maintenance_lease(
                "target-rag",
                operation="add",
                rag_root=rag_root,
            )
            replacement = PersistentWorkerManager(
                rag_root=rag_root,
                dbs_root=rag_root / "dbs",
                manager_generation="manager-replacement-test",
            )
            resumed = replacement.resume_db(
                "target-rag",
                lease_id=lease.lease_id,
            )
            self.assertEqual("db_resumed", resumed["status"])
            mismatch = replacement.resume_db(
                "target-rag",
                lease_id="different-lease",
            )
            self.assertEqual("db_not_released", mismatch["status"])
            db_maintenance.finish_maintenance(lease)
            replacement.shutdown()

    def test_new_durable_lease_replaces_stale_in_memory_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rag_root = Path(directory) / "rag"
            manager = PersistentWorkerManager(
                rag_root=rag_root,
                dbs_root=rag_root / "dbs",
                manager_generation="manager-stale-repair-test",
            )
            old = db_maintenance.acquire_maintenance_lease(
                "target-rag",
                operation="add",
                rag_root=rag_root,
            )
            old_release = manager.release_db(
                "target-rag",
                lease_id=old.lease_id,
                operation="add",
            )
            self.assertEqual("db_released", old_release["status"])
            db_maintenance.finish_maintenance(old)
            replacement = db_maintenance.acquire_maintenance_lease(
                "target-rag",
                operation="resume",
                rag_root=rag_root,
            )
            new_release = manager.release_db(
                "target-rag",
                lease_id=replacement.lease_id,
                operation="resume",
            )
            self.assertEqual("db_released", new_release["status"])
            self.assertEqual(
                replacement.lease_id,
                manager._releasing_dbs["target-rag"]["lease_id"],
            )
            manager.resume_db(
                "target-rag",
                lease_id=replacement.lease_id,
            )
            db_maintenance.finish_maintenance(replacement)
            manager.shutdown()

    def test_worker_release_transitions_are_serialized(self) -> None:
        manager = PersistentWorkerManager(
            rag_root=QUERY_ROOT.parent,
            dbs_root=QUERY_ROOT.parent / "dbs",
            manager_generation="manager-transition-lock-test",
        )
        lock = threading.Lock()
        active = 0
        maximum = 0

        def stop_worker(*, graceful):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return True

        manager._stop_worker = stop_worker  # type: ignore[method-assign]
        threads = [
            threading.Thread(
                target=manager.release_db,
                args=(db_name,),
                kwargs={
                    "lease_id": f"lease-{index}",
                    "operation": "add",
                },
            )
            for index, db_name in enumerate(
                ("first-rag", "second-rag"),
                start=1,
            )
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1.0)
        self.assertEqual(1, maximum)
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
