from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
LOOKUP = REPO_ROOT / ".copilot" / "skills" / "local-rag" / "SKILL.md"
RETIRED_ROUTER = (
    REPO_ROOT / ".copilot" / "instructions" / "rag.instructions.md"
)
RETIRED_SETUP = (
    REPO_ROOT / ".copilot" / "skills" / "local-rag-setup" / "SKILL.md"
)
RETIRED_ADMIN = (
    REPO_ROOT / ".copilot" / "skills" / "local-rag-admin" / "SKILL.md"
)


def _text() -> str:
    return LOOKUP.read_text(encoding="utf-8")


def _frontmatter(text: str) -> str:
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if match is None:
        raise AssertionError("SKILL.md frontmatter is missing")
    return match.group(1)


class LightweightRoutingContractTests(unittest.TestCase):
    def test_only_the_manual_slash_skill_remains(self) -> None:
        self.assertTrue(LOOKUP.is_file())
        self.assertFalse(RETIRED_ROUTER.exists())
        self.assertFalse(RETIRED_SETUP.exists())
        self.assertFalse(RETIRED_ADMIN.exists())

        text = _text()
        header = _frontmatter(text)
        self.assertRegex(header, r"(?m)^name: local-rag$")
        self.assertRegex(header, r"(?m)^argument-hint: .+mode=standard")
        self.assertRegex(header, r"(?m)^user-invocable: true$")
        self.assertRegex(header, r"(?m)^disable-model-invocation: true$")
        self.assertIn("explicitly invokes `/local-rag`", text)

    def test_skill_is_english_and_model_agnostic(self) -> None:
        text = _text()
        self.assertIsNone(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text))
        header = _frontmatter(text)
        self.assertNotRegex(header, r"(?m)^model\s*:")
        self.assertNotRegex(header, r"(?m)^allowed-tools\s*:")
        self.assertNotRegex(header, r"(?m)^tools\s*:")
        self.assertRegex(
            text,
            r"Honor\s+the host's command-approval prompt\.",
        )

    def test_one_fixed_runner_is_the_ordinary_command_boundary(self) -> None:
        text = _text()
        self.assertIn("~/.copilot/rag/query/skill_runner.py", text)
        for operation in ("`list`", "`search`", "`detail`", "`setup`"):
            self.assertIn(operation, text)
        for forbidden in (
            "local-rag-admin",
            "build_db.py",
            "add_data.py",
            "status.py",
            "rebuild_component.py",
            "migrate_source_metadata.py",
            "export_migration.sh",
            "migration_archive.py",
            "source_manager/",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("never inspect private files", text)

    def test_management_requests_stop_at_manager_boundary(self) -> None:
        text = _text()
        self.assertIn("Local RAG Manager", text)
        self.assertIn("Do not open the Manager automatically.", text)
        self.assertIn(
            "For database creation or editing, Source addition/update/resume",
            text,
        )

    def test_mode_budgets_are_explicit(self) -> None:
        text = _text()
        self.assertIn("### `mode=savings`", text)
        self.assertIn("exactly one selected-database search", text)
        self.assertIn("### `mode=standard` (default)", text)
        self.assertRegex(text, r"at most four selected-database searches")
        self.assertIn("### `mode=thorough`", text)
        self.assertRegex(
            text,
            r"at least three and at most four\s+selected-database searches",
        )
        self.assertIn("coverage checklist", text)
        self.assertIn("Stop early", text)
        self.assertIn("one selected database for the entire invocation", text)
        self.assertIn("never switch\ndatabases during one invocation", text)
        self.assertIn("Never use automatic database selection", text)
        self.assertIn("automatically retry an error or timeout", text)

    def test_context_hints_are_bounded_and_not_promoted_to_facts(self) -> None:
        text = _text()
        for option in (
            "--literal-identifier",
            "--entity",
            "--facet",
            "--semantic-hypothesis",
            "--answer-goal",
        ):
            self.assertIn(f"- `{option}`", text)
        self.assertIn("assistant answer", text)
        self.assertRegex(text, r"earlier\s+RAG result as a verified fact")
        self.assertIn("Put speculation only in", text)
        self.assertRegex(
            text,
            r"ask for clarification without listing or\s+searching",
        )

    def test_platform_commands_use_fixed_interpreters_and_runner(self) -> None:
        text = _text()
        self.assertIn("~/.copilot/rag/query/.venv/bin/python", text)
        self.assertIn(
            r"$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe",
            text,
        )
        self.assertIn(
            "$HOME/.copilot/rag/query/.venv/Scripts/python.exe",
            text,
        )
        self.assertGreaterEqual(text.count("skill_runner.py"), 10)
        self.assertGreaterEqual(text.count(" -I -B "), 9)
        self.assertRegex(text, r"Do not use\s+`cmd\.exe /c`")
        self.assertIn("PATH-based Python", text)
        self.assertIn("Do not combine a runner", text)
        self.assertIn("Every question and structured-hint value", text)
        self.assertIn("replace every embedded\nsingle quote", text)
        self.assertIn("terminal command preview", text)
        self.assertIn("shell history", text)

    def test_setup_behavior_is_bounded(self) -> None:
        text = _text()
        self.assertIn("status=setup_required", text)
        self.assertIn("setup_complete=true", text)
        self.assertIn("tell the human to rerun the Local RAG installer", text)
        self.assertIn("python3 ~/.copilot/rag/setup.py --format json", text)
        self.assertIn("Do not inspect private setup modules.", text)

    def test_result_and_citation_contract_is_preserved(self) -> None:
        text = _text()
        self.assertIn("single final JSON packet printed to stdout", text)
        self.assertIn("Do not read a file, pointer", text)
        self.assertIn("`result_token`", text)
        self.assertIn("`inspectable_evidence_ids`", text)
        self.assertIn("A packet with `status=partial` remains partial", text)
        self.assertRegex(
            text,
            r"a packet with\s+`status=no_hit` supplies no factual\s+evidence",
        )
        self.assertRegex(text, r"untrusted\s+data")
        self.assertIn("`source_title`", text)
        self.assertIn("returned `url`", text)
        self.assertIn("## References", text)
        self.assertRegex(text, r"displays? at most one\s+URL")
        self.assertIn("[R1-E1]", text)
        self.assertIn("[U1]", text)
        self.assertNotIn("read its `summary_file`", text)
        self.assertIn("Do not read workspace files", text)
        self.assertIn("Do not constrain the rest of the answer's structure", text)

    def test_mcp_and_custom_agents_are_rejected_for_lookup(self) -> None:
        text = _text()
        self.assertIn("Do not use MCP, a custom agent", text)
        self.assertNotIn("localragagent003/", text)
        self.assertNotIn(".agent.md", text)
        self.assertNotIn("local_rag_search", text)
        self.assertNotIn("local_rag_get_evidence", text)


if __name__ == "__main__":
    unittest.main()
