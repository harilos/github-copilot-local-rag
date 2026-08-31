from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
SKILL = REPOSITORY_ROOT / ".copilot" / "skills" / "local-rag" / "SKILL.md"
LEGACY_AGENT_ROOT = REPOSITORY_ROOT / ".copilot" / "agents"
WORKSPACE_AGENT_ROOT = REPOSITORY_ROOT / ".github" / "agents"
WORKSPACE_PROMPT_ROOT = REPOSITORY_ROOT / ".github" / "prompts"
sys.path.insert(0, str(HERE))

import windows_package_builder as package_builder  # noqa: E402


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise AssertionError("skill frontmatter is missing")
    try:
        header, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise AssertionError("skill frontmatter is not closed") from exc
    values: dict[str, object] = {}
    for line in header.splitlines():
        key, separator, raw = line.partition(":")
        if not separator or not key or key in values:
            raise AssertionError(f"invalid skill frontmatter line: {line!r}")
        value = raw.strip()
        if value.startswith(("'", '"', "[")):
            values[key] = ast.literal_eval(value)
        elif value in {"true", "false"}:
            values[key] = value == "true"
        else:
            values[key] = value
    return values, body


class PersonalSkillContractTests(unittest.TestCase):
    def test_local_rag_is_one_user_invocable_slash_skill(self) -> None:
        frontmatter, body = _frontmatter(SKILL.read_text(encoding="utf-8"))

        self.assertEqual("local-rag", frontmatter["name"])
        self.assertTrue(frontmatter["description"])
        self.assertTrue(frontmatter["argument-hint"])
        self.assertIs(True, frontmatter["user-invocable"])
        self.assertIs(True, frontmatter["disable-model-invocation"])
        for custom_agent_field in ("agent", "model", "tools"):
            self.assertNotIn(custom_agent_field, frontmatter)

        self.assertIn("skill_runner.py", body)
        self.assertIn(" list", body)
        self.assertIn(" search --db", body)
        self.assertNotIn("localragagent003", body)
        self.assertNotIn("local_rag_search", body)
        self.assertNotIn("local_rag_get_evidence", body)

    def test_no_workspace_prompt_agent_or_mcp_definition_is_active(self) -> None:
        self.assertFalse((REPOSITORY_ROOT / ".vscode" / "mcp.json").exists())
        self.assertEqual([], list(LEGACY_AGENT_ROOT.glob("*.agent.md")))
        self.assertEqual([], list(WORKSPACE_AGENT_ROOT.glob("*.agent.md")))
        self.assertEqual([], list(WORKSPACE_PROMPT_ROOT.glob("*.prompt.md")))

    def test_default_package_exposes_only_the_local_rag_skill(self) -> None:
        entries = package_builder._SNAPSHOT_MODULE._product_entries(
            REPOSITORY_ROOT / ".copilot",
            admin=False,
        )
        destinations = {entry.destination for entry in entries}
        active_skills = {
            destination
            for destination in destinations
            if destination.startswith(".copilot/skills/")
            and destination.endswith("/SKILL.md")
        }

        self.assertEqual(
            {".copilot/skills/local-rag/SKILL.md"},
            active_skills,
        )
        self.assertFalse(
            any(
                destination.startswith(".copilot/agents/")
                or destination.startswith(".copilot/instructions/")
                or destination.startswith(".copilot/rag/copilot-cli/")
                for destination in destinations
            )
        )


if __name__ == "__main__":
    unittest.main()
