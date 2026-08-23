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
REPOSITORY_ROOT = HERE.parents[1]
AGENT_NAMES = (
    "agent003-readonly-local-rag.agent.md",
    "internal-doc-deep-research.agent.md",
    "internal-doc-search.agent.md",
)


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
    for filename in (
        "mcp_config.py",
        "mcp_server.py",
        "copilot_cli_setup.py",
    ):
        shutil.copy2(
            REPOSITORY_ROOT / ".copilot" / "rag" / "query" / filename,
            payload / "rag" / "query" / filename,
        )
    shutil.copytree(
        REPOSITORY_ROOT / ".copilot" / "rag" / "copilot-cli",
        payload / "rag" / "copilot-cli",
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
    shutil.copytree(REPOSITORY_ROOT / ".copilot" / "agents", payload / "agents")

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
        python_version="3.13.15",
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
                for agent_name in AGENT_NAMES:
                    member = prefix + ".copilot/agents/" + agent_name
                    self.assertIn(member, names)
                    self.assertEqual(
                        (REPOSITORY_ROOT / ".copilot" / "agents" / agent_name).read_bytes(),
                        archive.read(member),
                    )
                for filename in (
                    "local-rag-agent003-savings.agent.md",
                    "local-rag-agent003-standard.agent.md",
                    "local-rag-agent003-thorough.agent.md",
                    "local-rag-agent003.ps1",
                ):
                    member = prefix + ".copilot/rag/copilot-cli/" + filename
                    self.assertIn(member, names)
                    self.assertEqual(
                        (
                            REPOSITORY_ROOT
                            / ".copilot"
                            / "rag"
                            / "copilot-cli"
                            / filename
                        ).read_bytes(),
                        archive.read(member),
                    )
                launcher_bytes = archive.read(prefix + "install.cmd")
                self.assertIn(b"\r\n", launcher_bytes)
                self.assertNotIn(b"\n", launcher_bytes.replace(b"\r\n", b""))
                self.assertNotIn(b"\r", launcher_bytes.replace(b"\r\n", b""))
                launcher = launcher_bytes.decode("utf-8")
                self.assertIn('"%~dp0internal\\install.ps1" ', launcher)
                self.assertNotIn(
                    'install.ps1" -ConfigureVSCodeAutoApprove', launcher
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
                    installer.index('$InstallStage = "copilot_cli_setup"'),
                    installer.index(
                        "[System.IO.Directory]::Move($StageRuntime, $TargetRuntime)"
                    ),
                )
                self.assertIn(
                    'Join-Path $env:APPDATA "Code\\User\\mcp.json"',
                    installer,
                )
                self.assertIn(
                    '"--vscode-mcp-config", $VSCodeMcpTarget', installer
                )
                self.assertEqual(1, installer.count("copilot_cli_setup.py"))
                self.assertNotIn(
                    'Join-Path $TargetQuery "mcp_config.py"', installer
                )
                readme = archive.read(prefix + "README-WINDOWS.md").decode(
                    "utf-8"
                )
                normalized_readme = " ".join(readme.split())
                self.assertIn("localragagent003", readme)
                self.assertIn(
                    "does not change VS Code approval settings",
                    normalized_readme,
                )
                self.assertNotIn("global auto-approve", readme)
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
            package.mkdir()
            profile.mkdir()
            (package / "internal").mkdir()
            (package / "internal" / "install.ps1").write_text(
                "exit 2\n", encoding="ascii"
            )
            (package / "install.cmd").write_text(
                package_builder._install_cmd(),
                encoding="utf-8",
                newline="\r\n",
            )
            environment = os.environ.copy()
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

    def test_prunes_only_known_foreign_arch_setuptools_launcher_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(root, no_database=True)
            resource = (
                request.runtime_root
                / "Scripts"
                / "Lib"
                / "site-packages"
                / "setuptools"
                / "cli-32.exe"
            )
            _write_pe(resource, 0x014C)

            result = build_package(request)

            with zipfile.ZipFile(result.zip_path) as archive:
                self.assertFalse(
                    any(name.endswith("/setuptools/cli-32.exe") for name in archive.namelist())
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(root, no_database=True)
            unknown = (
                request.runtime_root
                / "Scripts"
                / "Lib"
                / "site-packages"
                / "unrelated"
                / "cli-32.exe"
            )
            _write_pe(unknown, 0x014C)
            with self.assertRaisesRegex(ValueError, "not AMD64"):
                build_package(request)

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
        executable_python: bool = True,
    ) -> Path:
        package = root / "package"
        internal = package / "internal"
        internal.mkdir(parents=True)
        shutil.copy2(HERE / "install-template.ps1", internal / "install.ps1")
        query = package / ".copilot" / "rag" / "query"
        python = query / ".venv" / "Scripts" / "python.exe"
        if executable_python:
            python.parent.mkdir(parents=True, exist_ok=True)
            base_executable = Path(getattr(sys, "_base_executable", sys.executable))
            shutil.copy2(base_executable, python)
        else:
            _write_pe(python, machine)
        if not executable_python:
            (python.parent / "python313._pth").write_text(
                "..\\..\nimport site\n", encoding="utf-8"
            )
        (query / "product.txt").parent.mkdir(parents=True, exist_ok=True)
        (query / "product.txt").write_text(product_content, encoding="utf-8")
        for filename in (
            "mcp_config.py",
            "mcp_server.py",
            "copilot_cli_setup.py",
        ):
            shutil.copy2(
                REPOSITORY_ROOT
                / ".copilot"
                / "rag"
                / "query"
                / filename,
                query / filename,
            )
        shutil.copytree(
            REPOSITORY_ROOT / ".copilot" / "rag" / "copilot-cli",
            package / ".copilot" / "rag" / "copilot-cli",
        )
        shutil.copytree(
            REPOSITORY_ROOT / ".copilot" / "agents",
            package / ".copilot" / "agents",
        )
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
        environment_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("COPILOT_HOME", None)
        environment.pop("LOCAL_RAG_COPILOT_PROFILE_PATH", None)
        environment["USERPROFILE"] = str(profile)
        environment["APPDATA"] = str(profile / "AppData" / "Roaming")
        environment["LOCALAPPDATA"] = str(profile / "AppData" / "Local")
        if environment_overrides is not None:
            environment.update(environment_overrides)
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
            agents = profile / ".copilot" / "agents"
            for agent_name in AGENT_NAMES:
                self.assertEqual(
                    (REPOSITORY_ROOT / ".copilot" / "agents" / agent_name).read_bytes(),
                    (agents / agent_name).read_bytes(),
                )

            (target / "dbs" / "selected-rag" / "catalog.sqlite").write_bytes(
                b"old-db"
            )
            user_agent = agents / "internal-doc-search.agent.md"
            user_agent.write_bytes(b"user-owned-agent\r\n")
            package_deep_agent = (
                package
                / ".copilot"
                / "agents"
                / "internal-doc-deep-research.agent.md"
            )
            package_deep_agent.write_bytes(
                package_deep_agent.read_bytes() + b"\n# product revision 2\n"
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
            self.assertEqual(b"user-owned-agent\r\n", user_agent.read_bytes())
            self.assertEqual(
                package_deep_agent.read_bytes(),
                (agents / "internal-doc-deep-research.agent.md").read_bytes(),
            )

    def test_agent_creation_rolls_back_with_failed_product_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            package = self._package(root)
            installer = package / "internal" / "install.ps1"
            installer_text = installer.read_text(encoding="utf-8")
            rollback_point = (
                '\n    foreach ($Relative in @(\n'
                '        "rag\\query\\.packaged-runtime.json",'
            )
            self.assertIn(rollback_point, installer_text)
            installer.write_text(
                installer_text.replace(
                    rollback_point,
                    '\n    throw "synthetic post-copy failure"\n'
                    + rollback_point,
                    1,
                ),
                encoding="utf-8",
            )
            installed_agents = profile / ".copilot" / "agents"
            installed_agents.mkdir(parents=True)
            product_search = (
                REPOSITORY_ROOT
                / ".copilot"
                / "agents"
                / "internal-doc-search.agent.md"
            )
            installed_search = installed_agents / product_search.name
            installed_search.write_bytes(product_search.read_bytes())
            unrelated = installed_agents / "user.agent.md"
            unrelated.write_bytes(b"user-owned-agent")

            completed = self._run(package, profile)

            self.assertNotEqual(0, completed.returncode)
            self.assertEqual(product_search.read_bytes(), installed_search.read_bytes())
            self.assertFalse(
                (installed_agents / "internal-doc-deep-research.agent.md").exists()
            )
            self.assertEqual(b"user-owned-agent", unrelated.read_bytes())

    def test_non_amd64_binary_is_rejected_before_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            package = self._package(root, machine=0x014C, executable_python=False)
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

    def test_legacy_approval_flags_are_noops_and_settings_stay_identical(self) -> None:
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
                package,
                profile,
                "-ConfigureVSCodeAutoApprove",
                "-SkipVSCodeAutoApprove",
                "-RetryVSCodeApprovals",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(original, settings.read_bytes())
            self.assertEqual(
                [], list(settings.parent.glob("*.local-rag-backup-*"))
            )
            self.assertNotIn("VS Code 承認設定", completed.stdout)
            config = (profile / ".copilot" / "mcp-config.json").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn('"localragagent003"', config)

    def test_fresh_install_registers_mcp_and_preserves_jsonc_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            profile.mkdir()
            config = profile / ".copilot" / "mcp-config.json"
            config.parent.mkdir()
            vscode_config = (
                profile
                / "AppData"
                / "Roaming"
                / "Code"
                / "User"
                / "mcp.json"
            )
            vscode_config.parent.mkdir(parents=True)
            original = (
                b'\xef\xbb\xbf{\r\n  // keep\r\n  "inputs": [],\r\n'
                b'  "servers": {"foreign": {"type": "http", '
                b'"url": "https://example.test"}},\r\n}\r\n'
            )
            config.write_bytes(original)
            vscode_config.write_bytes(
                b'{\r\n  // keep vscode\r\n  "inputs": [],\r\n}\r\n'
            )
            package = self._package(root)
            completed = self._run(package, profile)
            self.assertEqual(0, completed.returncode, completed.stderr)
            rendered = config.read_bytes()
            self.assertTrue(rendered.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"// keep\r\n", rendered)
            self.assertIn(b'"foreign"', rendered)
            self.assertIn(b'"inputs"', rendered)
            self.assertIn(b'"localragagent003"', rendered)
            vscode_rendered = vscode_config.read_bytes()
            self.assertIn(b"// keep vscode\r\n", vscode_rendered)
            self.assertIn(b'"inputs"', vscode_rendered)
            self.assertIn(b'"localragagent003"', vscode_rendered)
            self.assertIn("Copilot CLI MCP: configured", completed.stdout)
            self.assertIn("Copilot CLI agents: installed", completed.stdout)
            self.assertIn(
                "Copilot CLI launcher-scoped read-only approval: enabled",
                completed.stdout,
            )

            second = self._run(package, profile, "-ReplaceExistingDatabases")
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(rendered, config.read_bytes())
            self.assertEqual(vscode_rendered, vscode_config.read_bytes())
            self.assertIn(
                "Copilot CLI MCP: already_configured", second.stdout
            )
            self.assertIn(
                "Copilot CLI launcher-scoped read-only approval: already_enabled",
                second.stdout,
            )

    def test_missing_copilot_cli_warns_without_failing_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            package = self._package(root)
            cli_free_path = os.pathsep.join(
                entry
                for entry in os.environ.get("PATH", "").split(os.pathsep)
                if entry
                and not any(
                    (Path(entry) / name).is_file()
                    for name in ("copilot", "copilot.cmd", "copilot.exe")
                )
            )

            completed = self._run(
                package,
                profile,
                environment_overrides={"PATH": cli_free_path},
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn(
                "GitHub Copilot CLI was not detected", completed.stdout
            )
            self.assertIn(
                "Copilot CLI executable: not_detected", completed.stdout
            )
            self.assertIn("Copilot CLI MCP: configured", completed.stdout)

    def test_explicit_copilot_home_and_profile_path_are_respected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            cli_home = root / "cli-config-root"
            cli_profile = root / "PowerShell" / "profile.ps1"
            package = self._package(root)

            completed = self._run(
                package,
                profile,
                environment_overrides={
                    "COPILOT_HOME": str(cli_home),
                    "LOCAL_RAG_COPILOT_PROFILE_PATH": str(cli_profile),
                },
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue((cli_home / "mcp-config.json").is_file())
            for filename in (
                "local-rag-agent003-savings.agent.md",
                "local-rag-agent003-standard.agent.md",
                "local-rag-agent003-thorough.agent.md",
            ):
                self.assertTrue((cli_home / "agents" / filename).is_file())
                self.assertFalse(
                    (profile / ".copilot" / "agents" / filename).exists()
                )
            self.assertTrue(
                (
                    profile
                    / ".copilot"
                    / "copilot-cli"
                    / "owned-manifest.json"
                ).is_file()
            )
            self.assertIn(
                "# >>> Local RAG Agent003 CLI (owned) >>>",
                cli_profile.read_text(encoding="utf-8"),
            )

    def test_foreign_mcp_name_collision_rolls_back_every_published_file(self) -> None:
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

            config = profile / ".copilot" / "mcp-config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            original_config = (
                b'{"mcpServers":{"localragagent003":{"type":"http",'
                b'"url":"https://foreign.example"}}}\n'
            )
            config.write_bytes(original_config)
            package = self._package(root)
            completed = self._run(
                package,
                profile,
                "-ReplaceExistingDatabases",
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertTrue((runtime / "old.txt").exists())
            self.assertEqual(b"old-model", (model / "model.onnx").read_bytes())
            self.assertEqual(b"old-db", (database / "catalog.sqlite").read_bytes())
            self.assertEqual(
                "old-product\n",
                (target / "query" / "product.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(original_config, config.read_bytes())
            self.assertIn("Copilot CLI setup failed", completed.stdout)
            self.assertIn(
                "Copilot CLI MCP: blocked_collision", completed.stdout
            )
            self.assertFalse(
                (profile / "Documents" / "PowerShell" / "profile.ps1").exists()
            )
            self.assertFalse((profile / ".copilot" / "copilot-cli").exists())

    def test_vscode_mcp_collision_rolls_back_every_published_file(self) -> None:
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
            portable_config = profile / ".copilot" / "mcp-config.json"
            portable_original = b'{"inputs":["portable-keep"]}\n'
            portable_config.write_bytes(portable_original)
            vscode_config = (
                profile
                / "AppData"
                / "Roaming"
                / "Code"
                / "User"
                / "mcp.json"
            )
            vscode_config.parent.mkdir(parents=True)
            vscode_original = (
                b'{"servers":{"localragagent003":{"type":"http",'
                b'"url":"https://foreign.example"}}}\n'
            )
            vscode_config.write_bytes(vscode_original)

            package = self._package(root)
            completed = self._run(
                package,
                profile,
                "-ReplaceExistingDatabases",
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertTrue((runtime / "old.txt").exists())
            self.assertEqual(b"old-model", (model / "model.onnx").read_bytes())
            self.assertEqual(b"old-db", (database / "catalog.sqlite").read_bytes())
            self.assertEqual(
                "old-product\n",
                (target / "query" / "product.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(portable_original, portable_config.read_bytes())
            self.assertEqual(vscode_original, vscode_config.read_bytes())
            self.assertIn("Copilot CLI setup failed", completed.stdout)
            self.assertIn(
                "Copilot CLI MCP: blocked_collision", completed.stdout
            )
            self.assertFalse(
                (profile / "Documents" / "PowerShell" / "profile.ps1").exists()
            )
            self.assertFalse((profile / ".copilot" / "copilot-cli").exists())


if __name__ == "__main__":
    unittest.main()
