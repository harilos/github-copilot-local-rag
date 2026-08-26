from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
AGENTS_ROOT = REPOSITORY_ROOT / ".copilot" / "rag" / "copilot-cli"
LEGACY_AGENT_ROOT = REPOSITORY_ROOT / ".copilot" / "agents"
WORKSPACE_AGENT_ROOT = REPOSITORY_ROOT / ".github" / "agents"
sys.path.insert(0, str(HERE))

import windows_package_builder as package_builder  # noqa: E402


AGENTS = {
    "local-rag-agent003-savings.agent.md": {
        "name": "LOCAL-RAG-節約",
        "description": "Local RAGを検索し、必要最小限の根拠で簡潔に回答します。",
        "cap": "at most two search calls",
    },
    "local-rag-agent003-standard.agent.md": {
        "name": "LOCAL-RAG-標準",
        "description": "Local RAGを検索し、根拠を確認してバランスよく回答します。",
        "cap": "five total tool calls",
    },
    "local-rag-agent003-thorough.agent.md": {
        "name": "LOCAL-RAG-徹底検索",
        "description": "Local RAGを複数の観点から検索し、根拠を突き合わせて回答します。",
        "cap": "seven total tool calls cap",
    },
}
TOOLS = [
    "localragagent003/local_rag_search",
    "localragagent003/local_rag_get_evidence",
]


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise AssertionError("agent frontmatter is missing")
    try:
        header, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise AssertionError("agent frontmatter is not closed") from exc
    values: dict[str, object] = {}
    for line in header.splitlines():
        key, separator, raw = line.partition(":")
        if not separator or not key or key in values:
            raise AssertionError(f"invalid agent frontmatter line: {line!r}")
        value = raw.strip()
        if value.startswith("["):
            values[key] = ast.literal_eval(value)
        elif value in {"true", "false"}:
            values[key] = value == "true"
        else:
            values[key] = value
    return values, body


class CustomAgentContractTests(unittest.TestCase):
    def test_shared_agent_schema_and_tool_allowlists(self) -> None:
        self.assertEqual(
            set(AGENTS),
            {path.name for path in AGENTS_ROOT.glob("*.agent.md")},
        )
        for filename, expected in AGENTS.items():
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            frontmatter, body = _frontmatter(text)
            self.assertEqual(expected["name"], frontmatter["name"])
            self.assertEqual(expected["description"], frontmatter["description"])
            self.assertEqual(TOOLS, frontmatter["tools"])
            self.assertIs(False, frontmatter["infer"])
            self.assertNotIn("target", frontmatter)
            self.assertNotIn("model", frontmatter)
            self.assertIn(expected["cap"], body)
            self.assertIn("local_rag_search", body)
            self.assertIn("local_rag_get_evidence", body)
            self.assertIn("permission", body)
            self.assertIn("same turn", body)

    def test_agents_have_no_pointer_or_non_rag_tool_detour(self) -> None:
        for filename in AGENTS:
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"```(?:powershell|shell|bash)")
            self.assertNotIn("summary_file", text)
            self.assertNotIn("result_set_id", text)
            self.assertNotIn("Get-Content", text)
            self.assertNotIn("runInTerminal", text)
            self.assertNotIn("session_store_sql", text)

    def test_legacy_and_workspace_agent_definitions_are_absent(self) -> None:
        self.assertEqual([], list(LEGACY_AGENT_ROOT.glob("*.agent.md")))
        self.assertEqual([], list(WORKSPACE_AGENT_ROOT.glob("*.agent.md")))

    def test_both_windows_distribution_paths_use_setup_owned_agents(self) -> None:
        entries = package_builder._SNAPSHOT_MODULE._product_entries(
            REPOSITORY_ROOT / ".copilot",
            admin=False,
        )
        packaged = {
            entry.destination
            for entry in entries
            if entry.destination.startswith(".copilot/agents/")
        }
        self.assertEqual(set(), packaged)

        portable_template = HERE / "install-template.ps1"
        manager_template = (
            REPOSITORY_ROOT
            / ".copilot"
            / "rag"
            / "source_manager"
            / "windows-install-template.ps1"
        )
        self.assertEqual(portable_template.read_bytes(), manager_template.read_bytes())
        template = portable_template.read_text(encoding="utf-8")
        self.assertNotIn("Test-LocalRagAgentRelativePath", template)
        self.assertNotIn("Test-KnownProductAgentRevision", template)
        for legacy in (
            "internal-doc-search.agent.md",
            "agent003-readonly-local-rag.agent.md",
            "internal-doc-deep-research.agent.md",
        ):
            self.assertNotIn(legacy, template)


if __name__ == "__main__":
    unittest.main()
