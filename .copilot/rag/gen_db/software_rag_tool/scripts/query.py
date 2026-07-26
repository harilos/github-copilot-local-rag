from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from software_rag_tool.search_api import (
    payload_to_text,
    run_adaptive_search_payload,
    run_search_payload,
)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
    parser.add_argument("--retrieval-mode", choices=["hybrid", "lexical", "dense"], default="hybrid")
    parser.add_argument("--lexical-only", action="store_true", help="Skip dense vector search")
    parser.add_argument("--disable-identifier-diagnostics", action="store_true", help="Skip identifier diagnostics for pure retrieval benchmarking")
    parser.add_argument(
        "--adaptive-hybrid",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    question = sys.stdin.read().strip() if args.stdin else (args.question or "").strip()
    if not question:
        parser.error("question is required unless --stdin provides input")

    # Keep stdout reserved for the requested JSON/prompt contract. Native
    # runtime initialization messages are diagnostic stderr.
    with contextlib.redirect_stdout(sys.stderr):
        if (
            args.adaptive_hybrid
            and not args.lexical_only
            and args.retrieval_mode == "hybrid"
        ):
            payload = run_adaptive_search_payload(
                db_name=args.db,
                question=question,
                top_k=args.top_k,
                source=args.source,
                max_chars=args.max_chars,
                budget_tokens=args.budget_tokens or None,
                explain=args.explain,
                include_db_hint=args.include_db_hint,
                identifier_diagnostics=not args.disable_identifier_diagnostics,
            )
        else:
            payload = run_search_payload(
                db_name=args.db,
                question=question,
                top_k=args.top_k,
                source=args.source,
                max_chars=args.max_chars,
                budget_tokens=args.budget_tokens or None,
                explain=args.explain,
                include_db_hint=args.include_db_hint,
                use_dense=not args.lexical_only,
                retrieval_mode="lexical" if args.lexical_only else args.retrieval_mode,
                identifier_diagnostics=not args.disable_identifier_diagnostics,
            )
    print(payload_to_text(payload, args.format, explain=args.explain))


if __name__ == "__main__":
    main()
