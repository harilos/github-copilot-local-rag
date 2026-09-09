from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic_io import atomic_write_json


SCHEMA_VERSION = "local-rag.data-lifecycle.v1"
MARKER_NAME = "data-lifecycle.json"
READY = "ready"
SEARCHABLE_PARTIAL = "searchable_partial"
SEARCHABLE_STATUSES = frozenset({READY, SEARCHABLE_PARTIAL})
RESETTING = "resetting"
REFETCH_REQUIRED = "refetch_required"
ATTENTION_REQUIRED = "attention_required"
NON_READY_STATUSES = frozenset({RESETTING, REFETCH_REQUIRED, ATTENTION_REQUIRED})
STATUSES = frozenset({*SEARCHABLE_STATUSES, *NON_READY_STATUSES})
LEGACY_EPOCH = "legacy"


class DataLifecycleError(RuntimeError):
    code = "database_refresh_required"


@dataclass(frozen=True)
class Lifecycle:
    db_name: str
    epoch: str
    status: str
    reset_run_id: str
    source_config_digest: str
    started_at: str
    updated_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": SCHEMA_VERSION,
            "db_name": self.db_name,
            "epoch": self.epoch,
            "status": self.status,
            "reset_run_id": self.reset_run_id,
            "source_config_digest": self.source_config_digest,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


def lifecycle_path(db_root: Path) -> Path:
    return Path(db_root) / MARKER_NAME


def read_lifecycle(db_root: Path, *, allow_missing: bool = True) -> Lifecycle | None:
    root = Path(db_root)
    path = lifecycle_path(root)
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        if allow_missing:
            return None
        raise DataLifecycleError("database lifecycle marker is missing")
    except (OSError, UnicodeError) as exc:
        raise DataLifecycleError("database lifecycle marker is unreadable") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataLifecycleError("database lifecycle marker is invalid") from exc
    if not isinstance(payload, dict):
        raise DataLifecycleError("database lifecycle marker must be an object")
    expected = {
        "schema_version",
        "db_name",
        "epoch",
        "status",
        "reset_run_id",
        "source_config_digest",
        "started_at",
        "updated_at",
    }
    if set(payload) != expected or payload.get("schema_version") != SCHEMA_VERSION:
        raise DataLifecycleError("database lifecycle marker schema is invalid")
    db_name = _plain(payload.get("db_name"), "db_name")
    if db_name != root.name:
        raise DataLifecycleError("database lifecycle identity mismatch")
    epoch = _uuid(payload.get("epoch"), "epoch")
    reset_run_id = _uuid(payload.get("reset_run_id"), "reset_run_id")
    status = _plain(payload.get("status"), "status")
    if status not in STATUSES:
        raise DataLifecycleError("database lifecycle status is invalid")
    digest = _plain(payload.get("source_config_digest"), "source_config_digest")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise DataLifecycleError("database lifecycle source digest is invalid")
    started_at = _timestamp(payload.get("started_at"), "started_at")
    updated_at = _timestamp(payload.get("updated_at"), "updated_at")
    return Lifecycle(
        db_name=db_name,
        epoch=epoch,
        status=status,
        reset_run_id=reset_run_id,
        source_config_digest=digest,
        started_at=started_at,
        updated_at=updated_at,
    )


def new_reset_lifecycle(db_root: Path, *, source_config_digest: str) -> Lifecycle:
    now = _now()
    return Lifecycle(
        db_name=Path(db_root).name,
        epoch=str(uuid.uuid4()),
        status=RESETTING,
        reset_run_id=str(uuid.uuid4()),
        source_config_digest=_digest(source_config_digest),
        started_at=now,
        updated_at=now,
    )


def write_lifecycle(db_root: Path, lifecycle: Lifecycle) -> None:
    root = Path(db_root)
    if lifecycle.db_name != root.name:
        raise DataLifecycleError("database lifecycle identity mismatch")
    atomic_write_json(lifecycle_path(root), lifecycle.as_dict())


def transition_lifecycle(
    db_root: Path,
    lifecycle: Lifecycle,
    *,
    status: str,
    source_config_digest: str | None = None,
) -> Lifecycle:
    if status not in STATUSES:
        raise DataLifecycleError("database lifecycle status is invalid")
    current = read_lifecycle(db_root, allow_missing=False)
    if current is None or current.epoch != lifecycle.epoch:
        raise DataLifecycleError("database lifecycle changed during operation")
    updated = Lifecycle(
        db_name=lifecycle.db_name,
        epoch=lifecycle.epoch,
        status=status,
        reset_run_id=lifecycle.reset_run_id,
        source_config_digest=(
            _digest(source_config_digest)
            if source_config_digest is not None
            else lifecycle.source_config_digest
        ),
        started_at=lifecycle.started_at,
        updated_at=_now(),
    )
    write_lifecycle(db_root, updated)
    return updated


def capture_ready_epoch(db_root: Path) -> str:
    if not Path(db_root).is_dir():
        raise DataLifecycleError("database is unavailable")
    lifecycle = read_lifecycle(db_root)
    if lifecycle is None:
        return LEGACY_EPOCH
    if lifecycle.status not in SEARCHABLE_STATUSES:
        raise DataLifecycleError("database refresh is required")
    return lifecycle.epoch


def assert_ready_epoch(db_root: Path, expected_epoch: str) -> str:
    current = capture_ready_epoch(db_root)
    if current != str(expected_epoch):
        raise DataLifecycleError("database lifecycle changed during read")
    return current


def canonical_digest(records: Iterable[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(records),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def empty_config_digest() -> str:
    return canonical_digest([])


def _plain(value: Any, label: str) -> str:
    text = str(value or "")
    if not text or any(ord(char) < 0x20 for char in text):
        raise DataLifecycleError(f"database lifecycle {label} is invalid")
    return text


def _uuid(value: Any, label: str) -> str:
    text = _plain(value, label)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise DataLifecycleError(f"database lifecycle {label} is invalid") from exc
    if str(parsed) != text:
        raise DataLifecycleError(f"database lifecycle {label} is invalid")
    return text


def _digest(value: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise DataLifecycleError("database lifecycle source digest is invalid")
    return text


def _timestamp(value: Any, label: str) -> str:
    text = _plain(value, label)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataLifecycleError(f"database lifecycle {label} is invalid") from exc
    return text


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
