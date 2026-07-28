from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from help_links import MANAGER_HELP_EPILOG
from software_rag_tool.dbs import list_db_names, require_db_name
from software_rag_tool.paths import dbs_dir
from software_rag_tool.source_metadata_migration import (
    migrate_source_metadata,
)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description=(
            "Preview or migrate DB-local Source settings to "
            "rag-source-metadata-v1."
        ),
        epilog=MANAGER_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="Migrate only the selected DB")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply each safe DB-local migration atomically",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
    )
    args = parser.parse_args()

    root = dbs_dir().expanduser().resolve()
    names = (
        [require_db_name(args.db)]
        if args.db
        else list_db_names(root)
    )
    results = [
        migrate_source_metadata(
            root,
            name,
            apply=bool(args.apply),
        )
        for name in names
    ]
    payload = {
        "schema": "local-rag.source-metadata-migration.v1",
        "apply": bool(args.apply),
        "results": results,
    }
    if args.format == "json":
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return
    for result in results:
        print(f"{result['db']}: {result['status']}")


if __name__ == "__main__":
    main()
