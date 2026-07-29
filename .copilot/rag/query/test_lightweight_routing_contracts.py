from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ROUTER = REPO_ROOT / ".copilot" / "instructions" / "rag.instructions.md"
LOOKUP = REPO_ROOT / ".copilot" / "skills" / "local-rag" / "SKILL.md"
RETIRED_ADMIN = (
    REPO_ROOT / ".copilot" / "skills" / "local-rag-admin" / "SKILL.md"
)


class LightweightRoutingContractTests(unittest.TestCase):
    def test_copilot_documents_are_english_and_admin_skill_is_retired(
        self,
    ) -> None:
        self.assertFalse(RETIRED_ADMIN.exists())
        for path in (ROUTER, LOOKUP):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text),
                path,
            )

    def test_only_public_root_entry_points_are_exposed(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROUTER, LOOKUP)
        )
        for entry_point in (
            "~/.copilot/rag/list_dbs.py",
            "~/.copilot/rag/search.py",
        ):
            self.assertIn(entry_point, combined)
        for forbidden in (
            "/query/search.py",
            "/query/list_dbs.py",
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
            self.assertNotIn(forbidden, combined)

    def test_management_requests_stop_at_manager_boundary(self) -> None:
        for path in (ROUTER, LOOKUP):
            text = path.read_text(encoding="utf-8")
            self.assertIn("Local RAG Manager", text)
            self.assertIn("Do not open the Manager automatically.", text)
        lookup = LOOKUP.read_text(encoding="utf-8")
        self.assertIn(
            "For database creation or editing, Source addition/update/resume",
            lookup,
        )

    def test_lookup_contract_is_one_list_then_one_search(self) -> None:
        text = LOOKUP.read_text(encoding="utf-8")
        self.assertIn("database-list calls: 0", text)
        self.assertIn("database-list calls: exactly 1", text)
        self.assertIn("search calls: 1", text)
        self.assertIn("Never use `--auto`.", text)
        self.assertIn("Never retry", text)
        self.assertIn("--compact-json", text)
        self.assertIn("--result-delivery file", text)
        self.assertIn("read `summary_file` once", text)
        self.assertRegex(
            text,
            r"through the\s+same public search entry point",
        )

    def test_context_references_use_hints_without_rewriting_prompt(
        self,
    ) -> None:
        text = LOOKUP.read_text(encoding="utf-8")
        for option in (
            "--literal-identifier",
            "--entity",
            "--facet",
            "--semantic-hypothesis",
            "--answer-goal",
        ):
            self.assertIn(f"- `{option}`", text)
        self.assertIn(
            "Never append earlier messages to the positional question.",
            text,
        )
        self.assertIn("assistant answer", text)
        self.assertIn("Put speculation only in `--semantic-hypothesis`", text)
        self.assertRegex(
            text,
            r"ask for\s+clarification without listing or searching",
        )

    def test_platform_commands_use_the_installed_venv_and_public_script(
        self,
    ) -> None:
        text = LOOKUP.read_text(encoding="utf-8")
        self.assertIn("~/.copilot/rag/query/.venv/bin/python", text)
        self.assertIn(
            r"$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe",
            text,
        )
        self.assertIn(
            "$HOME/.copilot/rag/query/.venv/Scripts/python.exe",
            text,
        )
        self.assertIn("$HOME/.copilot/rag/search.py", text)
        self.assertIn("Do not probe", text)
        self.assertIn("do not use `cmd.exe /c`", text)

    def test_stale_notice_and_uri_citation_contracts_are_explicit(self) -> None:
        for path in (ROUTER, LOOKUP):
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "local_rag_content_snapshot_older_than_30_days",
                text,
            )
            self.assertRegex(
                text,
                r"(?:at most|exactly)\s+once in the current\s+chat",
            )
            self.assertIn("`source_url`", text)
            self.assertIn("`source_permalink`", text)
            self.assertIn("## References", text)
            self.assertRegex(text.casefold(), r"never\s+show a raw url")

    def test_no_skill_sets_a_model_or_creates_an_agent_contract(self) -> None:
        for path in (ROUTER, LOOKUP):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^model\s*:")
            self.assertNotIn(".agent.md", text)


if __name__ == "__main__":
    unittest.main()
