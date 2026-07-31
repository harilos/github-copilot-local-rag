from __future__ import annotations

import json
import unittest

from source_manager.errors import SourceManagerError
from source_manager.gitlab_issue_fixes import parse_gitlab_api_project_web_url
from source_manager.gitlab_issues import GitLabProject, _fetch_discussions


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
