from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from help_links import MANAGER_HELP_EPILOG
from software_rag_tool.config import DEFAULT_INGESTION_BATCH_SIZE_FILES
from software_rag_tool.dbs import collection_name_for_db, ensure_db_layout, require_db_name
from software_rag_tool.env import load_env
from software_rag_tool.incremental import add_or_update_root
from software_rag_tool.paths import dbs_dir


_PROGRESS_FRAME = "@@LOCAL_RAG_PROGRESS_V1@@"
_RESULT_FRAME = "@@LOCAL_RAG_RESULT_V1@@"
_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".log",
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".xlsx",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".cs",
        ".rb",
        ".php",
        ".sh",
        ".ps1",
        ".sql",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
    }
)


class _ManagerProtocolWriter:
    """Route legacy ADD logs away from framed result stdout."""

    def __init__(self) -> None:
        self._buffer = ""

    def write(self, value: str) -> int:
        self._buffer += str(value)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(value)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""
        sys.stderr.flush()

    @staticmethod
    def _emit(line: str) -> None:
        text = str(line).strip()
        if not text:
            return
        # Structured file-level progress is emitted by _AddProgressWatcher.
        # Suppress the older batch-only line so it cannot overwrite an exact
        # total/current-file display with an unknown-total event.
        if text.startswith("PROGRESS "):
            return
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


