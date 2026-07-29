from __future__ import annotations

import unittest
from unittest import mock

from source_manager.progress import ProgressRenderer


class ProgressRendererTests(unittest.TestCase):
    def test_exact_total_has_real_percentage(self) -> None:
        output: list[str] = []
        renderer = ProgressRenderer(
            output.append,
            operation="Source追加",
            provider="Redmine",
            is_tty=False,
            clock=lambda: 10.0,
        )
        renderer(
            {
                "phase": "redmine.detail",
                "label_ja": "Redmine Issue詳細取得",
                "completed": 12,
                "total": 40,
                "unit": "件",
                "total_kind": "exact",
                "current_item": "Issue #1234",
                "elapsed_seconds": 18,
            }
        )
        self.assertEqual(1, len(output))
        self.assertIn("30%（12/40件）", output[0])
        self.assertIn("Issue #1234", output[0])

    def test_unknown_total_never_invents_percentage(self) -> None:
        output: list[str] = []
        renderer = ProgressRenderer(
            output.append,
            operation="Source更新",
            is_tty=False,
            clock=lambda: 10.0,
        )
        renderer(
            {
                "phase": "svn.checkout",
                "completed": 15,
                "unit": "件",
                "total_kind": "unknown",
                "elapsed_seconds": 12,
            }
        )
        self.assertNotIn("%", output[0])
        self.assertIn("15件処理済み", output[0])

    def test_zero_exact_total_is_target_none(self) -> None:
        output: list[str] = []
        renderer = ProgressRenderer(
            output.append,
            operation="Source更新",
            is_tty=False,
            clock=lambda: 10.0,
        )
        renderer(
            {
                "phase": "fetch",
                "completed": 0,
                "total": 0,
                "total_kind": "exact",
            }
        )
        self.assertIn("対象なし", output[0])
        self.assertNotIn("%", output[0])

    def test_rate_limit_and_immediate_phase_change(self) -> None:
        output: list[str] = []
        now = [10.0]
        renderer = ProgressRenderer(
            output.append,
            operation="Source更新",
            is_tty=False,
            clock=lambda: now[0],
        )
        renderer({"phase": "fetch", "completed": 1})
        now[0] = 10.2
        renderer({"phase": "fetch", "completed": 2})
        renderer({"phase": "reflect", "completed": 2})
        self.assertEqual(2, len(output))

    def test_http_attempt_shows_sanitized_headers(self) -> None:
        output: list[str] = []
        renderer = ProgressRenderer(
            output.append,
            operation="Source更新",
            provider="redmine",
            is_tty=False,
            clock=lambda: 10.0,
        )
        renderer(
            {
                "event": "redmine.http_attempt",
                "method": "GET",
                "url": "https://issues.example.invalid/issues.json",
                "attempt": 1,
                "max_attempts": 3,
                "timeout_seconds": 10,
                "status": 429,
                "reason": "Too Many Requests",
                "retry": True,
                "retry_after": "2",
                "request_headers": {
                    "Accept": "application/json",
                    "X-Redmine-API-Key": "<REDACTED>",
                },
                "response_headers": {
                    "Content-Type": "application/json",
                    "Set-Cookie": "<REDACTED>",
                },
            }
        )
        self.assertEqual(1, len(output))
        self.assertIn("Retry-After=2", output[0])
        self.assertIn("request headers", output[0])
        self.assertIn("response headers", output[0])
        self.assertIn("<REDACTED>", output[0])

    def test_callback_failure_is_swallowed(self) -> None:
        renderer = ProgressRenderer(
            lambda _message: (_ for _ in ()).throw(RuntimeError("display")),
            operation="Source更新",
            is_tty=False,
        )
        renderer({"phase": "fetch", "completed": 1})

    def test_tty_builtin_print_flushes_and_reuses_the_current_line(self) -> None:
        with mock.patch("builtins.print") as printer:
            renderer = ProgressRenderer(
                print,
                operation="Source追加",
                is_tty=True,
                clock=lambda: 10.0,
            )
            renderer({"phase": "fetch", "completed": 1})
            running = printer.call_args
            self.assertTrue(str(running.args[0]).startswith("\r"))
            self.assertEqual("", running.kwargs["end"])
            self.assertTrue(running.kwargs["flush"])

            renderer(
                {
                    "phase": "fetch",
                    "completed": 1,
                    "status": "completed",
                }
            )
            completed = printer.call_args
            self.assertEqual("\n", completed.kwargs["end"])
            self.assertTrue(completed.kwargs["flush"])


if __name__ == "__main__":
    unittest.main()
