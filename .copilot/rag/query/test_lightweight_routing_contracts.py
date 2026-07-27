from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ROUTER = REPO_ROOT / ".copilot" / "instructions" / "rag.instructions.md"
LOOKUP = REPO_ROOT / ".copilot" / "skills" / "local-rag" / "SKILL.md"
ADMIN = REPO_ROOT / ".copilot" / "skills" / "local-rag-admin" / "SKILL.md"
COMPLIANCE_CASES = (
    REPO_ROOT
    / ".copilot"
    / "rag"
    / "docs"
    / "tests"
    / "data"
    / "copilot-compliance-cases-v1.jsonl"
)


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

    def test_lookup_guards_reproduced_one_shot_failures(self) -> None:
        lookup = LOOKUP.read_text(encoding="utf-8")
        router = ROUTER.read_text(encoding="utf-8")
        for text in (lookup, router):
            self.assertIn("human-only", text)
            self.assertRegex(text, r"Local\s+RAG Manager")
        self.assertIn(
            "including wrapper text before a colon",
            router,
        )
        self.assertIn(
            "latest human-authored visible prompt",
            lookup,
        )
        for excluded in (
            "runtime",
            "session-limit",
            "status",
            "system-reminder",
        ):
            self.assertIn(excluded, lookup)
        self.assertIn("PowerShell here-string", lookup)
        self.assertIn("multiline text container", lookup)
        self.assertIn("Never invent a free-text answer goal.", lookup)
        self.assertIn(
            "Do not correct the command and try again.",
            lookup,
        )
        self.assertRegex(
            lookup,
            r"each planning option is followed by\s+exactly one quoted value",
        )
        self.assertIn(
            "does not apply when the user asks only to display a",
            lookup,
        )
        self.assertIn(
            "explicitly overrides the executed-lookup verbatim rule",
            lookup,
        )
        self.assertIn("exactly one code block", lookup)
        self.assertIn("a second command", lookup)

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
        self.assertIn(
            "$HOME/.copilot/rag/query/.venv/Scripts/python.exe",
            text,
        )
        self.assertIn(
            "$HOME/.copilot/rag/query/search.py",
            text,
        )
        self.assertIn(
            "Do not switch to the POSIX `bin/python` layout",
            text,
        )

    def test_windows_git_bash_case_uses_windows_venv_layout(self) -> None:
        cases = [
            json.loads(line)
            for line in COMPLIANCE_CASES.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        for case_id in ("CPL-014", "CPL-015"):
            case = next(value for value in cases if value["id"] == case_id)
            self.assertIn(
                case["assertions"].get("assistant_command_shell"),
                {"powershell", "git-bash"},
            )
            self.assertEqual(
                case["assertions"].get("required_skill_calls"),
                {"local-rag": 1},
            )
            required = case["assertions"]["assistant_contains"]
            prompt = case["turns"][0]["prompt"]
            self.assertIn(
                "Lookup question (copy exactly as the final argument):\n"
                "{{DIRECT_QUESTION}}\nFirst load",
                prompt,
            )
            for value in (
                "--db {{EXPLICIT_DB}}",
                "--include-db-hint",
                "--compact-json",
                "--result-delivery file",
                "{{DIRECT_QUESTION}}",
            ):
                self.assertIn(value, required)
        case = next(value for value in cases if value["id"] == "CPL-015")
        required = case["assertions"]["assistant_contains"]
        forbidden = case["assertions"]["assistant_not_contains"]
        self.assertIn(
            "$HOME/.copilot/rag/query/.venv/Scripts/python.exe",
            required,
        )
        self.assertIn(
            "$HOME/.copilot/rag/query/search.py",
            required,
        )
        self.assertIn(".venv/bin/python", forbidden)

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
