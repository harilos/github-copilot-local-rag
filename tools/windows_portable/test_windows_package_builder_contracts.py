from __future__ import annotations

import base64
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import windows_package_builder as package_builder
from windows_package_builder import BuildRequest, build_package


HERE = Path(__file__).resolve().parent


def _decoded_banner(launcher: str) -> str:
    match = re.search(r"FromBase64String\('([^']+)'\)", launcher)
    if match is None:
        raise AssertionError("encoded Windows banner is missing")
    return base64.b64decode(match.group(1)).decode("utf-8")


def _write_pe(path: Path, machine: int = 0x8664) -> None:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _fixture(root: Path, *, database_names: tuple[str, ...] = ("alpha-rag",)):
    payload = root / "payload"
    runtime = root / "runtime"
    model = root / "model"
    dbs = root / "dbs"
    output = root / "output"

    (payload / "rag" / "query").mkdir(parents=True)
    (payload / "rag" / "query" / "search.py").write_text(
        "print('search')\n", encoding="utf-8"
    )
    (payload / "rag" / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (payload / "rag" / "config").mkdir()
    (payload / "rag" / "config" / "network.example.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (payload / "rag" / "config" / "network.json").write_text(
        '{"secret":true}\n', encoding="utf-8"
    )
    (payload / "rag" / "query" / ".rag-deps-installed").write_text(
        "stale\n", encoding="utf-8"
    )
    (payload / "rag" / "query" / ".packaged-runtime.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (payload / "rag" / "query" / "portable_db_install.py").write_text(
        "raise SystemExit\n", encoding="utf-8"
    )
    (payload / "rag" / "query" / "portable_db_smoke.py").write_text(
        "raise SystemExit\n", encoding="utf-8"
    )
    (payload / "rag" / "query" / "portable_runtime.py").write_text(
        "raise SystemExit\n", encoding="utf-8"
    )

    _write_pe(runtime / "Scripts" / "python.exe")
    _write_pe(runtime / "python313.dll")
    (runtime / "Scripts" / "python313._pth").write_text(
        "python313.zip\nimport site\n", encoding="utf-8"
    )
    (runtime / "Scripts" / "python313.zip").write_bytes(b"stdlib")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    (runtime / "Lib" / "site-packages" / "module.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )

    model.mkdir()
    (model / "model.onnx").write_bytes(b"model")
    (model / "config.json").write_text("{}\n", encoding="utf-8")

    dbs.mkdir()
    for name in database_names:
        database = dbs / name
        database.mkdir()
        connection = sqlite3.connect(database / "catalog.sqlite")
        try:
            connection.execute("CREATE TABLE fixture (value TEXT NOT NULL)")
            connection.execute("INSERT INTO fixture VALUES (?)", (name,))
            connection.commit()
        finally:
            connection.close()
        (database / "VERSION.json").write_text(
            '{"schema_version":"1"}\n', encoding="utf-8"
        )
        (database / "db.json").write_text(
            '{"name":"' + name + '"}\n', encoding="utf-8"
        )
        (database / "index").mkdir()
        (database / "index" / "data.bin").write_bytes(b"index")
        for admin in ("data", "logs", "sources"):
            (database / admin).mkdir()
            (database / admin / "credentials.json").write_text(
                '{"token":"must-not-ship"}\n', encoding="utf-8"
            )

    return payload, runtime, model, dbs, output


def _request(
    root: Path,
    *,
    database_names: tuple[str, ...] = ("alpha-rag",),
    no_database: bool = False,
    profile: str = "search-only",
) -> BuildRequest:
    payload, runtime, model, dbs, output = _fixture(
        root, database_names=database_names
    )
    return BuildRequest(
        payload_root=payload,
        runtime_root=runtime,
        model_root=model,
        output_dir=output,
        databases_root=None if no_database else dbs,
        database_names=() if no_database else database_names,
        no_database=no_database,
        version="1.2.3",
        profile=profile,
        python_version="3.13.5",
    )


class WindowsPackageBuilderContractTests(unittest.TestCase):
    def test_builds_copy_ready_zip_without_test_validation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory))
            result = build_package(request)
            self.assertEqual(("alpha-rag",), result.database_names)

            with zipfile.ZipFile(result.zip_path) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                prefix = "local-rag-windows-x64-1.2.3/"
                self.assertIn(prefix + "install.cmd", names)
                launcher = archive.read(prefix + "install.cmd").decode("utf-8")
                self.assertIn(
                    '"%~dp0internal\\install.ps1" '
                    "-ConfigureVSCodeAutoApprove",
                    launcher,
                )
                self.assertLess(
                    launcher.index("Local-RAG"),
                    launcher.index(":local_rag_parse"),
                )
                localized_banner = _decoded_banner(launcher)
                for fragment in (
                    "秘密等級: xxxx",
                    "開発者: harlos",
                    "辞書メンテナンス: yyyy",
                    "配布用:",
                    "受領済みDB",
                    "管理用:",
                    "DBやSourceの追加・更新",
                    "自分で資料を追加・更新",
                    "https://github.com/harilos/github-copilot-local-rag",
                ):
                    self.assertIn(fragment, localized_banner)
                self.assertIn('if /I "%~1"=="-NoPause"', launcher)
                self.assertIn("shift", launcher)
                self.assertEqual(1, launcher.count("pause >nul"))
                self.assertNotIn("%*", launcher)
                self.assertIn(":local_rag_powershell_unavailable", launcher)
                self.assertIn(
                    'if not "%local_rag_rc%"=="0" if not "%local_rag_rc%"=="1"',
                    launcher,
                )
                self.assertIn("PowerShellを起動できませんでした。", launcher)
                self.assertIn(
                    "portable-install-launcher-%RANDOM%-%RANDOM%.log",
                    launcher,
                )
                self.assertIn(prefix + "internal/install.ps1", names)
                self.assertIn(
                    prefix + ".copilot/rag/query/.venv/Scripts/python.exe",
                    names,
                )
                self.assertIn(
                    prefix
                    + ".copilot/rag/models/ruri-v3-30m-onnx-int8/model.onnx",
                    names,
                )
                self.assertIn(
                    prefix + ".copilot/rag/dbs/alpha-rag/catalog.sqlite",
                    names,
                )
                for admin in ("data", "logs", "sources"):
                    self.assertFalse(
                        any(
                            name.startswith(
                                prefix
                                + ".copilot/rag/dbs/alpha-rag/"
                                + admin
                                + "/"
                            )
                            for name in names
                        )
                    )
                for retired in (
                    "PACKAGE-MANIFEST.json",
                    "SHA256SUMS",
                    ".copilot/rag/query/.packaged-runtime.json",
                    ".copilot/rag/query/portable_db_install.py",
                    ".copilot/rag/query/portable_db_smoke.py",
                    ".copilot/rag/query/portable_runtime.py",
                    ".copilot/rag/query/.rag-deps-installed",
                    ".copilot/rag/config/network.json",
                ):
                    self.assertNotIn(prefix + retired, names)

                installer = archive.read(
                    prefix + "internal/install.ps1"
                ).decode("utf-8")
                for removed in (
                    "Get-FileHash",
                    "PACKAGE-MANIFEST",
                    "--verify-only",
                    "--refresh-completion-marker",
                    "list_dbs.py",
                    "portable_db_smoke.py\")",
                    "portable_db_install.py\")",
                ):
                    self.assertNotIn(removed, installer)
                self.assertIn("Assert-Amd64PortableRuntime", installer)
                self.assertIn("-ReplaceExistingDatabases", installer)
                self.assertIn("[System.IO.Directory]::Move", installer)
                self.assertLess(
                    installer.index(
                        "[System.IO.Directory]::Move($TargetRuntime, $BackupRuntime)"
                    ),
                    installer.index("$PayloadRoot ="),
                )
                self.assertGreater(
                    installer.rindex("if ($ConfigureVSCodeAutoApprove)"),
                    installer.index(
                        "[System.IO.Directory]::Move($StageRuntime, $TargetRuntime)"
                    ),
                )
                readme = archive.read(prefix + "README-WINDOWS.md").decode(
                    "utf-8"
                )
                normalized_readme = " ".join(readme.split())
                self.assertIn("global auto-approve", readme)
                self.assertIn(
                    "all tools and terminal commands", normalized_readme
                )
                self.assertIn("-SkipVSCodeAutoApprove", readme)
                self.assertIn("leave VS Code settings unchanged", readme)
                self.assertIn("-RetryVSCodeApprovals", readme)
                self.assertIn("-NoPause", readme)
                self.assertIn("waits exactly once", readme)
                self.assertIn("%LOCALAPPDATA%\\LocalRAG\\logs", readme)
                self.assertIn("absolute log path", readme)

    def test_standalone_banner_values_are_replaceable_at_build_time(self) -> None:
        custom = {
            "classification": "internal",
            "developer": "alice",
            "dictionary_maintenance": "team-z",
        }
        with mock.patch.dict(
            package_builder._WINDOWS_BANNER_MODULE.WINDOWS_BANNER,
            custom,
            clear=True,
        ):
            launcher = package_builder._install_cmd()
        localized_banner = _decoded_banner(launcher)
        for value in custom.values():
            self.assertIn(value, localized_banner)
        self.assertIn("Local-RAG", launcher)

    @unittest.skipUnless(os.name == "nt", "install.cmd requires Windows")
    def test_launcher_renders_utf8_banner_before_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            internal = package / "internal"
            internal.mkdir()
            (package / "install.cmd").write_text(
                package_builder._install_cmd(),
                encoding="utf-8",
                newline="\r\n",
            )
            (internal / "install.ps1").write_text(
                "param(\n"
                "  [switch]$ConfigureVSCodeAutoApprove,\n"
                "  [switch]$SkipVSCodeAutoApprove,\n"
                "  [switch]$RetryVSCodeApprovals,\n"
                "  [switch]$ReplaceExistingDatabases,\n"
                "  [switch]$LauncherArgumentError\n"
                ")\n"
                "exit 0\n",
                encoding="ascii",
            )
            completed = subprocess.run(
                [
                    os.environ.get("ComSpec", "cmd.exe"),
                    "/d",
                    "/c",
                    "install.cmd",
                    "-NoPause",
                ],
                cwd=package,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Local-RAG", completed.stdout)
        self.assertIn("秘密等級: xxxx", completed.stdout)
        self.assertIn("開発者: harlos", completed.stdout)
        self.assertIn("辞書メンテナンス: yyyy", completed.stdout)
        self.assertNotIn("Press any key", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "install.cmd requires Windows")
    def test_launcher_logs_japanese_failure_when_powershell_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            profile = root / "profile"
            package.mkdir()
            profile.mkdir()
            (package / "install.cmd").write_text(
                package_builder._install_cmd(),
                encoding="utf-8",
                newline="\r\n",
            )
            environment = os.environ.copy()
            environment["SystemRoot"] = str(root / "missing-windows")
            environment["LOCALAPPDATA"] = str(profile / "AppData" / "Local")
            environment["TEMP"] = str(root / "temp")
            (root / "temp").mkdir()
            completed = subprocess.run(
                [
                    os.environ.get("ComSpec", "cmd.exe"),
                    "/d",
                    "/c",
                    "install.cmd",
                    "-NoPause",
                ],
                cwd=package,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            logs = list(
                (profile / "AppData" / "Local" / "LocalRAG" / "logs").glob(
                    "portable-install-launcher-*.log"
                )
            )
            self.assertEqual(1, len(logs))
            log_path = logs[0].resolve()
            log_text = logs[0].read_text(encoding="utf-8")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("Local RAG インストール結果: 失敗 (FAILED)", completed.stdout)
        self.assertIn("PowerShellを起動できませんでした。", completed.stdout)
        self.assertNotIn("Press any key", completed.stdout)
        self.assertIn("Local RAG インストール結果: 失敗 (FAILED)", log_text)
        self.assertIn("PowerShellを起動できませんでした。", log_text)
        self.assertIn(str(log_path), completed.stdout)

    @unittest.skipUnless(os.name == "nt", "install.cmd requires Windows")
    def test_launcher_logs_failure_when_powershell_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            profile = root / "profile"
            fake_windows = root / "fake-windows"
            powershell = (
                fake_windows
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            package.mkdir()
            profile.mkdir()
            powershell.parent.mkdir(parents=True)
            powershell.write_bytes(b"not a Windows executable")
            (package / "install.cmd").write_text(
                package_builder._install_cmd(),
                encoding="utf-8",
                newline="\r\n",
            )
            environment = os.environ.copy()
            environment["SystemRoot"] = str(fake_windows)
            environment["LOCALAPPDATA"] = str(profile / "AppData" / "Local")
            environment["TEMP"] = str(root / "temp")
            (root / "temp").mkdir()
            completed = subprocess.run(
                [
                    os.environ.get("ComSpec", "cmd.exe"),
                    "/d",
                    "/c",
                    "install.cmd",
                    "-NoPause",
                ],
                cwd=package,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            logs = list(
                (profile / "AppData" / "Local" / "LocalRAG" / "logs").glob(
                    "portable-install-launcher-*.log"
                )
            )
            self.assertEqual(1, len(logs))
            log_text = logs[0].read_text(encoding="utf-8")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("PowerShellを起動できませんでした。", completed.stdout)
        self.assertNotIn("Press any key", completed.stdout)
        self.assertIn("PowerShellを起動できませんでした。", log_text)

    def test_builds_zero_one_two_and_five_database_packages(self) -> None:
        for count in (0, 1, 2, 5):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                names = tuple(f"db-{index}-rag" for index in range(count))
                request = _request(
                    Path(directory),
                    database_names=names,
                    no_database=count == 0,
                )
                result = build_package(request)
                self.assertEqual(names, result.database_names)
                with zipfile.ZipFile(result.zip_path) as archive:
                    archived = {
                        name.split("/")[4]
                        for name in archive.namelist()
                        if "/.copilot/rag/dbs/" in name
                        and len(name.split("/")) > 4
                    }
                self.assertEqual(set(names), archived)

    def test_rejects_non_amd64_runtime_before_publishing_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(root)
            _write_pe(request.runtime_root / "Scripts" / "python.exe", 0x014C)
            with self.assertRaisesRegex(ValueError, "not AMD64"):
                build_package(request)
            self.assertFalse(
                (request.output_dir / "local-rag-windows-x64-1.2.3.zip").exists()
            )

    def test_rejects_unknown_profile_and_database_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory), profile="unknown")
            with self.assertRaisesRegex(ValueError, "unsupported package profile"):
                build_package(request)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(root)
            invalid = request.databases_root / "_invalid-rag"
            (request.databases_root / "alpha-rag").rename(invalid)
            request = BuildRequest(
                **{
                    **request.__dict__,
                    "database_names": ("_invalid-rag",),
                }
            )
            with self.assertRaisesRegex(ValueError, "database name is invalid"):
                build_package(request)

    def test_allows_only_fixed_public_ca_pem_paths(self) -> None:
        for relative in (
            Path("Lib/site-packages/certifi/cacert.pem"),
            Path("Lib/site-packages/grpc/_cython/_credentials/roots.pem"),
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                request = _request(Path(directory), no_database=True)
                target = request.runtime_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"public CA roots")
                self.assertTrue(build_package(request).zip_path.is_file())

        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory), no_database=True)
            private_key = request.runtime_root / "Lib" / "site-packages" / "private.pem"
            private_key.parent.mkdir(parents=True, exist_ok=True)
            private_key.write_bytes(b"private")
            with self.assertRaisesRegex(ValueError, "possible credential"):
                build_package(request)

    def test_rejects_secret_or_private_key_in_search_database_payload(self) -> None:
        for relative, content in (
            (Path("index/.env"), b"TOKEN=secret\n"),
            (Path("index/innocent.bin"), b"-----BEGIN PRIVATE KEY-----\n"),
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                request = _request(Path(directory))
                target = request.databases_root / "alpha-rag" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "forbidden_package_source"):
                    build_package(request)

    def test_rechecks_database_sources_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory))
            original = package_builder._SNAPSHOT_MODULE._copy_stable_regular_file
            changed = False

            def replace_after_selection(source, destination, *, source_root=None):
                nonlocal changed
                if source.name == "data.bin" and not changed:
                    source.write_bytes(b"-----BEGIN PRIVATE KEY-----\n")
                    changed = True
                return original(
                    source,
                    destination,
                    source_root=source_root,
                )

            with mock.patch.object(
                package_builder._SNAPSHOT_MODULE,
                "_copy_stable_regular_file",
                side_effect=replace_after_selection,
            ):
                with self.assertRaisesRegex(
                    ValueError, "forbidden_package_source"
                ):
                    build_package(request)

    def test_normalizes_query_root_runtime_path_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory), no_database=True)
            path_file = request.runtime_root / "Scripts" / "python313._pth"
            path_file.write_text(
                "python313.zip\n..\\..\nimport site\n..\\..\n",
                encoding="utf-8",
            )
            result = build_package(request)
            with zipfile.ZipFile(result.zip_path) as archive:
                value = archive.read(
                    "local-rag-windows-x64-1.2.3/"
                    ".copilot/rag/query/.venv/Scripts/python313._pth"
                ).decode("utf-8")
            self.assertEqual(1, value.splitlines().count(r"..\.."))
            self.assertLess(
                value.splitlines().index(r"..\.."),
                value.splitlines().index("import site"),
            )


