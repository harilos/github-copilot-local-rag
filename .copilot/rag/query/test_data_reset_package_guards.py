from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.data_lifecycle import (  # noqa: E402
    DataLifecycleError,
    empty_config_digest,
    new_reset_lifecycle,
    write_lifecycle,
)
from source_manager import database_copy_core, packages  # noqa: E402


class DataResetPackageGuardTests(unittest.TestCase):
    def test_distribution_and_database_copy_reject_nonready_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-generation-") as temporary:
            root = Path(temporary)
            dbs = root / "dbs"
            db = dbs / "fixture-rag"
            db.mkdir(parents=True)
            (db / "db.json").write_text("{}", encoding="utf-8")
            marker = new_reset_lifecycle(
                db, source_config_digest=empty_config_digest()
            )
            write_lifecycle(db, marker)
            with self.assertRaises(DataLifecycleError):
                packages._database_entries(
                    dbs, db_names=[db.name], distribution=True
                )
            with self.assertRaises(DataLifecycleError):
                database_copy_core.copy_database(
                    db,
                    dbs / "copy-rag",
                    destination_name="copy-rag",
                    title="Copy",
                    query_hint="fixture",
                    rag_root=root,
                )
            self.assertFalse((dbs / "copy-rag").exists())


if __name__ == "__main__":
    unittest.main()
