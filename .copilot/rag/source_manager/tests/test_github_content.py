from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from source_manager.errors import SourceManagerError
from source_manager.github_content import (
    fetch_github_issues,
    generated_github_issues_link,
    generated_github_wiki_link,
    parse_github_repository_url,
)
from source_manager.providers import build_fetch_plan, validate_provider_config
from source_manager.runner import register_source


class GitHubContentTests(unittest.TestCase):
    def test_repository_url_is_canonical_and_derives_wiki_clone(self) -> None:
        repository = parse_github_repository_url(
            "https://github.com/gollum/gollum/"
        )
        self.assertEqual(repository.slug, "gollum/gollum")
        self.assertEqual(
            repository.wiki_clone_url,
            "https://github.com/gollum/gollum.wiki.git",
        )

    def test_repository_url_rejects_non_top_pages_and_credentials(self) -> None:
        invalid = (
            "http://github.com/gollum/gollum",
            "https://token@github.com/gollum/gollum",
            "https://github.com/gollum/gollum/issues",
            "https://github.com/gollum",
            "https://github.com/gollum/gollum?tab=readme",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SourceManagerError):
                parse_github_repository_url(value)

    def test_provider_plans_are_stable_and_explicit(self) -> None:
        issues = validate_provider_config(
            "github_issues",
            {
                "repository_url": "https://github.com/gollum/gollum",
                "state": "open",
                "include_comments": False,
            },
        )
        self.assertEqual(issues["state"], "open")
        self.assertFalse(issues["include_comments"])
        issue_plan = build_fetch_plan(
            source_key="source",
            provider="github_issues",
            settings=issues,
            logical_root="sources/source/work",
            work_path="sources/source/work",
        )
        self.assertEqual(issue_plan.steps[0].operation, "github_fetch_issues")

        wiki_plan = build_fetch_plan(
            source_key="source",
            provider="github_wiki",
            settings={"repository_url": "https://github.com/gollum/gollum"},
            logical_root="sources/source/work",
            work_path="sources/source/work",
        )
        self.assertEqual(wiki_plan.steps[0].operation, "git_fetch_wiki")

    def test_generated_links_use_issue_number_and_wiki_page(self) -> None:
        issues = generated_github_issues_link(
            "https://github.com/gollum/gollum"
        )
        wiki = generated_github_wiki_link(
            "https://github.com/gollum/gollum"
        )
        self.assertEqual(
            issues["settings"]["url_template"],
            "https://github.com/gollum/gollum/issues/{number}",
        )
        self.assertEqual(
            wiki["settings"]["url_template"],
            "https://github.com/gollum/gollum/wiki/{page}",
        )

    def test_issue_fetch_materializes_issues_and_comments_atomically(self) -> None:
        issue = {
            "number": 12,
            "title": "Example issue",
            "state": "open",
            "body": "Issue body",
            "comments": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "user": {"login": "alice"},
            "labels": [{"name": "bug"}],
        }
        pull_request = {"number": 13, "pull_request": {"url": "ignored"}}
        comment = {
            "body": "Comment body",
            "created_at": "2026-01-03T00:00:00Z",
            "user": {"login": "bob"},
        }
        responses = [json.dumps([[issue, pull_request]]), json.dumps([[comment]])]

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, responses.pop(0), "")

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            work.mkdir()
            (work / "stale.txt").write_text("stale", encoding="utf-8")
            result = fetch_github_issues(
                {
                    "repository_url": "https://github.com/gollum/gollum",
                    "state": "all",
                    "include_comments": True,
                },
                work,
                runner,
            )
            self.assertEqual(result["documents"], 1)
            self.assertFalse((work / "stale.txt").exists())
            text = (work / "issues" / "12.md").read_text(encoding="utf-8")
            self.assertIn("Example issue", text)
            self.assertIn("Issue body", text)
            self.assertIn("Comment body", text)
            self.assertIn("https://github.com/gollum/gollum/issues/12", text)

    def test_failed_fetch_leaves_existing_work_unchanged(self) -> None:
        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 1, "", "not authenticated")

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            work.mkdir()
            original = work / "keep.txt"
            original.write_bytes(b"keep")
            with self.assertRaisesRegex(SourceManagerError, "GitHub API request failed"):
                fetch_github_issues(
                    {
                        "repository_url": "https://github.com/gollum/gollum",
                        "state": "all",
                        "include_comments": True,
                    },
                    work,
                    runner,
                )
            self.assertEqual(original.read_bytes(), b"keep")

    def test_registration_generates_pending_source_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary) / "example-rag"
            db_root.mkdir()
            result = register_source(
                db_root,
                source_type="github_issues",
                display_name="Issues",
                fetch={
                    "repository_url": "https://github.com/gollum/gollum",
                    "state": "all",
                    "include_comments": True,
                },
            )
            source_file = (
                db_root
                / "sources"
                / result["local_source_key"]
                / "source.json"
            )
            payload = json.loads(source_file.read_text(encoding="utf-8"))
            pending = payload["pending_metadata"]
            self.assertEqual(pending["source_type"], "github_issues")
            self.assertEqual(pending["link"]["strategy"], "regex-template")


if __name__ == "__main__":
    unittest.main()
