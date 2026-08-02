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
        self.assertIn('[sys.executable, "-B", str(LOWER_SETUP)', text)

    def test_manual_gate_repair_disables_child_bytecode(self) -> None:
        repair = (RAG_ROOT / "repair_setup_gate.py").read_text(encoding="utf-8")
        self.assertIn('str(VENV_PYTHON),\n            "-B",', repair)
        self.assertIn('"PYTHONDONTWRITEBYTECODE": "1"', repair)

    def test_public_lookup_entrypoints_seed_the_rag_import_root(self) -> None:
        for name in ("list_dbs.py", "search.py"):
            text = (RAG_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("RAG_ROOT = Path(__file__).resolve().parent", text)
            self.assertIn("sys.path.insert(0, str(RAG_ROOT))", text)

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
        self.assertIn("fixed, verified Python runtime", text)
        self.assertIn(".venv\\Scripts\\python.exe", text)
        self.assertIn("packaged setup is offline", text.casefold())
        self.assertIn("~/.copilot/rag/setup.py", text)
        self.assertIn("Do not invoke `query/setup.py` directly", text)


if __name__ == "__main__":
    unittest.main()
