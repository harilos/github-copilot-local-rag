from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from source_manager import gitlab_wiki_runtime, providers
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

    def test_package_contract_does_not_require_retired_allowlist(self) -> None:
        class CurrentPackages:
            pass

        packages = CurrentPackages()
        gitlab_wiki_runtime._install_package_contract(packages)
        self.assertTrue(
            getattr(packages, gitlab_wiki_runtime._PACKAGE_MARKER)
        )
        self.assertFalse(hasattr(packages, "_DISTRIBUTION_TOOL_MODULES"))

    def test_fetch_confirms_inventory_before_page_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            access_url = "https://gitlab.example/internal-gitlab"
            token_env = gitlab_token_env(access_url)
            settings = {
                "gitlab_url": access_url,
                "project_url": f"{access_url}/group/project",
                "token_env": token_env,
            }
            responses = {
                "/internal-gitlab/api/v4/projects/group%2Fproject": {
                    "id": 9,
                    "web_url": (
                        "https://browser.example/external-gitlab/group/project"
                    ),
                    "path_with_namespace": "group/project",
                },
                "/internal-gitlab/api/v4/projects/9/wikis": [
                    {"slug": "home", "title": "Home"},
                    {"slug": "guide/start", "title": "Start"},
                ],
                "/internal-gitlab/api/v4/projects/9/wikis/home": {
                    "slug": "home",
                    "title": "Home",
                    "content": "Hello",
                },
                "/internal-gitlab/api/v4/projects/9/wikis/guide%2Fstart": {
                    "slug": "guide/start",
                    "title": "Start",
                    "content": "Guide",
                },
            }
            order: list[str] = []

            def request(url: str, _headers: dict[str, str]):
                self.assertTrue(url.startswith(access_url))
                path = url.split("https://gitlab.example", 1)[1].split("?", 1)[0]
                payload = responses[path]
                headers = {"X-Total": "2"} if path.endswith("/wikis") else {}
                if path.endswith("/wikis"):
                    order.append("inventory")
                elif "/wikis/" in path:
                    self.assertEqual(["inventory", "confirmation"], order[:2])
                    order.append("detail")
                return 200, json.dumps(payload).encode(), headers

            def confirm_inventory(items: list[object]) -> None:
                self.assertEqual(2, len(items))
                order.append("confirmation")

            result = fetch_gitlab_wiki(
                settings,
                work,
                request,
                {token_env: "secret"},
                inventory_callback=confirm_inventory,
            )
            self.assertEqual(2, result["documents"])
            self.assertEqual("inventory", order[0])
            self.assertEqual("confirmation", order[1])
            self.assertEqual(2, order.count("detail"))
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
