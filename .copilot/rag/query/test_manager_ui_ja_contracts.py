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

    def test_github_form_keeps_internal_contract_values(self) -> None:
        manager, outputs = self.manager(
            [
                "",
                "2",
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
        self.assertIn("ローカルRAGの初期設定", completed.stdout)
        self.assertIn(manage.MANAGER_HELP_URL, completed.stdout)


if __name__ == "__main__":
    unittest.main()
