from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import mock


MANAGER_PATH = Path(__file__).resolve().parents[1] / "manage.py"
MODULE_NAME = "local_rag_manage_source_menu_routing"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MANAGER_PATH)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = manage
SPEC.loader.exec_module(manage)


class ManagerSourceMenuRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-manager-source-menu-"
        )
        self.base = Path(self.temporary.name)
        self.rag_root = self.base / "rag"
        self.dbs_root = self.rag_root / "dbs"
        self.database = self.dbs_root / "example-rag"
        self.database.mkdir(parents=True)
        self.runtime = (
            self.rag_root / "query" / ".venv" / "bin" / "python"
        )
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(
        self,
        answers: list[str],
    ) -> tuple[Any, list[str]]:
        values = iter(answers)
        output: list[str] = []
        instance = manage.LocalRagManager(
            rag_root=self.rag_root,
            dbs_root=self.dbs_root,
            runtime_python=self.runtime,
            input_fn=lambda _prompt: next(values),
            output_fn=output.append,
        )
        return instance, output

    def test_runtime_menu_keeps_teams_gitlab_and_other_unique(
        self,
    ) -> None:
        manager, output = self.manager(["0"])

        manager._add_source_screen("example-rag")

        menu_entries: list[tuple[str, str]] = []
        for line in output:
            matched = re.fullmatch(r"([0-9]+)\. (.+)", line)
            if matched is not None:
                menu_entries.append(matched.groups())
        self.assertEqual(
            [
                ("1", "GitHubリポジトリ"),
                ("2", "SVN"),
                ("3", "Redmineプロジェクト"),
                (
                    "4",
                    "SharePoint同期フォルダ【追加・更新はWindowsのみ】",
                ),
                (
                    "5",
                    "Teams共有フォルダ【OneDrive同期・Windowsのみ】",
                ),
                ("6", "GitLab Issue"),
                ("7", "手元の資料を一度だけ取り込む（Other）"),
                ("0", "戻る"),
            ],
            menu_entries,
        )
        keys = [key for key, _label in menu_entries]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            "source_manager.teams_source",
            manager._add_source_screen.__module__,
        )

    def test_runtime_menu_routes_teams_gitlab_and_other_forms(
        self,
    ) -> None:
        routes = {
            "5": "_prompt_new_teams_source",
            "6": "_prompt_new_gitlab_issues_source",
            "7": "_prompt_new_other_source",
        }
        form_names = tuple(routes.values())

        for choice, expected_name in routes.items():
            with self.subTest(choice=choice, form=expected_name):
                manager, _output = self.manager([choice])
                patched: dict[str, Any] = {}
                with ExitStack() as stack:
                    for name in form_names:
                        patched[name] = stack.enter_context(
                            mock.patch.object(
                                manager,
                                name,
                                return_value=None,
                            )
                        )
                    manager._add_source_screen("example-rag")

                patched[expected_name].assert_called_once_with()
                for name in form_names:
                    if name != expected_name:
                        patched[name].assert_not_called()

    def test_gitlab_issue_link_uses_project_workflow_not_regex_prompts(
        self,
    ) -> None:
        manager, output = self.manager(["", "", ""])
        project_url = "https://gitlab.example.invalid/group/project"

        result = manager._prompt_source_link(
            existing={
                "display_name": "GitLab tickets",
                "enabled": True,
                "provider": "gitlab_issues",
                "strategy": "regex-template",
                "settings": {
                    "path_pattern": (
                        r"^issues/(?P<issue_iid>[0-9]+)\.md$"
                    ),
                    "url_template": (
                        f"{project_url}/-/issues/{{issue_iid}}"
                    ),
                },
            }
        )

        assert result is not None
        self.assertEqual("gitlab_issues", result["provider"])
        self.assertEqual("regex-template", result["strategy"])
        self.assertEqual(
            {
                "path_pattern": (
                    r"^issues/(?P<issue_iid>[0-9]+)\.md$"
                ),
                "url_template": (
                    f"{project_url}/-/issues/{{issue_iid}}"
                ),
            },
            result["settings"],
        )
        rendered = "\n".join(output)
        self.assertIn("GitLab Issueではリンク方式を自動設定", rendered)
        self.assertNotIn("正規表現テンプレートは上級者向け", rendered)


if __name__ == "__main__":
    unittest.main()
