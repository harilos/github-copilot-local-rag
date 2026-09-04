from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import atomic_io


WINDOWS_READER = r"""
import ctypes, sys, time
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
# Allow concurrent reads/writes, but deliberately omit FILE_SHARE_DELETE.
handle = kernel.CreateFileW(sys.argv[1], 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
if handle == wintypes.HANDLE(-1).value:
    print("OPEN_FAILED:" + str(ctypes.get_last_error()), flush=True)
    raise SystemExit(1)
try:
    print("READY", flush=True)
    time.sleep(float(sys.argv[2]))
finally:
    kernel.CloseHandle(handle)
"""


def windows_error(code: int) -> OSError:
    error = PermissionError("injected replacement failure")
    error.winerror = code
    return error


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


class AtomicReplaceRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = Path("source.tmp")
        self.target = Path("target.json")

    @contextmanager
    def retry_environment(self, *, platform="nt", side_effect=None):
        clock = FakeClock()
        # Replace this module's os binding, not global os.name/Path behavior.
        with (
            mock.patch.object(atomic_io, "os", wraps=os) as operating_system,
            mock.patch.object(atomic_io.time, "monotonic", side_effect=clock.monotonic) as monotonic,
            mock.patch.object(atomic_io.time, "sleep", side_effect=clock.sleep),
        ):
            operating_system.name = platform
            operating_system.replace.return_value = None
            operating_system.replace.side_effect = side_effect
            yield clock, operating_system.replace, monotonic

    def test_success_does_not_sleep(self) -> None:
        with self.retry_environment() as (clock, replace, _):
            atomic_io._replace_with_retry(self.source, self.target)
        replace.assert_called_once_with(self.source, self.target)
        self.assertEqual(clock.delays, [])

    def test_generic_retry_returns_operation_result(self) -> None:
        result = object()
        operation = mock.Mock(side_effect=[windows_error(32), result])
        with self.retry_environment() as (clock, replace, _):
            self.assertIs(atomic_io.retry_windows_sharing(operation), result)
        self.assertEqual(operation.call_count, 2)
        self.assertEqual(clock.delays, [0.01])
        replace.assert_not_called()

    def test_each_retryable_windows_error_recovers(self) -> None:
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                with self.retry_environment(side_effect=[windows_error(code), None]) as (clock, replace, _):
                    atomic_io._replace_with_retry(self.source, self.target)
                self.assertEqual(replace.call_count, 2)
                self.assertEqual(clock.delays, [0.01])

    def test_backoff_doubles_and_caps_at_one_hundred_ms(self) -> None:
        failures = [windows_error(32) for _ in range(7)]
        with self.retry_environment(side_effect=[*failures, None]) as (clock, replace, _):
            atomic_io._replace_with_retry(self.source, self.target)
        self.assertEqual(replace.call_count, 8)
        self.assertEqual(clock.delays, [0.01, 0.02, 0.04, 0.08, 0.1, 0.1, 0.1])

    def test_winerror_five_twice_then_success(self) -> None:
        with self.retry_environment(side_effect=[windows_error(5), windows_error(5), None]) as (clock, replace, _):
            atomic_io._replace_with_retry(self.source, self.target)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(clock.delays, [0.01, 0.02])

    def test_persistent_failure_is_bounded_by_monotonic_deadline(self) -> None:
        error = windows_error(5)
        with self.retry_environment(side_effect=error) as (clock, replace, monotonic):
            with self.assertRaises(OSError) as raised:
                atomic_io._replace_with_retry(self.source, self.target)
        self.assertIs(raised.exception, error)
        self.assertAlmostEqual(sum(clock.delays), 2.0)
        self.assertLessEqual(max(clock.delays), 0.1)
        self.assertLess(clock.delays[-1], 0.1)
        self.assertLessEqual(replace.call_count, 25)
        self.assertGreater(monotonic.call_count, 1)

    def test_deadline_accounts_for_time_spent_in_replace(self) -> None:
        error = windows_error(33)
        with self.retry_environment() as (clock, replace, _):
            def slow_replace(*_args):
                clock.now += 2.1
                raise error

            replace.side_effect = slow_replace
            with self.assertRaises(OSError) as raised:
                atomic_io._replace_with_retry(self.source, self.target)
        self.assertIs(raised.exception, error)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(clock.delays, [])

    def test_unrelated_windows_errors_are_not_retried(self) -> None:
        for error in (windows_error(2), windows_error(112), PermissionError("no winerror")):
            with self.subTest(error=repr(error)):
                with self.retry_environment(side_effect=error) as (clock, replace, _):
                    with self.assertRaises(OSError) as raised:
                        atomic_io._replace_with_retry(self.source, self.target)
                self.assertIs(raised.exception, error)
                self.assertEqual(replace.call_count, 1)
                self.assertEqual(clock.delays, [])

    def test_non_windows_does_not_retry_even_a_windows_error(self) -> None:
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                error = windows_error(code)
                with self.retry_environment(platform="posix", side_effect=error) as (clock, replace, monotonic):
                    with self.assertRaises(OSError) as raised:
                        atomic_io._replace_with_retry(self.source, self.target)
                self.assertIs(raised.exception, error)
                self.assertEqual(replace.call_count, 1)
                self.assertEqual(clock.delays, [])
                monotonic.assert_not_called()

    def test_non_oserror_is_not_retried(self) -> None:
        error = ValueError("injected non-OS failure")
        with self.retry_environment(side_effect=error) as (clock, replace, _):
            with self.assertRaises(ValueError) as raised:
                atomic_io._replace_with_retry(self.source, self.target)
        self.assertIs(raised.exception, error)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(clock.delays, [])


