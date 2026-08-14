from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json
from .paths import logs_dir


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
    data = read_progress()
    data.update({key: value for key, value in updates.items() if value is not None})
    data["updated_at"] = utc_now()
    path = progress_path()
    atomic_write_json(path, data)
    return data


def emit_event(event: str, **fields: Any) -> None:
    record = {
        "event": event,
        "at": utc_now(),
        **{key: value for key, value in fields.items() if value is not None},
    }
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        f.write("\n")
