from __future__ import annotations

import sys
import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_links
from source_manager.github_content import (
    generated_github_issues_link,
    generated_github_wiki_link,
)


class GitHubContentLinkTests(unittest.TestCase):
    def test_issue_document_resolves_to_github_issue(self) -> None:
        source = {
            "source_id": "source-a",
            "source_type": "github_issues",
            "link": generated_github_issues_link(
                "https://github.com/gollum/gollum"
            ),
        }
        preview = source_links.resolve_mapping_preview(
            source,
            ["root/issues/12.md"],
        )
        self.assertEqual(preview[0]["status"], "resolved")
        self.assertEqual(
            preview[0]["source_url"],
            "https://github.com/gollum/gollum/issues/12",
        )

    def test_wiki_document_resolves_to_github_wiki_page(self) -> None:
        source = {
            "source_id": "source-a",
            "source_type": "github_wiki",
            "link": generated_github_wiki_link(
                "https://github.com/gollum/gollum"
            ),
        }
        preview = source_links.resolve_mapping_preview(
            source,
            ["root/Home.md"],
        )
        self.assertEqual(preview[0]["status"], "resolved")
        self.assertEqual(
            preview[0]["source_url"],
            "https://github.com/gollum/gollum/wiki/Home",
        )


if __name__ == "__main__":
    unittest.main()
