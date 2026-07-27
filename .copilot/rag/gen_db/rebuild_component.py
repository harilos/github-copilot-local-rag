from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.catalog import counts as catalog_counts
from software_rag_tool.catalog import rebuild_from_clean
from software_rag_tool.dbs import collection_name_for_db, ensure_db_layout, require_db_name
from software_rag_tool.daemon_control import database_mutation_guard
from software_rag_tool.env import load_env
from software_rag_tool.incremental import add_or_update_root
from software_rag_tool.jsonl import read_jsonl
from software_rag_tool.manifest import write_manifest
from software_rag_tool.paths import clean_dir, dbs_dir, logs_dir
from software_rag_tool.store import collection_count, load_records, reset_collection, upsert_records


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target DB name, e.g. project-rag")
    parser.add_argument("--component", required=True, choices=["lexical", "catalog", "vector", "extract", "all"])
    parser.add_argument("--batch-size-files", type=int, default=20)
    args = parser.parse_args()

    db_name = require_db_name(args.db)
    with database_mutation_guard(db_name, rag_root=RAG_ROOT):
        _rebuild(args, db_name)


def _rebuild(args: argparse.Namespace, db_name: str) -> None:
    db_root = ensure_db_layout(dbs_dir(), db_name)
    os.environ["RAG_DB_NAME"] = db_name
    os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
    os.environ.setdefault("CHROMA_COLLECTION", collection_name_for_db(db_name))

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
        progress = _load_json(logs_dir() / "progress.json")
        root = str(progress.get("root") or "")
        source_id = str(progress.get("source_id") or "")
        if not root or not source_id:
            raise RuntimeError("extract rebuild requires a previous run with root/source_id in logs/progress.json")
        summary = add_or_update_root(
            root=Path(root),
            source_id=source_id,
            batch_size_files=args.batch_size_files,
            reset_db=True,
            reset_clean=True,
            retry_errors=True,
            operation=str(progress.get("operation") or "build"),
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


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    main()
