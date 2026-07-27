from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ROUTER = REPO_ROOT / ".copilot" / "instructions" / "rag.instructions.md"
LOOKUP = REPO_ROOT / ".copilot" / "skills" / "local-rag" / "SKILL.md"
ADMIN = REPO_ROOT / ".copilot" / "skills" / "local-rag-admin" / "SKILL.md"


class LightweightRoutingContractTests(unittest.TestCase):
    def test_copilot_instruction_and_skill_bodies_are_english(self) -> None:
        for path in (ROUTER, LOOKUP, ADMIN):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text),
                path,
            )

    def test_router_separates_lookup_from_administration(self) -> None:
        text = ROUTER.read_text(encoding="utf-8")
        self.assertIn("Use the `local-rag` skill for ordinary lookup.", text)
        self.assertIn("Use the `local-rag-admin` skill only for setup", text)
        self.assertIn("Do not load administrative instructions during ordinary lookup.", text)

    def test_lookup_contract_is_one_list_then_one_search(self) -> None:
        text = LOOKUP.read_text(encoding="utf-8")
        self.assertIn("Run `list_dbs.py --format json` exactly once.", text)
        self.assertIn(
            "Pass the user's complete original question as the final argument",
            text,
        )
        self.assertIn("## One-command decision", text)
        self.assertIn("Never run `list_dbs.py` merely to confirm", text)
        self.assertIn("Do not issue a second search automatically.", text)
        self.assertIn("--compact-json", text)
        self.assertIn("Do not run", text)
        self.assertIn("`jq`", text)
        self.assertIn("--result-delivery file", text)
        self.assertIn("Read the returned `summary_file` exactly", text)
        self.assertIn("Run `result_detail.py` exactly once.", text)
        self.assertIn("do not run", text)
        self.assertIn("`list_dbs.py` or `search.py` again", text)
        self.assertIn("source path", text)
        self.assertIn("Do not strengthen", text)
        self.assertIn("not \"destroyed\"", text)
        self.assertIn("Do not use `--auto`.", text)
        self.assertNotRegex(text, r"(?m)^\s*--retrieval-mode(?:\s|$)")

    def test_lookup_uses_only_platform_venv_commands(self) -> None:
        text = LOOKUP.read_text(encoding="utf-8")
        self.assertIn("~/.copilot/rag/query/.venv/bin/python", text)
        self.assertIn(
            r"$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe",
            text,
        )
        self.assertIn("Do not try `python`,", text)
        self.assertIn("`--literal-identifier`", text)
        self.assertIn("Do not use `cmd.exe /c`", text)

    def test_admin_preserves_required_management_operations(self) -> None:
        text = ADMIN.read_text(encoding="utf-8").lower()
        for operation in (
            "setup",
            "proxy",
            "certificate",
            "create",
            "build",
            "add",
            "status",
            "resume",
            "force rebuild",
            "component rebuild",
        ):
            self.assertIn(operation, text)

    def test_no_skill_sets_a_model_or_creates_an_agent_contract(self) -> None:
        for path in (ROUTER, LOOKUP, ADMIN):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^model\s*:")
            self.assertNotIn(".agent.md", text)


if __name__ == "__main__":
    unittest.main()
