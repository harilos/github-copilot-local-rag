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
from software_rag_tool.catalog import counts as catalog_counts
from software_rag_tool.catalog import rebuild_from_clean
from software_rag_tool.dbs import require_db_name
from software_rag_tool.env import load_env
from software_rag_tool.incremental import add_or_update_root
from software_rag_tool.ingestion_paths import validated_saved_ingestion
from software_rag_tool.jsonl import read_jsonl
from software_rag_tool.manifest import write_manifest
from software_rag_tool.paths import clean_dir, dbs_dir, logs_dir
from software_rag_tool.store import collection_count, load_records, reset_collection, upsert_records
from software_rag_tool.writer_runtime import (
    DB_BUSY_EXIT_CODE,
    DatabaseBusyError,
    busy_error_payload,
    database_writer_session,
)


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        epilog=MANAGER_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", required=True, help="Target DB name, e.g. project-rag")
    parser.add_argument("--component", required=True, choices=["lexical", "catalog", "vector", "extract", "all"])
    parser.add_argument(
        "--batch-size-files",
        type=int,
        default=None,
        help="For extract rebuild, must match the saved ingestion batch size",
    )
    args = parser.parse_args()

    try:
        db_name = require_db_name(args.db)
        with database_writer_session(dbs_dir(), db_name):
            _rebuild(args, db_name)
    except DatabaseBusyError as exc:
        print(
            json.dumps(
                busy_error_payload(
                    exc,
                    operation=f"rebuild.{args.component}",
                    db_name=str(args.db),
                ),
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return DB_BUSY_EXIT_CODE
    return 0


def _rebuild(args: argparse.Namespace, db_name: str) -> None:
    if args.component in {"lexical", "catalog"}:
        count = rebuild_from_clean(reset=True)
        print(json.dumps({"db": db_name, "component": args.component, "catalog": catalog_counts(), "rebuilt_records": count}, ensure_ascii=False, indent=2))
        return

    if args.component == "vector":
        records = load_records()
        if not records:
            raise RuntimeError(f"No clean jsonl records found under {clean_dir()}")
        reset_collection()
        upserted = upsert_records(records)
        count = collection_count()
        write_manifest(count)
        print(json.dumps({"db": db_name, "component": "vector", "upserted_records": upserted, "collection_count": count}, ensure_ascii=False, indent=2))
        return

    if args.component == "extract":
        scope = _extract_rebuild_scope()
        if args.batch_size_files is not None and (
            type(args.batch_size_files) is not int
            or args.batch_size_files != scope["batch_size_files"]
        ):
            raise RuntimeError(
                "extract rebuild requires the saved ingestion batch_size_files; "
                "omit --batch-size-files or use the saved value"
            )
        summary = add_or_update_root(
            root=Path(scope["root"]),
            source_id=scope["source_id"],
            scan_subdir=scope["scan_subdir"],
            include_root_name_in_path=scope.get("include_root_name_in_path", True),
            batch_size_files=scope["batch_size_files"],
            reset_db=True,
            reset_clean=True,
            retry_errors=True,
            operation=scope["operation"],
            chunk_max_chars=scope["chunk_max_chars"],
            chunk_overlap=scope["chunk_overlap"],
            persistent_root_identity=scope.get("resolved_root"),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return

    if args.component == "all":
        records = _all_clean_records()
        if not records:
            raise RuntimeError(f"No clean jsonl records found under {clean_dir()}")
        reset_collection()
        upserted = upsert_records(records)
        catalog_count = rebuild_from_clean(reset=True)
        count = collection_count()
        write_manifest(count)
        print(
            json.dumps(
                {
                    "db": db_name,
                    "component": "all",
                    "vector_upserted_records": upserted,
                    "catalog_rebuilt_records": catalog_count,
                    "collection_count": count,
                    "catalog": catalog_counts(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def _all_clean_records() -> list[dict]:
    records: list[dict] = []
    directory = clean_dir()
    if directory.exists():
        for path in sorted(directory.rglob("*.jsonl")):
            records.extend(read_jsonl(path))
    return records


def _extract_rebuild_scope() -> dict:
    """Require the canonical ingestion scope, never a progress-derived command."""

    state = _load_json(logs_dir() / "index_state.json")
    scope = validated_saved_ingestion(state)
    if scope is None:
        raise RuntimeError(
            "extract rebuild requires valid canonical ingestion scope in "
            "index_state.json; run the source update with explicit settings first"
        )
    if scope.get("privacy_safe_root") or scope["root"] == "<EXTERNAL_SOURCE_ROOT>":
        raise RuntimeError(
            "extract rebuild cannot recover a privacy-safe external root; "
            "run the source update/rebuild with its original root instead"
        )
    return scope


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError(f"extract rebuild cannot read valid {path.name}") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"extract rebuild requires an object in {path.name}")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
