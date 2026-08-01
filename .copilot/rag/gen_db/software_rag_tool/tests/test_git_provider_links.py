from __future__ import annotations

import unittest

from software_rag_tool import source_links
from software_rag_tool.git_provider_links import install_git_provider_link_runtime


class GitProviderLinkTests(unittest.TestCase):
    def test_runtime_install_is_idempotent(self) -> None:
        normalize = source_links._normalize_repository_url
        install_git_provider_link_runtime(source_links)
        install_git_provider_link_runtime(source_links)
        self.assertIs(normalize, source_links._normalize_repository_url)

    def _validate_repository(
        self,
        *,
        provider: str,
        strategy: str,
        repository_url: str,
    ) -> dict[str, object]:
        return source_links.validate_source_link(
            {
                "enabled": True,
                "provider": provider,
                "strategy": strategy,
                "settings": {
                    "repository_url": repository_url,
                    "ref": "main",
                    "permalink_enabled": False,
                },
            }
        )

    def test_github_and_gitlab_repository_roots_remain_valid(self) -> None:
        github = self._validate_repository(
            provider="github",
            strategy="github-blob",
            repository_url="https://github.com/example/repository.git",
        )
        self.assertEqual(
            "https://github.com/example/repository",
            github["settings"]["repository_url"],
        )
        gitlab = self._validate_repository(
            provider="gitlab",
            strategy="gitlab-blob",
            repository_url="https://gitlab.example.com/group/repository.git",
        )
        self.assertEqual(
            "https://gitlab.example.com/group/repository",
            gitlab["settings"]["repository_url"],
        )

    def test_github_and_gitlab_file_urls_are_rejected(self) -> None:
        invalid = (
            (
                "github",
                "github-blob",
                "https://github.com/example/repository/blob/main/README.md",
            ),
            (
                "github",
                "github-blob",
                "https://github.com/example/repository/tree/main/docs",
            ),
            (
                "gitlab",
                "gitlab-blob",
                "https://gitlab.example.com/group/repository/-/blob/main/README.md",
            ),
            (
                "gitlab",
                "gitlab-blob",
                "https://gitlab.example.com/group/repository/-/tree/main/docs",
            ),
        )
        for provider, strategy, repository_url in invalid:
            with self.subTest(repository_url=repository_url):
                with self.assertRaises(source_links.SourceLinkError):
                    self._validate_repository(
                        provider=provider,
                        strategy=strategy,
                        repository_url=repository_url,
                    )

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
