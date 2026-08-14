from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import require_db_name  # noqa: E402
from software_rag_tool.env import load_env  # noqa: E402
from software_rag_tool.paths import dbs_dir  # noqa: E402
from software_rag_tool.source_delete import delete_source_data  # noqa: E402
from software_rag_tool.writer_runtime import (  # noqa: E402
    DB_BUSY_EXIT_CODE,
    DatabaseBusyError,
    busy_error_payload,
    database_writer_session,
)

PROGRESS_FRAME = "@@LOCAL_RAG_PROGRESS_V1@@"
RESULT_FRAME = "@@LOCAL_RAG_RESULT_V1@@"


def main() -> int:
    _configure_utf8_stdio()
    load_env()
    parser = argparse.ArgumentParser(
        description="Local RAG内部用: 1つのSourceの検索データを削除します。",
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--manager-protocol-v1",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--remove-source-metadata",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    try:
        db_name = require_db_name(args.db)
        with database_writer_session(dbs_dir(), db_name) as target:
            if args.remove_source_metadata:
                from source_manager.metadata import remove_source_metadata

                remove_source_metadata(
                    target.db_root,
                    args.source_id,
                    RAG_ROOT,
                )
            result = delete_source_data(
                args.source_id,
                progress_callback=(
                    _write_progress if args.manager_protocol_v1 else None
                ),
            )
            if args.remove_source_metadata:
                result["metadata_removed"] = True
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.manager_protocol_v1 else 2,
            sort_keys=True,
        )
        print((RESULT_FRAME if args.manager_protocol_v1 else "") + encoded)
        return 0
    except DatabaseBusyError as exc:
        print(
            json.dumps(
                busy_error_payload(
                    exc,
                    operation="delete_source",
                    db_name=str(args.db),
                ),
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return DB_BUSY_EXIT_CODE
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


def _write_progress(event: dict[str, object]) -> None:
    try:
        sys.stderr.write(
            PROGRESS_FRAME
            + json.dumps(event, ensure_ascii=False, sort_keys=True)
            + "\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


if __name__ == "__main__":
    raise SystemExit(main())
