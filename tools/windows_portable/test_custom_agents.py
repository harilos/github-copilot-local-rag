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
        "name": "社内文書検索",
        "tools": ["execute"],
        "model": "GPT-5 mini",
    },
    "internal-doc-deep-research.agent.md": {
        "name": "社内文書徹底調査",
        "tools": ["execute", "read", "search", "web"],
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
            self.assertTrue(str(frontmatter["description"]).strip())
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

    def test_each_agent_uses_the_fixed_windows_search_once(self) -> None:
        required_fragments = (
            '$env:USERPROFILE\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe',
            " -B ",
            '$env:USERPROFILE\\.copilot\\rag\\search.py',
            '--db "<選択したDB>"',
            "--include-db-hint",
            "--compact-json",
            "--result-delivery file",
            "--format json",
            '"<利用者の質問全文>"',
        )
        for filename in AGENTS:
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            blocks = re.findall(r"```powershell\n(.*?)\n```", text, re.DOTALL)
            commands = [block for block in blocks if "rag\\search.py" in block]
            self.assertEqual(1, len(commands), filename)
            command = commands[0]
            positions = [command.index(fragment) for fragment in required_fragments]
            self.assertEqual(positions, sorted(positions), filename)
            self.assertEqual(1, text.count("<利用者の質問全文>"), filename)
            self.assertEqual(1, text.count("rag\\search.py"), filename)
            self.assertEqual(1, text.count("rag\\list_dbs.py"), filename)
            self.assertNotIn("--result-delivery stdout", text)
            self.assertNotIn("--no-daemon", text)
            self.assertIn("summary_file", text)

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
