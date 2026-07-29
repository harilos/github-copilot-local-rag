from __future__ import annotations

import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
COPILOT_ROOT = RAG_ROOT.parent


class CopilotSetupRoutingContracts(unittest.TestCase):
    def test_public_setup_wrapper_exists(self) -> None:
        setup = RAG_ROOT / "setup.py"
        self.assertTrue(setup.is_file())
        text = setup.read_text(encoding="utf-8")
        self.assertIn('LOWER_SETUP = RAG_ROOT / "query" / "setup.py"', text)
        self.assertIn("sys.executable", text)

    def test_lookup_skill_routes_setup_required_to_setup_skill(self) -> None:
        text = (COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("`local-rag-setup` Skill", text)
        self.assertNotIn("Please run setup from Local RAG Manager", text)
        self.assertNotIn("Do not run setup from Copilot", text)

    def test_shared_instructions_allow_copilot_setup(self) -> None:
        text = (
            COPILOT_ROOT / "instructions" / "rag.instructions.md"
        ).read_text(encoding="utf-8")
        self.assertIn("run the public `~/.copilot/rag/setup.py`", text)
        self.assertIn("Do not redirect", text)
        self.assertNotIn("Do not attempt setup from Copilot", text)

    def test_setup_skill_uses_public_entry_point(self) -> None:
        text = (
            COPILOT_ROOT / "skills" / "local-rag-setup" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Initial setup is performed by Copilot", text)
        self.assertIn("~/.copilot/rag/setup.py", text)
        self.assertIn("Do not invoke `query/setup.py` directly", text)


if __name__ == "__main__":
    unittest.main()
