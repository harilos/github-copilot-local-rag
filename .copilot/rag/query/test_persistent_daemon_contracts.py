from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(QUERY_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.daemon_control import (  # noqa: E402
    release_db_before_mutation,
)


class PersistentDaemonContracts(unittest.TestCase):
    def test_client_and_manager_imports_are_native_runtime_free(self) -> None:
        script = f"""
import importlib.util, json, sys
from pathlib import Path
root = Path({str(QUERY_ROOT)!r})
sys.path.insert(0, str(root))
for name in ('search', 'ragd'):
    before = set(sys.modules)
    spec = importlib.util.spec_from_file_location(name + '_probe', root / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loaded = set(sys.modules) - before
    forbidden = [
        value for value in loaded
        if value.split('.')[0] in {{
            'chromadb', 'onnxruntime', 'transformers',
            'sudachipy', 'sentence_transformers'
        }}
    ]
    if forbidden or 'software_rag_tool.search_api' in loaded:
        raise SystemExit(json.dumps({{'name': name, 'forbidden': forbidden}}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_absent_daemon_is_already_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = release_db_before_mutation(
                "ac-rag",
                rag_root=root,
            )
        self.assertEqual("no_daemon", result["status"])

    def test_runtime_fingerprint_includes_manager_and_worker(self) -> None:
        search_source = (QUERY_ROOT / "search.py").read_text(
            encoding="utf-8"
        )
        daemon_source = (QUERY_ROOT / "ragd.py").read_text(
            encoding="utf-8"
        )
        for filename in ("rag_manager.py", "rag_worker.py"):
            self.assertIn(filename, search_source)
            self.assertIn(filename, daemon_source)

    def test_admin_mutations_request_db_release(self) -> None:
        for relative in (
            "gen_db/add_data.py",
            "gen_db/rebuild_component.py",
            "gen_db/software_rag_tool/scripts/index_build.py",
        ):
            text = (RAG_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("database_mutation_guard", text)


if __name__ == "__main__":
    unittest.main()
