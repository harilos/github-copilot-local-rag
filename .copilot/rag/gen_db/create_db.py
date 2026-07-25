from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import ensure_db_layout, read_db_version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="New DB name, e.g. project-rag")
    parser.add_argument("--title")
    parser.add_argument("--query-hint", help="Short DB-specific guidance written to DB_PROFILE.md")
    args = parser.parse_args()

    dbs_root = Path(os.getenv("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))).expanduser().resolve()
    root = ensure_db_layout(dbs_root, args.db, title=args.title, query_hint=args.query_hint)
    print(f"Created DB layout: {root}")
    version = read_db_version(root)
    if version:
        print(f"Version file: {root / 'VERSION.json'}")
        print(f"DB hash: {version.get('db_hash')}")


if __name__ == "__main__":
    main()
