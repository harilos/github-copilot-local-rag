from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from windows_package_builder import BuildRequest, build_package


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dbs-root", type=Path)
    parser.add_argument("--db", dest="database_names", action="append")
    parser.add_argument("--no-database", action="store_true")
    parser.add_argument("--database-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--version", required=True)
    parser.add_argument("--profile", choices=("search-only", "admin-full"), required=True)
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()
    if args.no_database and (args.database_names or args.database_root or args.dbs_root):
        parser.error("--no-database cannot be combined with database arguments")
    if args.database_root and (args.database_names or args.dbs_root):
        parser.error("legacy --database-root cannot be combined with canonical arguments")
    if args.database_names and not args.dbs_root:
        parser.error("--db requires --dbs-root")
    names = tuple(args.database_names or ())
    if args.dbs_root and not args.database_names:
        sys.path.insert(0, str(args.payload_root / "rag"))
        from multi_select import database_selection_rows, discover_database_summaries, toggle_selection
        selection = toggle_selection(database_selection_rows(discover_database_summaries(args.dbs_root), args.dbs_root), ask=_ask, output=print, invalid=lambda message: print(f"Invalid selection: {message}"), title="Database selection (initially all selected)")
        if selection.mode == "cancelled":
            raise SystemExit("database selection cancelled")
        names = selection.keys
    result = build_package(BuildRequest(payload_root=args.payload_root, runtime_root=args.runtime_root, model_root=args.model_root, output_dir=args.output_dir, databases_root=args.dbs_root, database_names=names, no_database=args.no_database, database_root=args.database_root, version=args.version, profile=args.profile, python_version=args.python_version))
    print(json.dumps({"zip_path":str(result.zip_path),"database_names":list(result.database_names)}, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
