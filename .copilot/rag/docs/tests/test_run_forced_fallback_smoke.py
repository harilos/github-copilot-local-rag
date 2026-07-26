from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).with_name("run_forced_fallback_smoke.py")
SPEC = importlib.util.spec_from_file_location(
    "run_forced_fallback_smoke",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HangingDaemonContractTests(unittest.TestCase):
    def test_health_publishes_authenticated_runtime_identity(self) -> None:
        server = SimpleNamespace(
            generation="injected-generation",
            code_fingerprint="runtime-fingerprint",
        )
        health = MODULE.hanging_daemon_health(server)
        self.assertEqual(os.getpid(), health["pid"])
        self.assertEqual("injected-generation", health["generation"])
        self.assertEqual("runtime-fingerprint", health["code_fingerprint"])
        self.assertTrue(health["ready"])
        self.assertEqual("READY", health["lifecycle_state"])


if __name__ == "__main__":
    unittest.main()
