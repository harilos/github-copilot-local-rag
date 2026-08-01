from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MANAGER_PATH = Path(__file__).resolve().parents[1] / "manage.py"
SPEC = importlib.util.spec_from_file_location(
    "local_rag_manage_github_content",
    MANAGER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manage
SPEC.loader.exec_module(manage)


class GitHubContentManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-manager-github-content-"
        )
        self.rag_root = Path(self.temporary.name) / "rag"
        self.dbs_root = self.rag_root / "dbs"
        (self.dbs_root / "example-rag").mkdir(parents=True)
        self.runtime = (
            self.rag_root / "query" / ".venv" / "Scripts" / "python.exe"
        )
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_bytes(b"")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(self, answers: list[str]):
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

    def test_source_menu_exposes_github_issues_and_wiki(self) -> None:
        manager, output = self.manager(["0"])
        manager._add_source_screen("example-rag")
        rendered = "\n".join(output)
        self.assertIn("GitHub Issues", rendered)
        self.assertIn("GitHub Wiki", rendered)

    def test_github_issue_form_uses_repository_and_generated_link(self) -> None:
        manager, _output = self.manager(
            ["https://github.com/gollum/gollum", "Gollum Issues"]
        )
        result = manager._prompt_new_github_issues_source()
        assert result is not None
        self.assertEqual(result["source_type"], "github_issues")
        self.assertEqual(result["fetch"]["state"], "all")
        self.assertEqual(result["link"]["strategy"], "regex-template")

    def test_github_wiki_form_derives_wiki_source(self) -> None:
        manager, _output = self.manager(
            ["https://github.com/gollum/gollum", "Gollum Wiki"]
        )
        result = manager._prompt_new_github_wiki_source()
        assert result is not None
        self.assertEqual(result["source_type"], "github_wiki")
        self.assertEqual(
            result["fetch"]["repository_url"],
            "https://github.com/gollum/gollum",
        )
        self.assertEqual(result["link"]["strategy"], "regex-template")


if __name__ == "__main__":
    unittest.main()
