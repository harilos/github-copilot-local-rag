from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from software_rag_tool.dbs import collection_name_for_db, ensure_db_layout, require_db_name
from software_rag_tool.env import load_env
from software_rag_tool.jsonl import write_jsonl
from software_rag_tool.paths import clean_dir, dbs_dir
from software_rag_tool.records import build_records

# This preparation command reads local source files only. It intentionally
# does not resolve or probe network configuration.


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target DB name. Must match '<name>-rag'.")
    parser.add_argument("--root", required=True, help="Input document directory")
    parser.add_argument(
        "--source-id",
        default="local",
        help=(
            "Source ID stored in metadata. Keep each provider separate; "
            "generic examples: sharepoint-docs, redmine-issues, "
            "github-repository, filesystem-docs."
        ),
    )
    parser.add_argument("--out", default="local.jsonl", help="Output JSONL filename under data/clean")
    args = parser.parse_args()

    db_name = require_db_name(args.db)
    db_root = ensure_db_layout(dbs_dir(), db_name)
    os.environ["RAG_DB_NAME"] = db_name
    os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
    os.environ.setdefault(
        "CHROMA_COLLECTION",
        collection_name_for_db(db_name),
    )

    records, errors = build_records(
        Path(args.root),
        source_id=args.source_id,
    )
    out_path = clean_dir() / args.out
    count = write_jsonl(out_path, records)
    if errors:
        error_path = clean_dir() / "prepare_errors.json"
        error_path.write_text(
            json.dumps(errors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"Wrote {count} records: {out_path}")
    if errors:
        print(f"Skipped {len(errors)} file(s): {error_path}")


if __name__ == "__main__":
    main()