class _AddProgressWatcher:
    """Publish current ADD file, exact total, and a continuously refreshed ETA."""

    def __init__(self, path: Path, *, enabled: bool) -> None:
        self._path = Path(path)
        self._enabled = bool(enabled)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = time.monotonic()
        self._last_emit = float("-inf")
        self._last_signature = ""
        self._last_payload: dict[str, Any] | None = None
        self._file_positions: dict[str, int] | None = None
        self._file_position_scope: tuple[str, str, str] | None = None
        try:
            self._baseline_mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            self._baseline_mtime_ns = None
        self._saw_current_run = False

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._publish(force=True)

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self._publish(force=False)

    def _publish(self, *, force: bool) -> None:
        now = time.monotonic()
        snapshot = self._read_snapshot()
        if snapshot is not None:
            payload = self._progress_payload(snapshot, now)
            signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            changed = signature != self._last_signature
            if changed or force or now - self._last_emit >= 5.0:
                self._emit(payload)
                self._last_payload = payload
                self._last_signature = signature
                self._last_emit = now
            return
        if (
            force
            or self._last_payload is None
            or now - self._last_emit < 5.0
        ):
            return
        heartbeat = dict(self._last_payload)
        heartbeat["elapsed_seconds"] = round(max(0.0, now - self._started), 3)
        self._refresh_remaining(heartbeat)
        self._emit(heartbeat)
        self._last_payload = heartbeat
        self._last_signature = json.dumps(
            heartbeat,
            ensure_ascii=False,
            sort_keys=True,
        )
        self._last_emit = now

    def _read_snapshot(self) -> dict[str, Any] | None:
        try:
            stat_result = self._path.stat()
        except OSError:
            return None
        if (
            not self._saw_current_run
            and self._baseline_mtime_ns is not None
            and stat_result.st_mtime_ns == self._baseline_mtime_ns
        ):
            return None
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        self._saw_current_run = True
        return value

    def _progress_payload(
        self,
        snapshot: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        total = _non_negative_int(snapshot.get("files_total"))
        done = _non_negative_int(snapshot.get("files_done"))
        if done == 0:
            done = sum(
                _non_negative_int(snapshot.get(key))
                for key in ("indexed_files", "skipped_files", "error_files")
            )
        done = min(done, total) if total else done
        phase = str(snapshot.get("phase") or "reflect")
        status = str(snapshot.get("status") or "running")
        current = str(snapshot.get("current_file") or "").strip()
        if not current and status == "running" and phase in {
            "delete",
            "embedding",
            "catalog",
            "save_state",
        }:
            batch = snapshot.get("current_batch_files")
            if isinstance(batch, list) and batch:
                current = str(batch[-1] or "").strip()
        if total == 0 and phase == "scan":
            current = ""

        current_index = self._exact_file_position(snapshot, current)
        if current_index is None:
            current_index = 0
            if total:
                current_index = total if done >= total else min(total, done + 1)

        payload: dict[str, Any] = {
            "event": "add.file_progress",
            "phase": "reflect",
            "label_ja": "ADD検索反映",
            "status": status,
            "completed": done,
            "total": total,
            "total_kind": "exact",
            "unit": "件",
            "current_index": current_index,
            "current_item": current,
            "add_phase": phase,
            "elapsed_seconds": round(max(0.0, now - self._started), 3),
        }
        self._refresh_remaining(payload)
        return payload

    def _exact_file_position(
        self,
        snapshot: dict[str, Any],
        current: str,
    ) -> int | None:
        if not current:
            return None
        scan_root_text = str(snapshot.get("scan_root") or "").strip()
        resolved_root_text = str(snapshot.get("resolved_root") or "").strip()
        root_name = str(snapshot.get("root_display_name") or "").strip()
        if not scan_root_text or not resolved_root_text or not root_name:
            return None
        scope = (scan_root_text, resolved_root_text, root_name)
        if self._file_positions is None or self._file_position_scope != scope:
            self._file_positions = self._build_file_positions(
                Path(scan_root_text),
                Path(resolved_root_text),
                root_name,
            )
            self._file_position_scope = scope
        return self._file_positions.get(current)

    @staticmethod
    def _build_file_positions(
        scan_root: Path,
        resolved_root: Path,
        root_name: str,
    ) -> dict[str, int]:
        try:
            scan = scan_root.expanduser().resolve(strict=True)
            root = resolved_root.expanduser().resolve(strict=True)
            scan.relative_to(root)
        except (OSError, ValueError):
            return {}
        discovered: list[Path] = []
        try:
            for directory, child_directories, filenames in os.walk(
                scan,
                topdown=True,
                followlinks=False,
            ):
                child_directories.sort()
                for filename in sorted(filenames):
                    path = Path(directory) / filename
                    if (
                        path.is_file()
                        and path.suffix.lower() in _SUPPORTED_EXTENSIONS
                    ):
                        discovered.append(path)
        except OSError:
            return {}
        positions: dict[str, int] = {}
        for index, path in enumerate(sorted(discovered), start=1):
            try:
                relative = path.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                continue
            stored = PurePosixPath(root_name, *relative.parts).as_posix()
            positions[stored] = index
        return positions

    def _refresh_remaining(self, payload: dict[str, Any]) -> None:
        total = _non_negative_int(payload.get("total"))
        done = min(_non_negative_int(payload.get("completed")), total)
        remaining = max(0, total - done)
        if total == 0 or remaining == 0:
            payload["eta_seconds"] = 0.0
            payload.pop("remaining_seconds_min", None)
            payload.pop("remaining_seconds_max", None)
            return
        elapsed = max(0.0, time.monotonic() - self._started)
        if done > 0 and elapsed > 0:
            payload["eta_seconds"] = round(
                min(365.0 * 24.0 * 3600.0, elapsed / done * remaining),
                3,
            )
            payload.pop("remaining_seconds_min", None)
            payload.pop("remaining_seconds_max", None)
            return
        payload.pop("eta_seconds", None)
        payload["remaining_seconds_min"] = remaining * 60
        payload["remaining_seconds_max"] = remaining * 300

    @staticmethod
    def _emit(payload: dict[str, Any]) -> None:
        sys.stderr.write(
            _PROGRESS_FRAME
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            + "\n"
        )
        sys.stderr.flush()


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(
        epilog=MANAGER_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", required=True, help="Target DB name, e.g. project-rag")
    parser.add_argument("--root", required=True, help="Input document directory")
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
        "--resume",
        action="store_true",
        help=(
            "Resume only when root, source ID, and scan subdirectory match saved state; "
            "the saved document batch size is reused"
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
    parser.add_argument("--reset-db", action="store_true", help="Delete and recreate the Chroma collection before adding data")
    parser.add_argument("--reset-clean", action="store_true", help="Delete clean records and resume state before adding data")
    parser.add_argument("--retry-errors", action="store_true", help="Retry unchanged files that previously failed extraction")
    parser.add_argument("--chunk-max-chars", type=int, default=1400, help="Optional chunk size for evaluation builds")
    parser.add_argument("--chunk-overlap", type=int, default=160, help="Optional chunk overlap for evaluation builds")
    parser.add_argument("--operation", default="add", choices=["add", "build"], help=argparse.SUPPRESS)
    parser.add_argument(
        "--manager-protocol-v1",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.resume and (args.reset_db or args.reset_clean):
        parser.error("--resume cannot be combined with --reset-db or --reset-clean")

    db_name = require_db_name(args.db)
    db_root = ensure_db_layout(dbs_dir(), db_name)
    os.environ["RAG_DB_NAME"] = db_name
    os.environ["RAG_OUTPUT_ROOT"] = str(db_root)
    os.environ.setdefault(
        "CHROMA_COLLECTION",
        collection_name_for_db(db_name),
    )
    protocol_writer = _ManagerProtocolWriter()
    output_context = (
        contextlib.redirect_stdout(protocol_writer)
        if args.manager_protocol_v1
        else contextlib.nullcontext()
    )
    watcher = _AddProgressWatcher(
        db_root / "logs" / "progress.json",
        enabled=args.manager_protocol_v1,
    )
    watcher.start()
    try:
        with output_context:
            summary = add_or_update_root(
                root=Path(args.root),
                source_id=args.source_id,
                scan_subdir=args.scan_subdir,
                include_root_name_in_path=True,
                batch_size_files=args.batch_size_files,
                reset_db=args.reset_db,
                reset_clean=args.reset_clean,
                retry_errors=args.retry_errors,
                operation=args.operation,
                chunk_max_chars=args.chunk_max_chars,
                chunk_overlap=args.chunk_overlap,
                resume=args.resume,
            )
    finally:
        watcher.stop()
    if args.manager_protocol_v1:
        protocol_writer.flush()
        print(
            _RESULT_FRAME
            + json.dumps(summary, ensure_ascii=False, sort_keys=True)
        )
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