@unittest.skipUnless(os.name == "nt", "Windows PowerShell integration")
class WindowsPortableInstallerIntegrationTests(unittest.TestCase):
    def _package(
        self,
        root: Path,
        *,
        machine: int = 0x8664,
        database_content: bytes = b"new-db",
        product_content: str = "new\n",
        executable_python: bool = False,
    ) -> Path:
        package = root / "package"
        internal = package / "internal"
        internal.mkdir(parents=True)
        shutil.copy2(HERE / "install-template.ps1", internal / "install.ps1")
        query = package / ".copilot" / "rag" / "query"
        python = query / ".venv" / "Scripts" / "python.exe"
        if executable_python:
            python.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(sys.executable), python)
        else:
            _write_pe(python, machine)
        if not executable_python:
            (python.parent / "python313._pth").write_text(
                "..\\..\nimport site\n", encoding="utf-8"
            )
        (query / "product.txt").parent.mkdir(parents=True, exist_ok=True)
        (query / "product.txt").write_text(product_content, encoding="utf-8")
        model = (
            package
            / ".copilot"
            / "rag"
            / "models"
            / "ruri-v3-30m-onnx-int8"
        )
        model.mkdir(parents=True)
        (model / "model.onnx").write_bytes(b"new-model")
        database = package / ".copilot" / "rag" / "dbs" / "selected-rag"
        database.mkdir(parents=True)
        (database / "catalog.sqlite").write_bytes(database_content)
        return package

    def _run(
        self,
        package: Path,
        profile: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["USERPROFILE"] = str(profile)
        environment["APPDATA"] = str(profile / "AppData" / "Roaming")
        environment["LOCALAPPDATA"] = str(profile / "AppData" / "Local")
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        return subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(package / "internal" / "install.ps1"),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )

    def test_fresh_install_and_explicit_same_name_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            package = self._package(root)
            completed = self._run(package, profile)
            self.assertEqual(0, completed.returncode, completed.stderr)
            logs = list(
                (profile / "AppData" / "Local" / "LocalRAG" / "logs").glob(
                    "portable-install-*.log"
                )
            )
            self.assertEqual(1, len(logs))
            self.assertIn(str(logs[0].resolve()), completed.stdout)
            self.assertIn(
                "Local RAG インストール結果: 成功 (SUCCESS)",
                completed.stdout,
            )
            target = profile / ".copilot" / "rag"
            self.assertEqual(
                b"new-db",
                (target / "dbs" / "selected-rag" / "catalog.sqlite").read_bytes(),
            )

            (target / "dbs" / "selected-rag" / "catalog.sqlite").write_bytes(
                b"old-db"
            )
            unrelated = target / "dbs" / "unrelated-rag"
            unrelated.mkdir()
            (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")
            rejected = self._run(package, profile)
            self.assertNotEqual(0, rejected.returncode)
            self.assertEqual(
                b"old-db",
                (target / "dbs" / "selected-rag" / "catalog.sqlite").read_bytes(),
            )
            replaced = self._run(
                package, profile, "-ReplaceExistingDatabases"
            )
            self.assertEqual(0, replaced.returncode, replaced.stderr)
            self.assertEqual(
                b"new-db",
                (target / "dbs" / "selected-rag" / "catalog.sqlite").read_bytes(),
            )
            self.assertEqual(
                "keep\n",
                (unrelated / "keep.txt").read_text(encoding="utf-8"),
            )

    def test_vscode_failure_keeps_published_runtime_and_databases_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            target = profile / ".copilot" / "rag"
            runtime = target / "query" / ".venv"
            _write_pe(runtime / "Scripts" / "python.exe")
            (runtime / "old.txt").write_text("old-runtime\n", encoding="utf-8")
            (target / "query" / "product.txt").write_text(
                "old-product\n", encoding="utf-8"
            )
            model = target / "models" / "ruri-v3-30m-onnx-int8"
            model.mkdir(parents=True)
            (model / "model.onnx").write_bytes(b"old-model")
            database = target / "dbs" / "selected-rag"
            database.mkdir(parents=True)
            (database / "catalog.sqlite").write_bytes(b"old-db")

            package = self._package(root)
            completed = self._run(
                package,
                profile,
                "-ReplaceExistingDatabases",
                "-ConfigureVSCodeAutoApprove",
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse((runtime / "old.txt").exists())
            self.assertTrue((runtime / "Scripts" / "python.exe").is_file())
            self.assertEqual(b"new-model", (model / "model.onnx").read_bytes())
            self.assertEqual(b"new-db", (database / "catalog.sqlite").read_bytes())
            self.assertEqual(
                "new\n",
                (target / "query" / "product.txt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Local RAG インストール結果: 失敗 (FAILED)",
                completed.stdout,
            )
            self.assertIn("ランタイム: READY", completed.stdout)
            self.assertIn("データベース: READY", completed.stdout)
            self.assertIn("VS Code 承認設定: FAILED", completed.stdout)
            self.assertIn("ポリシー有効性: UNKNOWN", completed.stdout)

    def test_non_amd64_binary_is_rejected_before_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            package = self._package(root, machine=0x014C)
            completed = self._run(package, profile)
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse((profile / ".copilot").exists())
            logs = list(
                (profile / "AppData" / "Local" / "LocalRAG" / "logs").glob(
                    "portable-install-*.log"
                )
            )
            self.assertEqual(1, len(logs))
            self.assertIn(str(logs[0].resolve()), completed.stdout)
            self.assertIn(
                "Local RAG インストール結果: 失敗 (FAILED)",
                completed.stdout,
            )

    def test_skip_vscode_auto_approve_leaves_settings_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            settings = (
                profile
                / "AppData"
                / "Roaming"
                / "Code"
                / "User"
                / "settings.json"
            )
            settings.parent.mkdir(parents=True)
            original = b'\xef\xbb\xbf{\r\n  // keep\r\n  "x": false,\r\n}\r\n'
            settings.write_bytes(original)
            package = self._package(root)
            completed = self._run(
                package, profile, "-SkipVSCodeAutoApprove"
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(original, settings.read_bytes())
            self.assertEqual(
                [], list(settings.parent.glob("*.local-rag-backup-*"))
            )
            self.assertIn(
                "VS Code 承認設定: SKIPPED_BY_USER", completed.stdout
            )
            self.assertIn("ポリシー有効性: UNKNOWN", completed.stdout)

    def test_opt_in_configures_global_setting_and_reports_unknown_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            settings = (
                profile
                / "AppData"
                / "Roaming"
                / "Code"
                / "User"
                / "settings.json"
            )
            settings.parent.mkdir(parents=True)
            original = (
                b'{\r\n  // keep\r\n  '
                b'"chat.tools.global.autoApprove": false,\r\n}\r\n'
            )
            settings.write_bytes(original)
            package = self._package(root, executable_python=True)
            shutil.copy2(
                HERE.parents[1]
                / ".copilot"
                / "rag"
                / "query"
                / "vscode_settings.py",
                package
                / ".copilot"
                / "rag"
                / "query"
                / "vscode_settings.py",
            )
            completed = self._run(
                package, profile, "-ConfigureVSCodeAutoApprove"
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            rendered = settings.read_bytes()
            self.assertIn(b"// keep\r\n", rendered)
            self.assertIn(
                b'"chat.tools.global.autoApprove": true,\r\n', rendered
            )
            backups = list(settings.parent.glob("*.local-rag-backup-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(original, backups[0].read_bytes())
            self.assertIn(
                "VS Code 承認設定: CONFIGURED_ON_DISK", completed.stdout
            )
            self.assertIn("ポリシー有効性: UNKNOWN", completed.stdout)

    def test_logical_vscode_opt_in_failure_is_retryable_without_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            target = profile / ".copilot" / "rag"
            runtime = target / "query" / ".venv"
            _write_pe(runtime / "Scripts" / "python.exe")
            (runtime / "old.txt").write_text("old-runtime\n", encoding="utf-8")
            (target / "query" / "product.txt").write_text(
                "old-product\n", encoding="utf-8"
            )
            model = target / "models" / "ruri-v3-30m-onnx-int8"
            model.mkdir(parents=True)
            (model / "model.onnx").write_bytes(b"old-model")
            database = target / "dbs" / "selected-rag"
            database.mkdir(parents=True)
            (database / "catalog.sqlite").write_bytes(b"old-db")

            package = self._package(root, executable_python=True)
            vscode = package / ".copilot" / "rag" / "query" / "vscode_settings.py"
            vscode.write_text(
                "import json\n"
                "print(json.dumps({'status': 'partial_failure'}))\n",
                encoding="utf-8",
            )
            completed = self._run(
                package,
                profile,
                "-ReplaceExistingDatabases",
                "-ConfigureVSCodeAutoApprove",
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse((runtime / "old.txt").exists())
            self.assertEqual(b"new-model", (model / "model.onnx").read_bytes())
            self.assertEqual(b"new-db", (database / "catalog.sqlite").read_bytes())
            self.assertEqual(
                "new\n",
                (target / "query" / "product.txt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Local RAG インストール結果: 失敗 (FAILED)",
                completed.stdout,
            )
            self.assertIn("ランタイム: READY", completed.stdout)
            self.assertIn("データベース: READY", completed.stdout)
            self.assertIn("VS Code 承認設定: FAILED", completed.stdout)
            self.assertIn("ポリシー有効性: UNKNOWN", completed.stdout)

            installed_vscode = target / "query" / "vscode_settings.py"
            installed_vscode.write_text(
                "import json\n"
                "print(json.dumps({'status': 'configured_on_disk'}))\n",
                encoding="utf-8",
            )
            runtime_before = (runtime / "Scripts" / "python.exe").read_bytes()
            model_before = (model / "model.onnx").read_bytes()
            database_before = (database / "catalog.sqlite").read_bytes()
            retried = self._run(
                package,
                profile,
                "-ConfigureVSCodeAutoApprove",
                "-RetryVSCodeApprovals",
            )
            self.assertEqual(0, retried.returncode, retried.stderr)
            self.assertEqual(
                runtime_before, (runtime / "Scripts" / "python.exe").read_bytes()
            )
            self.assertEqual(model_before, (model / "model.onnx").read_bytes())
            self.assertEqual(
                database_before, (database / "catalog.sqlite").read_bytes()
            )
            self.assertIn("ランタイム: READY", retried.stdout)
            self.assertIn("データベース: READY", retried.stdout)
            self.assertIn(
                "VS Code 承認設定: CONFIGURED_ON_DISK", retried.stdout
            )


if __name__ == "__main__":
    unittest.main()
