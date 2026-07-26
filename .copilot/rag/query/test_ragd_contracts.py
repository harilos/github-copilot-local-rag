from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
RAGD_SPEC = importlib.util.spec_from_file_location("rag_daemon", QUERY_ROOT / "ragd.py")
assert RAGD_SPEC and RAGD_SPEC.loader
RAGD = importlib.util.module_from_spec(RAGD_SPEC)
RAGD_SPEC.loader.exec_module(RAGD)


class DaemonOwnershipTests(unittest.TestCase):
    def test_health_lifecycle_distinguishes_starting_ready_and_busy(self) -> None:
        server = SimpleNamespace(
            state_lock=threading.Lock(),
            active_requests=0,
            request_sequence=0,
            runtime_ready=False,
            generation="generation",
            started_at="now",
            started_monotonic=time.monotonic(),
            code_fingerprint="fingerprint",
            idle_timeout=600,
        )
        self.assertEqual("STARTING", RAGD._server_health_payload(server)["lifecycle_state"])
        server.runtime_ready = True
        self.assertEqual("READY", RAGD._server_health_payload(server)["lifecycle_state"])
        server.active_requests = 1
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


if __name__ == "__main__":
    unittest.main()
