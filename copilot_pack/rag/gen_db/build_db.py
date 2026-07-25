from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target DB name, e.g. project-rag")
    parser.add_argument("--root", required=True, help="Input directory")
    parser.add_argument("--source-id", default="local")
    parser.add_argument("--batch-size-files", type=int, default=20)
    parser.add_argument("--resume", action="store_true", help="Resume from logs/index_state.json when possible")
    parser.add_argument("--force-rebuild", action="store_true", help="Delete clean records and recreate the Chroma collection")
    parser.add_argument("--append", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--retry-errors", action="store_true", help="Retry unchanged files that previously failed extraction")
    args = parser.parse_args()
    if args.resume and args.force_rebuild:
        parser.error("--resume and --force-rebuild cannot be used together")

    env = os.environ.copy()
    env.setdefault("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))
    add_data = RAG_ROOT / "gen_db" / "add_data.py"
    cmd = [
        sys.executable,
        str(add_data),
        "--db",
        args.db,
        "--root",
        args.root,
        "--source-id",
        args.source_id,
        "--batch-size-files",
        str(args.batch_size_files),
        "--operation",
        "build",
    ]
    if args.force_rebuild:
        cmd.extend(["--reset-db", "--reset-clean"])
    if args.retry_errors:
        cmd.append("--retry-errors")
    subprocess.check_call(cmd, env=env)


if __name__ == "__main__":
    main()
