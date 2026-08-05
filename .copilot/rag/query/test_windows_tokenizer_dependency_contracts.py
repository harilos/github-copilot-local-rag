from __future__ import annotations

import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RAG_ROOT.parents[1]


def _tokenizer_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().strip('",')
        if "==" not in line:
            continue
        name, version = line.split("==", 1)
        if name.casefold() in {"sudachipy", "sudachidict-core"}:
            versions[name.casefold()] = version
    return versions


class WindowsTokenizerDependencyContracts(unittest.TestCase):
    def test_database_generator_matches_windows_search_runtime(self) -> None:
        canonical = _tokenizer_versions(
            RAG_ROOT / "query" / "requirements-search.txt"
        )
        self.assertEqual(
            {"sudachipy": "0.6.10", "sudachidict-core": "20250515"},
            canonical,
        )
        generator_root = RAG_ROOT / "gen_db" / "software_rag_tool"
        for path in (
            REPOSITORY_ROOT
            / "tools"
            / "windows_portable"
            / "requirements-search.lock",
            generator_root / "requirements.txt",
            generator_root / "pyproject.toml",
        ):
            with self.subTest(path=path):
                self.assertEqual(canonical, _tokenizer_versions(path))


if __name__ == "__main__":
    unittest.main()
