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
            "sys.version_info >= (3, 10)",
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
        self.assertNotIn("Remove-Item", self.installer)

    def test_runtime_is_created_on_the_admin_machine_not_copied(self) -> None:
        self.assertIn('($Parts -icontains ".venv")', self.installer)
        self.assertIn('SetupArguments @("--format", "json")', self.installer)
        self.assertNotIn("Copy-Item -Recurse", self.installer)

    def test_vscode_auto_approve_is_explicit_and_uses_installed_runtime(self) -> None:
        for fragment in (
            "[switch]$ConfigureVSCodeAutoApprove",
            "if ($ConfigureVSCodeAutoApprove)",
            '"rag\\query\\vscode_settings.py"',
            "--copilot-home $Target",
            '"configured_on_disk", "already_configured"',
            "Restart VS Code",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.installer)

    def test_result_is_unambiguous_and_identifies_the_failed_stage(self) -> None:
        for fragment in (
            "=== Local RAG install: SUCCESS ===",
            "=== Local RAG install: FAILED ===",
            "Failed stage: $InstallStage",
            '$InstallStage = "validate_payload"',
            '$InstallStage = "runtime_create"',
            '$InstallStage = "list_dbs"',
            '$InstallStage = "vscode_auto_approve"',
            'Runtime: $RuntimeStatus',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.installer)


if __name__ == "__main__":
    unittest.main()