class AtomicFixture:
    def setUp(self) -> None:
        self.fixture = tempfile.TemporaryDirectory(prefix="atomic-issue20-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.target = self.root / "progress.json"
        self.previous = b'{"generation":"old"}\n'
        self.target.write_bytes(self.previous)

    def assert_no_temporary_files(self) -> None:
        self.assertEqual(list(self.root.glob(".*.tmp")), [])


class AtomicPublicationTests(AtomicFixture, unittest.TestCase):
    def test_json_round_trip_preserves_unicode(self) -> None:
        payload = {"generation": "new", "message": "完了"}
        atomic_io.atomic_write_json(self.target, payload)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), payload)
        self.assert_no_temporary_files()

    def test_retry_exposes_complete_old_then_complete_new_bytes_only(self) -> None:
        replacement = b'{"generation":"new","complete":true}\n'
        real_replace = os.replace
        attempted = []
        clock = FakeClock()

        def replace(source, target):
            attempted.append(Path(source))
            self.assertEqual(Path(target).read_bytes(), self.previous)
            self.assertEqual(Path(source).read_bytes(), replacement)
            if len(attempted) < 4:
                raise windows_error(32)
            real_replace(source, target)
            self.assertEqual(Path(target).read_bytes(), replacement)

        with (
            mock.patch.object(atomic_io, "os", wraps=os) as operating_system,
            mock.patch.object(atomic_io.time, "monotonic", side_effect=clock.monotonic),
            mock.patch.object(atomic_io.time, "sleep", side_effect=clock.sleep),
        ):
            operating_system.name = "nt"
            operating_system.replace.side_effect = replace
            atomic_io.atomic_write_bytes(self.target, replacement)
        self.assertEqual(len(attempted), 4)
        self.assertEqual(len(set(attempted)), 1)
        self.assert_no_temporary_files()

    def test_exhausted_retry_preserves_old_bytes_and_cleans_temporary(self) -> None:
        error = windows_error(5)
        clock = FakeClock()
        with (
            mock.patch.object(atomic_io, "os", wraps=os) as operating_system,
            mock.patch.object(atomic_io.time, "monotonic", side_effect=clock.monotonic),
            mock.patch.object(atomic_io.time, "sleep", side_effect=clock.sleep),
        ):
            operating_system.name = "nt"
            operating_system.replace.side_effect = error
            with self.assertRaises(OSError) as raised:
                atomic_io.atomic_write_json(self.target, {"generation": "new"})
        self.assertIs(raised.exception, error)
        self.assertEqual(self.target.read_bytes(), self.previous)
        self.assert_no_temporary_files()

    def test_unrelated_replace_failure_preserves_old_and_cleans_temporary(self) -> None:
        error = windows_error(112)
        with mock.patch.object(atomic_io.os, "replace", side_effect=error) as replace:
            with self.assertRaises(OSError) as raised:
                atomic_io.atomic_write_json(self.target, {"generation": "new"})
        self.assertIs(raised.exception, error)
        replace.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.previous)
        self.assert_no_temporary_files()

    def test_failed_temporary_cleanup_cannot_mask_original_error(self) -> None:
        original = windows_error(112)
        cleanup = windows_error(5)
        with (
            mock.patch.object(atomic_io.os, "replace", side_effect=original),
            mock.patch.object(Path, "unlink", side_effect=cleanup) as unlink,
        ):
            with self.assertRaises(OSError) as raised:
                atomic_io.atomic_write_json(self.target, {"generation": "new"})
        self.assertIs(raised.exception, original)
        unlink.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.previous)
        # The injected unlink failure necessarily leaves one fixture-only temp.
        self.assertEqual(len(list(self.root.glob(".*.tmp"))), 1)

    def test_fsync_failure_preserves_old_and_cleans_temporary(self) -> None:
        original = OSError("injected fsync failure")
        with (
            mock.patch.object(atomic_io.os, "fsync", side_effect=original),
            mock.patch.object(atomic_io.os, "replace") as replace,
        ):
            with self.assertRaises(OSError) as raised:
                atomic_io.atomic_write_json(self.target, {"generation": "new"})
        self.assertIs(raised.exception, original)
        replace.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.previous)
        self.assert_no_temporary_files()


