from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import windows_distribution  # noqa: E402


def _write_amd64_pe(path: Path) -> None:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, 0x8664)
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
        requirements = windows_distribution.SEARCH_REQUIREMENTS.read_text(
            encoding="utf-8"
        )
        for required in (
            "chromadb==",
            "onnxruntime==",
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
            with zipfile.ZipFile(output) as archive:
                install_cmd = archive.read("install.cmd").decode("ascii")
            self.assertIn("-ConfigureVSCodeAutoApprove", install_cmd)
            self.assertIn(
                "$SkipVSCodeAutoApprove",
                windows_distribution.INSTALL_TEMPLATE.read_text(
                    encoding="utf-8"
                ),
            )

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
                "C:\\Users\\builder\\private\n",
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
            "=== Local RAG install: SUCCESS ===",
            "=== Local RAG install: FAILED ===",
            "Failed stage:",
            "Runtime:",
            "Databases:",
            "VS Code approvals:",
            "Policy effectiveness:",
            '"CONFIGURED_ON_DISK"',
            '"SKIPPED_BY_USER"',
            '"FAILED"',
            '"UNKNOWN"',
        ):
            self.assertIn(fragment, template)
        settings_index = template.rindex("if ($ConfigureVSCodeAutoApprove) {")
        self.assertLess(
            template.index(
                "[System.IO.Directory]::Move($StageRuntime, $TargetRuntime)"
            ),
            settings_index,
        )
        self.assertLess(template.index('$DatabaseStatus = "READY"'), settings_index)
        self.assertNotIn("$VscodeText.Trim()", template)
        self.assertNotIn("list_dbs.py", template)

        source_installer = (RAG_ROOT.parents[1] / "install.ps1").read_text(
            encoding="utf-8"
        )
        for fragment in (
            "Runtime:",
            "Databases:",
            "VS Code approvals:",
            "Policy effectiveness:",
            '"CONFIGURED_ON_DISK"',
            '"SKIPPED_BY_USER"',
            '"FAILED"',
            '"UNKNOWN"',
        ):
            self.assertIn(fragment, source_installer)

    def test_readmes_describe_global_scope_opt_out_and_tool_boundary(self) -> None:
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
        self.assertIn("chat.tools.global.autoApprove", combined)
        self.assertIn("全tool／terminal command", combined)
        self.assertIn("-SkipVSCodeAutoApprove", combined)
        self.assertIn("runInTerminal", combined)
        self.assertIn("readFile", combined)
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


if __name__ == "__main__":
    unittest.main()
