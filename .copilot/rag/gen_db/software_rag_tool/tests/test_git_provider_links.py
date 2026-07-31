from __future__ import annotations

import unittest

from software_rag_tool import source_links


class GitProviderLinkTests(unittest.TestCase):
    def test_exact_azure_provider_uses_azure_root_validation(self) -> None:
        valid = source_links.validate_source_link(
            {
                "enabled": True,
                "provider": "azure-devops",
                "strategy": "azure-devops-item",
                "settings": {
                    "repository_url": (
                        "https://dev.azure.com/example/"
                        "project/_git/repository"
                    ),
                    "ref": "main",
                    "permalink_enabled": False,
                },
            }
        )
        self.assertEqual("azure-devops", valid["provider"])

        with self.assertRaises(source_links.SourceLinkError):
            source_links.validate_source_link(
                {
                    "enabled": True,
                    "provider": "azure-devops",
                    "strategy": "azure-devops-item",
                    "settings": {
                        "repository_url": (
                            "https://example.com/not-an-azure-repository"
                        ),
                        "ref": "main",
                        "permalink_enabled": False,
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
