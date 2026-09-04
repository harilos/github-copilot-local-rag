from __future__ import annotations

import errno
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from software_rag_tool import read_io


class WindowsReadRetryTests(unittest.TestCase):
    def exercise(self, error, *, platform="nt", persistent=False):
        clock = [0.0]
        delays = []
        target = mock.Mock()
        target.read_text.side_effect = error if persistent else [error, "complete"]

        def sleep(delay):
            clock[0] += delay
            delays.append(delay)

        with mock.patch.object(read_io, "os", wraps=os) as operating_system, mock.patch.object(read_io.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(read_io.time, "sleep", side_effect=sleep):
            operating_system.name = platform
            try:
                result = read_io.read_text_with_windows_retry(target, encoding="utf-8")
                raised = None
            except BaseException as exc:
                result, raised = None, exc
        return target, delays, result, raised

    def test_windows_crt_eacces_read_recovers(self):
        target, delays, result, error = self.exercise(PermissionError(errno.EACCES, "injected"))
        self.assertIsNone(error)
        self.assertEqual(result, "complete")
        self.assertEqual(delays, [0.01])
        self.assertEqual(target.read_text.call_count, 2)
        target.read_text.assert_called_with(encoding="utf-8")

    def test_windows_native_codes_read_recovers(self):
        for code in (5, 32, 33):
            error = OSError("injected")
            error.winerror = code
            with self.subTest(code=code):
                _, delays, result, raised = self.exercise(error)
                self.assertIsNone(raised)
                self.assertEqual(result, "complete")
                self.assertEqual(delays, [0.01])

    def test_all_other_errors_raise_immediately(self):
        errors = [PermissionError(errno.EPERM, "injected"), OSError(errno.ENOSPC, "injected"), FileNotFoundError(), TypeError("invalid"), UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid")]
        native_other = PermissionError(errno.EACCES, "injected")
        native_other.winerror = 112
        errors.append(native_other)
        for error in errors:
            with self.subTest(type=type(error).__name__):
                target, delays, _, raised = self.exercise(error)
                self.assertIs(raised, error)
                self.assertEqual(delays, [])
                self.assertEqual(target.read_text.call_count, 1)

    def test_non_windows_crt_error_is_not_retried(self):
        error = PermissionError(errno.EACCES, "injected")
        target, delays, _, raised = self.exercise(error, platform="posix")
        self.assertIs(raised, error)
        self.assertEqual(target.read_text.call_count, 1)
        self.assertEqual(delays, [])

    def test_persistent_denial_is_bounded_and_preserves_exception(self):
        error = PermissionError(errno.EACCES, "injected")
        target, delays, _, raised = self.exercise(error, persistent=True)
        self.assertIs(raised, error)
        self.assertAlmostEqual(sum(delays), 2.0)
        self.assertEqual(delays[:4], [0.01, 0.02, 0.04, 0.08])
        self.assertLessEqual(max(delays), 0.1)
        self.assertEqual(target.read_text.call_count, len(delays) + 1)


if __name__ == "__main__":
    unittest.main()
