from __future__ import annotations

import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
import sys

if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager.diagnostics import (  # noqa: E402
    bounded_diagnostic,
    exception_diagnostic,
    process_diagnostic,
    render_diagnostic,
)
from source_manager.errors import sanitize_diagnostic  # noqa: E402


class DiagnosticContracts(unittest.TestCase):
    def test_bounded_output_keeps_head_tail_and_omitted_count(self) -> None:
        raw = "HEAD-" + ("x" * 80_000) + "-TAIL"
        result = bounded_diagnostic(raw, limit=65_536)
        self.assertTrue(result["truncated"])
        self.assertGreater(result["omitted_chars"], 0)
        self.assertTrue(result["text"].startswith("HEAD-"))
        self.assertTrue(result["text"].endswith("-TAIL"))
        self.assertIn("文字を省略", result["text"])

    def test_exception_chain_and_process_fields_are_rendered(self) -> None:
        try:
            try:
                raise OSError("inner failure")
            except OSError as inner:
                raise RuntimeError("outer failure") from inner
        except RuntimeError as exc:
            diagnostic = exception_diagnostic(
                exc,
                operation="Source更新",
                stage="reflect.add",
                db_name="fixture-rag",
                source_name="Fixture",
                source_key="src_fixture-0123456789ab",
                provider="redmine",
                can_resume=True,
                events_jsonl="sources/src_fixture/events.jsonl",
            )
        process = process_diagnostic(
            arguments=["python", "tool.py", "--db", "fixture-rag"],
            cwd="/safe/work",
            returncode=7,
            elapsed_seconds=1.25,
            stdout="stdout text",
            stderr="stderr text",
        )
        rendered = "\n".join(
            render_diagnostic(diagnostic, process=process)
        )
        for expected in (
            "発生日時:",
            "run_id:",
            "操作名: Source更新",
            "処理段階: reflect.add",
            "DB名: fixture-rag",
            "Source名: Fixture",
            "Source key: src_fixture-0123456789ab",
            "Provider: redmine",
            "RuntimeError: outer failure",
            "OSError: inner failure",
            "再開可能: はい",
            "終了コード: 7",
            "経過時間: 1.250秒",
            "stdout: 11文字",
            "stderr: 11文字",
            "traceback",
        ):
            self.assertIn(expected, rendered)

    def test_credentials_are_redacted_from_diagnostics(self) -> None:
        raw = (
            "Authorization: Bearer abc\n"
            "Cookie: session=abc\n"
            "Set-Cookie: session=def\n"
            "https://user:pass@example.invalid/path"
            "?token=secret&safe=value\n"
            "X-Redmine-API-Key: redmine-secret"
        )
        sanitized = sanitize_diagnostic(raw, max_chars=65_536)
        for secret in (
            "Bearer abc",
            "session=abc",
            "session=def",
            "user:pass",
            "token=secret",
            "redmine-secret",
        ):
            self.assertNotIn(secret, sanitized)
        self.assertIn("<REDACTED>", sanitized)
        self.assertIn("safe=value", sanitized)

    def test_standalone_named_token_is_redacted(self) -> None:
        sanitized = sanitize_diagnostic(
            "RuntimeError: token=not-for-output",
            max_chars=65_536,
        )
        self.assertNotIn("not-for-output", sanitized)
        self.assertIn("token=<REDACTED>", sanitized)

    def test_separate_secret_command_argument_is_redacted(self) -> None:
        process = process_diagnostic(
            arguments=[
                "provider",
                "--api-key",
                "super-secret-value",
                "--token=inline-secret",
                "--safe",
                "visible",
            ],
            cwd="/safe/work",
            returncode=1,
            elapsed_seconds=1,
        )
        command = " ".join(process["command"])
        self.assertNotIn("super-secret-value", command)
        self.assertNotIn("inline-secret", command)
        self.assertIn("--safe visible", command)


if __name__ == "__main__":
    unittest.main()
