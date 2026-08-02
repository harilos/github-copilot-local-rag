from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PUBLIC_SEARCH = _load(
    "completion_marker_self_heal_search",
    RAG_ROOT / "search.py",
)


class CompletionMarkerSelfHealContractTests(unittest.TestCase):
    @staticmethod
    def _runtime_path(query_root: Path) -> Path:
        return query_root / ".venv" / (
            "Scripts/python.exe"
            if sys.platform.startswith("win")
            else "bin/python"
        )

    def _roots(self, temporary: str) -> tuple[Path, Path, Path, Path]:
        rag_root = Path(temporary) / "rag"
        query_root = rag_root / "query"
        python = self._runtime_path(query_root)
        marker = query_root / ".venv" / ".rag-deps-installed"
        python.parent.mkdir(parents=True)
        python.touch()
        (query_root / "setup.py").write_text("# fixture\n", encoding="utf-8")
        return rag_root, query_root, python, marker

    def test_invalid_gate_self_heals_silently_before_normal_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rag_root, query_root, python, _marker = self._roots(temporary)
            completed = SimpleNamespace(
                returncode=0,
                stdout=b'{"setup_complete":true}\n',
                stderr=b"internal verification output\n",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(PUBLIC_SEARCH, "RAG_ROOT", rag_root),
                mock.patch.object(PUBLIC_SEARCH, "_QUERY_ROOT", query_root),
                mock.patch.object(
                    PUBLIC_SEARCH,
                    "completion_contract_valid",
                    side_effect=[
                        (False, "completion_marker_requirements"),
                        (True, None),
                    ],
                ) as valid,
                mock.patch.object(
                    PUBLIC_SEARCH.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                repaired = PUBLIC_SEARCH._self_heal_lookup_gate(
                    ["--db", "example-rag", "what changed?"]
                )

            self.assertTrue(repaired)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(2, valid.call_count)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(str(python), command[0])
            self.assertEqual("-B", command[1])
            self.assertEqual(str(query_root / "setup.py"), command[2])
            self.assertIn("--repair-completion-marker", command)
            self.assertEqual(subprocess.PIPE, run.call_args.kwargs["stdout"])
            self.assertEqual(subprocess.PIPE, run.call_args.kwargs["stderr"])
            self.assertEqual(str(rag_root), run.call_args.kwargs["cwd"])
            self.assertEqual(
                "1",
                run.call_args.kwargs["env"][
                    PUBLIC_SEARCH._SELF_HEAL_ACTIVE_ENV
                ],
            )
            self.assertEqual(
                "1",
                run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"],
            )

    def test_valid_gate_does_not_start_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rag_root, query_root, _python, _marker = self._roots(temporary)
            with (
                mock.patch.object(PUBLIC_SEARCH, "RAG_ROOT", rag_root),
                mock.patch.object(PUBLIC_SEARCH, "_QUERY_ROOT", query_root),
                mock.patch.object(
                    PUBLIC_SEARCH,
                    "completion_contract_valid",
                    return_value=(True, None),
                ),
                mock.patch.object(PUBLIC_SEARCH.subprocess, "run") as run,
            ):
                repaired = PUBLIC_SEARCH._self_heal_lookup_gate(
                    ["--db=example-rag", "question"]
                )
            self.assertFalse(repaired)
            run.assert_not_called()

    def test_help_and_cached_detail_never_trigger_self_heal(self) -> None:
        with (
            mock.patch.object(
                PUBLIC_SEARCH,
                "completion_contract_valid",
            ) as valid,
            mock.patch.object(PUBLIC_SEARCH.subprocess, "run") as run,
        ):
            self.assertFalse(
                PUBLIC_SEARCH._self_heal_lookup_gate(["--help"])
            )
            self.assertFalse(
                PUBLIC_SEARCH._self_heal_lookup_gate(
                    [
                        "--result-set-id",
                        "00000000-0000-0000-0000-000000000000",
                        "--item-id",
                        "E1",
                    ]
                )
            )
        valid.assert_not_called()
        run.assert_not_called()

    def test_failed_repair_is_silent_and_falls_back_to_existing_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rag_root, query_root, _python, _marker = self._roots(temporary)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(PUBLIC_SEARCH, "RAG_ROOT", rag_root),
                mock.patch.object(PUBLIC_SEARCH, "_QUERY_ROOT", query_root),
                mock.patch.object(
                    PUBLIC_SEARCH,
                    "completion_contract_valid",
                    side_effect=[
                        (False, "completion_marker_unreadable"),
                        (False, "completion_marker_model_load"),
                    ],
                ),
                mock.patch.object(
                    PUBLIC_SEARCH.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=1,
                        stdout=b'{"setup_complete":false}\n',
                        stderr=b"sensitive internal diagnostic\n",
                    ),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                repaired = PUBLIC_SEARCH._self_heal_lookup_gate(
                    ["--db", "example-rag", "question"]
                )

            self.assertFalse(repaired)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())

    def test_concurrent_success_is_accepted_even_if_our_repair_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rag_root, query_root, _python, _marker = self._roots(temporary)
            with (
                mock.patch.object(PUBLIC_SEARCH, "RAG_ROOT", rag_root),
                mock.patch.object(PUBLIC_SEARCH, "_QUERY_ROOT", query_root),
                mock.patch.object(
                    PUBLIC_SEARCH,
                    "completion_contract_valid",
                    side_effect=[
                        (False, "completion_marker_requirements"),
                        (True, None),
                    ],
                ),
                mock.patch.object(
                    PUBLIC_SEARCH.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=1,
                        stdout=b"",
                        stderr=b"",
                    ),
                ),
            ):
                repaired = PUBLIC_SEARCH._self_heal_lookup_gate(
                    ["--db", "example-rag", "question"]
                )
            self.assertTrue(repaired)


if __name__ == "__main__":
    unittest.main()
