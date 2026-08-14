from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _append_trace(record: dict[str, object]) -> None:
    path = Path(os.environ["LRR_AGENT_TRACE_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")


def main() -> int:
    _append_trace(
        {
            "schema_version": "lrr-agent-002-tool-trace-v1",
            "event": "list_dbs",
            "scenario": os.environ.get("LRR_AGENT_SCENARIO", ""),
            "python": sys.executable,
            "script": str(Path(__file__).resolve()),
            "argv": sys.argv[1:],
        }
    )
    if sys.argv[1:] != ["--format", "json"]:
        print("invalid list_dbs argv", file=sys.stderr)
        return 64
    value = {
        "schema": "local-rag.database-list.v2",
        "status": "ok",
        "databases": [
            {
                "name": "alpha-rag",
                "title": "Alpha sealed fixture",
                "query_hint": "Agent-002 deterministic fixture A",
            },
            {
                "name": "beta-rag",
                "title": "Beta sealed fixture",
                "query_hint": "Agent-002 deterministic fixture B",
            },
        ],
    }
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
