from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STALE_AFTER_DAYS = 30
WRAPPER_METADATA_NAME = "rag-wrapper.json"
WRAPPER_METADATA_SCHEMA = "local-rag.wrapper.v1"
STALE_NOTICE_CODE = "local_rag_content_snapshot_older_than_30_days"
STALE_NOTICE_SCOPE = "conversation"
STALE_NOTICE_DEDUPE_KEY = "local_rag_content_snapshot_stale"
STALE_NOTICE_MESSAGE_JA = (
    "このRAGの内容更新時点から30日以上経過しています。"
    "内容が古い可能性があるため、必要なら管理者から最新版を"
    "受け取ってください。"
)


def database_freshness(
    db_root: Path | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    snapshot = _content_snapshot_at(db_root)
    parsed = _parse_timestamp(snapshot)
    if parsed is None:
        return {
            "status": "unknown",
            "content_snapshot_at": None,
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
            "content_snapshot_at": None,
            "age_days": None,
        }
    age_days = int(age_seconds // 86_400)
    return {
        "status": (
            "stale"
            if age_seconds >= STALE_AFTER_DAYS * 86_400
            else "current"
        ),
        "content_snapshot_at": _iso_z(parsed),
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
            "scope": STALE_NOTICE_SCOPE,
            "dedupe_key": STALE_NOTICE_DEDUPE_KEY,
            "message_ja": STALE_NOTICE_MESSAGE_JA,
        }
    output["database_freshness"] = freshness
    return output


def _content_snapshot_at(db_root: Path | None) -> str | None:
    if db_root is None:
        return None
    wrapper_file = Path(db_root) / WRAPPER_METADATA_NAME
    content_snapshot_at = _validated_timestamp_field(
        wrapper_file,
        schema_field="schema_version",
        schema=WRAPPER_METADATA_SCHEMA,
        field="content_snapshot_at",
    )
    if content_snapshot_at is not None:
        return content_snapshot_at
    return None


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
