from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from source_manager import providers
from source_manager.gitlab_issues import gitlab_token_env
from source_manager.gitlab_wiki import (
    decode_gitlab_wiki_page_relative_path,
    fetch_gitlab_wiki,
    generated_gitlab_wiki_link,
    gitlab_wiki_page_relative_path,
    validate_gitlab_wiki_work_tree,
)


class GitLabWikiTests(unittest.TestCase):
    def test_provider_and_link_contract(self) -> None:
        settings = providers.validate_provider_config(
            "gitlab_wiki",
            {
                "gitlab_url": "https://gitlab.example",
                "project_url": "https://gitlab.example/group/project",
                "token_env": gitlab_token_env("https://gitlab.example"),
            },
        )
        self.assertEqual(
            "https://gitlab.example/group/project",
            settings["project_url"],
        )
        self.assertEqual(
            "gitlab-wiki",
            generated_gitlab_wiki_link(
                settings["project_url"], settings["gitlab_url"]
            )["strategy"],
        )

    def test_path_round_trip(self) -> None:
        relative = gitlab_wiki_page_relative_path("日本語/設計")
        self.assertEqual(
            "日本語/設計",
            decode_gitlab_wiki_page_relative_path(relative),
        )

    def test_fetch_writes_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            token_env = gitlab_token_env("https://gitlab.example")
            settings = {
                "gitlab_url": "https://gitlab.example",
                "project_url": "https://gitlab.example/group/project",
                "token_env": token_env,
            }
            responses = {
                "/api/v4/projects/group%2Fproject": {
                    "id": 9,
                    "web_url": "https://browser.example/group/project",
                    "path_with_namespace": "group/project",
                },
                "/api/v4/projects/9/wikis": [
                    {"slug": "home", "title": "Home"},
                    {"slug": "guide/start", "title": "Start"},
                ],
                "/api/v4/projects/9/wikis/home": {
                    "slug": "home",
                    "title": "Home",
                    "content": "Hello",
                },
                "/api/v4/projects/9/wikis/guide%2Fstart": {
                    "slug": "guide/start",
                    "title": "Start",
                    "content": "Guide",
                },
            }

            def request(url: str, _headers: dict[str, str]):
                path = url.split("https://gitlab.example", 1)[1].split("?", 1)[0]
                payload = responses[path]
                headers = {"X-Total": "2"} if path.endswith("/wikis") else {}
                return 200, json.dumps(payload).encode(), headers

            result = fetch_gitlab_wiki(
                settings,
                work,
                request,
                {token_env: "secret"},
            )
            self.assertEqual(2, result["documents"])
            self.assertEqual(
                2,
                validate_gitlab_wiki_work_tree(
                    settings,
                    work,
                    expected_documents=2,
                ),
            )


if __name__ == "__main__":
    unittest.main()
