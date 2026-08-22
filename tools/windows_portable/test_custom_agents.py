from __future__ import annotations

import ast
import hashlib
import re
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
AGENTS_ROOT = REPOSITORY_ROOT / ".copilot" / "agents"
sys.path.insert(0, str(HERE))

import windows_package_builder as package_builder  # noqa: E402


AGENTS = {
    "internal-doc-search.agent.md": {
        "name": "LOCAL-RAG-節約",
        "description": "Local RAGを必ず検索し、最小限の検索と根拠確認で短く回答します。",
        "tools": ["localragagent003/*"],
        "model": "GPT-5 mini (copilot)",
        "cap": "合計2回",
    },
    "agent003-readonly-local-rag.agent.md": {
        "name": "LOCAL-RAG-標準",
        "description": "Local RAGを必ず検索し、質問に合う検索量と形式で根拠付き回答します。",
        "tools": ["localragagent003/*"],
        "cap": "合計5回",
    },
    "internal-doc-deep-research.agent.md": {
        "name": "LOCAL-RAG-徹底検索",
        "description": "Local RAGを必ず複数の観点から検索し、Evidenceを突き合わせて回答します。",
        "tools": ["localragagent003/*"],
        "model": "GPT-5.3-Codex (copilot)",
        "cap": "合計7回",
    },
}


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
    def test_agent_schema_models_and_tool_allowlists(self) -> None:
        self.assertEqual(set(AGENTS), {path.name for path in AGENTS_ROOT.iterdir()})
        for filename, expected in AGENTS.items():
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            frontmatter, body = _frontmatter(text)
            self.assertEqual(expected["name"], frontmatter["name"])
            self.assertEqual(expected["description"], frontmatter["description"])
            self.assertEqual(expected["tools"], frontmatter["tools"])
            self.assertEqual([], frontmatter["agents"])
            self.assertIs(True, frontmatter["user-invocable"])
            self.assertIs(True, frontmatter["disable-model-invocation"])
            self.assertTrue(
                {"edit", "agent", "todo"}.isdisjoint(frontmatter["tools"])
            )
            self.assertNotIn("target", frontmatter)
            if "model" in expected:
                self.assertEqual(expected["model"], frontmatter["model"])
            else:
                self.assertNotIn("model", frontmatter)
                self.assertIn("Auto選択を継承", body)
            self.assertIn(expected["cap"], body)
            self.assertIn("local_rag_search", body)
            self.assertIn("local_rag_get_evidence", body)
            self.assertIn("許可を求め", body)
            self.assertIn("まだ検索していない", body)
            self.assertIn("同じturn", body)
            self.assertIn("選択も求めず", body)
            self.assertNotIn("候補が一意でなければ選択を求め", body)

    def test_agents_have_no_pointer_or_non_rag_tool_detour(self) -> None:
        for filename in AGENTS:
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"```(?:powershell|shell|bash)")
            self.assertNotIn("summary_file", text)
            self.assertNotIn("result_set_id", text)
            self.assertNotIn("Get-Content", text)
            self.assertNotIn("runInTerminal", text)
            self.assertNotIn("session_store_sql", text)

    def test_standard_workspace_mirror_is_byte_identical(self) -> None:
        product = AGENTS_ROOT / "agent003-readonly-local-rag.agent.md"
        workspace = (
            REPOSITORY_ROOT
            / ".github"
            / "agents"
            / "agent003-readonly-local-rag.agent.md"
        )
        self.assertEqual(product.read_bytes(), workspace.read_bytes())

    def test_both_windows_distribution_paths_include_only_product_agents(self) -> None:
        entries = package_builder._SNAPSHOT_MODULE._product_entries(
            REPOSITORY_ROOT / ".copilot",
            admin=False,
        )
        packaged = {
            entry.destination
            for entry in entries
            if entry.destination.startswith(".copilot/agents/")
        }
        self.assertEqual(
            {f".copilot/agents/{filename}" for filename in AGENTS},
            packaged,
        )

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
        self.assertIn("function Test-LocalRagAgentRelativePath", template)
        self.assertIn("function Test-KnownProductAgentRevision", template)
        for filename in AGENTS:
            self.assertIn(f'"agents\\{filename}"', template)
            agent_text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            normalized_hash = hashlib.sha256(
                agent_text.replace("\r\n", "\n").replace("\r", "\n").encode(
                    "utf-8"
                )
            ).hexdigest()
            self.assertIn(normalized_hash, template)


if __name__ == "__main__":
    unittest.main()
