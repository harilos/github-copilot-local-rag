from __future__ import annotations

import argparse
import json
import os
import stat
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
    args = parser.parse_args()
    try:
        db_name = require_db_name(args.db)
        db_root = _validated_database_root(db_name)
        os.environ["RAG_DB_NAME"] = db_name
        os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
        os.environ["CHROMA_DIR_V2"] = str(db_root / "index" / "chroma")
        os.environ["CHROMA_COLLECTION"] = collection_name_for_db(db_name)
        result = delete_source_data(
            args.source_id,
            progress_callback=(
                _write_progress if args.manager_protocol_v1 else None
            ),
        )
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.manager_protocol_v1 else 2,
            sort_keys=True,
        )
        print((RESULT_FRAME if args.manager_protocol_v1 else "") + encoded)
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


def _validated_database_root(db_name: str) -> Path:
    root = dbs_dir()
    root_metadata = os.lstat(root)
    if _is_link_or_reparse(root_metadata, root) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise ValueError("database root is missing or unsafe")
    db_root = root / db_name
    metadata = os.lstat(db_root)
    if _is_link_or_reparse(metadata, db_root) or not stat.S_ISDIR(
        metadata.st_mode
    ):
        raise ValueError("database directory is missing or unsafe")
    if db_root.resolve(strict=True).parent != root.resolve(strict=True):
        raise ValueError("database directory escaped dbs root")
    return db_root


def _is_link_or_reparse(metadata: os.stat_result, path: Path) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


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
