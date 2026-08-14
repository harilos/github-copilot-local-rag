from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from software_rag_tool.dbs import require_db_name
from software_rag_tool.env import load_env
from software_rag_tool.paths import chroma_dir, dbs_dir
from software_rag_tool.store import build_index, collection_name
from software_rag_tool.writer_runtime import (
    DB_BUSY_EXIT_CODE,
    DatabaseBusyError,
    busy_error_payload,
    database_writer_session,
)


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target DB name. Must match '<name>-rag'.")
    parser.add_argument("--no-reset", action="store_true", help="Do not delete the existing collection before indexing")
    args = parser.parse_args()

    try:
        db_name = require_db_name(args.db)
        with database_writer_session(dbs_dir(), db_name):
            count = build_index(reset=not args.no_reset)
            print(
                f"Indexed {count} records into "
                f"'{collection_name()}' at {chroma_dir()}"
            )
    except DatabaseBusyError as exc:
        print(
            json.dumps(
                busy_error_payload(
                    exc,
                    operation="index_build",
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
