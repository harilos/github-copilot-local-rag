from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
DBS_ROOT = Path(os.getenv("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))).expanduser().resolve()
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import list_db_names, read_db_config, read_profile_hint


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        help="Use 'json' for the compact Copilot contract or 'text' for human-readable output.",
    )
    args = parser.parse_args()

    try:
        databases = database_summaries(DBS_ROOT)
    except Exception as exc:
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "status": "error",
                        "databases": [],
                        "error": {
                            "kind": type(exc).__name__,
                            "message": str(exc),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(1)
        raise

    if args.format == "json":
        print(json.dumps({"databases": databases}, ensure_ascii=False, indent=2))
        return
    if args.format == "text":
        print(human_text(databases))
        return

    # Preserve the original no-argument JSON interface for existing callers.
    legacy = []
    for item in databases:
        root = DBS_ROOT / item["name"]
        legacy.append(
            {
                "db": item["name"],
                "config": read_db_config(root),
                "hint": item["query_hint"],
            }
        )
    print(json.dumps({"dbs": legacy}, ensure_ascii=False, indent=2))


def database_summaries(dbs_root: Path) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for name in list_db_names(dbs_root):
        root = dbs_root / name
        config = read_db_config(root)
        summaries.append(
            {
                "name": name,
                "title": str(config.get("title") or name),
                "query_hint": read_profile_hint(root, max_chars=240),
            }
        )
    return summaries


def human_text(databases: list[dict[str, str]]) -> str:
    if not databases:
        return "No local RAG databases are installed."
    lines: list[str] = []
    for item in databases:
        lines.append(f"{item['name']}: {item['title']}")
        if item["query_hint"]:
            lines.append(f"  {item['query_hint']}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
