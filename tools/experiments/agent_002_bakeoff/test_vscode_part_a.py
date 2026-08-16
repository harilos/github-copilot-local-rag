from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.experiments.agent_002_bakeoff.vscode_offline_parser import self_test


ROOT = Path(__file__).resolve().parents[3]
AGENTS = ROOT / "tools" / "experiments" / "agents"
CASES = Path(__file__).with_name("vscode_cases.json")
EXPECTED_NAMES = tuple(f"AGENT002-{letter}" for letter in "ABCD")
EXPECTED_DATABASES = {"agent002-evidence-rag", "agent002-decoy-rag"}


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8", errors="strict")
    match = re.match(r"\A---\n(?P<header>.*?)\n---\n(?P<body>.*)\Z", text, re.S)
    if match is None:
        raise AssertionError(f"missing frontmatter: {path}")
    values: dict[str, str] = {}
    for line in match.group("header").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            raise AssertionError(f"invalid frontmatter line: {line}")
        values[key.strip()] = value.strip()
    return values, match.group("body")


class VscodePartAContracts(unittest.TestCase):
    def test_candidate_frontmatter_and_tool_contract(self) -> None:
        files = sorted(AGENTS.glob("agent002-?.agent.md"))
        self.assertEqual(4, len(files))
        names = []
        for path in files:
            frontmatter, body = _frontmatter(path)
            names.append(frontmatter["name"])
            self.assertEqual("vscode", frontmatter.get("target"))
            self.assertEqual("GPT-5 mini", frontmatter.get("model"))
            self.assertEqual("['execute']", frontmatter.get("tools"))
            self.assertEqual("[]", frontmatter.get("agents"))
            self.assertEqual("true", frontmatter.get("user-invocable"))
            self.assertEqual("true", frontmatter.get("disable-model-invocation"))
            self.assertNotIn("fallback", frontmatter)
            self.assertEqual(1, body.count("rag\\search.py"))
            self.assertEqual(1, body.count("rag\\list_dbs.py"))
            self.assertIn("retry", body.lower())
            self.assertIn("Q", body)
            self.assertIn("pass `$q` as the final native argument", body.lower())
        self.assertEqual(list(EXPECTED_NAMES), names)

    def test_candidates_are_outside_product_payload(self) -> None:
        for path in AGENTS.glob("*.agent.md"):
            relative = path.relative_to(ROOT).as_posix()
            self.assertTrue(relative.startswith("tools/experiments/agents/"))
            self.assertFalse(relative.startswith(".copilot/"))

    def test_vscode_cases_use_only_two_sealed_databases(self) -> None:
        payload = json.loads(CASES.read_text(encoding="utf-8", errors="strict"))
        self.assertEqual(24, payload["prompt_limit"])
        self.assertEqual(100, payload["credit_cap"])
        self.assertEqual(list(EXPECTED_NAMES), [item["agent"] for item in payload["candidates"]])
        serialized = json.dumps(payload, ensure_ascii=False)
        discovered = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*-rag", serialized))
        self.assertEqual(EXPECTED_DATABASES, discovered)

    def test_db_selection_turn_keeps_the_original_question(self) -> None:
        payload = json.loads(CASES.read_text(encoding="utf-8", errors="strict"))
        case = next(item for item in payload["stage1"] if item["id"] == "S1-DB-SELECT")
        self.assertEqual(2, len(case["turns"]))
        self.assertEqual("agent002-decoy-rag", case["turns"][1])
        self.assertIn(case["question"], case["turns"][0])
        self.assertEqual(
            ["list_dbs", "search", "read_summary"],
            case["expected_tool_sequence"],
        )

    def test_balanced_stage1_order(self) -> None:
        payload = json.loads(CASES.read_text(encoding="utf-8", errors="strict"))
        orders = list(payload["balanced_stage1_order"].values())
        self.assertEqual(3, len(orders))
        for order in orders:
            self.assertEqual({"A", "B", "C", "D"}, set(order))
        self.assertEqual(["A", "B", "C"], [order[0] for order in orders])

    def test_offline_parser_synthetic_schema(self) -> None:
        self_test()

    @unittest.skipUnless(sys.platform == "win32", "PowerShell argv contract is Windows-specific")
    def test_powershell_single_quoted_query_variable_preserves_exact_text(self) -> None:
        query = "\u9867\u5ba2 `A-\u03a9` \u306e\u8b58\u5225\u5b50 \"Q'$()\" \u3068\n\u6539\u884c\u3092\u542b\u3080\u9805\u76ee\u300c\u96ea\u2603\u300d"
        literal = "'" + query.replace("'", "''") + "'"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "argv.bin"
            quote = lambda value: "'" + value.replace("'", "''") + "'"
            command = (
                f"$q = {literal}; "
                f"[IO.File]::WriteAllBytes({quote(str(output))}, "
                "[Text.Encoding]::UTF8.GetBytes($q))"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
            self.assertEqual(query.encode("utf-8"), output.read_bytes())


if __name__ == "__main__":
    unittest.main()
