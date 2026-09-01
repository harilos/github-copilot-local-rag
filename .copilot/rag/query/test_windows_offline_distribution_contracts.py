from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
for module_root in (RAG_ROOT, QUERY_ROOT):
    if str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))

from source_manager import windows_distribution  # noqa: E402
import mcp_config  # noqa: E402


def _parse_mcp_config(text: str) -> dict[str, object]:
    body = text[1:] if text.startswith("\ufeff") else text
    value, _view = mcp_config._JsoncLexer(body).document()
    return value


def _decoded_banner(launcher: str) -> str:
    match = re.search(r"FromBase64String\('([^']+)'\)", launcher)
    if match is None:
        raise AssertionError("encoded Windows banner is missing")
    return base64.b64decode(match.group(1)).decode("utf-8")


def _write_amd64_pe(path: Path) -> None:
    _write_pe(path, 0x8664)


def _write_pe(path: Path, machine: int) -> None:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


class WindowsOfflineDistributionContracts(unittest.TestCase):
    def test_locked_runtime_and_search_requirements_are_present(self) -> None:
        lock = windows_distribution._load_lock()
        repository_lock = json.loads(
            (
                RAG_ROOT.parents[1]
                / "tools"
                / "windows_portable"
                / "runtime-lock.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(repository_lock, lock)
        self.assertEqual("amd64", lock["platform"]["arch"])
        self.assertTrue(lock["python"]["url"].startswith("https://www.python.org/"))
        self.assertEqual(
            "requirements-windows-search.lock",
            windows_distribution.SEARCH_REQUIREMENTS.name,
        )
        requirements = windows_distribution.SEARCH_REQUIREMENTS.read_text(
            encoding="utf-8"
        )
        for required in (
            "chromadb==",
            "onnxruntime==",
            "pip==24.3.1",
            "transformers==",
            "SudachiPy==",
        ):
            self.assertIn(required, requirements)
        for administrator_only in (
            "sentence-transformers",
            "optimum",
            "python-docx",
            "python-pptx",
            "openpyxl",
        ):
            self.assertNotIn(administrator_only, requirements)

    def test_runtime_entries_require_embedded_amd64_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            scripts = runtime / "Scripts"
            _write_amd64_pe(scripts / "python.exe")
            (scripts / "python313._pth").write_text(
                "python313.zip\n.\n..\\..\nLib\\site-packages\nimport site\n",
                encoding="ascii",
            )
            (scripts / "python313.zip").write_bytes(b"stdlib")
            module = scripts / "Lib" / "site-packages" / "demo.py"
            module.parent.mkdir(parents=True)
            module.write_text("VALUE = 1\n", encoding="utf-8")

            entries = windows_distribution._runtime_entries(runtime)
            destinations = {entry.destination for entry in entries}
            self.assertIn(
                ".copilot/rag/query/.venv/Scripts/python.exe",
                destinations,
            )
            self.assertIn(
                ".copilot/rag/query/.venv/Scripts/Lib/site-packages/demo.py",
                destinations,
            )

    def test_search_runtime_keeps_pip_and_prunes_exact_distlib_launchers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            scripts = runtime / "Scripts"
            _write_amd64_pe(scripts / "python.exe")
            (scripts / "python313._pth").write_text(
                "python313.zip\n.\n..\\..\nLib\\site-packages\nimport site\n",
                encoding="ascii",
            )
            (scripts / "python313.zip").write_bytes(b"stdlib")
            site = scripts / "Lib" / "site-packages"
            pip_root = site / "pip"
            (pip_root / "__init__.py").parent.mkdir(parents=True)
            (pip_root / "__init__.py").write_text(
                '__version__ = "24.3.1"\n', encoding="utf-8"
            )
            (site / "pip-24.3.1.dist-info").mkdir()
            for name, machine in windows_distribution.PIP_DISTLIB_LAUNCHERS.items():
                _write_pe(pip_root / "_vendor" / "distlib" / name, machine)
            public_ca = pip_root / "_vendor" / "certifi" / "cacert.pem"
            public_ca.parent.mkdir(parents=True)
            public_ca.write_bytes(b"public roots")

            windows_distribution._prune_pip_distlib_launchers(runtime)

            self.assertTrue((pip_root / "__init__.py").is_file())
            self.assertTrue((site / "pip-24.3.1.dist-info").is_dir())
            for name in windows_distribution.PIP_DISTLIB_LAUNCHERS:
                self.assertFalse((pip_root / "_vendor" / "distlib" / name).exists())
            destinations = {
                entry.destination
                for entry in windows_distribution._runtime_entries(runtime)
            }
            self.assertIn(
                ".copilot/rag/query/.venv/Scripts/Lib/site-packages/pip/__init__.py",
                destinations,
            )
            self.assertIn(
                ".copilot/rag/query/.venv/Scripts/Lib/site-packages/pip/_vendor/certifi/cacert.pem",
                destinations,
            )

    def test_search_runtime_rejects_noncanonical_distlib_launchers(self) -> None:
        for mutation in ("unexpected", "wrong-machine"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory) / "runtime"
                site = runtime / "Scripts" / "Lib" / "site-packages"
                pip_root = site / "pip"
                (pip_root / "__init__.py").parent.mkdir(parents=True)
                (pip_root / "__init__.py").write_text(
                    '__version__ = "24.3.1"\n', encoding="utf-8"
                )
                (site / "pip-24.3.1.dist-info").mkdir()
                for name, machine in windows_distribution.PIP_DISTLIB_LAUNCHERS.items():
                    _write_pe(pip_root / "_vendor" / "distlib" / name, machine)
                if mutation == "unexpected":
                    _write_pe(
                        pip_root / "_vendor" / "distlib" / "unexpected.exe",
                        0x8664,
                    )
                    expected = "windows_runtime_pip_launcher_set_invalid"
                else:
                    _write_pe(
                        pip_root / "_vendor" / "distlib" / "t64.exe",
                        0x014C,
                    )
                    expected = "windows_runtime_pip_launcher_invalid"
                with self.assertRaisesRegex(
                    windows_distribution.packages.PackageError,
                    expected,
                ):
                    windows_distribution._prune_pip_distlib_launchers(runtime)

    def test_windows_builder_publishes_runtime_and_installer(self) -> None:
        def prepare_runtime(
            _copilot_home: Path,
            runtime: Path,
            *,
            emit: object,
        ) -> None:
            del emit
            scripts = runtime / "Scripts"
            _write_amd64_pe(scripts / "python.exe")
            (scripts / "python313._pth").write_text(
                "python313.zip\n.\n..\\..\nLib\\site-packages\nimport site\n",
                encoding="ascii",
            )
            (scripts / "python313.zip").write_bytes(b"stdlib")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "offline.zip"
            with (
                mock.patch.object(
                    windows_distribution.sys,
                    "platform",
                    "win32",
                ),
                mock.patch.object(
                    windows_distribution,
                    "_validate_model",
                ),
                mock.patch.object(
                    windows_distribution,
                    "_prepare_runtime",
                    side_effect=prepare_runtime,
                ),
            ):
                result = (
                    windows_distribution.create_windows_distribution_package(
                        RAG_ROOT.parent,
                        output,
                        db_names=(),
                    )
                )
            self.assertFalse(result["recipient_python_required"])
            self.assertFalse(result["recipient_network_required"])
            self.assertFalse(result["verification"]["list_dbs_executed"])
            self.assertFalse(result["verification"]["real_search_executed"])
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
            self.assertIn("install.cmd", names)
            self.assertIn("internal/install.ps1", names)
            self.assertIn(
                ".copilot/rag/query/.venv/Scripts/python.exe",
                names,
            )
            self.assertIn(".copilot/rag/list_dbs.py", names)
            self.assertIn(".copilot/rag/query/list_dbs.py", names)
            self.assertFalse(
                any(name.startswith(".copilot/agents/") for name in names)
            )
            self.assertIn(".copilot/skills/local-rag/SKILL.md", names)
            self.assertNotIn(
                ".copilot/skills/local-rag-setup/SKILL.md",
                names,
            )
            self.assertFalse(
                any(name.startswith(".copilot/instructions/") for name in names)
            )
            self.assertFalse(
                any(
                    name.startswith(".copilot/rag/copilot-cli/")
                    for name in names
                )
            )
            self.assertNotIn(".copilot/rag/query/mcp_server.py", names)
            self.assertIn(
                ".copilot/rag/query/agent003_answer_packet.py",
                names,
            )
            self.assertIn(".copilot/rag/query/result_bundle.py", names)
            self.assertIn(".copilot/rag/query/result_gateway.py", names)
            self.assertIn(".copilot/rag/query/skill_runner.py", names)
            self.assertIn(".copilot/rag/query/copilot_cli_setup.py", names)
            self.assertIn(".copilot/rag/query/mcp_config.py", names)
            with zipfile.ZipFile(output) as archive:
                install_cmd = archive.read("install.cmd").decode("utf-8")
            self.assertNotIn(
                'install.ps1" -ConfigureVSCodeAutoApprove', install_cmd
            )
            self.assertLess(
                install_cmd.index("Local-RAG"),
                install_cmd.index(":local_rag_parse"),
            )
            localized_banner = _decoded_banner(install_cmd)
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
            self.assertIn('if /I "%~1"=="-NoPause"', install_cmd)
            self.assertIn("shift", install_cmd)
            self.assertEqual(1, install_cmd.count("pause >nul"))
            self.assertNotIn("%*", install_cmd)
            self.assertIn(":local_rag_powershell_unavailable", install_cmd)
            self.assertIn(
                'if not "%local_rag_rc%"=="0" if not "%local_rag_rc%"=="1"',
                install_cmd,
            )
            self.assertIn("PowerShellを起動できませんでした。", install_cmd)
            self.assertIn("portable-install-launcher-%RANDOM%-%RANDOM%.log", install_cmd)
            template = windows_distribution.INSTALL_TEMPLATE.read_text(
                encoding="utf-8"
            )
            self.assertIn('$InstallStage = "slash_skill"', template)
            self.assertIn('$InstallStage = "retire_agent003"', template)
            self.assertIn('"retire",', template)
            self.assertEqual(1, template.count("copilot_cli_setup.py"))
            self.assertIn('"rag\\query\\vscode_settings.py"', template)
            self.assertNotIn('"rag\\query\\agent003_answer_packet.py"', template)
            self.assertIn('"rag\\query\\mcp_server.py"', template)
            self.assertIn(
                '"rag\\copilot-cli\\local-rag-agent003.ps1"', template
            )
            self.assertIn('"instructions\\rag.instructions.md"', template)
            self.assertIn('"skills\\local-rag-setup\\SKILL.md"', template)
            self.assertNotIn(
                'Join-Path $TargetQuery "vscode_settings.py"', template
            )
            self.assertNotIn("Invoke-VSCodeApprovalConfiguration", template)

    def test_runtime_rejects_machine_absolute_pth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            scripts = runtime / "Scripts"
            _write_amd64_pe(scripts / "python.exe")
            (scripts / "python313._pth").write_text(
                "python313.zip\n..\\..\nLib\\site-packages\nimport site\n",
                encoding="ascii",
            )
            (scripts / "python313.zip").write_bytes(b"stdlib")
            site = scripts / "Lib" / "site-packages"
            site.mkdir(parents=True)
            (site / "unsafe.pth").write_text(
                "C:" + "\\Users\\builder\\private\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "windows_runtime_pth_not_portable",
            ):
                windows_distribution._runtime_entries(runtime)

    def test_generated_installer_has_unambiguous_result_contract(self) -> None:
        template = windows_distribution.INSTALL_TEMPLATE.read_text(
            encoding="utf-8"
        )
        repository_template = (
            RAG_ROOT.parents[1]
            / "tools"
            / "windows_portable"
            / "install-template.ps1"
        ).read_text(encoding="utf-8")
        self.assertEqual(repository_template, template)
        for fragment in (
            "[Text.Encoding]::UTF8",
            "5oiQ5Yqf",
            "5aSx5pWX",
            "PT09IExvY2FsIFJBRyDjgqTjg7Pjgrnjg4jjg7zjg6vntZDmnpw6",
            "5Yem55CG5q616ZqOOiA=",
            "44Op44Oz44K/44Kk44OgOiA=",
            "44OH44O844K/44OZ44O844K5OiA=",
            "44Ot44KwOiA=",
            "Start-Transcript",
            "Stop-Transcript",
            "portable-install-{0}-{1}.log",
            '$env:LOCALAPPDATA',
            '$env:TEMP',
            '$InstallStage = "slash_skill"',
            '$InstallStage = "retire_agent003"',
            'Join-Path $Target "skills\\local-rag\\SKILL.md"',
            'Join-Path $env:APPDATA "Code\\User\\mcp.json"',
            '"retire",',
            '"--vscode-mcp-config", $VSCodeMcpTarget',
            "if ($null -ne $VSCodeMcpTarget)",
            'Join-Path $TargetQuery "copilot_cli_setup.py"',
            '"Local RAG slash Skill: "',
            '"Legacy Agent003 integration: "',
            "Use /local-rag in GitHub Copilot Chat",
            "did not enable MCP or change VS Code approval settings",
            "function Move-PublishedRuntime",
            "for ($Attempt = 1; $Attempt -le 4; $Attempt++)",
        ):
            self.assertIn(fragment, template)
        settings_index = template.index('$InstallStage = "retire_agent003"')
        self.assertLess(
            template.index(
                "Move-PublishedRuntime -Source $StageRuntime -Destination $TargetRuntime"
            ),
            settings_index,
        )
        self.assertLess(template.index('$DatabaseStatus = "READY"'), settings_index)
        self.assertEqual(1, template.count("copilot_cli_setup.py"))
        self.assertNotIn('Join-Path $TargetQuery "mcp_config.py"', template)
        self.assertNotIn("$VscodeText.Trim()", template)
        self.assertNotIn("list_dbs.py", template)
        self.assertNotIn("Read-Host", template)
        self.assertNotIn("Pause", template)
        self.assertNotIn("Copilot CLI MCP:", template)
        self.assertNotIn("Copilot CLI agents:", template)
        self.assertNotIn("launcher-scoped read-only approval", template)
        self.assertNotIn("Select one of the three LOCAL-RAG Agents", template)
        self.assertNotIn("APPDATA is required", template)
        self.assertNotIn(
            '"install",\n        "--copilot-home"', template
        )

        source_installer = (RAG_ROOT.parents[1] / "install.ps1").read_text(
            encoding="utf-8"
        )
        for fragment in (
            "Runtime:",
            "Databases:",
            "Local RAG slash Skill:",
            "Legacy Agent003 integration:",
            '$InstallStage = "slash_skill"',
            '$InstallStage = "retire_agent003"',
            '"retire",',
            'Join-Path $Target "rag\\query\\copilot_cli_setup.py"',
        ):
            self.assertIn(fragment, source_installer)
        self.assertEqual(1, source_installer.count("copilot_cli_setup.py"))
        self.assertNotIn(
            'Join-Path $Target "rag\\query\\mcp_config.py"',
            source_installer,
        )
        self.assertNotIn("--vscode-mcp-config", source_installer)
        self.assertNotIn("Copilot CLI MCP:", source_installer)
        self.assertNotIn("Copilot CLI agents:", source_installer)
        self.assertNotIn("launcher-scoped read-only approval", source_installer)
        self.assertNotIn(
            '"install",\n    "--copilot-home"', source_installer
        )

    def test_readmes_describe_slash_skill_and_approval_boundary(self) -> None:
        repository = RAG_ROOT.parents[1]
        documents = (
            (repository / "README.md").read_text(encoding="utf-8"),
            (
                repository
                / "tools"
                / "windows_portable"
                / "windows_package_builder.py"
            ).read_text(encoding="utf-8"),
            Path(windows_distribution.__file__).read_text(encoding="utf-8"),
        )
        combined = "\n".join(documents)
        self.assertNotIn("chat.tools.global.autoApprove", combined)
        self.assertNotIn("全tool／terminal command", combined)
        self.assertNotIn("localragagent003", combined)
        self.assertIn("/local-rag", combined)
        self.assertIn("slash", combined.casefold())
        self.assertIn("does not register an MCP server", combined)
        self.assertIn("does not change VS Code approval settings", combined)
        self.assertIn("-NoPause", combined)
        self.assertIn("portable-install-<timestamp>-<pid>.log", combined)
        self.assertIn("%LOCALAPPDATA%\\LocalRAG\\logs", combined)
        self.assertIn("Copilotによる実地受入はinstallerや", combined)

    def test_manager_routes_windows_distribution_to_offline_builder(self) -> None:
        manager = (RAG_ROOT / "manage.py").read_text(encoding="utf-8")
        cli = (RAG_ROOT / "make_distribution_package.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('sys.platform.startswith("win")', manager)
        self.assertIn("create_windows_distribution_package", manager)
        self.assertIn("=== Package creation: SUCCESS ===", manager)
        self.assertIn("=== Package creation: FAILED ===", manager)
        self.assertIn('sys.platform.startswith("win")', cli)
        self.assertIn("create_windows_distribution_package", cli)
        self.assertIn("=== Package creation: SUCCESS ===", cli)
        self.assertIn("=== Package creation: FAILED ===", cli)

    def test_result_contract_explicitly_marks_recipient_offline(self) -> None:
        source = Path(windows_distribution.__file__).read_text(encoding="utf-8")
        self.assertIn('"recipient_python_required": False', source)
        self.assertIn('"recipient_network_required": False', source)
        self.assertIn('"list_dbs_executed": False', source)
        self.assertIn('"real_search_executed": False', source)
        self.assertNotIn('str(list_dbs)', source)


class McpConfigContractTests(unittest.TestCase):
    def test_owned_config_is_fixed_lowercase_and_closed(self) -> None:
        self.assertEqual("localragagent003", mcp_config.SERVER_NAME)
        config = mcp_config.owned_server_config()
        self.assertEqual(
            "${userHome}\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe",
            config["command"],
        )
        self.assertEqual(
            [
                "-B",
                "${userHome}\\.copilot\\rag\\query\\mcp_server.py",
                "--rag-root",
                "${userHome}\\.copilot\\rag",
            ],
            config["args"],
        )
        self.assertEqual("${userHome}\\.copilot\\rag", config["cwd"])
        rendered = json.dumps(config)
        for forbidden in ("workspaceFolder", "http://", "https://", '"env"'):
            self.assertNotIn(forbidden, rendered)
        cli = mcp_config.owned_cli_server_config(Path(r"C:\Local RAG"))
        self.assertEqual("local", cli["type"])
        self.assertEqual(
            ["local_rag_search", "local_rag_get_evidence"], cli["tools"]
        )
        self.assertEqual(180000, cli["timeout"])
        self.assertTrue(Path(cli["command"]).is_absolute())
        self.assertEqual({"TEMP", "TMP"}, set(cli["env"]))
        self.assertTrue(
            all(Path(value).is_absolute() for value in cli["env"].values())
        )
        self.assertIn("--python", cli["args"])
        self.assertIn("--spool-root", cli["args"])
        self.assertNotIn("cwd", cli)

    def test_fresh_add_and_idempotence(self) -> None:
        source = "{}\n"
        patched = mcp_config.patch_mcp_config(source)
        self.assertEqual(
            mcp_config.owned_server_config(),
            _parse_mcp_config(patched)["servers"][mcp_config.SERVER_NAME],
        )
        self.assertEqual(patched, mcp_config.patch_mcp_config(patched))

    def test_preserves_bom_crlf_comments_inputs_and_unrelated_server(self) -> None:
        source = (
            "\ufeff{\r\n"
            "  // keep this comment\r\n"
            '  "inputs": [{"id":"keep-input"}],\r\n'
            '  "note": "literal comma,}",\r\n'
            '  "servers": {\r\n'
            '    "other": {"url":"https://example.test"}, // keep server\r\n'
            "  },\r\n"
            "}\r\n"
        )
        patched = mcp_config.patch_mcp_config(source)
        self.assertTrue(patched.startswith("\ufeff"))
        self.assertIn("// keep this comment\r\n", patched)
        self.assertIn("// keep server\r\n", patched)
        self.assertNotIn("\n", patched.replace("\r\n", ""))
        parsed = _parse_mcp_config(patched)
        self.assertEqual([{"id": "keep-input"}], parsed["inputs"])
        self.assertEqual("literal comma,}", parsed["note"])
        self.assertEqual(
            {"url": "https://example.test"}, parsed["servers"]["other"]
        )
        self.assertEqual(
            mcp_config.owned_server_config(),
            parsed["servers"][mcp_config.SERVER_NAME],
        )

    def test_exact_legacy_owned_revision_updates_only_its_value(self) -> None:
        legacy = mcp_config._legacy_owned_server_configs()[0]
        source = (
            '{"inputs":[1],"servers":{"other":{"command":"keep"},'
            f'"{mcp_config.SERVER_NAME}":'
            + json.dumps(legacy, separators=(",", ":"))
            + "}}\n"
        )
        patched = mcp_config.patch_mcp_config(source)
        parsed = _parse_mcp_config(patched)
        self.assertEqual([1], parsed["inputs"])
        self.assertEqual(
            {"command": "keep"}, parsed["servers"]["other"]
        )
        self.assertEqual(
            mcp_config.owned_server_config(),
            parsed["servers"][mcp_config.SERVER_NAME],
        )
        camel_source = source.replace(
            f'"{mcp_config.SERVER_NAME}"',
            f'"{mcp_config._LEGACY_SERVER_NAME}"',
            1,
        )
        camel_parsed = _parse_mcp_config(
            mcp_config.patch_mcp_config(camel_source)
        )
        self.assertNotIn(
            mcp_config._LEGACY_SERVER_NAME, camel_parsed["servers"]
        )
        self.assertEqual(
            mcp_config.owned_server_config(),
            camel_parsed["servers"][mcp_config.SERVER_NAME],
        )

    def test_foreign_collision_is_byte_invariant_and_creates_no_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".copilot"
            home.mkdir()
            target = home / mcp_config.MCP_CONFIG_NAME
            original = (
                b'{"mcpServers":{"localragagent003":'
                b'{"type":"local","command":"foreign"}}}\r\n'
            )
            target.write_bytes(original)
            with self.assertRaises(mcp_config.McpConfigCollisionError):
                mcp_config.configure_mcp(home)
            self.assertEqual(original, target.read_bytes())
            self.assertEqual([], list(home.glob("*.local-rag-backup-*")))

    def test_disk_write_is_atomic_optional_backup_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".copilot"
            home.mkdir()
            target = home / mcp_config.MCP_CONFIG_NAME
            original = b'\xef\xbb\xbf{\r\n  // keep\r\n  "mcpServers": {}\r\n}\r\n'
            target.write_bytes(original)
            with mock.patch.object(
                mcp_config.os,
                "replace",
                wraps=os.replace,
            ) as replace:
                result = mcp_config.configure_mcp(home)
            self.assertEqual("configured_on_disk", result["status"])
            self.assertEqual(1, replace.call_count)
            backup = Path(str(result["backup"]))
            self.assertEqual(original, backup.read_bytes())
            written = target.read_bytes()
            self.assertTrue(written.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", written)
            second = mcp_config.configure_mcp(home, create_backup=False)
            self.assertEqual("already_configured", second["status"])
            self.assertEqual(written, target.read_bytes())

    def test_dual_target_configuration_preserves_each_jsonc_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".copilot"
            vscode = root / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
            home.mkdir()
            vscode.parent.mkdir(parents=True)
            portable = home / mcp_config.MCP_CONFIG_NAME
            portable.write_bytes(
                b'{\r\n  // portable\r\n  "inputs": [],\r\n}\r\n'
            )
            vscode.write_bytes(
                b'\xef\xbb\xbf{\r\n  // vscode\r\n  "servers": '
                b'{"foreign":{"url":"https://example.test"}},\r\n}\r\n'
            )

            result = mcp_config.configure_mcp_targets(
                home, vscode, create_backup=False
            )

            self.assertEqual("configured_on_disk", result["status"])
            self.assertEqual(
                ["copilot_cli", "vscode_default_profile"],
                [target["kind"] for target in result["targets"]],
            )
            self.assertIn(b"// portable\r\n", portable.read_bytes())
            self.assertTrue(vscode.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"// vscode\r\n", vscode.read_bytes())
            self.assertIn(b'"foreign"', vscode.read_bytes())
            portable_parsed = _parse_mcp_config(
                portable.read_text(encoding="utf-8-sig")
            )
            self.assertEqual(
                mcp_config.owned_cli_server_config(home),
                portable_parsed["mcpServers"][mcp_config.SERVER_NAME],
            )
            vscode_parsed = _parse_mcp_config(
                vscode.read_text(encoding="utf-8-sig")
            )
            self.assertEqual(
                mcp_config.owned_server_config(),
                vscode_parsed["servers"][mcp_config.SERVER_NAME],
            )
            before = (portable.read_bytes(), vscode.read_bytes())
            second = mcp_config.configure_mcp_targets(
                home, vscode, create_backup=False
            )
            self.assertEqual("already_configured", second["status"])
            self.assertEqual(
                ["already_configured", "already_configured"],
                [target["status"] for target in second["targets"]],
            )
            self.assertEqual(before, (portable.read_bytes(), vscode.read_bytes()))

    def test_dual_target_collision_is_validated_before_either_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".copilot"
            vscode = root / "Code" / "User" / "mcp.json"
            home.mkdir()
            vscode.parent.mkdir(parents=True)
            portable = home / mcp_config.MCP_CONFIG_NAME
            portable_original = b'{"inputs":["keep"]}\n'
            vscode_original = (
                b'{"servers":{"localragagent003":'
                b'{"type":"http","url":"https://foreign.example"}}}\n'
            )
            portable.write_bytes(portable_original)
            vscode.write_bytes(vscode_original)

            with self.assertRaises(mcp_config.McpConfigCollisionError):
                mcp_config.configure_mcp_targets(home, vscode)

            self.assertEqual(portable_original, portable.read_bytes())
            self.assertEqual(vscode_original, vscode.read_bytes())
            self.assertEqual([], list(root.rglob("*.local-rag-backup-*")))

    def test_dual_target_second_write_failure_rolls_back_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".copilot"
            vscode = root / "Code" / "User" / "mcp.json"
            home.mkdir()
            vscode.parent.mkdir(parents=True)
            portable = home / mcp_config.MCP_CONFIG_NAME
            portable_original = b'{"portable":"keep"}\n'
            vscode_original = b'{"vscode":"keep"}\n'
            portable.write_bytes(portable_original)
            vscode.write_bytes(vscode_original)
            atomic_write = mcp_config._atomic_write_bytes
            failed = False

            def fail_vscode_once(path, content, *, boundary):
                nonlocal failed
                if Path(path) == vscode and not failed:
                    failed = True
                    raise OSError("injected second-target failure")
                return atomic_write(Path(path), content, boundary=Path(boundary))

            with mock.patch.object(
                mcp_config,
                "_atomic_write_bytes",
                side_effect=fail_vscode_once,
            ):
                with self.assertRaisesRegex(
                    OSError, "injected second-target failure"
                ):
                    mcp_config.configure_mcp_targets(
                        home, vscode, create_backup=False
                    )

            self.assertEqual(portable_original, portable.read_bytes())
            self.assertEqual(vscode_original, vscode.read_bytes())
            self.assertEqual([], list(root.rglob("*.local-rag-backup-*")))

    def test_dual_target_uninstall_preserves_foreign_jsonc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".copilot"
            vscode = root / "Code" / "User" / "mcp.json"
            home.mkdir()
            vscode.parent.mkdir(parents=True)
            portable = home / mcp_config.MCP_CONFIG_NAME
            portable.write_text(
                '{\n  "unknown": true,\n  "mcpServers": {}\n}\n',
                encoding="utf-8",
            )
            vscode.write_text(
                '{\n  // keep\n  "servers": '
                '{"foreign":{"url":"https://example.test"}}\n}\n',
                encoding="utf-8",
            )
            mcp_config.configure_mcp_targets(
                home,
                vscode,
                install_root=home,
                create_backup=False,
            )

            result = mcp_config.unconfigure_mcp_targets(
                home,
                vscode,
                install_root=home,
                create_backup=False,
            )

            self.assertEqual("configured_on_disk", result["status"])
            cli_document = _parse_mcp_config(
                portable.read_text(encoding="utf-8")
            )
            vscode_document = _parse_mcp_config(
                vscode.read_text(encoding="utf-8")
            )
            self.assertEqual(True, cli_document["unknown"])
            self.assertEqual({}, cli_document["mcpServers"])
            self.assertEqual(
                {"foreign": {"url": "https://example.test"}},
                vscode_document["servers"],
            )
            self.assertIn("// keep", vscode.read_text(encoding="utf-8"))

    def test_fresh_disk_write_can_disable_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".copilot"
            result = mcp_config.configure_mcp(home, create_backup=False)
            self.assertEqual("configured_on_disk", result["status"])
            self.assertIsNone(result["backup"])
            self.assertEqual(
                mcp_config.owned_cli_server_config(home),
                _parse_mcp_config(
                    (home / mcp_config.MCP_CONFIG_NAME).read_text(
                        encoding="utf-8"
                    )
                )["mcpServers"][mcp_config.SERVER_NAME],
            )

    def test_cli_merge_preserves_vscode_root_and_uninstall_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".copilot"
            home.mkdir()
            target = home / mcp_config.MCP_CONFIG_NAME
            original = (
                b'{\r\n  // keep VS Code compatibility\r\n'
                b'  "servers": {"foreign":{"command":"keep"},'
                b'"localragagent003":'
                + json.dumps(
                    mcp_config.owned_vscode_server_config(),
                    separators=(",", ":"),
                ).encode("utf-8")
                + b'},\r\n'
                b'  "unknown": {"keep":true}\r\n}\r\n'
            )
            target.write_bytes(original)
            configured = mcp_config.configure_mcp(
                home, install_root=home, create_backup=False
            )
            self.assertEqual("configured_on_disk", configured["status"])
            configured_bytes = target.read_bytes()
            self.assertIn(b'"servers"', configured_bytes)
            self.assertIn(b'"unknown"', configured_bytes)
            self.assertIn(b'"mcpServers"', configured_bytes)
            self.assertEqual(
                1, configured_bytes.count(b'"localragagent003"')
            )

            removed = mcp_config.unconfigure_mcp(
                home, install_root=home, create_backup=False
            )
            self.assertEqual("configured_on_disk", removed["status"])
            parsed = _parse_mcp_config(target.read_text(encoding="utf-8"))
            self.assertEqual(
                {"foreign": {"command": "keep"}}, parsed["servers"]
            )
            self.assertEqual({"keep": True}, parsed["unknown"])
            self.assertEqual({}, parsed["mcpServers"])
            second = mcp_config.unconfigure_mcp(
                home, install_root=home, create_backup=False
            )
            self.assertEqual("already_configured", second["status"])

    def test_rejects_reparse_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".copilot"
            home.mkdir()
            target = home / mcp_config.MCP_CONFIG_NAME
            original = b"{}\n"
            target.write_bytes(original)
            with mock.patch.object(
                mcp_config,
                "_is_reparse",
                side_effect=lambda path: Path(path) == target,
            ):
                with self.assertRaises(ValueError):
                    mcp_config.configure_mcp(home)
            self.assertEqual(original, target.read_bytes())

    def test_rejects_malformed_duplicate_and_non_object_servers(self) -> None:
        invalid = (
            '{"servers":[]}',
            '{"servers":',
            '{"servers":{},"servers":{}}',
            '{"servers":{"x":1,"x":2}}',
            '{"value":NaN}',
        )
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(ValueError):
                mcp_config.patch_mcp_config(source)
        cli_invalid = (
            '{"mcpServers":[]}',
            '{"mcpServers":',
            '{"mcpServers":{},"mcpServers":{}}',
            '{"mcpServers":{"x":1,"x":2}}',
        )
        for source in cli_invalid:
            with self.subTest(source=source), self.assertRaises(ValueError):
                mcp_config.patch_cli_mcp_config(source, Path(r"C:\LocalRAG"))

    def test_cli_returns_json_for_success_and_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".copilot"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = mcp_config.main(
                    ["--copilot-home", str(home), "--no-backup"]
                )
            self.assertEqual(0, code)
            self.assertEqual(
                "configured_on_disk", json.loads(output.getvalue())["status"]
            )

            target = home / mcp_config.MCP_CONFIG_NAME
            target.write_text(
                '{"mcpServers":{"localragagent003":{"command":"foreign"}}}',
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = mcp_config.main(
                    ["--copilot-home", str(home), "--no-backup"]
                )
            self.assertEqual(2, code)
            self.assertEqual(
                "collision", json.loads(output.getvalue())["status"]
            )


if __name__ == "__main__":
    unittest.main()
