from __future__ import annotations

import json
import re
import tempfile
import tomllib
import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RAG_ROOT.parents[1]
SEARCH_LOCK = RAG_ROOT / "query" / "requirements-windows-search.lock"
ADMIN_LOCK = RAG_ROOT / "query" / "requirements-windows-admin.lock"


def _locked_requirements(
    path: Path,
    visited: set[Path] | None = None,
) -> dict[str, str]:
    visited = visited or set()
    resolved = path.resolve()
    if resolved in visited:
        return {}
    visited.add(resolved)
    locked: dict[str, str] = {}
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            locked.update(
                _locked_requirements(resolved.parent / line[3:].strip(), visited)
            )
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^;\s]+)",
            line,
        )
        if match is None:
            raise AssertionError(f"requirement is not exact: {resolved}: {line}")
        name, version = match.groups()
        key = name.casefold().replace("_", "-")
        if key in locked and locked[key] != version:
            raise AssertionError(f"conflicting lock for {name}")
        locked[key] = version
    return locked


class Python313DependencyContracts(unittest.TestCase):
    def test_python_contract_and_official_windows_runtime_are_exact(self) -> None:
        pyproject = tomllib.loads(
            (
                RAG_ROOT / "gen_db" / "software_rag_tool" / "pyproject.toml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(">=3.13,<3.14", pyproject["project"]["requires-python"])
        expected = {
            "version": "3.13.15",
            "url": (
                "https://www.python.org/ftp/python/3.13.15/"
                "python-3.13.15-embed-amd64.zip"
            ),
            "sha256": (
                "d1f04d990aee1253d8569e8e5104e30f"
                "a9f5fa830899f14843448872d936a2cf"
            ),
            "spdx_url": (
                "https://www.python.org/ftp/python/3.13.15/"
                "python-3.13.15-embed-amd64.zip.spdx.json"
            ),
        }
        product = json.loads(
            (RAG_ROOT / "source_manager" / "windows-runtime-lock.json")
            .read_text(encoding="utf-8")
        )
        repository = json.loads(
            (REPOSITORY_ROOT / "tools/windows_portable/runtime-lock.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(product, repository)
        self.assertEqual(expected, product["python"])

    def test_windows_profiles_are_complete_exact_and_separate(self) -> None:
        search = _locked_requirements(SEARCH_LOCK)
        admin = _locked_requirements(ADMIN_LOCK)
        self.assertEqual(93, len(search))
        self.assertEqual(120, len(admin))
        self.assertLess(set(search), set(admin))
        for name in (
            "sentence-transformers",
            "optimum",
            "torch",
            "pypdf",
            "python-docx",
            "python-pptx",
            "openpyxl",
        ):
            self.assertNotIn(name, search)
            self.assertIn(name, admin)
        self.assertEqual("0.6.10", search["sudachipy"])
        self.assertEqual("20250515", search["sudachidict-core"])
        self.assertEqual("1.5.9", search["chromadb"])
        self.assertEqual("2.15.0", search["pydantic-settings"])
        self.assertEqual("1.5.2", search["hf-xet"])
        self.assertEqual("24.3.1", admin["pip"])

    def test_direct_metadata_and_source_requirements_are_exact_and_aligned(self) -> None:
        tool_root = RAG_ROOT / "gen_db" / "software_rag_tool"
        metadata = tomllib.loads(
            (tool_root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"]
        requirements = _locked_requirements(tool_root / "requirements.txt")
        with tempfile.TemporaryDirectory() as directory:
            metadata_file = Path(directory) / "metadata.txt"
            metadata_file.write_text(
                "\n".join(metadata) + "\n", encoding="utf-8"
            )
            self.assertEqual(requirements, _locked_requirements(metadata_file))
        self.assertEqual("2.0.0", requirements["optimum"])

    def test_active_ci_installer_and_lock_entry_points_use_python313(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/source-link-e2e.yml").read_text(
            encoding="utf-8"
        )
        installer = (REPOSITORY_ROOT / "install.ps1").read_text(encoding="utf-8")
        setup = (RAG_ROOT / "query/setup.py").read_text(encoding="utf-8")
        self.assertIn('python-version: "3.13"', workflow)
        self.assertNotRegex(workflow, r'python-version:\s*"3\.(?:10|11|12)"')
        self.assertIn("(3, 13) <= sys.version_info[:2] < (3, 14)", setup)
        self.assertIn("requirements-windows-admin.lock", setup)
        self.assertIn("sys.implementation.name == 'cpython'", installer)
        self.assertIn("(3, 13) <= sys.version_info[:2] < (3, 14)", installer)
        for wrapper, canonical in (
            ("requirements-search.lock", SEARCH_LOCK),
            ("requirements-admin.lock", ADMIN_LOCK),
        ):
            content = (
                REPOSITORY_ROOT / "tools/windows_portable" / wrapper
            ).read_text(encoding="utf-8")
            self.assertEqual(
                f"../../{canonical.relative_to(REPOSITORY_ROOT).as_posix()}",
                content.split("-r ", 1)[1].strip(),
            )


if __name__ == "__main__":
    unittest.main()
