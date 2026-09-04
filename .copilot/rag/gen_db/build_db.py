from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
QUERY_ROOT = RAG_ROOT / "query"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(QUERY_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from help_links import MANAGER_HELP_EPILOG
from windows_runtime import is_fixed_windows_runtime
from setup_contract import completion_contract_valid, completion_marker_for
from software_rag_tool.config import DEFAULT_INGESTION_BATCH_SIZE_FILES


def main() -> int:
    parser = argparse.ArgumentParser(
        epilog=MANAGER_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", required=True, help="Target DB name, e.g. project-rag")
    parser.add_argument("--root", required=True, help="Input directory")
    parser.add_argument(
        "--source-id",
        default="local",
        help=(
            "Stable ingestion Source ID. Keep each provider separate; "
            "generic examples: sharepoint-docs, redmine-issues, "
            "github-repository, gitlab-repository, azure-repository, "
            "svn-repository, filesystem-docs."
        ),
    )
    parser.add_argument(
        "--scan-subdir",
        help="Relative subdirectory to scan while keeping paths relative to --root",
    )
    parser.add_argument(
        "--include-root-name-in-path",
        action="store_true",
        help=(
            "Accepted for compatibility. The root directory name is always "
            "included in stored document paths."
        ),
    )
    parser.add_argument(
        "--batch-size-files",
        type=int,
        default=None,
        help=(
            "Documents committed per checkpoint (default: "
            f"{DEFAULT_INGESTION_BATCH_SIZE_FILES}; resume reuses the saved value)"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume from logs/index_state.json and reuse the saved document "
            "batch size"
        ),
    )
    parser.add_argument("--force-rebuild", action="store_true", help="Delete clean records and recreate the Chroma collection")
    parser.add_argument("--append", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--persistent-root-identity", help=argparse.SUPPRESS)
    parser.add_argument("--retry-errors", action="store_true", help="Retry unchanged files that previously failed extraction")
    parser.add_argument(
        "--chunk-max-chars",
        type=int,
        default=1400,
        help="Optional character ceiling per chunk for evaluation builds",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=160,
        help="Optional character ceiling for overlap in evaluation builds",
    )
    args = parser.parse_args()
    if args.resume and args.force_rebuild:
        parser.error("--resume and --force-rebuild cannot be used together")

    python = _runtime_python_or_exit()
    env = os.environ.copy()
    configured_root = env.get("RAG_DBS_ROOT", "").strip()
    selected_root = Path(configured_root) if configured_root else RAG_ROOT / "dbs"
    if not selected_root.is_absolute():
        selected_root = RAG_ROOT / selected_root
    for key in (
        "RAG_DB_NAME",
        "RAG_OUTPUT_ROOT",
        "LOCALRAG_OUTPUT_ROOT",
        "CHROMA_DIR_V2",
        "CHROMA_COLLECTION",
    ):
        env.pop(key, None)
    env["RAG_DBS_ROOT"] = str(selected_root.resolve(strict=False))
    add_data = RAG_ROOT / "gen_db" / "add_data.py"
    cmd = [
        python,
        str(add_data),
        "--db",
        args.db,
        "--root",
        args.root,
        "--source-id",
        args.source_id,
        "--include-root-name-in-path",
        "--operation",
        "build",
        "--chunk-max-chars",
        str(args.chunk_max_chars),
        "--chunk-overlap",
        str(args.chunk_overlap),
    ]
    if args.batch_size_files is not None:
        cmd.extend(["--batch-size-files", str(args.batch_size_files)])
    if args.persistent_root_identity is not None:
        cmd.extend(["--persistent-root-identity", args.persistent_root_identity])
    if args.force_rebuild:
        cmd.extend(["--reset-db", "--reset-clean"])
    if args.scan_subdir is not None:
        cmd.extend(["--scan-subdir", args.scan_subdir])
    if args.resume:
        cmd.append("--resume")
    if args.retry_errors:
        cmd.append("--retry-errors")
    completed = subprocess.run(
        cmd,
        check=False,
        cwd=str(RAG_ROOT),
        env=env,
    )
    return int(completed.returncode)


def _runtime_python_or_exit() -> str:
    query_root = RAG_ROOT / "query"
    venv_python = query_root / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    marker = completion_marker_for(query_root)
    marker_valid = (
        sys.platform.startswith("win")
        and is_fixed_windows_runtime(query_root)
    )
    if not marker_valid:
        marker_valid, _marker_reason = completion_contract_valid(
            marker,
            RAG_ROOT,
        )
    if venv_python.exists() and marker_valid:
        return str(venv_python)
    if os.getenv("RAG_ALLOW_UNINITIALIZED_RUNTIME", "").lower() in {"1", "true", "yes"}:
        return sys.executable
    print("RAG runtime is not initialized. Run the initial setup, then retry DB generation.", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
