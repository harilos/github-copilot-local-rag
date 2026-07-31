from __future__ import annotations

import json
import unittest

from source_manager.errors import SourceManagerError
from source_manager.gitlab_issue_fixes import parse_gitlab_api_project_web_url
from source_manager.gitlab_issues import GitLabProject, _fetch_discussions


class GitLabIssueFixTests(unittest.TestCase):
    def test_api_hostname_can_differ_from_configured_access_hostname(self) -> None:
        parsed = parse_gitlab_api_project_web_url(
            "https://browser.example/group/project",
            "https://access.example",
        )
        self.assertEqual("https://access.example/group/project", parsed.project_url)
        self.assertEqual("group/project", parsed.project_path)

    def test_empty_page_before_x_total_is_complete_fails_immediately(self) -> None:
        project = GitLabProject(
            "https://gitlab.example",
            "https://gitlab.example/api/v4",
            "https://gitlab.example/group/project",
            "group/project",
            1,
        )
        calls: list[str] = []
        first = [{"id": str(index), "notes": []} for index in range(20)]

        def request(url: str, _headers: dict[str, str]):
            calls.append(url)
            payload = first if len(calls) == 1 else []
            return 200, json.dumps(payload).encode(), {
                "X-Total": "22",
                "X-Next-Page": "",
            }

        with self.assertRaisesRegex(
            SourceManagerError,
            "gitlab_discussions_changed",
        ):
            _fetch_discussions(
                project,
                1,
                request,
                {},
                progress_callback=None,
            )
        self.assertEqual(2, len(calls))

    def test_zero_total_empty_page_is_valid(self) -> None:
        project = GitLabProject(
            "https://gitlab.example",
            "https://gitlab.example/api/v4",
            "https://gitlab.example/group/project",
            "group/project",
            1,
        )
        result = _fetch_discussions(
            project,
            1,
            lambda _url, _headers: (200, b"[]", {"X-Total": "0"}),
            {},
            progress_callback=None,
        )
        self.assertEqual([], result)


if __name__ == "__main__":
    unittest.main()
