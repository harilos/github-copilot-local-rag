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


def _print_prompt(rows: list[dict], question: str, db_name: str, max_chars: int, *, db_hint: str = "", explain: bool = False) -> None:
    lines = ["## Retrieved evidence", f"Database: {db_name}", ""]
    if db_hint:
        lines.extend(["## DB hint", db_hint, ""])
    if not rows:
        lines.extend(["Status: no_evidence", "", "根拠が不足している場合は断定しないこと。", "", "## Question", question])
        print("\n".join(lines))
        return
    for row in rows:
        meta = row.get("metadata") or {}
        path = meta.get("path") or row.get("id")
        section = meta.get("section_path") or meta.get("chunk_title") or ""
        location = f" - {section}" if section else ""
        lines.append(f"[R{row['rank']}] {path}{location}")
        lines.append((row.get("text") or "")[:max_chars])
        if explain and row.get("debug"):
            lines.append(f"signals={','.join(row.get('signals') or [])} debug={json.dumps(row['debug'], ensure_ascii=False)}")
        lines.append("")
    lines.append("回答では根拠IDとsource locationを引用すること。")
    lines.append("根拠が不足する場合は断定しないこと。")
    lines.append("\n# Question\n")
    lines.append(question)
    print("\n".join(lines))


def _json_payload(rows: list[dict], question: str, db_name: str, max_chars: int, *, db_hint: str = "") -> dict:
    evidence = []
    results = []
    warnings = []
    truncated = False
    for row in rows:
        warnings.extend((row.get("debug") or {}).get("warnings") or [])
        meta = row.get("metadata") or {}
        text = row.get("text") or ""
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        item = {
            "id": f"R{row['rank']}",
            "source": {
                "path": meta.get("path") or "",
                "title": meta.get("title") or meta.get("chunk_title") or "",
                "revision": f"sha256:{meta.get('content_hash')}" if meta.get("content_hash") else "",
            },
            "location": {
                "section": meta.get("section_path") or meta.get("chunk_title") or "",
                "lines": meta.get("lines") or None,
                "page": meta.get("page") or None,
                "slide": meta.get("slide") or None,
            },
            "text": text,
            "signals": row.get("signals") or [],
        }
        if row.get("debug"):
            item["debug"] = row["debug"]
        evidence.append(item)
        result = dict(row)
        result["text"] = text
        results.append(result)
    return {
        "schema": "local-rag.search.v1",
        "db": db_name,
        "db_hint": db_hint,
        "query": question,
        "generation": 1,
        "status": "ok" if evidence else "no_evidence",
        "evidence": evidence,
        "results": results,
        "warnings": sorted(set(warnings)),
        "truncated": truncated or any(bool(row.get("truncated")) for row in rows),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?")
    parser.add_argument("--db", required=True, help="Target DB name. Must match '<name>-rag'.")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("TOP_K", "8")))
    parser.add_argument("--source", choices=["local", "confluence", "any"], default="any")
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--budget-tokens", type=int, default=0)
    parser.add_argument("--stdin", action="store_true", help="Read the question from stdin")
    parser.add_argument("--explain", action="store_true", help="Include retriever ranks and RRF debug information")
    parser.add_argument("--format", choices=["json", "prompt"], default="json")
    parser.add_argument("--include-db-hint", action="store_true")
    args = parser.parse_args()
    question = sys.stdin.read().strip() if args.stdin else (args.question or "").strip()
    if not question:
        parser.error("question is required unless --stdin provides input")

    db_name = require_db_name(args.db)
    db_root = ensure_db_layout(dbs_dir(), db_name)
    os.environ["RAG_DB_NAME"] = db_name
    os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
    os.environ.setdefault("CHROMA_COLLECTION", collection_name_for_db(db_name))

    rows = query(
        question,
        top_k=args.top_k,
        source=args.source,
        budget_tokens=args.budget_tokens or None,
        explain=args.explain,
    )
    db_hint = read_profile_hint(db_root) if args.include_db_hint else ""
    if args.format == "json":
        print(json.dumps(_json_payload(rows, question, db_name, args.max_chars, db_hint=db_hint), ensure_ascii=False, indent=2))
        return
    _print_prompt(rows, question, db_name, args.max_chars, db_hint=db_hint, explain=args.explain)


if __name__ == "__main__":
    main()
