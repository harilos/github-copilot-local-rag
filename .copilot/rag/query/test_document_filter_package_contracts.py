from __future__ import annotations

import types
import unittest
from pathlib import Path

from source_manager import packages
from source_manager.copy_only_packages import _without_bootstrap


class DocumentFilterPackageContractTests(unittest.TestCase):
    def test_generated_packages_include_document_filter_runtime(self) -> None:
        copilot_home = Path(__file__).resolve().parents[2]
        distribution, _databases = packages._distribution_entries(
            copilot_home,
            db_names=None,
        )
        admin, _databases = packages._admin_entries(
            copilot_home,
            db_names=None,
        )
        distribution_paths = {
            entry.destination for entry in distribution
        }
        admin_paths = {entry.destination for entry in admin}
        tool_prefix = (
            ".copilot/rag/gen_db/software_rag_tool/"
            "software_rag_tool/"
        )
        for filename in (
            "document_extensions.py",
            "extractors.py",
            "ingestion_paths.py",
            "records.py",
        ):
            self.assertIn(tool_prefix + filename, distribution_paths)
            self.assertIn(tool_prefix + filename, admin_paths)
        self.assertIn(
            ".copilot/rag/gen_db/add_data_documents_only.py",
            admin_paths,
        )
        self.assertNotIn(
            ".copilot/rag/gen_db/add_data_documents_only.py",
            distribution_paths,
        )

    def test_copy_only_package_contract_removes_bootstrap(self) -> None:
        entries = [
            types.SimpleNamespace(destination="bootstrap.py", mode="bootstrap"),
            types.SimpleNamespace(destination=".copilot/rag/search.py", mode="copy"),
        ]
        filtered = _without_bootstrap(entries)
        self.assertEqual(1, len(filtered))
        self.assertEqual(".copilot/rag/search.py", filtered[0].destination)


if __name__ == "__main__":
    unittest.main()
