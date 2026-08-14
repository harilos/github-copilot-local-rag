from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from software_rag_tool.atomic_io import atomic_write_json
from software_rag_tool.dbs import require_db_name
from software_rag_tool.env import load_env
from software_rag_tool.jsonl import write_jsonl
from software_rag_tool.paths import clean_dir, dbs_dir
from software_rag_tool.records import build_records
from software_rag_tool.writer_runtime import (
    DB_BUSY_EXIT_CODE,
    DatabaseBusyError,
    busy_error_payload,
    database_writer_session,
)

# This preparation command reads local source files only. It intentionally
# does not resolve or probe network configuration.


def main() -> int:
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

    try:
        db_name = require_db_name(args.db)
        with database_writer_session(dbs_dir(), db_name):
            records, errors = build_records(
                Path(args.root),
                source_id=args.source_id,
            )
            out_path = clean_dir() / args.out
            count = write_jsonl(out_path, records)
            if errors:
                error_path = clean_dir() / "prepare_errors.json"
                atomic_write_json(error_path, errors, sort_keys=False)
            print(f"Wrote {count} records: {out_path}")
            if errors:
                print(f"Skipped {len(errors)} file(s): {error_path}")
    except DatabaseBusyError as exc:
        print(
            json.dumps(
                busy_error_payload(
                    exc,
                    operation="prepare",
                    db_name=str(args.db),
                ),
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return DB_BUSY_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
