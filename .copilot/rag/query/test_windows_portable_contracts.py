from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from portable_runtime import (
    MANIFEST_SCHEMA,
    PortableRuntimeError,
    load_and_verify_runtime,
)
from setup_contract import completion_marker_for
_SETUP_SPEC = importlib.util.spec_from_file_location(
    "windows_portable_query_setup", Path(__file__).with_name("setup.py")
)
SETUP = importlib.util.module_from_spec(_SETUP_SPEC)
_SETUP_SPEC.loader.exec_module(SETUP)



class PortableRuntimeContractTests(unittest.TestCase):
    @staticmethod
    def _pe_amd64() -> bytes:
        payload = bytearray(128)
        payload[:2] = b"MZ"
        struct.pack_into("<I", payload, 0x3C, 64)
        payload[64:68] = b"PE\0\0"
        struct.pack_into("<H", payload, 68, 0x8664)
        return bytes(payload)


    def _fixture(self, root: Path, *, profile: str = "search-only") -> Path:
        query = root / "query"
        runtime = query / ".venv"
        scripts = runtime / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        files = {
            "Scripts/python.exe": self._pe_amd64(),
            "Scripts/python313.dll": self._pe_amd64(),
            "Scripts/python313._pth": b"python313.zip\nLib/site-packages\n",
            "Scripts/python313.zip": b"demo-stdlib",
            "Lib/site-packages/demo.py": b"VALUE = 1\n",
            "Lib/site-packages/demo-1.0.dist-info/METADATA": (
                b"Name: demo\nVersion: 1.0\n"
            ),
        }
        for relative, content in files.items():
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "product_version": "1.0.1",
            "profile": profile,
            "platform": {"os": "windows", "arch": "amd64"},
            "python": {"version": "3.13.5", "executable": "Scripts/python.exe"},
            "dependency_lock_sha256": "a" * 64,
            "model_fingerprint": "b" * 64,
            "distributions": [{"name": "demo", "version": "1.0"}],
            "files": [
                {
                    "path": relative,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for relative, content in sorted(files.items())
            ],
        }
        path = query / ".packaged-runtime.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    @staticmethod
    def _requirements_fixture(root: Path) -> None:
        paths = (
            root / "query" / "requirements.txt",
            root
            / "gen_db"
            / "software_rag_tool"
            / "requirements.txt",
        )
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("demo==1.0\n", encoding="utf-8")

    def test_completion_marker_resolver_separates_packaged_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query = root / "query"
            query.mkdir(parents=True)
            self.assertEqual(query / ".venv" / ".rag-deps-installed", completion_marker_for(query))
            (query / ".packaged-runtime.json").write_text("{}", encoding="utf-8")
            self.assertEqual(query / ".rag-deps-installed", completion_marker_for(query))

    def test_valid_manifest_is_verified_without_network_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._fixture(root)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with mock.patch("portable_runtime.platform.system", return_value="Windows"), mock.patch(
                "portable_runtime.platform.machine", return_value="AMD64"
            ):
                result = load_and_verify_runtime(manifest)
            self.assertEqual("search-only", result.profile)
            self.assertEqual("3.13.5", result.python_version)
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_traversal_duplicate_and_unexpected_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._fixture(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["files"].append(dict(payload["files"][0]))
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(PortableRuntimeError, "duplicate"):
                load_and_verify_runtime(path, check_platform=False)

            payload["files"][-1]["path"] = "../escape.exe"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(PortableRuntimeError, "unsafe"):
                load_and_verify_runtime(path, check_platform=False)

    def test_tampered_file_and_unsupported_profile_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._fixture(root)
            (root / "query" / ".venv" / "Scripts" / "python.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(PortableRuntimeError, "size|SHA-256"):
                load_and_verify_runtime(path, check_platform=False)

            path = self._fixture(root, profile="unknown")
            with self.assertRaisesRegex(PortableRuntimeError, "profile"):
                load_and_verify_runtime(path, check_platform=False)

    def test_normal_packaged_setup_never_resolves_network_or_runs_pip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._fixture(root)
            query = manifest.parent
            venv = query / ".venv"
            python = venv / "Scripts" / "python.exe"
            marker = venv / ".rag-deps-installed"
            verification = {
                "status": "runtime_ready_no_db",
                "setup_complete": True,
                "lookup_ready": False,
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
                    "packaged_runtime": "pass",
                },
                "databases": {"healthy": [], "unhealthy": []},
                "warnings": [],
                "next_action": None,
            }
            output = io.StringIO()
            try:
                with (
                    mock.patch.object(sys, "argv", ["setup.py", "--format", "json"]),
                    mock.patch.object(
                        SETUP, "_setup_paths", return_value=(query, venv, python, marker)
                    ),
                    mock.patch(
                        "portable_runtime.platform.system", return_value="Windows"
                    ),
                    mock.patch(
                        "portable_runtime.platform.machine", return_value="AMD64"
                    ),
                    mock.patch.object(SETUP, "resolve_network_configuration") as network,
                    mock.patch.object(SETUP, "_run_child") as run_child,
                    mock.patch.object(
                        SETUP, "_run_verification", return_value=verification
                    ),
                    mock.patch.object(SETUP, "_write_completion_marker"),
                    mock.patch.object(
                        SETUP, "completion_contract_valid", return_value=(True, None)
                    ),
                    mock.patch.dict(os.environ, {"APPDATA": ""}),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(0, SETUP.main())
                network.assert_not_called()
                run_child.assert_not_called()
                payload = json.loads(output.getvalue())

                self.assertTrue(payload["setup_complete"])
                self.assertEqual("off", payload["network"]["mode"])
            finally:
                SETUP._release_setup_lock()

    def test_packaged_setup_writes_state_outside_runtime_and_reverifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._fixture(root)
            self._requirements_fixture(root)
            query = manifest.parent
            venv = query / ".venv"
            python = venv / "Scripts" / "python.exe"
            marker = completion_marker_for(query)
            verification = {
                "status": "runtime_ready_no_db",
                "setup_complete": True,
                "lookup_ready": False,
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
                    "packaged_runtime": "pass",
                },
                "databases": {"healthy": [], "unhealthy": []},
                "warnings": [],
                "next_action": None,
            }
            output = io.StringIO()
            try:
                with (
                    mock.patch.object(sys, "argv", ["setup.py", "--format", "json"]),
                    mock.patch.object(SETUP, "RAG_ROOT", root),
                    mock.patch.object(
                        SETUP, "_setup_paths", return_value=(query, venv, python, marker)
                    ),
                    mock.patch(
                        "portable_runtime.platform.system", return_value="Windows"
                    ),
                    mock.patch(
                        "portable_runtime.platform.machine", return_value="AMD64"
                    ),
                    mock.patch.object(
                        SETUP, "_run_verification", return_value=verification
                    ),
                    mock.patch.dict(os.environ, {"APPDATA": ""}),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(0, SETUP.main())
                self.assertTrue(marker.is_file())
                self.assertFalse((venv / ".rag-deps-installed").exists())
                load_and_verify_runtime(manifest, check_platform=False)
                previous_marker = marker.read_bytes()
                SETUP._release_setup_lock()
                failed_output = io.StringIO()
                with (
                    mock.patch.object(
                        sys, "argv", ["setup.py", "--format", "json"]
                    ),
                    mock.patch.object(SETUP, "RAG_ROOT", root),
                    mock.patch.object(
                        SETUP,
                        "_setup_paths",
                        return_value=(query, venv, python, marker),
                    ),
                    mock.patch(
                        "portable_runtime.platform.system",
                        return_value="Windows",
                    ),
                    mock.patch(
                        "portable_runtime.platform.machine",
                        return_value="AMD64",
                    ),
                    mock.patch.object(
                        SETUP, "_run_verification", return_value=verification
                    ),
                    mock.patch.object(
                        SETUP,
                        "_write_completion_marker",
                        side_effect=lambda *_args, **_kwargs: marker.write_text(
                            "{invalid", encoding="utf-8"
                        ),
                    ),
                    mock.patch.dict(os.environ, {"APPDATA": ""}),
                    contextlib.redirect_stdout(failed_output),
                ):
                    self.assertEqual(1, SETUP.main())
                self.assertEqual(previous_marker, marker.read_bytes())
            finally:
                SETUP._release_setup_lock()

    def test_packaged_verify_only_does_not_create_lock_or_change_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._fixture(root)
            self._requirements_fixture(root)
            query = manifest.parent
            venv = query / ".venv"
            python = venv / "Scripts" / "python.exe"
            marker = completion_marker_for(query)
            verification = {
                "status": "runtime_ready_no_db",
                "setup_complete": True,
                "lookup_ready": False,
                "runtime": {},
                "databases": {"healthy": [], "unhealthy": []},
                "warnings": [],
            }
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            output = io.StringIO()
            with (
                mock.patch.object(
                    sys, "argv", ["setup.py", "--verify-only", "--format", "json"]
                ),
                mock.patch.object(SETUP, "RAG_ROOT", root),
                mock.patch.object(
                    SETUP, "_setup_paths", return_value=(query, venv, python, marker)
                ),
                mock.patch("portable_runtime.platform.system", return_value="Windows"),
                mock.patch("portable_runtime.platform.machine", return_value="AMD64"),
                mock.patch.object(SETUP, "_run_verification", return_value=verification),
                mock.patch.object(SETUP, "_acquire_setup_lock") as acquire_lock,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(0, SETUP.main())
            acquire_lock.assert_not_called()
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse((query / ".setup.lock").exists())

if __name__ == "__main__":
    unittest.main()
