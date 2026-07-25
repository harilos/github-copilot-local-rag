from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
DBS_ROOT = Path(os.getenv("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))).expanduser().resolve()
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import resolve_db_name


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="+")
    parser.add_argument("--db", help="Target DB name, e.g. project-rag")
    parser.add_argument("--auto", action="store_true", help="Allow natural-language RAG trigger when DB name is omitted")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--format", choices=["json", "prompt"], default="prompt")
    parser.add_argument("--include-db-hint", action="store_true")
    args = parser.parse_args()

    question = " ".join(args.question).strip()
    resolution = resolve_db_name(question, args.db, DBS_ROOT, args.auto)
    if not resolution.triggered:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": resolution.reason,
                    "message": "DB名（例: xxx-rag）を明示するか、RAG検索が必要な自然言語指示で --auto を使ってください。",
                    "available_dbs": resolution.candidates,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not resolution.db_name:
        print(
            json.dumps(
                {
                    "status": "needs_db",
                    "reason": resolution.reason,
                    "available_dbs": resolution.candidates,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    script = TOOL_ROOT / "scripts" / "query.py"
    venv_python = Path(__file__).resolve().parent / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    python = str(venv_python) if venv_python.exists() else sys.executable
    env = os.environ.copy()
    env.setdefault("RAG_DBS_ROOT", str(DBS_ROOT))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = [
        python,
        str(script),
        question,
        "--db",
        resolution.db_name,
        "--top-k",
        str(args.top_k),
        "--max-chars",
        str(args.max_chars),
        "--format",
        args.format,
    ]
    if args.include_db_hint:
        cmd.append("--include-db-hint")
    raise SystemExit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    main()
