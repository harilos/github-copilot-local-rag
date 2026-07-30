from __future__ import annotations

import types
import unittest

from source_manager import packages
from source_manager.copy_only_packages import _without_bootstrap


class DocumentFilterPackageContractTests(unittest.TestCase):
    def test_generated_packages_include_document_filter_runtime(self) -> None:
        self.assertIn(
            "document_extensions.py",
            packages._DISTRIBUTION_TOOL_MODULES,
        )
        self.assertIn(
            "add_data_documents_only.py",
            packages._ADMIN_GEN_DB_FILES,
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
