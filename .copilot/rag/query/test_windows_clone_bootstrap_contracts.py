from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class WindowsCloneBootstrapContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = (REPOSITORY_ROOT / "install.ps1").read_text(
            encoding="utf-8"
        )

    def test_fresh_clone_builds_runtime_and_verifies_public_entrypoint(self) -> None:
        for fragment in (
            "Resolve-BootstrapPython",
            'Command = "py"',
            'Command = "python"',
            "(3, 13) <= sys.version_info[:2] < (3, 14)",
            'SetupArguments @("--format", "json")',
            '"rag\\list_dbs.py"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.installer)

    def test_source_clone_does_not_copy_database_payload(self) -> None:
        self.assertIn('($Parts[1] -ieq "dbs")', self.installer)
        self.assertIn(
            "Existing databases were not overwritten",
            self.installer,
        )
        self.assertNotIn('Join-Path $Target "rag\\dbs"', self.installer)

    def test_runtime_is_created_on_the_admin_machine_not_copied(self) -> None:
        self.assertIn('($Parts -icontains ".venv")', self.installer)
        self.assertIn('SetupArguments @("--format", "json")', self.installer)
        self.assertNotIn("Copy-Item -Recurse", self.installer)

    def test_mcp_config_uses_installed_runtime_without_global_approval(self) -> None:
        for fragment in (
            "[switch]$ConfigureVSCodeAutoApprove",
            'Join-Path $Target "rag\\query\\copilot_cli_setup.py"',
            '"--copilot-home", $CopilotCliHome',
            '"--install-root", $Target',
            '"--profile-path", $CopilotProfilePath',
            "$env:COPILOT_HOME",
            "$env:USERPROFILE",
            "$env:LOCAL_RAG_COPILOT_PROFILE_PATH",
            'Write-Host "Copilot CLI MCP:',
            'Write-Host "Copilot CLI agents:',
            "Copilot CLI launcher-scoped read-only approval:",
            'Write-Host "Copilot CLI executable:',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.installer)
        self.assertEqual(1, self.installer.count("copilot_cli_setup.py"))
        self.assertNotIn(
            'Join-Path $Target "rag\\query\\mcp_config.py"',
            self.installer,
        )
        self.assertNotIn("if ($ConfigureVSCodeAutoApprove)", self.installer)
        self.assertNotIn('Join-Path $Target "rag\\query\\vscode_settings.py"', self.installer)

    def test_result_is_unambiguous_and_identifies_the_failed_stage(self) -> None:
        for fragment in (
            "=== Local RAG install: SUCCESS ===",
            "=== Local RAG install: FAILED ===",
            "Failed stage: $InstallStage",
            '$InstallStage = "validate_payload"',
            '$InstallStage = "runtime_create"',
            '$InstallStage = "list_dbs"',
            '$InstallStage = "copilot_cli_setup"',
            'Runtime: $RuntimeStatus',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.installer)


if __name__ == "__main__":
    unittest.main()
