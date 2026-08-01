from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAG_ROOT / "gen_db" / "software_rag_tool"))

from software_rag_tool.search_api import run_search_payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="append", dest="databases", required=True)
    args = parser.parse_args()
    results = {}
    for name in args.databases:
        payload = run_search_payload(db_name=name, question="portable install health check", top_k=1, use_dense=False, retrieval_mode="lexical", identifier_diagnostics=False)
        if payload.get("schema") != "local-rag.search.v1" or payload.get("selected_db") != name or payload.get("status") not in {"ok", "partial", "no_hit"}:
            raise SystemExit(f"database smoke search failed: {name}")
        results[name] = payload.get("status")
    print(json.dumps({"status":"ok", "databases":results}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
