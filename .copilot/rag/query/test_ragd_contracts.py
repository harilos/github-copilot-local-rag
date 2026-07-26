from __future__ import annotations

import importlib.util
import json
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


class DaemonOwnershipTests(unittest.TestCase):
    def test_health_lifecycle_distinguishes_starting_ready_and_busy(self) -> None:
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
        )
        self.assertEqual("STARTING", RAGD._server_health_payload(server)["lifecycle_state"])
        self.assertFalse(RAGD._server_health_payload(server)["dense_ready"])
        server.runtime_ready = True
        self.assertEqual("READY", RAGD._server_health_payload(server)["lifecycle_state"])
        server.dense_ready = True
        self.assertTrue(RAGD._server_health_payload(server)["dense_ready"])
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


class DaemonRetrievalRouteTests(unittest.TestCase):
    def test_default_hybrid_request_uses_adaptive_route(self) -> None:
        request = {
            "db": "ac-rag",
            "question": "semantic question",
            "retrieval_mode": "hybrid",
            "adaptive_hybrid": True,
        }
        with (
            patch.object(
                RAGD,
                "run_adaptive_search_payload",
                return_value={"status": "ok"},
            ) as adaptive,
            patch.object(RAGD, "run_search_payload") as standard,
        ):
            self.assertEqual({"status": "ok"}, RAGD._execute_search_payload(request))
        adaptive.assert_called_once()
        standard.assert_not_called()

    def test_explicit_lexical_request_keeps_evaluation_route(self) -> None:
        request = {
            "db": "ac-rag",
            "question": "lexical question",
            "retrieval_mode": "lexical",
            "adaptive_hybrid": True,
        }
        with (
            patch.object(RAGD, "run_adaptive_search_payload") as adaptive,
            patch.object(
                RAGD,
                "run_search_payload",
                return_value={"status": "ok"},
            ) as standard,
        ):
            self.assertEqual({"status": "ok"}, RAGD._execute_search_payload(request))
        adaptive.assert_not_called()
        self.assertEqual("lexical", standard.call_args.kwargs["retrieval_mode"])


if __name__ == "__main__":
    unittest.main()
