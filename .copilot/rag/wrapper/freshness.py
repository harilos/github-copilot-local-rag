from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STALE_AFTER_DAYS = 30
STALE_NOTICE_CODE = "local_rag_snapshot_older_than_30_days"
STALE_NOTICE_MESSAGE_JA = (
    "このRAGは配布または全体更新から30日以上経過しています。"
    "内容が古い可能性があるため、必要なら管理者から最新版を"
    "受け取ってください。"
)


def database_freshness(
    db_root: Path | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    snapshot = _catalog_snapshot_at(db_root)
    parsed = _parse_timestamp(snapshot)
    if parsed is None:
        return {
            "status": "unknown",
            "snapshot_at": None,
            "age_days": None,
        }
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (
        current.astimezone(timezone.utc) - parsed
    ).total_seconds()
    if age_seconds < 0:
        return {
            "status": "unknown",
            "snapshot_at": None,
            "age_days": None,
        }
    age_days = int(age_seconds // 86_400)
    return {
        "status": (
            "stale"
            if age_seconds >= STALE_AFTER_DAYS * 86_400
            else "current"
        ),
        "snapshot_at": _iso_z(parsed),
        "age_days": age_days,
    }


def add_freshness(
    payload: dict[str, Any],
    db_root: Path | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    output = dict(payload)
    freshness = database_freshness(db_root, now=now)
    if freshness["status"] == "stale":
        freshness["chat_notice"] = {
            "code": STALE_NOTICE_CODE,
            "scope": "conversation_once",
            "message_ja": STALE_NOTICE_MESSAGE_JA,
        }
    output["database_freshness"] = freshness
    return output


def _catalog_snapshot_at(db_root: Path | None) -> str | None:
    if db_root is None:
        return None
    snapshot_file = Path(db_root) / "db-snapshot.json"
    snapshot_at = _validated_timestamp_field(
        snapshot_file,
        schema_field="schema_version",
        schema="local-rag-db-snapshot-v1",
        field="snapshot_at",
    )
    if snapshot_at is not None:
        return snapshot_at
    version_file = Path(db_root) / "VERSION.json"
    return _validated_timestamp_field(
        version_file,
        schema_field="schema",
        schema="local-rag.db-version.v1",
        field="created_at",
    )


def _validated_timestamp_field(
    path: Path,
    *,
    schema_field: str,
    schema: str,
    field: str,
) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > 65_536:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get(schema_field) != schema
    ):
        return None
    value = payload.get(field)
    text = str(value or "").strip()
    return text if _parse_timestamp(text) is not None else None


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