@unittest.skipUnless(os.name == "nt", "requires real Windows file sharing")
class RealWindowsReaderTests(AtomicFixture, unittest.TestCase):
    @contextmanager
    def reader(self, seconds: float):
        child = subprocess.Popen(
            [sys.executable, "-c", WINDOWS_READER, str(self.target), str(seconds)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
            yield
        finally:
            try:
                _, stderr = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                self.fail("fixture reader did not close its handle")
            self.assertEqual(child.returncode, 0, stderr)

    def test_unretried_replace_reproduces_real_windows_sharing_failure(self) -> None:
        source = self.root / "candidate.tmp"
        source.write_bytes(b'{"generation":"new"}\n')
        with self.reader(0.3):
            with self.assertRaises(OSError) as raised:
                os.replace(source, self.target)
            self.assertIn(raised.exception.winerror, {5, 32, 33})
            self.assertEqual(self.target.read_bytes(), self.previous)
        source.unlink()

    def test_short_real_reader_allows_atomic_retry_to_succeed(self) -> None:
        with self.reader(0.3):
            started = time.monotonic()
            atomic_io.atomic_write_json(self.target, {"generation": "new"})
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"generation": "new"})
        self.assert_no_temporary_files()
        print(json.dumps({"case": "real_reader_short", "artifact": "<fixture>/progress.json", "elapsed_ms": round(elapsed * 1000, 1), "generation": "new", "orphan_temp_count": 0}))

    def test_long_real_reader_times_out_without_changing_old_file(self) -> None:
        with self.reader(3.0):
            started = time.monotonic()
            with self.assertRaises(OSError) as raised:
                atomic_io.atomic_write_json(self.target, {"generation": "new"})
            elapsed = time.monotonic() - started
            self.assertIn(raised.exception.winerror, {5, 32, 33})
            self.assertEqual(self.target.read_bytes(), self.previous)
            self.assert_no_temporary_files()
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 2.75)
        print(json.dumps({"case": "real_reader_long", "artifact": "<fixture>/progress.json", "elapsed_ms": round(elapsed * 1000, 1), "winerror": raised.exception.winerror, "previous_bytes_intact": True, "orphan_temp_count": 0}))


if __name__ == "__main__":
    unittest.main()
