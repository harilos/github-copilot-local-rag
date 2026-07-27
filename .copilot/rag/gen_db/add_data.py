from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import collection_name_for_db, ensure_db_layout, require_db_name
from software_rag_tool.daemon_control import database_mutation_guard
from software_rag_tool.env import load_env
from software_rag_tool.incremental import add_or_update_root
from software_rag_tool.paths import dbs_dir


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target DB name, e.g. project-rag")
    parser.add_argument("--root", required=True, help="Input document directory")
    parser.add_argument("--source-id", default="local")
    parser.add_argument(
        "--scan-subdir",
        help="Relative subdirectory to scan while keeping paths relative to --root",
    )
    parser.add_argument(
        "--include-root-name-in-path",
        action="store_true",
        help=(
            "Accepted for compatibility. The root directory name is always "
            "included in stored document paths."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume only when root, source ID, and scan subdirectory match saved state",
    )
    parser.add_argument("--batch-size-files", type=int, default=20)
    parser.add_argument("--reset-db", action="store_true", help="Delete and recreate the Chroma collection before adding data")
    parser.add_argument("--reset-clean", action="store_true", help="Delete clean records and resume state before adding data")
    parser.add_argument("--retry-errors", action="store_true", help="Retry unchanged files that previously failed extraction")
    parser.add_argument("--chunk-max-chars", type=int, default=1400, help="Optional chunk size for evaluation builds")
    parser.add_argument("--chunk-overlap", type=int, default=160, help="Optional chunk overlap for evaluation builds")
    parser.add_argument("--operation", default="add", choices=["add", "build"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.resume and (args.reset_db or args.reset_clean):
        parser.error("--resume cannot be combined with --reset-db or --reset-clean")

    db_name = require_db_name(args.db)
    with database_mutation_guard(db_name, rag_root=RAG_ROOT):
        db_root = ensure_db_layout(dbs_dir(), db_name)
        os.environ["RAG_DB_NAME"] = db_name
        os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
        os.environ.setdefault(
            "CHROMA_COLLECTION",
            collection_name_for_db(db_name),
        )
        summary = add_or_update_root(
            root=Path(args.root),
            source_id=args.source_id,
            scan_subdir=args.scan_subdir,
            include_root_name_in_path=True,
            batch_size_files=args.batch_size_files,
            reset_db=args.reset_db,
            reset_clean=args.reset_clean,
            retry_errors=args.retry_errors,
            operation=args.operation,
            chunk_max_chars=args.chunk_max_chars,
            chunk_overlap=args.chunk_overlap,
            resume=args.resume,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
