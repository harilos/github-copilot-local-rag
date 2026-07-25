from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import read_db_version, require_db_name
from software_rag_tool.env import load_env
from software_rag_tool.paths import dbs_dir
from software_rag_tool.catalog import counts as catalog_counts


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target DB name, e.g. project-rag")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status")
    parser.add_argument("--stale-minutes", type=int, default=30)
    parser.add_argument("--tail-events", type=int, default=5)
    args = parser.parse_args()

    db_name = require_db_name(args.db)
    os.environ.setdefault("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))
    db_root = dbs_dir() / db_name
    os.environ["RAG_DB_NAME"] = db_name
    os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
    logs_root = db_root / "logs"

    progress = _load_json(logs_root / "progress.json")
    state = _load_json(logs_root / "index_state.json")
    manifest = _load_json(db_root / "index" / "manifest.json")
    version = read_db_version(db_root)
    errors = _load_json(logs_root / "prepare_errors.json", default=[])
    events = _tail_jsonl(logs_root / "events.jsonl", args.tail_events)
    catalog = catalog_counts()

    status = _effective_status(progress, args.stale_minutes)
    operation = str(progress.get("operation") or "")
    root = str(progress.get("root") or "")
    source_id = str(progress.get("source_id") or "")
    resume_command = _resume_command(db_name, operation, root, source_id)
    state_files = state.get("files") if isinstance(state, dict) else {}
    if not isinstance(state_files, dict):
        state_files = {}

    output = {
        "db": db_name,
        "db_root": str(db_root),
        "exists": db_root.exists(),
        "version": version,
        "status": status,
        "raw_status": progress.get("status") or "not_started",
        "phase": progress.get("phase") or "",
        "updated_at": progress.get("updated_at") or "",
        "operation": operation,
        "root": root,
        "source_id": source_id,
        "files_total": progress.get("files_total") or 0,
        "files_done": progress.get("files_done") or 0,
        "indexed_files": progress.get("indexed_files") or _count_state(state_files, "indexed"),
        "skipped_files": progress.get("skipped_files") or 0,
        "error_files": progress.get("error_files") or _count_state(state_files, "error"),
        "upserted_records": progress.get("upserted_records") or 0,
        "deleted_records": progress.get("deleted_records") or 0,
        "collection_count": progress.get("collection_count") or manifest.get("record_count") or 0,
        "catalog": catalog,
        "current_file": progress.get("current_file") or "",
        "current_batch_files": progress.get("current_batch_files") or [],
        "current_batch_records_done": progress.get("current_batch_records_done") or 0,
        "current_batch_records_total": progress.get("current_batch_records_total") or 0,
        "last_error": progress.get("last_error") or "",
        "errors_path": str(logs_root / "prepare_errors.json"),
        "error_count_total": len(errors) if isinstance(errors, list) else 0,
        "events": events,
        "can_resume": bool(resume_command and status in {"failed", "stale_running", "completed", "not_started"}),
        "appears_active": status == "running",
        "resume_command": resume_command,
        "force_rebuild_command": _force_rebuild_command(db_name, root, source_id),
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return

    _print_human(output)


def _load_json(path: Path, default: Any | None = None) -> Any:
    if default is None:
        default = {}
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _tail_jsonl(path: Path, count: int) -> list[dict[str, Any]]:
    if count <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    output: list[dict[str, Any]] = []
    for line in lines[-count:]:
        try:
            output.append(json.loads(line))
        except json.JSONDecodeError:
            output.append({"raw": line})
    return output


def _effective_status(progress: dict[str, Any], stale_minutes: int) -> str:
    raw = str(progress.get("status") or "not_started")
    if raw != "running":
        return raw
    updated = _parse_time(str(progress.get("updated_at") or ""))
    if not updated:
        return raw
    age = datetime.now(timezone.utc) - updated
    if age.total_seconds() > stale_minutes * 60:
        return "stale_running"
    return raw


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _count_state(files: dict[str, Any], status: str) -> int:
    return sum(1 for item in files.values() if isinstance(item, dict) and item.get("status") == status)


def _resume_command(db_name: str, operation: str, root: str, source_id: str) -> list[str]:
    if not root or not source_id:
        return []
    if operation == "build":
        return [
            "python",
            "~/.copilot/rag/gen_db/build_db.py",
            "--db",
            db_name,
            "--root",
            root,
            "--source-id",
            source_id,
            "--resume",
        ]
    return [
        "python",
        "~/.copilot/rag/gen_db/add_data.py",
        "--db",
        db_name,
        "--root",
        root,
        "--source-id",
        source_id,
    ]


def _force_rebuild_command(db_name: str, root: str, source_id: str) -> list[str]:
    if not root or not source_id:
        return []
    return [
        "python",
        "~/.copilot/rag/gen_db/build_db.py",
        "--db",
        db_name,
        "--root",
        root,
        "--source-id",
        source_id,
        "--force-rebuild",
    ]


def _format_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _print_human(output: dict[str, Any]) -> None:
    print(f"DB: {output['db']}")
    version = output.get("version") or {}
    if version:
        print(f"Version: created_at={version.get('created_at')} db_hash={version.get('db_hash')}")
    print(f"Status: {output['status']} phase={output['phase']} updated_at={output['updated_at']}")
    print(f"Root: {output['root']}")
    print(f"Source: {output['source_id']}")
    print(
        "Files: "
        f"{output['files_done']}/{output['files_total']} "
        f"indexed={output['indexed_files']} skipped={output['skipped_files']} errors={output['error_files']}"
    )
    print(
        "Records: "
        f"upserted={output['upserted_records']} deleted={output['deleted_records']} "
        f"collection={output['collection_count']}"
    )
    catalog = output.get("catalog") or {}
    print(
        "Catalog: "
        f"exists={catalog.get('exists')} chunks={catalog.get('chunks', 0)} "
        f"documents={catalog.get('documents', 0)} fts={catalog.get('fts_rows', 0)} "
        f"identifier_terms={catalog.get('identifier_terms', 0)} "
        f"identifier_postings={catalog.get('identifier_postings', catalog.get('identifiers', 0))}"
    )
    current = output.get("current_file")
    if current:
        print(f"Current file: {current}")
    if output.get("last_error"):
        print(f"Last error: {output['last_error']}")
    if output["appears_active"]:
        print("Action: a run appears active; check status again before starting another run.")
    elif output["can_resume"]:
        print("Resume:")
        print(_format_command(output["resume_command"]))
    if output.get("force_rebuild_command"):
        print("Force rebuild:")
        print(_format_command(output["force_rebuild_command"]))


if __name__ == "__main__":
    main()
