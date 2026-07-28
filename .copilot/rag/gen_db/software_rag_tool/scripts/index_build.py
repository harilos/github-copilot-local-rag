from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from software_rag_tool.dbs import collection_name_for_db, ensure_db_layout, require_db_name
from software_rag_tool.daemon_control import database_mutation_guard
from software_rag_tool.env import load_env
from software_rag_tool.paths import chroma_dir, dbs_dir
from software_rag_tool.store import build_index, collection_name


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target DB name. Must match '<name>-rag'.")
    parser.add_argument("--no-reset", action="store_true", help="Do not delete the existing collection before indexing")
    args = parser.parse_args()

    db_name = require_db_name(args.db)
    with database_mutation_guard(
        db_name,
        operation="rebuild_vector",
        dbs_root=dbs_dir(),
    ):
        db_root = ensure_db_layout(dbs_dir(), db_name)
        os.environ["RAG_DB_NAME"] = db_name
        os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
        os.environ.setdefault(
            "CHROMA_COLLECTION",
            collection_name_for_db(db_name),
        )
        count = build_index(reset=not args.no_reset)
    print(f"Indexed {count} records into '{collection_name()}' at {chroma_dir()}")


if __name__ == "__main__":
    main()
