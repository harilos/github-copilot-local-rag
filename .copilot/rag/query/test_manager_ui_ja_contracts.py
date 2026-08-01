from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MANAGER_PATH = Path(__file__).resolve().parents[1] / "manage.py"
SPEC = importlib.util.spec_from_file_location(
    "local_rag_manage_ui_ja",
    MANAGER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


class _Stream:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class ManagerJapaneseUiTests(unittest.TestCase):
    def manager(
        self,
        answers: list[str] | None = None,
        *,
        color: bool = False,
    ):
        outputs: list[str] = []
        values = iter(answers or [])
        temporary = tempfile.TemporaryDirectory(
            prefix="rag-manager-ui-ja-"
        )
        self.addCleanup(temporary.cleanup)
        rag_root = Path(temporary.name) / "rag"
        (rag_root / "dbs").mkdir(parents=True)
        runtime = rag_root / "query" / ".venv" / (
            "Scripts/python.exe"
            if sys.platform.startswith("win")
            else "bin/python"
        )
        runtime.parent.mkdir(parents=True)
        runtime.write_text("", encoding="utf-8")
        manager = manage.LocalRagManager(
            rag_root=rag_root,
            runtime_python=runtime,
            input_fn=lambda _prompt: next(values),
            output_fn=outputs.append,
            color=color,
        )
        return manager, outputs

    def test_non_tty_and_no_color_disable_ansi(self) -> None:
        self.assertFalse(manage.LocalRagManager._supports_color(_Stream(False)))
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(
                manage.LocalRagManager._supports_color(_Stream(True))
            )
            manager, _outputs = self.manager(color=True)
            self.assertFalse(manager.use_color)

    def test_error_has_text_label_and_optional_color(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("NO_COLOR", None)
            manager, outputs = self.manager(color=True)
            manager._print_error("入力が不正です。")
            self.assertIn("\033[31m", outputs[0])
            self.assertIn("[エラー]", outputs[0])

        plain, plain_outputs = self.manager(color=False)
        plain._print_error("入力が不正です。")
        self.assertNotIn("\033[", plain_outputs[0])
        self.assertEqual("[エラー] 入力が不正です。", plain_outputs[0])

    def test_required_field_retries_and_shows_example(self) -> None:
        manager, outputs = self.manager(["", "main"])
        value = manager._prompt_preserving_value(
            "ブランチ・タグ・コミット（ref）",
            "",
            required=True,
            description="GitHub上で通常表示する版です。",
            examples=("main",),
        )
        self.assertEqual("main", value)
        text = "\n".join(outputs)
        self.assertIn("【必須】", text)
        self.assertIn("[エラー]", text)
        self.assertIn("入力例: main", text)

    def test_optional_field_and_explicit_cancel_are_distinct(self) -> None:
        manager, outputs = self.manager([":q"])
        value = manager._prompt_preserving_value(
            "追加パス",
            "",
            required=False,
            empty_help="リポジトリ直下",
        )
        self.assertIsNone(value)
        self.assertIn("【任意】", "\n".join(outputs))

    def test_select_value_accepts_an_internal_default_on_enter(self) -> None:
        manager, _outputs = self.manager([""])
        selected = manager._select_value(
            "取り込む範囲",
            (
                ("recursive", "配下も含める"),
                ("direct", "この階層だけ"),
            ),
            default="recursive",
        )
        self.assertEqual("recursive", selected)

    def test_github_form_keeps_internal_contract_values(self) -> None:
        manager, outputs = self.manager(
            [
                "",
                "2",
                "1",
                "https://github.com/owner/repository",
                "release/v2",
                "product-a",
                "0123456789abcdef0123456789abcdef01234567",
            ]
        )
        value = manager._prompt_source_link()
        assert value is not None
        self.assertEqual("github", value["provider"])
        self.assertEqual("github-blob", value["strategy"])
        self.assertEqual("release/v2", value["settings"]["ref"])
        self.assertEqual(
            "product-a",
            value["settings"]["repository_path_prefix"],
        )
        text = "\n".join(outputs)
        self.assertIn("GitHubリポジトリ内の追加パス【任意】", text)
        self.assertIn("RAG保存パスから取り除くprefixではありません", text)
        self.assertIn("固定リンク用コミット【任意】", text)
        self.assertNotIn("リンク方式を選択", text)

    def test_git_repository_group_exposes_gitlab_and_azure_values(self) -> None:
        gitlab, gitlab_outputs = self.manager(
            [
                "",
                "2",
                "2",
                "https://gitlab.com/group/subgroup/repository",
                "release/v2",
                "",
                "",
            ]
        )
        gitlab_value = gitlab._prompt_source_link()
        assert gitlab_value is not None
        self.assertEqual("gitlab", gitlab_value["provider"])
        self.assertEqual("gitlab-blob", gitlab_value["strategy"])
        self.assertIn(
            "Gitホスティングサービスを選択",
            "\n".join(gitlab_outputs),
        )

        azure, azure_outputs = self.manager(
            [
                "",
                "2",
                "3",
                "https://dev.azure.com/organization/project/_git/repository",
                "main",
                "docs",
                "0123456789abcdef0123456789abcdef01234567",
            ]
        )
        azure_value = azure._prompt_source_link()
        assert azure_value is not None
        self.assertEqual("azure_devops", azure_value["provider"])
        self.assertEqual("azure-devops-item", azure_value["strategy"])
        self.assertEqual("docs", azure_value["settings"]["repository_path_prefix"])
        self.assertIn("ブランチ（ref）【必須】", "\n".join(azure_outputs))

    def test_existing_github_form_preserves_values(self) -> None:
        existing = {
            "provider": "github",
            "strategy": "github-blob",
            "enabled": True,
            "settings": {
                "repository_url": "https://github.com/owner/repository",
                "ref": "release/v2",
                "repository_path_prefix": "docs",
                "commit": "a" * 40,
                "permalink_enabled": True,
            },
        }
        manager, _outputs = self.manager(["", "", "", "", "", "", ""])
        value = manager._prompt_source_link(existing=existing)
        assert value is not None
        self.assertEqual("github", value["provider"])
        self.assertEqual("github-blob", value["strategy"])
        self.assertEqual(existing["settings"], value["settings"])

    def test_git_provider_switch_drops_previous_provider_settings(self) -> None:
        existing = {
            "provider": "github",
            "strategy": "github-blob",
            "enabled": True,
            "settings": {
                "repository_url": "https://github.com/owner/repository",
                "ref": "release/v1",
                "repository_path_prefix": "old-path",
                "commit": "a" * 40,
                "permalink_enabled": True,
            },
        }
        manager, _outputs = self.manager(
            [
                "",
                "",
                "2",
                "https://gitlab.com/group/repository",
                "main",
                "",
                "",
            ]
        )
        value = manager._prompt_source_link(existing=existing)
        assert value is not None
        self.assertEqual("gitlab", value["provider"])
        self.assertEqual("gitlab-blob", value["strategy"])
        self.assertEqual(
            {
                "repository_url": "https://gitlab.com/group/repository",
                "ref": "main",
                "permalink_enabled": False,
            },
            value["settings"],
        )

    def test_svn_forms_are_explicit_and_strategy_specific(self) -> None:
        svn_http, http_outputs = self.manager(
            [
                "",
                "5",
                "1",
                "https://svn.example.com/repos/project/trunk",
                "docs",
                "2",
                "1234",
            ]
        )
        http_value = svn_http._prompt_source_link()
        assert http_value is not None
        self.assertEqual("svn", http_value["provider"])
        self.assertEqual("svn-http", http_value["strategy"])
        self.assertEqual("1234", http_value["settings"]["revision"])
        self.assertIn(
            "Apache HTTP(S)互換（各ファイルを直接開く）",
            "\n".join(http_outputs),
        )

        svn_web, web_outputs = self.manager(
            [
                "",
                "5",
                "2",
                "https://svn-web.example.com/project/?view=summary#files",
            ]
        )
        web_value = svn_web._prompt_source_link()
        assert web_value is not None
        self.assertEqual("svn-web-root", web_value["strategy"])
        self.assertEqual(
            {
                "repository_url": (
                    "https://svn-web.example.com/project/"
                    "?view=summary#files"
                )
            },
            web_value["settings"],
        )
        web_text = "\n".join(web_outputs)
        self.assertNotIn("SVNリポジトリ内の追加パス【任意】", web_text)
        self.assertNotIn("SVNリビジョン番号", web_text)

    def test_svn_source_form_accepts_recent_update_window(self) -> None:
        manager, outputs = self.manager(
            [
                "https://svn.example.com/repos/project/trunk",
                "Recent specifications",
                "1",
                "4",
                "45",
                "1",
                "",
                "",
            ]
        )

        value = manager._prompt_new_svn_source()

        assert value is not None
        self.assertEqual("svn", value["source_type"])
        self.assertTrue(value["fetch"]["recursive"])
        self.assertEqual(45, value["fetch"]["updated_within_days"])
        self.assertIn(
            ("取得期間", "過去45日（SVN最終更新日時）"),
            value["summary"],
        )
        text = "\n".join(outputs)
        self.assertIn("ファイルのSVN最終更新日時", text)
        self.assertIn("制限しない【既定・従来どおり】", text)

    def test_svn_transport_source_defaults_to_no_browser_link(self) -> None:
        repository_url = "svn://127.0.0.1:3690/hogehoge-republic"
        manager, outputs = self.manager(
            [
                repository_url,
                "国家資料",
                "1",
                "5",
                "",
                "",
            ]
        )

        value = manager._prompt_new_svn_source()

        assert value is not None
        self.assertEqual(repository_url, value["fetch"]["repository_url"])
        self.assertNotIn("link", value)
        self.assertIn(
            "検索結果リンクを設定しない",
            "\n".join(outputs),
        )

    def test_svn_transport_source_accepts_separate_http_browser_url(self) -> None:
        manager, _outputs = self.manager(
            [
                "svn://127.0.0.1:3690/project",
                "Project",
                "1",
                "5",
                "1",
                "https://svn.example.com/repos/project",
                "",
            ]
        )

        value = manager._prompt_new_svn_source()

        assert value is not None
        self.assertEqual(
            "https://svn.example.com/repos/project",
            value["link"]["settings"]["repository_url"],
        )

    def test_http_svn_source_keeps_legacy_numeric_link_choices(self) -> None:
        http_manager, _outputs = self.manager(
            [
                "https://svn.example.com/repos/project",
                "Project",
                "1",
                "5",
                "1",
                "",
                "",
            ]
        )
        http_value = http_manager._prompt_new_svn_source()
        assert http_value is not None
        self.assertEqual("svn-http", http_value["link"]["strategy"])

        web_manager, _outputs = self.manager(
            [
                "https://svn.example.com/repos/project",
                "Project",
                "1",
                "5",
                "2",
                "https://svn-web.example.com/project",
                "",
            ]
        )
        web_value = web_manager._prompt_new_svn_source()
        assert web_value is not None
        self.assertEqual("svn-web-root", web_value["link"]["strategy"])

    def test_svn_browser_url_rejects_non_http_before_registration(self) -> None:
        manager, outputs = self.manager(
            [
                "svn://127.0.0.1:3690/project",
                "Project",
                "1",
                "5",
                "1",
                "svn://example.com/project",
            ]
        )

        value = manager._prompt_new_svn_source()

        self.assertIsNone(value)
        self.assertIn(
            "must be an HTTP or HTTPS URL",
            "\n".join(outputs),
        )

    def test_svn_strategy_switch_drops_hidden_settings(self) -> None:
        existing = {
            "provider": "svn",
            "strategy": "svn-http",
            "enabled": True,
            "settings": {
                "repository_url": "https://svn.example.com/repos/project",
                "repository_path_prefix": "docs",
                "permalink_enabled": True,
                "revision": 1234,
            },
        }
        manager, _outputs = self.manager(
            [
                "",
                "",
                "2",
                "https://svn-web.example.com/project#files",
            ]
        )
        value = manager._prompt_source_link(existing=existing)
        assert value is not None
        self.assertEqual("svn-web-root", value["strategy"])
        self.assertEqual(
            {"repository_url": "https://svn-web.example.com/project#files"},
            value["settings"],
        )

    def test_svn_web_root_preview_does_not_imply_path_use(self) -> None:
        class PreviewSourceLinks:
            @staticmethod
            def observed_root_from_paths(_paths):
                raise AssertionError(
                    "svn-web-root must not inspect observed roots"
                )

            @staticmethod
            def source_relative_path(_path, _root):
                raise AssertionError("svn-web-root must not derive a path")

        manager, outputs = self.manager()
        manager._print_source_link_preview(
            PreviewSourceLinks,
            {
                "provider": "svn",
                "strategy": "svn-web-root",
                "settings": {
                    "repository_url": (
                        "https://svn-web.example.com/project#files"
                    )
                },
            },
            ["Root/docs/manual.md"],
            preview=[
                {
                    "path": "Root/docs/manual.md",
                    "source_url": (
                        "https://svn-web.example.com/project#files"
                    ),
                    "status": "resolved",
                }
            ],
        )
        text = "\n".join(outputs)
        self.assertIn(
            "自動検出された保存ルート: このリンク方式では使用しません",
            text,
        )
        self.assertIn(
            "Source相対パス: このリンク方式では使用しません",
            text,
        )

    def test_search_result_is_human_summary_not_raw_json(self) -> None:
        manager, outputs = self.manager()
        manager._show_search_result(
            json.dumps(
                {
                    "status": "ok",
                    "answerability": "full",
                    "initial_response": {
                        "answer_draft_markdown": "要点です。[E1]",
                        "key_points": [
                            {
                                "text": "直接根拠から抽出した要点です。",
                                "source_ids": ["E1"],
                            }
                        ],
                        "limitations": [
                            "表の列見出しを確認できないため数値の意味は断定できません。"
                        ],
                    },
                    "evidence": [
                        {
                            "id": "E1",
                            "path": "Root/document.md",
                            "excerpt": "直接根拠です。",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        text = "\n".join(outputs)
        self.assertIn("検索結果", text)
        self.assertIn("要点です。[E1]", text)
        self.assertIn("直接根拠から抽出した要点です。 [E1]", text)
        self.assertIn("制限事項", text)
        self.assertIn("表の列見出しを確認できない", text)
        self.assertNotIn('"initial_response"', text)

    def test_manage_help_is_japanese_and_links_guide(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(MANAGER_PATH), "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode)
        self.assertIn("Sourceの追加・更新・再開", completed.stdout)
        self.assertIn(manage.MANAGER_HELP_URL, completed.stdout)


if __name__ == "__main__":
    unittest.main()
