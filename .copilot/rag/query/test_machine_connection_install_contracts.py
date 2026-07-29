from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class MachineConnectionInstallContracts(unittest.TestCase):
    def test_installers_exclude_active_machine_connection_files(self) -> None:
        shell = (REPOSITORY_ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (REPOSITORY_ROOT / "install.ps1").read_text(encoding="utf-8")
        for relative in (
            "source-connections.json",
            "source-connections.secrets.json",
            ".source-connections.key",
        ):
            with self.subTest(relative=relative):
                self.assertIn(relative, shell)
                self.assertIn(relative, powershell)

    def test_machine_connection_files_are_gitignored(self) -> None:
        ignored = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
        for relative in (
            ".copilot/rag/config/source-connections.json",
            ".copilot/rag/config/source-connections.secrets.json",
            ".copilot/rag/config/.source-connections.key",
        ):
            with self.subTest(relative=relative):
                self.assertIn(relative, ignored)


if __name__ == "__main__":
    unittest.main()
