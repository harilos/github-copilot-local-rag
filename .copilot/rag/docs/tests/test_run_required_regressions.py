from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


RUNNER_PATH = Path(__file__).with_name("run_required_regressions.py")
SPEC = importlib.util.spec_from_file_location(
    "local_rag_required_regressions",
    RUNNER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RequiredRegressionRunnerTests(unittest.TestCase):
    def test_discovery_finds_every_test_file_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            first = root / "test_first.py"
            second = root / "nested" / "test_second.py"
            ignored = root / "nested" / "helper.py"
            for path in (first, second, ignored):
                path.write_text("", encoding="utf-8")

            self.assertEqual(
                sorted([first.resolve(), second.resolve()]),
                runner.discover_test_files(root),
            )

    def test_environment_includes_both_runtime_roots(self) -> None:
        environment = runner.test_environment({"PYTHONPATH": "existing"})
        self.assertEqual(
            [
                str(runner.RAG_ROOT),
                str(runner.SOFTWARE_RAG_TOOL_ROOT),
                "existing",
            ],
            environment["PYTHONPATH"].split(os.pathsep),
        )

    def test_result_counts_are_parsed(self) -> None:
        self.assertEqual(
            (123, 2),
            runner.parse_test_counts(
                "Ran 123 tests in 1.0s\nOK (skipped=2)\n"
            ),
        )


if __name__ == "__main__":
    unittest.main()
