from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from source_manager.errors import SourceManagerError
from source_manager.gitlab_issue_fixes import parse_gitlab_api_project_web_url
from source_manager.gitlab_issues import (
    GitLabIssueInventoryItem,
    GitLabProject,
    _changed_issue_iids,
    _fetch_discussions,
    gitlab_issue_markdown,
)


class GitLabIssueFixTests(unittest.TestCase):
    def project(self) -> GitLabProject:
        return GitLabProject(
            "https://gitlab.example",
            "https://gitlab.example/api/v4",
            "https://gitlab.example/group/project",
            "group/project",
            1,
        )

    def test_api_hostname_can_differ_from_configured_access_hostname(self) -> None:
        parsed = parse_gitlab_api_project_web_url(
            "https://browser.example/group/project",
            "https://access.example",
        )
        self.assertEqual("https://access.example/group/project", parsed.project_url)
        self.assertEqual("group/project", parsed.project_path)

    def test_missing_next_page_before_total_uses_collected_discussions(self) -> None:
        calls: list[str] = []
        progress: list[dict[str, object]] = []
        first = [{"id": str(index), "notes": []} for index in range(20)]

        def request(url: str, _headers: dict[str, str]):
            calls.append(url)
            return 200, json.dumps(first).encode(), {
                "X-Total": "22",
                "X-Next-Page": "",
            }

        result = _fetch_discussions(
            self.project(),
            1,
            request,
            {},
            progress_callback=progress.append,
        )
        self.assertEqual(20, len(result))
        self.assertEqual(1, len(calls))
        self.assertEqual(
            "provider.pagination_fallback",
            progress[-1]["event"],
        )
        self.assertEqual(
            "no_next_page_before_total",
            progress[-1]["reason"],
        )

    def test_partial_discussions_are_retried_on_next_update(self) -> None:
        discussions = _fetch_discussions(
            self.project(),
            1,
            lambda _url, _headers: (
                200,
                b'[{"id":"one","notes":[]}]',
                {"X-Total": "2", "X-Next-Page": ""},
            ),
            {},
            progress_callback=None,
        )
        updated_at = "2026-07-31T00:00:00Z"
        markdown = gitlab_issue_markdown(
            self.project(),
            {
                "iid": 1,
                "id": 101,
                "title": "Example",
                "description": "Body",
                "updated_at": updated_at,
                "created_at": updated_at,
                "user_notes_count": 2,
                "state": "opened",
                "author": {"name": "User", "username": "user"},
                "assignees": [],
                "labels": [],
            },
            discussions,
        )
        self.assertIn('"discussions_complete":false', markdown)
        self.assertIn("次回更新時に再取得します", markdown)

        with tempfile.TemporaryDirectory() as temporary:
            issues = Path(temporary)
            (issues / "1.md").write_text(markdown, encoding="utf-8")
            inventory = [
                GitLabIssueInventoryItem(
                    iid=1,
                    issue_id=101,
                    updated_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
                    updated_at_text=updated_at,
                    user_notes_count=2,
                )
            ]
            self.assertEqual(
                [1],
                _changed_issue_iids(
                    inventory,
                    issues,
                    updated_after=None,
                ),
            )

    def test_invalid_next_page_uses_current_page(self) -> None:
        result = _fetch_discussions(
            self.project(),
            1,
            lambda _url, _headers: (
                200,
                b'[{"id":"one","notes":[]}]',
                {"X-Next-Page": "same"},
            ),
            {},
            progress_callback=None,
        )
        self.assertEqual(["one"], [item["id"] for item in result])

    def test_discussion_schema_error_still_fails(self) -> None:
        with self.assertRaisesRegex(
            SourceManagerError,
            "discussion schema is invalid",
        ):
            _fetch_discussions(
                self.project(),
                1,
                lambda _url, _headers: (
                    200,
                    b'[{"id":"one","notes":"invalid"}]',
                    {},
                ),
                {},
                progress_callback=None,
            )

    def test_zero_total_empty_page_is_valid(self) -> None:
        result = _fetch_discussions(
            self.project(),
            1,
            lambda _url, _headers: (200, b"[]", {"X-Total": "0"}),
            {},
            progress_callback=None,
        )
        self.assertEqual([], result)


if __name__ == "__main__":
    unittest.main()
