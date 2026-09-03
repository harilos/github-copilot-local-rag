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

    def test_lookup_skill_owns_bounded_setup_required_handling(self) -> None:
        text = (COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Setup handling", text)
        self.assertIn("run the same runner once with `setup`", text)
        self.assertIn("setup_complete=true", text)
        self.assertNotIn("Please run setup from Local RAG Manager", text)
        self.assertFalse(
            (
                COPILOT_ROOT
                / "skills"
                / "local-rag-setup"
                / "SKILL.md"
            ).exists()
        )

    def test_skill_frontmatter_is_manual_and_user_invocable(self) -> None:
        text = (COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        header = self._frontmatter(text)
        self.assertEqual("local-rag", header["name"])
        self.assertEqual("true", header["user-invocable"])
        self.assertEqual("true", header["disable-model-invocation"])
        self.assertIn("explicitly invokes `/local-rag`", text)
        self.assertFalse(
            (COPILOT_ROOT / "instructions" / "rag.instructions.md").exists()
        )

    def test_skill_forbids_preflight_private_reads(self) -> None:
        skill = (
            COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(skill.split())
        self.assertIn("Do not inspect, list, probe, or analyze", normalized)
        self.assertIn("runner fails", normalized)
        self.assertIn("private", skill.casefold())
        self.assertIn("use only the example for the current terminal shell", normalized)

    def test_skill_has_isolated_runner_examples_for_each_shell(self) -> None:
        text = (
            COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md"
        ).read_text(encoding="utf-8")
        powershell = text.split("### Windows PowerShell", 1)[1].split(
            "### Windows Git Bash", 1
        )[0]
        git_bash = text.split("### Windows Git Bash", 1)[1].split(
            "### macOS/Linux", 1
        )[0]
        for section in (powershell, git_bash):
            self.assertIn("skill_runner.py", section)
            self.assertIn(" list", section)
            self.assertIn(" search", section)
            self.assertIn(" detail", section)
            self.assertIn(" -I -X utf8 -B ", section)
        self.assertIn("single quote", text)
        self.assertIn("shell history", text)

    def test_missing_runtime_setup_uses_only_the_public_entry_point(self) -> None:
        text = (COPILOT_ROOT / "skills" / "local-rag" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("tell the human to rerun the Local RAG installer", text)
        self.assertIn("~/.copilot/rag/setup.py", text)
        self.assertIn("Do not inspect private setup modules", text)


if __name__ == "__main__":
    unittest.main()
