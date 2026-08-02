from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from windows_package_builder import (
    BuildRequest,
    _assert_no_forbidden_payload,
    _ensure_query_root_on_runtime_path,
    _normalized_databases,
    build_package,
)


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
                "Scripts/python313._pth": b"python313.zip\nLib/site-packages\nimport site\n",
                "Scripts/python313.zip": b"stdlib",
                "Lib/site-packages/demo.py": b"VALUE=1\n",
                "Lib/site-packages/demo-1.0.dist-info/METADATA": (
                    b"Name: demo\nVersion: 1.0\n"
                ),
                "Lib/site-packages/certifi/cacert.pem": b"PUBLIC CA BUNDLE\n",
                "Lib/site-packages/certifi-1.0.dist-info/METADATA": (
                    b"Name: certifi\nVersion: 1.0\n"
                ),
                "Lib/site-packages/grpc/_cython/_credentials/roots.pem": (
                    b"PUBLIC GRPC CA BUNDLE\n"
                ),
                "Lib/site-packages/grpcio-1.0.dist-info/METADATA": (
                    b"Name: grpcio\nVersion: 1.0\n"
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
                    no_database=True,
                )
            )

            self.assertTrue(result.zip_path.is_file())
            verified = subprocess.run([sys.executable, str(Path(__file__).with_name("verify_package.py")), str(result.zip_path)], capture_output=True, text=True, check=False)
            self.assertEqual(0, verified.returncode, verified.stderr)
            self.assertEqual(
                hashlib.sha256(result.zip_path.read_bytes()).hexdigest(),
                result.zip_sha256,
            )
            with zipfile.ZipFile(result.zip_path) as archive:
                names = set(archive.namelist())
                prefix = "local-rag-windows-x64-1.0.1/"
                self.assertIn(prefix + "PACKAGE-MANIFEST.json", names)
                manifest = json.loads(archive.read(prefix + "PACKAGE-MANIFEST.json"))
                self.assertEqual("local-rag.windows-package.v2", manifest["schema"])
                self.assertEqual([], manifest["databases"])
                self.assertFalse(any("/.copilot/rag/dbs/" in name for name in names))
                self.assertIn(prefix + "SHA256SUMS", names)
                self.assertIn(prefix + "sbom.spdx.json", names)
                self.assertIn(
                    prefix + ".copilot/rag/query/.packaged-runtime.json", names
                )
                path_file = (
                    prefix
                    + ".copilot/rag/query/.venv/Scripts/python313._pth"
                )
                path_lines = archive.read(path_file).decode("utf-8").splitlines()
                self.assertEqual(
                    1,
                    sum(
                        line.replace("/", "\\").casefold() == r"..\.."
                        for line in path_lines
                    ),
                )
                self.assertLess(path_lines.index(r"..\.."), path_lines.index("import site"))
                self.assertIn(
                    prefix
                    + ".copilot/rag/query/.venv/Lib/site-packages/certifi/cacert.pem",
                    names,
                )
                self.assertIn(
                    prefix
                    + ".copilot/rag/query/.venv/Lib/site-packages/grpc/"
                    + "_cython/_credentials/roots.pem",
                    names,
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
                self.assertNotIn(prefix + "install.ps1", names)
                launcher = archive.read(prefix + "install.cmd")
                launcher.decode("ascii")
                self.assertIn(b"internal\\install.ps1", launcher)
                installer = archive.read(prefix + "internal/install.ps1").decode("utf-8")
                self.assertIn("[System.IO.Directory]::Move", installer)
                self.assertIn("local-rag.windows-package.v2", installer)
                self.assertIn("portable_db_install.py", installer)
                self.assertIn("--defer-completion-marker", installer)
                self.assertIn("list_dbs.py", installer)
                self.assertIn("installed setup is not lookup-ready", installer)
                catch = installer.index("} catch {")
                self.assertLess(installer.index("Close-CompletionMarkerGate", catch), installer.index("$RuntimePublished", catch))
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


    def test_ascii_cmd_launcher_forwards_arguments_outside_package_cwd(self) -> None:
        prefix = "portable-" + "".join(map(chr, (0x65E5, 0x672C, 0x8A9E))) + "-space-"
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            root = Path(directory)
            internal = root / "internal"
            internal.mkdir()
            (root / "install.cmd").write_text(
                __import__("windows_package_builder")._install_cmd(),
                encoding="ascii",
                newline="\r\n",
            )
            (internal / "install.ps1").write_text(
                'param([string]$Value)\n[IO.File]::WriteAllText((Join-Path $PSScriptRoot "argv.txt"), $Value)\nexit 0\n',
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", str(root / "install.cmd"), "forwarded"],
                cwd=Path(__file__).parent,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("forwarded", (internal / "argv.txt").read_text(encoding="utf-8"))

    def test_builds_verified_canonical_one_two_and_five_database_zips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = Path(__file__).resolve().parents[2] / ".copilot"
            runtime = root / "runtime"
            for relative, data in {
                "Scripts/python.exe": b"MZ-python",
                "Scripts/python313.dll": b"MZ-dll",
                "Scripts/python313._pth": b"python313.zip\nLib/site-packages\n",
                "Scripts/python313.zip": b"stdlib",
                "Lib/site-packages/demo.py": b"VALUE=1\n",
                "Lib/site-packages/demo-1.0.dist-info/METADATA": b"Name: demo\nVersion: 1.0\n",
            }.items():
                target = runtime / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            model = root / "model"
            model.mkdir()
            (model / "model.onnx").write_bytes(b"model")
            dbs = root / "dbs"
            names = tuple(f"canonical-{index}-rag" for index in range(5))
            for name in names:
                database = dbs / name
                database.mkdir(parents=True)
                (database / "VERSION.json").write_text('{"schema":1}', encoding="utf-8")
                (database / "db.json").write_text(
                    json.dumps({"name": name, "display_name": name}), encoding="utf-8"
                )
                connection = sqlite3.connect(database / "catalog.sqlite")
                try:
                    connection.execute("CREATE TABLE fixture (value TEXT)")
                    connection.execute("INSERT INTO fixture VALUES (?)", (name,))
                    connection.commit()
                finally:
                    connection.close()
                index = database / "index"
                index.mkdir()
                (index / "vectors.bin").write_bytes(name.encode("ascii"))
            for count in (1, 2, 5):
                selected = names[:count]
                result = build_package(
                    BuildRequest(
                        payload_root=payload,
                        runtime_root=runtime,
                        model_root=model,
                        output_dir=root / f"out-{count}",
                        version=f"canonical-{count}",
                        profile="search-only",
                        python_version="3.13.5",
                        dependency_lock_sha256="a" * 64,
                        model_fingerprint="b" * 64,
                        databases_root=dbs,
                        database_names=selected,
                    )
                )
                verified = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("verify_package.py")), str(result.zip_path)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, verified.returncode, verified.stderr)
                with zipfile.ZipFile(result.zip_path) as archive:
                    prefix = f"local-rag-windows-x64-canonical-{count}/"
                    manifest = json.loads(archive.read(prefix + "PACKAGE-MANIFEST.json"))
                    self.assertEqual(list(selected), [item["name"] for item in manifest["databases"]])
                    packaged = {
                        item["path"].split("/")[3]
                        for item in manifest["files"]
                        if item["path"].startswith(".copilot/rag/dbs/")
                    }
                    self.assertEqual(set(selected), packaged)

    def test_canonical_interface_freezes_five_database_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbs = root / "dbs"
            names = tuple(f"fixture-{index}-rag" for index in range(5))
            for name in names:
                (dbs / name).mkdir(parents=True)
            request = BuildRequest(root, root, root, root, "1", "search-only", "3", "a" * 64, "b" * 64, databases_root=dbs, database_names=tuple(reversed(names)))
            selected, selected_root = _normalized_databases(request)
            self.assertEqual(names, selected)
            self.assertEqual(dbs.resolve(), selected_root)
            collision = BuildRequest(root, root, root, root, "1", "search-only", "3", "a" * 64, "b" * 64, databases_root=dbs, database_names=(names[0], names[0].upper()))
            with self.assertRaisesRegex(ValueError, "casefold"):
                _normalized_databases(collision)
        powershell = Path(__file__).with_name("build_package.ps1").read_text(encoding="utf-8")
        self.assertIn("[string[]]$DatabaseNames", powershell)
        self.assertIn("foreach ($DatabaseName in $DatabaseNames)", powershell)
        self.assertIn("--no-database", powershell)
        self.assertIn("Join-Path $ToolRoot $(", powershell)
        self.assertIn("& $Python.Source -B @Arguments", powershell)
        installer = Path(__file__).with_name("install-template.ps1").read_text(
            encoding="utf-8"
        )
        self.assertEqual(2, installer.count("& $SourcePython -B "))
        self.assertEqual(
            3,
            installer.count('& (Join-Path $TargetRuntime "Scripts\\python.exe") -B '),
        )
        self.assertIn('$SetupArguments = @("-B",', installer)
        self.assertIn('$SmokeArguments = @("-B",', installer)
        self.assertIn(
            '$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)',
            installer,
        )
        self.assertIn('$OutputEncoding = $Utf8NoBom', installer)
        self.assertIn('[Console]::OutputEncoding = $Utf8NoBom', installer)
        self.assertIn("Remove-Tree $BackupProduct", installer)
        self.assertIn("Remove-Tree $BackupDbs", installer)
        self.assertIn("[System.IO.FileAttributes]::ReadOnly", installer)
        self.assertIn("[System.IO.File]::SetAttributes", installer)
        self.assertIn("transaction tree containing a reparse point", installer)
        cli = Path(__file__).with_name("build_package.py").read_text(encoding="utf-8")
        self.assertIn("except EOFError:", cli)
        self.assertIn("ask=_ask", cli)

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
    def test_allows_only_the_fixed_public_ca_pem_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca_bundle = (
                root
                / ".copilot"
                / "rag"
                / "query"
                / ".venv"
                / "Lib"
                / "site-packages"
                / "certifi"
                / "cacert.pem"
            )
            ca_bundle.parent.mkdir(parents=True)
            ca_bundle.write_text("PUBLIC CA BUNDLE\n", encoding="ascii")
            grpc_bundle = (
                root
                / ".copilot"
                / "rag"
                / "query"
                / ".venv"
                / "Lib"
                / "site-packages"
                / "grpc"
                / "_cython"
                / "_credentials"
                / "roots.pem"
            )
            grpc_bundle.parent.mkdir(parents=True)
            grpc_bundle.write_text("PUBLIC GRPC CA BUNDLE\n", encoding="ascii")
            _assert_no_forbidden_payload(root)
            private_key = root / "private.pem"
            private_key.write_text("PRIVATE\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "possible credential"):
                _assert_no_forbidden_payload(root)
            private_key.unlink()
            private_key = root / "private.key"
            private_key.write_text("PRIVATE\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "possible credential"):
                _assert_no_forbidden_payload(root)
            private_key.unlink()

            misplaced_bundle = root / "other" / "roots.pem"
            misplaced_bundle.parent.mkdir()
            misplaced_bundle.write_text("PUBLIC CA BUNDLE\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "possible credential"):
                _assert_no_forbidden_payload(root)

    def test_normalizes_query_root_runtime_path_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            path_file = runtime / "Scripts" / "python313._pth"
            path_file.parent.mkdir(parents=True)
            path_file.write_text(
                "python313.zip\n../../\nLib/site-packages\n  ..\\..  \nimport site\n",
                encoding="utf-8",
            )
            _ensure_query_root_on_runtime_path(runtime)
            lines = path_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, lines.count(r"..\.."))
            self.assertEqual(
                lines.index("import site") - 1,
                lines.index(r"..\.."),
            )


if __name__ == "__main__":
    unittest.main()
