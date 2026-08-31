from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
sys.path.insert(0, str(QUERY_ROOT))

import skill_runner


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


class SkillRunnerContractTests(unittest.TestCase):
    def _run(self, arguments: list[str], *, returncode: int = 0):
        with mock.patch.object(
            skill_runner.subprocess,
            "run",
            return_value=_completed(returncode),
        ) as run:
            code = skill_runner.main(arguments)
        return code, run

    def _assert_parser_error(self, arguments: list[str]) -> None:
        with (
            mock.patch.object(skill_runner.subprocess, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            skill_runner.main(arguments)
        self.assertEqual(2, raised.exception.code)
        run.assert_not_called()

    def test_list_uses_only_the_root_public_json_entry_point(self) -> None:
        code, run = self._run(["list"], returncode=7)

        self.assertEqual(7, code)
        run.assert_called_once_with(
            [
                sys.executable,
                "-I",
                "-B",
                str(RAG_ROOT / "list_dbs.py"),
                "--format",
                "json",
            ],
            check=False,
            cwd=str(RAG_ROOT),
            env={**os.environ, "RAG_DBS_ROOT": str(RAG_ROOT / "dbs")},
            shell=False,
        )

    def test_search_fixes_output_flags_and_forwards_structured_hints(self) -> None:
        question = "--option-looking"
        code, run = self._run(
            [
                "search",
                "--db",
                "example-rag",
                "--question",
                question,
                "--answer-goal",
                "comparison",
                "--literal-identifier",
                "--ABC-123",
                "--entity",
                "--Component-A",
                "--facet",
                "--failure-mode",
                "--semantic-hypothesis",
                "--possible-legacy-behavior",
            ]
        )

        self.assertEqual(0, code)
        run.assert_called_once_with(
            [
                sys.executable,
                "-I",
                "-B",
                str(RAG_ROOT / "search.py"),
                "--db",
                "example-rag",
                "--include-db-hint",
                "--compact-json",
                "--result-delivery",
                "file",
                "--format",
                "json",
                "--stdin",
                "--answer-goal",
                "comparison",
                "--literal-identifier=--ABC-123",
                "--entity=--Component-A",
                "--facet=--failure-mode",
                "--semantic-hypothesis=--possible-legacy-behavior",
            ],
            check=False,
            cwd=str(RAG_ROOT),
            env={**os.environ, "RAG_DBS_ROOT": str(RAG_ROOT / "dbs")},
            input=question.encode("utf-8"),
            shell=False,
        )

    def test_detail_uses_public_search_and_bounded_cached_ids(self) -> None:
        result_set_id = "00000000-0000-0000-0000-000000000001"
        code, run = self._run(
            [
                "detail",
                "--result-set-id",
                result_set_id,
                "--item-id",
                "E1",
                "--item-id",
                "D2",
                "--detail-level",
                "expanded",
            ]
        )

        self.assertEqual(0, code)
        run.assert_called_once_with(
            [
                sys.executable,
                "-I",
                "-B",
                str(RAG_ROOT / "search.py"),
                "--result-set-id",
                result_set_id,
                "--item-id",
                "E1",
                "--item-id",
                "D2",
                "--detail-level",
                "expanded",
                "--result-delivery",
                "file",
            ],
            check=False,
            cwd=str(RAG_ROOT),
            env={**os.environ, "RAG_DBS_ROOT": str(RAG_ROOT / "dbs")},
            shell=False,
        )

    def test_setup_uses_only_the_root_public_json_entry_point(self) -> None:
        code, run = self._run(["setup"])

        self.assertEqual(0, code)
        run.assert_called_once_with(
            [
                sys.executable,
                "-I",
                "-B",
                str(RAG_ROOT / "setup.py"),
                "--format",
                "json",
            ],
            check=False,
            cwd=str(RAG_ROOT),
            env={**os.environ, "RAG_DBS_ROOT": str(RAG_ROOT / "dbs")},
            shell=False,
        )

    def test_unknown_options_never_start_a_child(self) -> None:
        self._assert_parser_error(["list", "--format", "text"])
        self._assert_parser_error(
            [
                "search",
                "--db",
                "example-rag",
                "--question",
                "question",
                "--no-daemon",
            ]
        )
        self._assert_parser_error(["setup", "--force-model"])

    def test_search_rejects_invalid_database_and_question(self) -> None:
        self._assert_parser_error(
            ["search", "--db", "../example-rag", "--question", "question"]
        )
        self._assert_parser_error(
            ["search", "--db", "example-rag", "--question", "   "]
        )
        self._assert_parser_error(
            ["search", "--db", "example-rag", "--question", "bad\x00text"]
        )
        self._assert_parser_error(
            [
                "search",
                "--db",
                "example-rag",
                "--question",
                "x" * (skill_runner.MAX_QUESTION_CHARS + 1),
            ]
        )

    def test_search_enforces_each_repeatable_hint_cap(self) -> None:
        cases = (
            ("--literal-identifier", 4),
            ("--entity", 6),
            ("--facet", 5),
            ("--semantic-hypothesis", 4),
        )
        for option, count in cases:
            with self.subTest(option=option):
                arguments = [
                    "search",
                    "--db",
                    "example-rag",
                    "--question",
                    "question",
                ]
                for index in range(count):
                    arguments.extend([option, f"value-{index}"])
                self._assert_parser_error(arguments)

    def test_search_rejects_requests_the_public_contract_cannot_accept(
        self,
    ) -> None:
        self._assert_parser_error(
            [
                "search",
                "--db",
                "example-rag",
                "--question",
                "あ" * 1_000,
            ]
        )

    def test_detail_rejects_invalid_or_unbounded_ids(self) -> None:
        valid_id = "00000000-0000-0000-0000-000000000001"
        self._assert_parser_error(
            [
                "detail",
                "--result-set-id",
                "../result",
                "--item-id",
                "E1",
            ]
        )
        self._assert_parser_error(
            [
                "detail",
                "--result-set-id",
                valid_id,
                "--item-id",
                "../../E1",
            ]
        )
        self._assert_parser_error(
            [
                "detail",
                "--result-set-id",
                valid_id,
                "--item-id",
                "E1",
                "--item-id",
                "E1",
            ]
        )
        self._assert_parser_error(
            [
                "detail",
                "--result-set-id",
                valid_id,
                "--item-id",
                "E1",
                "--item-id",
                "E2",
                "--detail-level",
                "deep",
            ]
        )
        self._assert_parser_error(
            [
                "detail",
                "--result-set-id",
                valid_id,
                "--item-id",
                "E1",
                "--item-id",
                "E2",
                "--item-id",
                "D1",
                "--item-id",
                "D2",
            ]
        )


if __name__ == "__main__":
    unittest.main()
