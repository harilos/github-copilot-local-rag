from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from windows_package_builder import BuildRequest, build_package


class WindowsPackageBuilderContractTests(unittest.TestCase):
    def test_builds_copy_ready_zip_and_excludes_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload" / ".copilot"
            query = payload / "rag" / "query"
            query.mkdir(parents=True)
            (query / "setup.py").write_text("print('setup')\n", encoding="utf-8")
            (query / ".rag-deps-installed").write_text("forbidden\n", encoding="utf-8")
            (query / ".rag-deps-installed.active.pre-update.123").write_text(
                "forbidden-backup\n", encoding="utf-8"
            )
            (payload / "rag" / "config").mkdir()
            (payload / "rag" / "config" / "network.json").write_text(
                '{"secret":true}', encoding="utf-8"
            )
            runtime = root / "runtime"
            scripts = runtime / "Scripts"
            site = runtime / "Lib" / "site-packages"
            private_db = payload / "rag" / "dbs" / "private" / "data"
            private_db.mkdir(parents=True)
            (private_db / "vectors.bin").write_bytes(b"private")
            private_model = payload / "rag" / "models" / "private"
            private_model.mkdir(parents=True)
            (private_model / "model.onnx").write_bytes(b"stale")

            scripts.mkdir(parents=True)
            site.mkdir(parents=True)
            for relative, data in {
                "Scripts/python.exe": b"MZ-python",
                "Scripts/python313.dll": b"MZ-dll",
                "Scripts/python313._pth": b"python313.zip\nLib/site-packages\n",
                "Scripts/python313.zip": b"stdlib",
                "Lib/site-packages/demo.py": b"VALUE=1\n",
                "Lib/site-packages/demo-1.0.dist-info/METADATA": (
                    b"Name: demo\nVersion: 1.0\n"
                ),
            }.items():
                target = runtime / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            model = root / "model"
            model.mkdir()
            (model / "model.onnx").write_bytes(b"model")
            output = root / "out"

            result = build_package(
                BuildRequest(
                    payload_root=payload,
                    runtime_root=runtime,
                    model_root=model,
                    output_dir=output,
                    version="1.0.1",
                    profile="search-only",
                    python_version="3.13.5",
                    dependency_lock_sha256="a" * 64,
                    model_fingerprint="b" * 64,
                )
            )

            self.assertTrue(result.zip_path.is_file())
            self.assertEqual(
                hashlib.sha256(result.zip_path.read_bytes()).hexdigest(),
                result.zip_sha256,
            )
            with zipfile.ZipFile(result.zip_path) as archive:
                names = set(archive.namelist())
                prefix = "local-rag-windows-x64-1.0.1/"
                self.assertIn(prefix + "PACKAGE-MANIFEST.json", names)
                self.assertIn(prefix + "SHA256SUMS", names)
                self.assertIn(prefix + "sbom.spdx.json", names)
                self.assertIn(
                    prefix + ".copilot/rag/query/.packaged-runtime.json", names
                )
                self.assertNotIn(
                    prefix + ".copilot/rag/query/.rag-deps-installed", names
                )
                self.assertFalse(
                    any("/.rag-deps-installed." in name for name in names)
                )
                self.assertNotIn(
                    prefix + ".copilot/rag/config/network.json", names
                )
                self.assertTrue(
                    all(".." not in Path(name).parts for name in names)
                )
                self.assertFalse(
                    any("/rag/dbs/private/" in name for name in names)
                )
                self.assertFalse(
                    any("/rag/models/private/" in name for name in names)
                )
                installer = archive.read(prefix + "install.ps1").decode("utf-8")
                self.assertIn("[System.IO.Directory]::Move", installer)
                self.assertIn("PACKAGE-MANIFEST.json", installer)
                self.assertIn("source-connections.secrets.json", installer)
                self.assertIn("rag\\dbs\\", installer)
                self.assertIn("[switch]$ConfigureVSCodeAutoApprove", installer)
                self.assertIn("--configure-vscode-auto-approve", installer)
                self.assertIn("explicit VS Code", installer)
                self.assertIn(
                    "auto-approve opt-in did not complete", installer
                )
                self.assertNotIn("Stop-Process", installer)
                self.assertNotIn("taskkill", installer.casefold())


    def test_rejects_symlinks_and_unknown_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / ".copilot"
            payload.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            model = root / "model"
            model.mkdir()
            with self.assertRaisesRegex(ValueError, "profile"):
                build_package(
                    BuildRequest(
                        payload_root=payload,
                        runtime_root=runtime,
                        model_root=model,
                        output_dir=root / "out",
                        version="1",
                        profile="other",
                        python_version="3",
                        dependency_lock_sha256="a" * 64,
                        model_fingerprint="b" * 64,
                    )
                )


if __name__ == "__main__":
    unittest.main()
