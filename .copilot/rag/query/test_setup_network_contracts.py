from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(QUERY_ROOT))
sys.path.insert(0, str(TOOL_ROOT))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SETUP = _load("rag_setup_contract", QUERY_ROOT / "setup.py")
SETUP_VERIFICATION = _load(
    "rag_setup_verification_contract",
    QUERY_ROOT / "setup_verification.py",
)
PROXY_CLIENT = _load("rag_proxy_client_contract", QUERY_ROOT / "proxy_client.py")
SEARCH = _load("rag_search_network_contract", QUERY_ROOT / "search.py")
RAGD = _load("rag_daemon_security_contract", QUERY_ROOT / "ragd.py")

from software_rag_tool.network import NetworkResolution
from setup_contract import (
    completion_contract_payload,
    completion_contract_valid,
    requirements_fingerprint,
)


class SetupNetworkContractTests(unittest.TestCase):
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
            "databases": {"healthy": ["ac-rag"], "unhealthy": []},
            "warnings": [],
            "next_action": None,
        }

    @classmethod
    def _write_current_valid_marker(cls, marker: Path) -> None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                completion_contract_payload(
                    runtime=cls._complete_verification()["runtime"],
                    rag_root=SETUP.RAG_ROOT,
                    verified_at="2026-07-27T00:00:00+00:00",
                )
            ),
            encoding="utf-8",
        )
        assert completion_contract_valid(marker, SETUP.RAG_ROOT)[0]

    def test_completion_contract_rejects_corruption_and_stale_requirements(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rag_root = Path(temporary)
            (rag_root / "query").mkdir()
            (
                rag_root / "gen_db" / "software_rag_tool"
            ).mkdir(parents=True)
            (rag_root / "query" / "requirements.txt").write_text(
                "-r ../gen_db/software_rag_tool/requirements.txt\n",
                encoding="utf-8",
            )
            nested = (
                rag_root
                / "gen_db"
                / "software_rag_tool"
                / "requirements.txt"
            )
            nested.write_text("chromadb>=1\n", encoding="utf-8")
            runtime = {
                "venv": "pass",
                "dependencies": "pass",
                "requirements": "pass",
                "pip_check": "pass",
                "model_files": "pass",
                "model_manifest": "pass",
                "model_load": "pass",
                "embedding_dimension": 256,
                "list_dbs": "pass",
            }
            marker = rag_root / "marker.json"
            marker.write_text(
                json.dumps(
                    completion_contract_payload(
                        runtime=runtime,
                        rag_root=rag_root,
                        verified_at="2026-07-27T00:00:00+00:00",
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                (True, None),
                completion_contract_valid(marker, rag_root),
            )
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["runtime"].pop("requirements")
            marker.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                (False, "completion_marker_requirements"),
                completion_contract_valid(marker, rag_root),
            )
            payload["runtime"]["requirements"] = "pass"
            marker.write_text(json.dumps(payload), encoding="utf-8")
            nested.write_text("chromadb>=2\n", encoding="utf-8")
            valid, reason = completion_contract_valid(marker, rag_root)
            self.assertFalse(valid)
            self.assertEqual("completion_marker_requirements", reason)
            marker.write_text("not json", encoding="utf-8")
            self.assertFalse(
                completion_contract_valid(marker, rag_root)[0]
            )

    def test_recursive_pep508_requirements_are_checked_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested.txt"
            nested.write_text(
                (
                    'Demo-Pkg[feature]>=1.5; python_version >= "3.0"\n'
                    'Skipped-Pkg>=999; python_version < "1.0"\n'
                ),
                encoding="utf-8",
            )
            main = root / "requirements.txt"
            main.write_text(
                "-r nested.txt\n--requirement=nested.txt\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                SETUP_VERIFICATION.importlib.metadata,
                "version",
                return_value="1.7",
            ) as version:
                checked = (
                    SETUP_VERIFICATION._verify_declared_requirements(
                        (main,),
                    )
                )
            self.assertEqual(1, checked)
            version.assert_called_once_with("Demo-Pkg")

    def test_requirements_check_rejects_unsatisfied_specifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            requirements = Path(temporary) / "requirements.txt"
            requirements.write_text(
                "Demo-Pkg>=2.0\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    SETUP_VERIFICATION.importlib.metadata,
                    "version",
                    return_value="1.9",
                ),
                self.assertRaisesRegex(RuntimeError, "does not satisfy"),
            ):
                SETUP_VERIFICATION._verify_declared_requirements(
                    (requirements,),
                )

    def test_verification_reports_requirements_failure_before_pip_check(
        self,
    ) -> None:
        with (
            mock.patch.object(
                SETUP_VERIFICATION.importlib,
                "import_module",
            ),
            mock.patch.object(
                SETUP_VERIFICATION,
                "_verify_declared_requirements",
                side_effect=RuntimeError("version mismatch"),
            ),
            mock.patch.object(
                SETUP_VERIFICATION.subprocess,
                "run",
                side_effect=AssertionError("pip check must not run"),
            ),
        ):
            payload = SETUP_VERIFICATION.verify_installation()
        self.assertFalse(payload["setup_complete"])
        self.assertEqual("requirements", payload["failed_check"])
        self.assertEqual("fail", payload["runtime"]["requirements"])

    def test_verify_only_is_json_pure_and_does_not_mutate_or_run_installers(
        self,
    ) -> None:
        network = self._network(selected_route="not_required")
        verification = {
            "status": "runtime_ready_no_db",
            "setup_complete": True,
            "lookup_ready": False,
            "runtime": {"venv": "pass"},
            "databases": {"healthy": [], "unhealthy": []},
            "warnings": [],
            "next_action": "Copy an existing database or build a new database.",
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["setup.py", "--verify-only", "--format", "json"],
            ),
            mock.patch.object(
                SETUP,
                "resolve_network_configuration",
                return_value=network,
            ) as resolve,
            mock.patch.object(
                SETUP,
                "_run_verification",
                return_value=verification,
            ),
            mock.patch.object(
                SETUP,
                "_run_child",
                side_effect=AssertionError("installer must not run"),
            ),
            mock.patch.object(
                SETUP,
                "_write_completion_marker",
                side_effect=AssertionError("marker must not be modified"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = SETUP.main()
        self.assertEqual(0, code)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["setup_complete"])
        self.assertEqual("not_required", payload["network"]["selected_route"])
        self.assertFalse(
            resolve.call_args.kwargs["external_operation"]
        )

    def test_refresh_completion_marker_is_offline_atomic_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            marker.write_text('{"stale":true}\n', encoding="utf-8")
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--refresh-completion-marker",
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
                    return_value=self._network(
                        selected_route="not_required"
                    ),
                ) as resolve,
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                mock.patch.object(
                    SETUP,
                    "_run_child",
                    side_effect=AssertionError("installer must not run"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = SETUP.main()
            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())
            marker_result = payload["completion_marker"]
            self.assertEqual("refreshed", marker_result["action"])
            self.assertTrue(marker_result["refreshed"])
            self.assertTrue(marker_result["valid"])
            self.assertEqual(
                requirements_fingerprint(SETUP.RAG_ROOT),
                marker_result["requirements_sha256"],
            )
            self.assertTrue(
                completion_contract_valid(marker, SETUP.RAG_ROOT)[0]
            )
            self.assertFalse(
                resolve.call_args.kwargs["external_operation"]
            )

    def test_refresh_failure_invalidates_existing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            self._write_current_valid_marker(marker)
            verification = {
                "status": "error",
                "setup_complete": False,
                "lookup_ready": False,
                "runtime": {"venv": "pass", "model_load": "fail"},
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
                        "--refresh-completion-marker",
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
                    return_value=self._network(
                        selected_route="not_required"
                    ),
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=verification,
                ),
                mock.patch.object(
                    SETUP,
                    "_write_completion_marker",
                    side_effect=AssertionError("marker must not be written"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = SETUP.main()
            self.assertEqual(1, code)
            self.assertFalse(marker.exists())

    def test_refresh_rejects_requirements_change_during_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            self._write_current_valid_marker(marker)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--refresh-completion-marker",
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
                    return_value=self._network(
                        selected_route="not_required"
                    ),
                ),
                mock.patch.object(
                    SETUP,
                    "requirements_fingerprint",
                    side_effect=["before", "after"],
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                mock.patch.object(
                    SETUP,
                    "_write_completion_marker",
                    side_effect=AssertionError("marker must not be written"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = SETUP.main()
            self.assertEqual(1, code)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                "requirements_changed_during_verification",
                payload["error_kind"],
            )
            self.assertFalse(marker.exists())

    def test_refresh_removes_marker_when_postvalidation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            self._write_current_valid_marker(marker)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--refresh-completion-marker",
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
                    return_value=self._network(
                        selected_route="not_required"
                    ),
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                mock.patch.object(
                    SETUP,
                    "completion_contract_valid",
                    return_value=(False, "completion_marker_model_load"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = SETUP.main()
            self.assertEqual(1, code)
            self.assertFalse(marker.exists())
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["setup_complete"])
            self.assertEqual(
                "completion_marker_postvalidation_failed",
                payload["error_kind"],
            )

    def test_refresh_write_failure_leaves_lookup_gate_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            self._write_current_valid_marker(marker)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--refresh-completion-marker",
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
                    return_value=self._network(
                        selected_route="not_required"
                    ),
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                mock.patch.object(
                    SETUP,
                    "_write_completion_marker",
                    side_effect=PermissionError("write denied"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = SETUP.main()
            self.assertEqual(1, code)
            self.assertFalse(marker.exists())
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["setup_complete"])
            self.assertEqual(
                "completion_marker_write_failed",
                payload["error_kind"],
            )

    def test_refresh_rejects_final_fingerprint_change_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / ".venv"
            python = venv / "bin" / "python"
            marker = venv / ".rag-deps-installed"
            python.parent.mkdir(parents=True)
            python.touch()
            self._write_current_valid_marker(marker)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup.py",
                        "--refresh-completion-marker",
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
                    return_value=self._network(
                        selected_route="not_required"
                    ),
                ),
                mock.patch.object(
                    SETUP,
                    "requirements_fingerprint",
                    side_effect=["same", "same", "changed"],
                ),
                mock.patch.object(
                    SETUP,
                    "_run_verification",
                    return_value=self._complete_verification(),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = SETUP.main()
            self.assertEqual(1, code)
            self.assertFalse(marker.exists())
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["setup_complete"])
            self.assertEqual(
                "requirements_changed_during_verification",
                payload["error_kind"],
            )

    def test_atomic_marker_replace_failure_preserves_old_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / ".rag-deps-installed"
            self._write_current_valid_marker(marker)
            original = marker.read_bytes()
            with (
                mock.patch.object(
                    SETUP.os,
                    "replace",
                    side_effect=PermissionError("replace denied"),
                ),
                self.assertRaises(PermissionError),
            ):
                SETUP._write_completion_marker(
                    marker,
                    self._complete_verification(),
                )
            self.assertEqual(original, marker.read_bytes())
            self.assertEqual(
                [],
                list(marker.parent.glob(f".{marker.name}.*")),
            )

    def test_legacy_marker_migration_is_offline_and_runs_no_installer(
        self,
    ) -> None:
        verification = {
            "status": "ready",
            "setup_complete": True,
            "lookup_ready": True,
            "runtime": {"embedding_dimension": 256},
            "databases": {"healthy": ["ac-rag"], "unhealthy": []},
            "warnings": [],
            "next_action": None,
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "setup.py",
                    "--migrate-legacy-marker",
                    "--format",
                    "json",
                ],
            ),
            mock.patch.object(
                SETUP,
                "resolve_network_configuration",
                return_value=self._network(selected_route="not_required"),
            ) as resolve,
            mock.patch.object(
                SETUP,
                "_is_legacy_completion_marker",
                return_value=True,
            ),
            mock.patch.object(
                SETUP,
                "_run_verification",
                return_value=verification,
            ),
            mock.patch.object(
                SETUP,
                "_run_child",
                side_effect=AssertionError("installer must not run"),
            ),
            mock.patch.object(SETUP, "_write_completion_marker") as write,
            mock.patch.object(
                SETUP, "completion_contract_valid", return_value=(True, None)
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = SETUP.main()
        self.assertEqual(0, code)
        self.assertTrue(json.loads(stdout.getvalue())["setup_complete"])
        self.assertFalse(resolve.call_args.kwargs["external_operation"])
        write.assert_called_once()

    def test_setup_resolves_once_and_never_places_proxy_in_argv(self) -> None:
        secret_proxy = "http://user:secret@proxy.example:8080"
        environment = {
            **os.environ,
            "HTTP_PROXY": secret_proxy,
            "HTTPS_PROXY": secret_proxy,
            "RAG_NETWORK_ROUTE_RESOLVED": "1",
            "RAG_NETWORK_ROUTE_METADATA": "{}",
        }
        network = NetworkResolution(
            environment=environment,
            details={"selected_route": "proxy"},
            warnings=[],
            _proxy_url=secret_proxy,
        )
        calls: list[tuple[list[str], dict[str, str], str]] = []

        def record(
            command: list[str],
            *,
            env: dict[str, str],
            phase: str,
        ) -> None:
            calls.append((command, env, phase))

        verification = {
            "status": "ready",
            "setup_complete": True,
            "lookup_ready": True,
            "runtime": {},
            "databases": {"healthy": ["ac-rag"], "unhealthy": []},
            "warnings": [],
            "next_action": None,
        }
        with (
            mock.patch.object(
                sys,
                "argv",
                ["setup.py", "--format", "json"],
            ),
            mock.patch.object(
                SETUP,
                "resolve_network_configuration",
                return_value=network,
            ) as resolve,
            mock.patch.object(SETUP, "_run_child", side_effect=record),
            mock.patch.object(SETUP, "_invalidate_completion_marker"),
            mock.patch.object(
                SETUP,
                "_run_verification",
                return_value=verification,
            ),
            mock.patch.object(SETUP, "_write_completion_marker"),
            mock.patch.object(
                SETUP, "completion_contract_valid", return_value=(True, None)
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, SETUP.main())
        self.assertEqual(1, resolve.call_count)
        self.assertGreaterEqual(len(calls), 3)
        for command, child_env, phase in calls:
            self.assertNotIn("secret", " ".join(command))
            if phase in {"pip_upgrade", "dependencies", "model_prepare"}:
                self.assertEqual(secret_proxy, child_env["HTTPS_PROXY"])

    def test_child_failure_redacts_proxy_credentials(self) -> None:
        proxy = "http://user:secret@proxy.example:8080"
        completed = subprocess.CompletedProcess(
            args=["pip"],
            returncode=1,
            stdout="",
            stderr=f"failed via {proxy}",
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(
                SETUP.subprocess,
                "run",
                return_value=completed,
            ),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SETUP.SetupStepError) as raised,
        ):
            SETUP._run_child(
                ["pip"],
                env={"HTTPS_PROXY": proxy},
                phase="dependencies",
            )
        combined = f"{stderr.getvalue()} {raised.exception}"
        self.assertNotIn("user", combined)
        self.assertNotIn("secret", combined)
        self.assertIn("***:***", combined)

    def test_child_spawn_error_becomes_sanitized_setup_step_error(self) -> None:
        with (
            mock.patch.object(
                SETUP.subprocess,
                "run",
                side_effect=OSError("spawn failed"),
            ),
            self.assertRaises(SETUP.SetupStepError) as raised,
        ):
            SETUP._run_child(
                ["missing"],
                env={},
                phase="dependencies",
            )
        self.assertEqual("dependencies", raised.exception.phase)
        self.assertNotIn("Traceback", str(raised.exception))

    def test_verification_timeout_returns_json_error_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            python = Path(temporary) / "python"
            python.touch()
            with mock.patch.object(
                SETUP.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["python"], 120),
            ):
                payload = SETUP._run_verification(python)
        self.assertFalse(payload["setup_complete"])
        self.assertEqual("verification_timeout", payload["error_kind"])

    def test_marker_write_failure_keeps_setup_json_pure(self) -> None:
        verification = {
            "status": "ready",
            "setup_complete": True,
            "lookup_ready": True,
            "runtime": {},
            "databases": {"healthy": ["ac-rag"], "unhealthy": []},
            "warnings": [],
            "next_action": None,
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["setup.py", "--format", "json"],
            ),
            mock.patch.object(
                SETUP,
                "resolve_network_configuration",
                return_value=self._network(selected_route="direct"),
            ),
            mock.patch.object(SETUP, "_run_child"),
            mock.patch.object(SETUP, "_invalidate_completion_marker"),
            mock.patch.object(
                SETUP,
                "_run_verification",
                return_value=verification,
            ),
            mock.patch.object(
                SETUP,
                "_write_completion_marker",
                side_effect=PermissionError("write denied"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = SETUP.main()
        self.assertEqual(1, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            "completion_marker_write_failed",
            payload["error_kind"],
        )

    def test_database_health_check_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dbs_root = Path(temporary)
            self._create_healthy_db(dbs_root, catalog_count=1, chroma_count=1)
            before = self._file_snapshot(dbs_root)
            with mock.patch.object(
                SETUP_VERIFICATION,
                "DBS_ROOT",
                dbs_root,
            ):
                SETUP_VERIFICATION._verify_database("test-rag")
            self.assertEqual(before, self._file_snapshot(dbs_root))
            self.assertFalse(
                list(dbs_root.rglob("*-wal")),
            )
            self.assertFalse(
                list(dbs_root.rglob("*-shm")),
            )

    def test_database_health_check_rejects_count_inconsistency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dbs_root = Path(temporary)
            self._create_healthy_db(dbs_root, catalog_count=2, chroma_count=1)
            with (
                mock.patch.object(
                    SETUP_VERIFICATION,
                    "DBS_ROOT",
                    dbs_root,
                ),
                self.assertRaisesRegex(ValueError, "count inconsistency"),
            ):
                SETUP_VERIFICATION._verify_database("test-rag")

    def test_database_health_check_rejects_collection_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dbs_root = Path(temporary)
            self._create_healthy_db(
                dbs_root,
                catalog_count=1,
                chroma_count=1,
            )
            (dbs_root / "test-rag" / "db.json").write_text(
                json.dumps({"collection": "wrong_collection"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    SETUP_VERIFICATION,
                    "DBS_ROOT",
                    dbs_root,
                ),
                self.assertRaisesRegex(ValueError, "collection mismatch"),
            ):
                SETUP_VERIFICATION._verify_database("test-rag")

    def test_database_health_check_rejects_embedding_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dbs_root = Path(temporary)
            self._create_healthy_db(
                dbs_root,
                catalog_count=1,
                chroma_count=1,
            )
            version_path = dbs_root / "test-rag" / "VERSION.json"
            version = json.loads(version_path.read_text(encoding="utf-8"))
            version["embedding"]["embedding_dimension"] = 128
            version_path.write_text(
                json.dumps(version),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    SETUP_VERIFICATION,
                    "DBS_ROOT",
                    dbs_root,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "embedding fingerprint mismatch",
                ),
            ):
                SETUP_VERIFICATION._verify_database("test-rag")

    @staticmethod
    def _network(*, selected_route: str) -> NetworkResolution:
        return NetworkResolution(
            environment={},
            details={
                "selected_route": selected_route,
                "no_proxy_includes_localhost": True,
            },
            warnings=[],
        )

    @staticmethod
    def _create_healthy_db(
        dbs_root: Path,
        *,
        catalog_count: int,
        chroma_count: int,
    ) -> None:
        from software_rag_tool.embeddings import embedding_fingerprint
        from software_rag_tool.tokenize import (
            tokenizer_fingerprint,
            tokenizer_runtime_descriptor,
        )

        root = dbs_root / "test-rag"
        chroma = root / "index" / "chroma"
        chroma.mkdir(parents=True)
        collection = "test_rag_ruri3_30m_int8_v1"
        (root / "db.json").write_text(
            json.dumps({"collection": collection}),
            encoding="utf-8",
        )
        (root / "VERSION.json").write_text(
            json.dumps(
                {
                    "schema": "local-rag.db-version.v1",
                    "collection": collection,
                    "embedding": embedding_fingerprint(),
                    "tokenizer": tokenizer_fingerprint(),
                    "tokenizer_config": tokenizer_runtime_descriptor(),
                }
            ),
            encoding="utf-8",
        )
        (root / "index" / "manifest.json").write_text(
            json.dumps(
                {
                    "collection": collection,
                    "catalog_schema_version": 2,
                    "tokenizer": tokenizer_fingerprint(),
                    "tokenizer_config": tokenizer_runtime_descriptor(),
                    "record_count": chroma_count,
                    **embedding_fingerprint(),
                }
            ),
            encoding="utf-8",
        )
        catalog = sqlite3.connect(root / "catalog.sqlite")
        catalog.execute("CREATE TABLE database_meta (key TEXT, value TEXT)")
        catalog.executemany(
            "INSERT INTO database_meta VALUES (?, ?)",
            [
                ("schema_version", "2"),
                ("tokenizer", tokenizer_fingerprint()),
            ],
        )
        catalog.execute("CREATE TABLE chunk (id INTEGER)")
        catalog.executemany(
            "INSERT INTO chunk VALUES (?)",
            [(index,) for index in range(catalog_count)],
        )
        catalog.commit()
        catalog.close()

        chroma_db = sqlite3.connect(chroma / "chroma.sqlite3")
        chroma_db.execute(
            "CREATE TABLE collections "
            "(id TEXT, name TEXT, dimension INTEGER)"
        )
        chroma_db.execute(
            "INSERT INTO collections VALUES (?, ?, ?)",
            ("collection-id", collection, 256),
        )
        chroma_db.execute(
            "CREATE TABLE segments (id TEXT, collection TEXT)"
        )
        chroma_db.execute(
            "INSERT INTO segments VALUES (?, ?)",
            ("segment-id", "collection-id"),
        )
        chroma_db.execute(
            "CREATE TABLE embeddings (id INTEGER, segment_id TEXT)"
        )
        chroma_db.executemany(
            "INSERT INTO embeddings VALUES (?, ?)",
            [(index, "segment-id") for index in range(chroma_count)],
        )
        chroma_db.commit()
        chroma_db.close()

    @staticmethod
    def _file_snapshot(root: Path) -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(root)): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
            if path.is_file()
        }


class ExternalAndLocalNetworkContractTests(unittest.TestCase):
    def test_daemon_state_token_is_owner_only_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission contract")
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "run"
            state = runtime / "ragd.json"
            RAGD._write_json_atomic(state, {"token": "daemon-secret"})
            self.assertEqual(
                0o700,
                stat.S_IMODE(runtime.stat().st_mode),
            )
            self.assertEqual(
                0o600,
                stat.S_IMODE(state.stat().st_mode),
            )

    def test_proxy_client_direct_route_uses_one_local_http_request(self) -> None:
        calls: list[bytes] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                calls.append(self.rfile.read(length))
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        stdout = io.StringIO()
        try:
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "proxy_client.py",
                        "--url",
                        f"http://127.0.0.1:{port}/rag",
                        "--db",
                        "ac-rag",
                        "--ignore-network-config",
                        "question",
                    ],
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(0, PROXY_CLIENT.main())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(1, len(calls))
        self.assertEqual({"status": "ok"}, json.loads(stdout.getvalue()))

    def test_proxy_http_407_does_not_retry_direct(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://service.example",
            407,
            "Proxy Authentication Required",
            {},
            None,
        )
        network = self._network_with_opener(opener)
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "proxy_client.py",
                    "--url",
                    "https://service.example",
                    "--db",
                    "ac-rag",
                    "question",
                ],
            ),
            mock.patch.object(
                PROXY_CLIENT,
                "resolve_network_configuration",
                return_value=network,
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, PROXY_CLIENT.main())
        self.assertEqual(1, opener.open.call_count)

    def test_proxy_tls_failure_does_not_retry_direct(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verify failed")
        )
        network = self._network_with_opener(opener)
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "proxy_client.py",
                    "--url",
                    "https://service.example",
                    "--db",
                    "ac-rag",
                    "question",
                ],
            ),
            mock.patch.object(
                PROXY_CLIENT,
                "resolve_network_configuration",
                return_value=network,
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, PROXY_CLIENT.main())
        self.assertEqual(1, opener.open.call_count)

    def test_local_search_modules_do_not_load_persistent_network_config(
        self,
    ) -> None:
        for name in ("search.py", "list_dbs.py", "ragd.py"):
            source = (QUERY_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("resolve_network_configuration", source)
            self.assertNotIn("network.json", source)

    def test_local_daemon_opener_has_no_proxy_handler_entries(self) -> None:
        handlers = [
            handler
            for handler in SEARCH._LOCAL_HTTP_OPENER.handlers
            if isinstance(handler, __import__("urllib.request").request.ProxyHandler)
        ]
        self.assertEqual([], handlers)

    def test_non_loopback_daemon_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "ragd.json"
            state.write_text(
                json.dumps(
                    {
                        "schema": "local-rag.ragd.v2",
                        "token": "secret-token",
                        "generation": "generation",
                        "transport": "tcp",
                        "host": "example.com",
                        "port": 1234,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(SEARCH, "STATE_FILE", state):
                self.assertIsNone(SEARCH._read_state())

    @staticmethod
    def _network_with_opener(opener: mock.Mock) -> mock.Mock:
        network = mock.Mock()
        network.build_url_opener.return_value = opener
        return network


if __name__ == "__main__":
    unittest.main()
