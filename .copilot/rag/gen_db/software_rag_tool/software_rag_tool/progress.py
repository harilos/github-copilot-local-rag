from __future__ import annotations

import json
import functools
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json, retry_windows_sharing
from .paths import logs_dir


class _RunObservation:
    def __init__(self) -> None:
        self.snapshot: dict[str, Any] | None = None
        self.disabled: set[str] = set()

    def disable(self, sink: str) -> None:
        if sink in self.disabled:
            return
        self.disabled.add(sink)
        # Never render exception text: Windows exceptions include absolute paths.
        try:
            print(f"WARNING observability_degraded: {sink} persistence disabled for this run; database outcome is reported separately.", file=sys.stderr)
        except OSError:
            pass  # A broken diagnostic stream must not replace the DB outcome.


_RUN: ContextVar[_RunObservation | None] = ContextVar("rag_observation_run", default=None)


def observability_run(function):
    """Scope optional sinks to a single ingestion; all other writes stay strict."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        state = _RunObservation()
        token = _RUN.set(state)
        try:
            result = function(*args, **kwargs)
            if state.disabled:
                result["observability_degraded"] = True
                result["observability_failed_sinks"] = sorted(state.disabled)
            return result
        finally:
            _RUN.reset(token)
    return wrapped


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def progress_path() -> Path:
    return logs_dir() / "progress.json"


def events_path() -> Path:
    return logs_dir() / "events.jsonl"


def read_progress() -> dict[str, Any]:
    path = progress_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def write_progress(**updates: Any) -> dict[str, Any]:
    state = _RUN.get()
    if state is None:
        data = read_progress()
    else:
        if state.snapshot is None:
            try:
                state.snapshot = retry_windows_sharing(read_progress)
            except OSError:
                state.disable("progress")
                state.snapshot = {}
        data = dict(state.snapshot)
    data.update({key: value for key, value in updates.items() if value is not None})
    data["updated_at"] = utc_now()
    # Validate even when disabled. Invalid payloads/programming errors must fail.
    json.dumps(data, ensure_ascii=False, sort_keys=True)
    if state is not None:
        state.snapshot = data
    if state is None or "progress" not in state.disabled:
        try:
            atomic_write_json(progress_path(), data)
        except OSError:
            if state is None:
                raise
            state.disable("progress")
    return data


def emit_event(event: str, **fields: Any) -> None:
    record = {
        "event": event,
        "at": utc_now(),
        **{key: value for key, value in fields.items() if value is not None},
    }
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    state = _RUN.get()
    if state is not None and "events" in state.disabled:
        return
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only retry open, which has appended no bytes yet. Retrying a failed
        # append/flush could duplicate part of an event. Disable on that failure.
        with retry_windows_sharing(lambda: path.open("a", encoding="utf-8")) as stream:
            stream.write(payload)
    except OSError:
        if state is None:
            raise
        state.disable("events")
