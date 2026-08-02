from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
INSTALLERS = (
    REPOSITORY_ROOT / "install.sh",
    REPOSITORY_ROOT / "install.ps1",
)
RETIRED_PATHS = (
    "rag/export_migration.sh",
    "rag/migration_archive.py",
    "rag/gen_db/migrate_source_metadata.py",
    (
        "rag/gen_db/software_rag_tool/software_rag_tool/"
        "source_metadata_migration.py"
    ),
    "rag/query/portable_db_install.py",
    "rag/query/portable_db_smoke.py",
    "skills/local-rag-admin/SKILL.md",
)


class LegacyMigrationExportTombstoneTests(unittest.TestCase):
    def test_retired_files_are_absent_from_the_current_payload(self) -> None:
        payload = REPOSITORY_ROOT / ".copilot"
        for relative in RETIRED_PATHS:
            self.assertFalse(
                payload.joinpath(*Path(relative).parts).exists(),
                relative,
            )

    def test_both_overlay_installers_remove_every_exact_tombstone(
        self,
    ) -> None:
        shell = INSTALLERS[0].read_text(encoding="utf-8")
        powershell = INSTALLERS[1].read_text(encoding="utf-8")
        for relative in RETIRED_PATHS:
            self.assertIn(relative, shell)
            self.assertIn(relative.replace("/", "\\"), powershell)

    def test_installers_do_not_prune_database_or_user_directories(self) -> None:
        shell = INSTALLERS[0].read_text(encoding="utf-8")
        powershell = INSTALLERS[1].read_text(encoding="utf-8")
        self.assertNotIn("rm -rf", shell)
        self.assertNotIn("Remove-Item", powershell)
        self.assertNotIn(r"rag\dbs", powershell)
        self.assertNotIn("rag/dbs", shell)


if __name__ == "__main__":
    unittest.main()
