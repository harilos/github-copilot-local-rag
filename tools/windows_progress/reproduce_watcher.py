"""Deterministically widen the original Watcher's ordinary reader lifetime.

Reads the pre-fix implementation from Git, never a user database. Outputs only
artifact aliases and thread roles, not fixture paths, database names or errors.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
from unittest.mock import patch

BASELINE = "435d794fe790a68be974db43e2b8e0bafcda1595"


def main() -> int:
    if os.name != "nt":
        print(json.dumps({"status": "requires_windows"}))
        return 2
    repo = Path(__file__).resolve().parents[2]
    rag = repo / ".copilot/rag"
    sys.path[:0] = [str(rag / "gen_db/software_rag_tool"), str(rag)]
    source = subprocess.check_output([
        "git", "-c", "safe.directory=" + repo.as_posix(), "show",
        BASELINE + ":.copilot/rag/gen_db/add_data.py",
    ], cwd=repo).decode("utf-8")
    legacy = types.ModuleType("legacy_add_watcher_reproduction")
    legacy.__file__ = str(rag / "gen_db/add_data.py")
    exec(compile(source, legacy.__file__, "exec"), legacy.__dict__)
    # Use original atomic implementation too, independent of current changes.
    atomic_source = subprocess.check_output([
        "git", "-c", "safe.directory=" + repo.as_posix(), "show",
        BASELINE + ":.copilot/rag/gen_db/software_rag_tool/software_rag_tool/atomic_io.py",
    ], cwd=repo).decode("utf-8")
    atomic = types.ModuleType("legacy_atomic_reproduction")
    exec(compile(atomic_source, "<baseline-atomic>", "exec"), atomic.__dict__)
    ready, release = threading.Event(), threading.Event()
    original_open = Path.open
    events: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="issue20-synthetic-") as directory:
        target = Path(directory) / "progress.json"
        atomic.atomic_write_json(target, {"phase": "scan"})
        before = target.read_bytes()
        watcher = legacy._AddProgressWatcher(target, enabled=True)
        watcher._saw_current_run = True

        def held_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == target and threading.current_thread() is not threading.main_thread():
                events.append({"step": "ordinary_read_handle_open", "thread": "watcher", "artifact": "progress.json"})
                ready.set()
                release.wait(5)
            return stream

        winerror = None
        with patch.object(Path, "open", held_open), patch.object(watcher, "_emit"):
            watcher.start()
            try:
                if not ready.wait(5):
                    raise RuntimeError("fixture reader did not become ready")
                events.append({"step": "atomic_replace_attempt", "thread": "producer", "artifact": "progress.json"})
                try:
                    atomic.atomic_write_json(target, {"phase": "extract"})
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    events.append({"step": "atomic_io.os.replace_failed", "thread": "producer", "winerror": winerror})
            finally:
                release.set()
                watcher.stop()
        print(json.dumps({"baseline": BASELINE, "events": events,
            "previous_bytes_intact": target.read_bytes() == before,
            "orphan_temps": len(list(Path(directory).glob("*.tmp"))),
            "status": "reproduced" if winerror in {5, 32, 33} else "not_reproduced"}))
        return 0 if winerror in {5, 32, 33} else 1


if __name__ == "__main__":
    raise SystemExit(main())
