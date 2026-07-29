from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import (  # noqa: E402
    collection_name_for_db,
    require_db_name,
)
from software_rag_tool.env import load_env  # noqa: E402
from software_rag_tool.paths import dbs_dir  # noqa: E402
from software_rag_tool.source_delete import delete_source_data  # noqa: E402


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="Local RAG内部用: 1つのSourceの検索データを削除します。",
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--source-id", required=True)
    args = parser.parse_args()
    try:
        db_name = require_db_name(args.db)
        db_root = dbs_dir() / db_name
        if db_root.is_symlink() or not db_root.is_dir():
            raise ValueError("database directory is missing or unsafe")
        if db_root.parent.resolve() != dbs_dir().resolve():
            raise ValueError("database directory escaped dbs root")
        os.environ["RAG_DB_NAME"] = db_name
        os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
        os.environ.setdefault(
            "CHROMA_COLLECTION",
            collection_name_for_db(db_name),
        )
        print(
            json.dumps(
                delete_source_data(args.source_id),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "operation": "delete_source",
                    "db": str(args.db),
                    "source_id": str(args.source_id),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
