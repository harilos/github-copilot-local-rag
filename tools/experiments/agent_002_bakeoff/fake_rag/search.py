from __future__ import annotations

import json
import os
import sys
from pathlib import Path


FIXED_PREFIX = [
    "--db",
    None,
    "--include-db-hint",
    "--compact-json",
    "--result-delivery",
    "file",
    "--format",
    "json",
]


def _append_trace(record: dict[str, object]) -> None:
    path = Path(os.environ["LRR_AGENT_TRACE_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")


def main() -> int:
    argv = sys.argv[1:]
    db = argv[1] if len(argv) > 1 and argv[0] == "--db" else ""
    question = argv[-1] if len(argv) == 9 else ""
    scenario = os.environ.get("LRR_AGENT_SCENARIO", "")
    summary_root = Path(os.environ["LRR_AGENT_SUMMARY_ROOT"]).resolve()
    summary_file = summary_root / f"{scenario}.md"
    _append_trace(
        {
            "schema_version": "lrr-agent-002-tool-trace-v1",
            "event": "search",
            "scenario": scenario,
            "python": sys.executable,
            "script": str(Path(__file__).resolve()),
            "argv": argv,
            "db": db,
            "question": question,
            "summary_file": str(summary_file),
        }
    )
    expected = list(FIXED_PREFIX)
    if len(argv) != 9:
        print("invalid search argv length", file=sys.stderr)
        return 64
    expected[1] = db
    if argv[:8] != expected:
        print("invalid search argv contract", file=sys.stderr)
        return 64
    if not db.endswith("-rag") or not question:
        print("invalid db or empty question", file=sys.stderr)
        return 64
    if scenario == "tool_error":
        print("sealed fixture tool error", file=sys.stderr)
        return 70
    if not summary_file.is_file():
        print("sealed fixture summary missing", file=sys.stderr)
        return 66
    pointer = {
        "schema_version": "rag-result-pointer-v1",
        "status": "ok",
        "result_set_id": f"agent-002-{scenario}",
        "summary_file": str(summary_file),
    }
    print(json.dumps(pointer, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
