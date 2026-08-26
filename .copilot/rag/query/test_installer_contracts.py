from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from setup_contract import (
    completion_contract_payload,
    completion_contract_valid,
)


QUERY_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = QUERY_ROOT.parents[2]
INSTALL_SH = REPOSITORY_ROOT / "install.sh"
INSTALL_PS1 = REPOSITORY_ROOT / "install.ps1"


class InstallerExclusionContractTests(unittest.TestCase):
    @staticmethod
    def _write_current_valid_marker(
        rag_root: Path,
        marker: Path,
    ) -> None:
        query_requirements = rag_root / "query" / "requirements.txt"
        tool_requirements = (
            rag_root
            / "gen_db"
            / "software_rag_tool"
            / "requirements.txt"
        )
        query_requirements.parent.mkdir(parents=True, exist_ok=True)
        tool_requirements.parent.mkdir(parents=True, exist_ok=True)
        query_requirements.write_text(
            "-r ../gen_db/software_rag_tool/requirements.txt\n",
            encoding="utf-8",
        )
        tool_requirements.write_text("demo>=1\n", encoding="utf-8")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                completion_contract_payload(
                    runtime={
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
                    rag_root=rag_root,
                    verified_at="2026-07-27T00:00:00+00:00",
                )
            ),
            encoding="utf-8",
        )
        assert completion_contract_valid(marker, rag_root)[0]

    def test_posix_and_windows_installers_express_same_exclusions(self) -> None:
        shell = INSTALL_SH.read_text(encoding="utf-8")
        powershell = INSTALL_PS1.read_text(encoding="utf-8")

        for fragment in (
            "./rag/config/network.json",
            "./rag/config/manage-custom.json",
            "./rag/config/windows-test-connection.local.json",
            "./rag/query/run",
            "*/.venv",
            "*/__pycache__",
            "*.pyc",
            "*.pyo",
            "*/.DS_Store",
        ):
            self.assertIn(fragment, shell)
        for fragment in (
            "Test-InstallPayloadExcluded",
            r"rag\config\network.json",
            r"rag\config\manage-custom.json",
            r"rag\config\windows-test-connection.local.json",
            r"rag\query\run",
            '".venv"',
            '"__pycache__"',
            '".pyc"',
            '".pyo"',
            '".DS_Store"',
        ):
            self.assertIn(fragment, powershell)
        self.assertNotIn(
            "Remove-Item -LiteralPath $Target -Recurse",
            powershell,
        )
        self.assertEqual(
            2,
            powershell.count(
                "Remove-Item -LiteralPath $ProductBackupRoot -Recurse -Force `"
            ),
        )
        self.assertEqual(2, powershell.count("-ErrorAction Stop"))
        self.assertNotIn("rm -rf", shell)
        self.assertIn("--refresh-completion-marker", shell)
        self.assertIn("--refresh-completion-marker", powershell)
        self.assertNotIn("--migrate-legacy-marker", shell)
        self.assertNotIn("--migrate-legacy-marker", powershell)
        self.assertIn("exit 1", shell)
        self.assertIn("throw (", powershell)
        self.assertIn("setup_required:", shell)
        self.assertIn("setup_required:", powershell)
        self.assertIn('if [ -x "$RUNTIME_PYTHON" ]; then', shell)
        self.assertIn(
            "if (Test-Path -LiteralPath $RuntimePython -PathType Leaf)",
            powershell,
        )
        self.assertIn(".pre-update.", shell)
        self.assertIn(".pre-update.", powershell)
        self.assertIn('"$LEGACY_MARKER"', shell)
        self.assertNotIn('"$ACTIVE_MARKER"', shell)
        self.assertIn("Move-CompletionMarker", powershell)
        self.assertNotIn("$ActiveMarker", powershell)
        self.assertIn("$LegacyMarker", powershell)
        self.assertIn("Close-CompletionMarkerGate", powershell)
        for retired in (
            "rag/export_migration.sh",
            "rag/migration_archive.py",
            "rag/gen_db/migrate_source_metadata.py",
            (
                "rag/gen_db/software_rag_tool/software_rag_tool/"
                "source_metadata_migration.py"
            ),
            "skills/local-rag-admin/SKILL.md",
        ):
            self.assertIn(retired, shell)
            self.assertIn(retired.replace("/", "\\"), powershell)

    @unittest.skipIf(
        os.name == "nt",
        "POSIX installer execution is not applicable on Windows",
    )
    def test_posix_install_preserves_runtime_and_skips_transients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            payload = source / ".copilot" / "rag"
            query = payload / "query"
            target = root / "target"
            target_query = target / "rag" / "query"

            source.mkdir()
            shutil.copy2(INSTALL_SH, source / "install.sh")
            (query / "run").mkdir(parents=True)
            (query / ".venv" / "bin").mkdir(parents=True)
            (query / "__pycache__").mkdir(parents=True)
            (payload / "config").mkdir(parents=True)
            (query / "search.py").write_text(
                "print('installed source')\n",
                encoding="utf-8",
            )
            (payload / "config" / "network.json").write_text(
                '{"source":"must-not-copy"}\n',
                encoding="utf-8",
            )
            (payload / "config" / "manage-custom.json").write_text(
                '{"source":"must-not-copy"}\n',
                encoding="utf-8",
            )
            (
                payload
                / "config"
                / "windows-test-connection.local.json"
            ).write_text(
                '{"source":"must-not-copy"}\n',
                encoding="utf-8",
            )
            (query / "run" / "ragd.json").write_text(
                '{"pid":82059,"source":"stale"}\n',
                encoding="utf-8",
            )
            (query / "run" / "source-only.tmp").write_text(
                "transient\n",
                encoding="utf-8",
            )
            (query / ".venv" / "bin" / "python").write_text(
                "source venv\n",
                encoding="utf-8",
            )
            (query / ".venv" / ".rag-deps-installed").write_text(
                "source marker\n",
                encoding="utf-8",
            )
            (query / "__pycache__" / "module.pyc").write_bytes(b"pyc")
            (query / "module.pyc").write_bytes(b"pyc")
            (query / "module.pyo").write_bytes(b"pyo")
            (query / ".DS_Store").write_bytes(b"finder")

            (target / "rag" / "config").mkdir(parents=True)
            (target_query / "run").mkdir(parents=True)
            (target_query / ".venv" / "bin").mkdir(parents=True)
            target_network = target / "rag" / "config" / "network.json"
            target_manage_custom = (
                target / "rag" / "config" / "manage-custom.json"
            )
            target_windows_test = (
                target
                / "rag"
                / "config"
                / "windows-test-connection.local.json"
            )
            target_state = target_query / "run" / "ragd.json"
            target_python = target_query / ".venv" / "bin" / "python"
            target_marker = (
                target_query / ".venv" / ".rag-deps-installed"
            )
            retired_files = (
                target / "rag" / "export_migration.sh",
                target / "rag" / "migration_archive.py",
                target / "rag" / "gen_db" / "migrate_source_metadata.py",
                (
                    target
                    / "rag"
                    / "gen_db"
                    / "software_rag_tool"
                    / "software_rag_tool"
                    / "source_metadata_migration.py"
                ),
                target / "skills" / "local-rag-admin" / "SKILL.md",
            )
            for retired in retired_files:
                retired.parent.mkdir(parents=True, exist_ok=True)
                retired.write_text("retired\n", encoding="utf-8")
            refresh_log = root / "refresh-arguments.txt"
            target_network.write_text(
                '{"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_manage_custom.write_text(
                '{"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_windows_test.write_text(
                '{"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_state.write_text(
                '{"pid":1234,"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_python.write_text(
                (
                    "#!/bin/sh\n"
                    f"printf '%s\\n' \"$@\" > '{refresh_log}'\n"
                    "printf '%s\\n' '{\"refreshed\":true}' > "
                    "\"$(dirname \"$0\")/../.rag-deps-installed\"\n"
                    "exit 0\n"
                ),
                encoding="utf-8",
            )
            target_python.chmod(0o755)
            self._write_current_valid_marker(
                target / "rag",
                target_marker,
            )
            before = {
                path: path.read_bytes()
                for path in (
                    target_network,
                    target_manage_custom,
                    target_windows_test,
                    target_state,
                    target_python,
                )
            }

            environment = os.environ.copy()
            environment["COPILOT_HOME"] = str(target)
            environment["HOME"] = str(root / "home")
            completed = subprocess.run(
                ["sh", str(source / "install.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=completed.stderr,
            )
            for path, contents in before.items():
                self.assertEqual(contents, path.read_bytes(), msg=str(path))
            self.assertEqual(
                '{"refreshed":true}\n',
                target_marker.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                [],
                list(target_marker.parent.glob(
                    ".rag-deps-installed.pre-update.*"
                )),
            )
            self.assertEqual(
                "print('installed source')\n",
                (target_query / "search.py").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "--refresh-completion-marker",
                refresh_log.read_text(encoding="utf-8"),
            )
            for relative in (
                "run/source-only.tmp",
                "__pycache__/module.pyc",
                "module.pyc",
                "module.pyo",
                ".DS_Store",
            ):
                self.assertFalse(
                    (target_query / relative).exists(),
                    msg=relative,
                )
            for retired in retired_files:
                self.assertFalse(retired.exists(), msg=str(retired))
            self.assertFalse(
                (target / "skills" / "local-rag-admin").exists()
            )

    @unittest.skipIf(
        os.name == "nt",
        "POSIX installer execution is not applicable on Windows",
    )
    def test_posix_install_fails_closed_when_runtime_refresh_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            query = source / ".copilot" / "rag" / "query"
            target = root / "target"
            target_query = target / "rag" / "query"
            query.mkdir(parents=True)
            shutil.copy2(INSTALL_SH, source / "install.sh")
            (query / "setup.py").write_text(
                "raise SystemExit(99)\n",
                encoding="utf-8",
            )
            (query / "product.py").write_text("NEW\n", encoding="utf-8")
            (target_query / ".venv" / "bin").mkdir(parents=True)
            (target_query / "product.py").write_text("OLD\n", encoding="utf-8")
            target_python = target_query / ".venv" / "bin" / "python"
            target_python.write_text(
                "#!/bin/sh\nexit 7\n",
                encoding="utf-8",
            )
            target_python.chmod(0o755)
            marker = target_query / ".venv" / ".rag-deps-installed"
            self._write_current_valid_marker(target / "rag", marker)
            marker_before = marker.read_bytes()
            completed = subprocess.run(
                ["sh", str(source / "install.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={
                    **os.environ,
                    "COPILOT_HOME": str(target),
                    "HOME": str(root / "home"),
                },
                timeout=30,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("setup_required:", completed.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual("NEW\n", (target_query / "product.py").read_text())
            backups = list(target_query.glob(
                ".rag-deps-installed.*.pre-update.*"
            ))
            self.assertEqual(1, len(backups))
            self.assertEqual(marker_before, backups[0].read_bytes())

    @unittest.skipIf(
        os.name == "nt",
        "POSIX installer execution is not applicable on Windows",
    )
    def test_posix_copy_failure_leaves_lookup_gate_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source_query = source / ".copilot" / "rag" / "query"
            target = root / "target"
            target_query = target / "rag" / "query"
            source_query.mkdir(parents=True)
            shutil.copy2(INSTALL_SH, source / "install.sh")
            (source_query / "search.py").write_text(
                "print('source')\n",
                encoding="utf-8",
            )
            (target_query / "search.py").mkdir(parents=True)
            marker = target_query / ".venv" / ".rag-deps-installed"
            self._write_current_valid_marker(target / "rag", marker)
            marker_before = marker.read_bytes()
            completed = subprocess.run(
                ["sh", str(source / "install.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={
                    **os.environ,
                    "COPILOT_HOME": str(target),
                    "HOME": str(root / "home"),
                },
                timeout=30,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(marker.exists())
            backups = list(target_query.glob(
                ".rag-deps-installed.*.pre-update.*"
            ))
            self.assertEqual(1, len(backups))
            self.assertEqual(marker_before, backups[0].read_bytes())


    @unittest.skipUnless(
        os.name == "nt",
        "PowerShell installer execution is Windows-specific",
    )
    def test_powershell_managed_marker_refresh_and_failure_gate_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            query = source / ".copilot" / "rag" / "query"
            query.mkdir(parents=True)
            shutil.copy2(INSTALL_PS1, source / "install.ps1")
            (query / "setup.py").write_text(
                "from pathlib import Path\n"
                "import os\n"
                "marker = Path(__file__).parent / '.venv' / '.rag-deps-installed'\n"
                "marker.parent.mkdir(parents=True, exist_ok=True)\n"
                "marker.write_bytes(b'new-marker')\n"
                "raise SystemExit(9 if os.environ.get('FAIL_REFRESH') else 0)\n",
                encoding="utf-8",
            )
            (query / "product.py").write_text("NEW\n", encoding="utf-8")
            rag = source / ".copilot" / "rag"
            (rag / "list_dbs.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8"
            )
            for filename in (
                "mcp_config.py",
                "mcp_server.py",
                "copilot_cli_setup.py",
            ):
                shutil.copy2(QUERY_ROOT / filename, query / filename)
            shutil.copytree(
                QUERY_ROOT.parent / "copilot-cli",
                rag / "copilot-cli",
            )

            def run_case(
                name: str,
                *,
                fail: bool,
                runtime: bool,
                cli_collision: bool = False,
            ):
                target = root / name
                target_query = target / "rag" / "query"
                (target_query / ".venv" / "Scripts").mkdir(parents=True)
                (target_query / ".packaged-runtime.json").write_text(
                    '{"schema":"fixture"}\n', encoding="utf-8"
                )
                (target_query / ".rag-deps-installed").write_bytes(b"retired")
                if runtime:
                    shutil.copy2(
                        Path(sys.executable),
                        target_query / ".venv" / "Scripts" / "python.exe",
                    )
                marker = target_query / ".venv" / ".rag-deps-installed"
                marker.write_bytes(b"old-marker")
                (target_query / "product.py").write_text("OLD\n", encoding="utf-8")
                if cli_collision:
                    (target / "mcp-config.json").write_bytes(
                        b'{"mcpServers":{"localragagent003":{"type":"http",'
                        b'"url":"https://foreign.example"}}}\n'
                    )
                environment = os.environ.copy()
                environment["COPILOT_HOME"] = str(target)
                environment["USERPROFILE"] = str(root / (name + "-profile"))
                environment["LOCAL_RAG_COPILOT_PROFILE_PATH"] = str(
                    root / (name + "-profile.ps1")
                )
                if fail:
                    environment["FAIL_REFRESH"] = "1"
                completed = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(source / "install.ps1"),
                        "-Target",
                        str(target),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    timeout=30,
                    check=False,
                )
                return completed, target_query, marker

            completed, target_query, legacy = run_case(
                "managed-success", fail=False, runtime=True
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(b"new-marker", legacy.read_bytes())
            self.assertFalse((target_query / ".rag-deps-installed").exists())
            self.assertFalse((target_query / ".packaged-runtime.json").exists())
            self.assertEqual(
                [], list(target_query.glob(".rag-deps-installed.*.pre-update.*"))
            )
            target = target_query.parents[1]
            self.assertTrue((target / "mcp-config.json").is_file())
            self.assertTrue((target / "copilot-cli" / "owned-manifest.json").is_file())
            shared_agents = {
                "local-rag-agent003-savings.agent.md",
                "local-rag-agent003-standard.agent.md",
                "local-rag-agent003-thorough.agent.md",
            }
            self.assertEqual(
                shared_agents,
                {path.name for path in (target / "agents").glob("*.agent.md")},
            )
            for filename in shared_agents:
                self.assertTrue((target / "agents" / filename).is_file())
            for filename in (
                "internal-doc-search.agent.md",
                "agent003-readonly-local-rag.agent.md",
                "internal-doc-deep-research.agent.md",
            ):
                self.assertFalse((target / "agents" / filename).exists())
            profile_path = root / "managed-success-profile.ps1"
            self.assertIn(
                "# >>> Local RAG Agent003 CLI (owned) >>>",
                profile_path.read_text(encoding="utf-8"),
            )
            for summary in (
                "Copilot CLI MCP: configured",
                "Copilot CLI agents: installed",
                "Copilot CLI launcher-scoped read-only approval: enabled",
            ):
                self.assertIn(summary, completed.stdout)
            self.assertRegex(
                completed.stdout,
                r"(?m)^Copilot CLI executable: (detected|not_detected)\r?$",
            )
            for label in (
                "Copilot CLI MCP:",
                "Copilot CLI agents:",
                "Copilot CLI launcher-scoped read-only approval:",
                "Copilot CLI executable:",
            ):
                self.assertEqual(1, completed.stdout.count(label))

            collision, collision_query, _ = run_case(
                "cli-collision",
                fail=False,
                runtime=True,
                cli_collision=True,
            )
            self.assertNotEqual(0, collision.returncode)
            collision_target = collision_query.parents[1]
            self.assertEqual(
                b'{"mcpServers":{"localragagent003":{"type":"http",'
                b'"url":"https://foreign.example"}}}\n',
                (collision_target / "mcp-config.json").read_bytes(),
            )
            self.assertEqual(
                "OLD\n",
                (collision_query / "product.py").read_text(encoding="utf-8"),
            )
            self.assertFalse((collision_target / "agents").exists())
            self.assertFalse((collision_target / "copilot-cli").exists())
            self.assertFalse((root / "cli-collision-profile.ps1").exists())
            self.assertIn(
                "Copilot CLI MCP: blocked_collision",
                collision.stdout,
            )
            for label in (
                "Copilot CLI MCP:",
                "Copilot CLI agents:",
                "Copilot CLI launcher-scoped read-only approval:",
                "Copilot CLI executable:",
            ):
                self.assertEqual(1, collision.stdout.count(label))

            for name, runtime in (("postvalidate-failure", True), ("python-missing", False)):
                completed, target_query, marker = run_case(
                    name, fail=True, runtime=runtime
                )
                self.assertNotEqual(0, completed.returncode)
                self.assertFalse(marker.exists())
                self.assertEqual("OLD\n", (target_query / "product.py").read_text())
                backups = list(target_query.glob(
                    ".rag-deps-installed.*.pre-update.*"
                ))
                self.assertEqual(1, len(backups))
                self.assertEqual(b"old-marker", backups[0].read_bytes())


    def test_generic_installers_keep_only_managed_marker_contract(self) -> None:
        module_path = (
            REPOSITORY_ROOT
            / ".copilot"
            / "rag"
            / "source_manager"
            / "package_installers.py"
        )
        spec = importlib.util.spec_from_file_location(
            "installer_contract_fixture", module_path
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        portable = (
            REPOSITORY_ROOT
            / "tools"
            / "windows_portable"
            / "install-template.ps1"
        ).read_text(encoding="utf-8")
        shell_entrypoints = (
            INSTALL_SH.read_text(encoding="utf-8"),
            module.INSTALL_SH_TEXT,
        )
        powershell_entrypoints = (
            INSTALL_PS1.read_text(encoding="utf-8"),
            module.INSTALL_PS1_TEXT,
        )
        for text in shell_entrypoints:
            for fragment in (
                'LEGACY_MARKER="$QUERY_ROOT/.venv/.rag-deps-installed"',
                'move_marker "$LEGACY_MARKER" legacy',
                "close_markers",
            ):
                self.assertIn(fragment, text)
            for fragment in ("PACKAGED_MANIFEST", "ACTIVE_MARKER", ' active'):
                self.assertNotIn(fragment, text)
        for fragment in (
            '$ActiveMarker = Join-Path',
            '$LegacyMarker = Join-Path',
            'Move-CompletionMarker',
            'Close-CompletionMarkerGate',
            '--refresh-completion-marker',
        ):
            self.assertNotIn(fragment, portable)
        self.assertIn('Assert-Amd64PortableRuntime', portable)
        for text in powershell_entrypoints:
            for fragment in (
                '$LegacyMarker = Join-Path',
                'Move-CompletionMarker',
                'Close-CompletionMarkerGate',
                '"legacy"',
            ):
                self.assertIn(fragment, text)
            for fragment in ('$PackagedManifest =', '$ActiveMarker =', '"active"'):
                self.assertNotIn(fragment, text)


    @unittest.skipUnless(os.name == "nt", "PowerShell execution is Windows-specific")
    def test_generated_powershell_installer_retires_packaged_state(self) -> None:
        module_path = (
            REPOSITORY_ROOT / ".copilot" / "rag" / "source_manager" / "package_installers.py"
        )
        spec = importlib.util.spec_from_file_location("generated_installer_fixture", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            query = source / ".copilot" / "rag" / "query"
            query.mkdir(parents=True)
            (source / "install.ps1").write_text(module.INSTALL_PS1_TEXT, encoding="utf-8")
            (query / "setup.py").write_text(
                "from pathlib import Path\n"
                "marker = Path(__file__).parent / '.venv' / '.rag-deps-installed'\n"
                "marker.parent.mkdir(parents=True, exist_ok=True)\n"
                "marker.write_bytes(b'new')\n",
                encoding="utf-8",
            )
            target = root / "target"
            target_query = target / "rag" / "query"
            scripts = target_query / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(Path(sys.executable), scripts / "python.exe")
            (target_query / ".packaged-runtime.json").write_text(
                '{"schema":"fixture"}\n', encoding="utf-8"
            )
            active = target_query / ".rag-deps-installed"
            active.write_bytes(b"old")
            managed = target_query / ".venv" / ".rag-deps-installed"
            managed.write_bytes(b"old-managed")
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(source / "install.ps1"), "-Target", str(target),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(b"new", managed.read_bytes())
            self.assertFalse(active.exists())
            self.assertFalse((target_query / ".packaged-runtime.json").exists())
            self.assertEqual(
                [], list(target_query.glob(".rag-deps-installed.*.pre-update.*"))
            )


if __name__ == "__main__":
    unittest.main()
