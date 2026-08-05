from __future__ import annotations

import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
COPILOT_ROOT = RAG_ROOT.parent


class CopilotSetupRoutingContracts(unittest.TestCase):
    @staticmethod
    def _frontmatter(text: str) -> dict[str, str]:
        if not text.startswith("---\n"):
            raise AssertionError("YAML frontmatter is missing")
        header = text.split("---\n", 2)[1]
        result: dict[str, str] = {}
        for line in header.splitlines():
            key, separator, value = line.partition(":")
            if not separator or not key.strip() or not value.strip():
                raise AssertionError(f"invalid YAML frontmatter line: {line}")
            result[key.strip()] = value.strip().strip('"').strip("'")
        return result

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

    def test_routing_frontmatter_is_global_but_activation_remains_explicit(self) -> None:
        text = (
            COPILOT_ROOT / "instructions" / "rag.instructions.md"
        ).read_text(encoding="utf-8")
        header = self._frontmatter(text)
        normalized = " ".join(text.split())
        self.assertEqual("Local RAG Routing", header["name"])
        self.assertEqual("**", header["applyTo"])
        self.assertIn("explicitly asks", text)
        self.assertIn("Do not activate lookup merely", normalized)

    def test_router_and_skill_forbid_preflight_private_reads(self) -> None:
        router = (
            COPILOT_ROOT / "instructions" / "rag.instructions.md"
        ).read_text(encoding="utf-8")
        skill = (
            COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for text in (router, skill):
            self.assertIn("Before ordinary lookup, do not Read", text)
            self.assertIn("public command fails", text.casefold())
            self.assertIn("private", text.casefold())
        normalized_router = " ".join(router.split())
        self.assertIn("PowerShell syntax in PowerShell", normalized_router)
        self.assertIn("Git Bash syntax in Git Bash", normalized_router)

    def test_skill_has_list_and_search_examples_for_both_windows_shells(self) -> None:
        text = (
            COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md"
        ).read_text(encoding="utf-8")
        powershell = text.split("### Windows PowerShell", 1)[1].split(
            "### Windows Git Bash", 1
        )[0]
        git_bash = text.split("### Windows Git Bash", 1)[1].split(
            "## Per-search result handling", 1
        )[0]
        for section in (powershell, git_bash):
            self.assertIn("list_dbs.py", section)
            self.assertIn("search.py", section)
        self.assertIn("Do not put", git_bash)

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
