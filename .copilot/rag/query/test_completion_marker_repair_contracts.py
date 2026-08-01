from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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


SETUP = _load("completion_marker_repair_setup", QUERY_ROOT / "setup.py")
PUBLIC_SEARCH = _load("completion_marker_repair_search", RAG_ROOT / "search.py")


class CompletionMarkerRepairContractTests(unittest.TestCase):
    @staticmethod
    def _network() -> SimpleNamespace:
        return SimpleNamespace(
            environment={},
            details={"selected_route": "not_required"},
            warnings=[],
        )

    @staticmethod
    def _complete_verification() -> dict[str, object]:
        return {
            "status": "ready",
            "setup_complete": True,
            "lookup_ready": True,
            "runtime": {
                "venv": "pass",
                "dependencies": "pass",
                "requirements": "pass",
                "pip_check": "pass",
                "model_files": "pass",
                "model_manifest": "pass",
                "model_load": "pass",
                "embedding_dimension": 256,
                "list_dbs": "pass",
            },
            "databases": {"healthy": ["example-rag"], "unhealthy": []},
            "warnings": [],
            "next_action": None,
        }

    def test_verify_only_reports_marker_reason_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            marker.write_text('{"stale":true}\n', encoding="utf-8")
            before = marker.read_bytes()
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["setup.py", "--verify-only", "--format", "json"],
                ),
                mock.patch.object(
                    SETUP,
                    "_setup_paths",
                    return_value=(root, venv, python, marker),
                ),
                mock.patch.object(
                    SETUP,
                    "resolve_network_configuration",
                    return_value=self._network(),
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                mock.patch.object(
                    SETUP,
                    "completion_contract_valid",
                    return_value=(False, "completion_marker_requirements"),
                ),
                mock.patch.object(
                    SETUP,
                    "_write_completion_marker",
                    side_effect=AssertionError("verify-only must not write"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = SETUP.main()
            self.assertEqual(0, code)
            self.assertEqual(before, marker.read_bytes())
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["setup_complete"])
            self.assertFalse(payload["lookup_ready"])
            self.assertEqual(
                "runtime_ready_completion_marker_repair_required",
                payload["status"],
            )
            marker_result = payload["completion_marker"]
            self.assertFalse(marker_result["valid"])
            self.assertEqual(
                "completion_marker_requirements",
                marker_result["reason"],
            )
            self.assertEqual(
                "repair_completion_marker_temporarily",
                marker_result["repair_action"],
            )
            self.assertIn("一時的", marker_result["repair_label_ja"])

    def test_temporary_repair_preserves_old_marker_when_verification_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            old = b'{"old":true}\n'
            marker.write_bytes(old)
            failure = {
                "status": "error",
                "setup_complete": False,
                "lookup_ready": False,
                "runtime": {"model_load": "fail"},
                "databases": {"healthy": [], "unhealthy": []},
                "warnings": [],
                "failed_check": "model_load",
                "error_kind": "RuntimeError",
                "next_action": "Run setup again.",
            }
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--repair-completion-marker",
                        "--format",
                        "json",
                    ],
                ),
                mock.patch.object(
                    SETUP,
                    "_setup_paths",
                    return_value=(root, venv, python, marker),
                ),
                mock.patch.object(
                    SETUP,
                    "resolve_network_configuration",
                    return_value=self._network(),
                ),
                mock.patch.object(
                    SETUP,
                    "requirements_fingerprint",
                    return_value="stable",
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=failure,
                ),
                mock.patch.object(
                    SETUP,
                    "_write_completion_marker",
                    side_effect=AssertionError("failed verification must not write"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = SETUP.main()
            self.assertEqual(1, code)
            self.assertEqual(old, marker.read_bytes())

    def test_temporary_repair_is_atomic_and_reports_temporary_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            marker.write_bytes(b'{"old":true}\n')
            stdout = io.StringIO()

            def write_marker(path: Path, *_args, **_kwargs) -> None:
                path.write_bytes(b'{"new":true}\n')

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--repair-completion-marker",
                        "--format",
                        "json",
                    ],
                ),
                mock.patch.object(
                    SETUP,
                    "_setup_paths",
                    return_value=(root, venv, python, marker),
                ),
                mock.patch.object(
                    SETUP,
                    "resolve_network_configuration",
                    return_value=self._network(),
                ),
                mock.patch.object(
                    SETUP,
                    "requirements_fingerprint",
                    side_effect=["stable", "stable", "stable"],
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                mock.patch.object(
                    SETUP,
                    "_write_completion_marker",
                    side_effect=write_marker,
                ),
                mock.patch.object(
                    SETUP,
                    "completion_contract_valid",
                    return_value=(True, None),
                ),
                mock.patch.object(
                    SETUP,
                    "_invalidate_completion_marker",
                    side_effect=AssertionError(
                        "temporary repair must not remove the old marker first"
                    ),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = SETUP.main()
            self.assertEqual(0, code)
            self.assertEqual(b'{"new":true}\n', marker.read_bytes())
            payload = json.loads(stdout.getvalue())
            marker_result = payload["completion_marker"]
            self.assertEqual("repaired_temporarily", marker_result["action"])
            self.assertTrue(marker_result["temporary"])
            self.assertIn("一時的", marker_result["label_ja"])

    def test_failed_postvalidation_restores_previous_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            old = b'{"old":true}\n'
            marker.write_bytes(old)

            def write_marker(path: Path, *_args, **_kwargs) -> None:
                path.write_bytes(b'{"new":true}\n')

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--repair-completion-marker",
                        "--format",
                        "json",
                    ],
                ),
                mock.patch.object(
                    SETUP,
                    "_setup_paths",
                    return_value=(root, venv, python, marker),
                ),
                mock.patch.object(
                    SETUP,
                    "resolve_network_configuration",
                    return_value=self._network(),
                ),
                mock.patch.object(
                    SETUP,
                    "requirements_fingerprint",
                    side_effect=["stable", "stable", "stable"],
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                mock.patch.object(
                    SETUP,
                    "_write_completion_marker",
                    side_effect=write_marker,
                ),
                mock.patch.object(
                    SETUP,
                    "completion_contract_valid",
                    return_value=(False, "completion_marker_model_load"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = SETUP.main()
            self.assertEqual(1, code)
            self.assertEqual(old, marker.read_bytes())

    def test_public_search_projects_reason_and_temporary_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            query_root = Path(temporary) / "query"
            python = query_root / ".venv" / (
                "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
            )
            python.parent.mkdir(parents=True)
            python.touch()
            with (
                mock.patch.object(PUBLIC_SEARCH, "_QUERY_ROOT", query_root),
                mock.patch.object(
                    PUBLIC_SEARCH,
                    "completion_contract_valid",
                    return_value=(False, "completion_marker_requirements"),
                ),
            ):
                payload = PUBLIC_SEARCH._setup_gate_projection(
                    {
                        "status": "setup_required",
                        "required_action": "initial_setup",
                    }
                )
            self.assertEqual(
                "repair_completion_marker_temporarily",
                payload["required_action"],
            )
            gate = payload["setup_gate"]
            self.assertEqual(
                "completion_marker_requirements",
                gate["completion_marker_reason"],
            )
            self.assertTrue(gate["repair_available"])
            self.assertIn("一時的", gate["repair_label_ja"])
            self.assertNotIn(str(query_root), json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
