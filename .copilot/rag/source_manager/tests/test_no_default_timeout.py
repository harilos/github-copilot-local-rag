from __future__ import annotations

import inspect
import unittest

from source_manager import run_streaming_process


class SourceOperationTimeoutContractTests(unittest.TestCase):
    def test_default_timeout_is_disabled(self) -> None:
        timeout = inspect.signature(run_streaming_process).parameters["timeout"]
        self.assertIsNone(timeout.default)

    def test_callers_can_still_request_an_explicit_timeout(self) -> None:
        defaults = dict(run_streaming_process.__kwdefaults__ or {})
        self.assertIn("timeout", defaults)
        self.assertIsNone(defaults["timeout"])
        self.assertIn("timeout", inspect.signature(run_streaming_process).parameters)


if __name__ == "__main__":
    unittest.main()
