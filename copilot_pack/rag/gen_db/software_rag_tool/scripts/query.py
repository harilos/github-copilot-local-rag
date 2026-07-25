from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from software_rag_tool.dbs import collection_name_for_db, ensure_db_layout, read_profile_hint, require_db_name
from software_rag_tool.env import load_env
from software_rag_tool.paths import dbs_dir
from software_rag_tool.store import query


def _print_prompt(rows: list[dict], question: str, max_chars: int) -> None:
    lines = ["# Context"]
    for row in rows:
        meta = row.get("metadata") or {}
        title = meta.get("chunk_title") or meta.get("path") or row.get("id")
        lines.append(f"\n## [{row['rank']}] {title}\n")
        lines.append((row.get("text") or "")[:max_chars])
    lines.append("\n# Question\n")
    lines.append(question)
    print("\n".join(lines))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--db", required=True, help="Target DB name. Must match '<name>-rag'.")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("TOP_K", "8")))
    parser.add_argument("--source", choices=["local", "confluence", "any"], default="any")
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--format", choices=["json", "prompt"], default="json")
    parser.add_argument("--include-db-hint", action="store_true")
    args = parser.parse_args()

    db_name = require_db_name(args.db)
    db_root = ensure_db_layout(dbs_dir(), db_name)
    os.environ["RAG_DB_NAME"] = db_name
    os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
    os.environ.setdefault("CHROMA_COLLECTION", collection_name_for_db(db_name))

    rows = query(args.question, top_k=args.top_k, source=args.source)
    if args.format == "json":
        trimmed = []
        for row in rows:
            copy = dict(row)
            copy["text"] = (copy.get("text") or "")[: args.max_chars]
            trimmed.append(copy)
        payload = {"db": db_name, "db_hint": read_profile_hint(db_root) if args.include_db_hint else "", "results": trimmed}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.include_db_hint:
        hint = read_profile_hint(db_root)
        if hint:
            print(f"# DB Hint\n\n{hint}\n")
    _print_prompt(rows, args.question, args.max_chars)


if __name__ == "__main__":
    main()
