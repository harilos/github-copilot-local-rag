from __future__ import annotations

import json
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import sanitize_diagnostic


DIAGNOSTIC_OUTPUT_LIMIT = 65_536
TRACEBACK_LIMIT = 1_048_576
_SECRET_ARGUMENTS = frozenset(
    {
        "--api-key",
        "--authorization",
        "--cookie",
        "--password",
        "--secret",
        "--token",
        "-p",
    }
)


def bounded_diagnostic(value: Any, *, limit: int = DIAGNOSTIC_OUTPUT_LIMIT) -> dict[str, Any]:
    """Sanitize output while preserving both the beginning and the end."""
    text = sanitize_diagnostic(value, max_chars=max(TRACEBACK_LIMIT, limit * 4))
    original_chars = len(text)
    if original_chars <= limit:
        return {
            "text": text,
            "chars": original_chars,
            "omitted_chars": 0,
            "truncated": False,
        }
    marker_reserve = 160
    available = max(2, limit - marker_reserve)
    head = available // 2
    tail = available - head
    omitted = max(0, original_chars - head - tail)
    rendered = (
        text[:head].rstrip()
        + f"\n...（{omitted:,}文字を省略）...\n"
        + text[-tail:].lstrip()
    )
    return {
        "text": rendered,
        "chars": original_chars,
        "omitted_chars": omitted,
        "truncated": True,
    }


def exception_diagnostic(
    exc: BaseException,
    *,
    operation: str,
    stage: str,
    db_name: str | None = None,
    source_name: str | None = None,
    source_key: str | None = None,
    provider: str | None = None,
    can_resume: bool | None = None,
    events_jsonl: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    chains: list[dict[str, str]] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chains.append(
            {
                "type": type(current).__name__,
                "message": sanitize_diagnostic(
                    str(current),
                    max_chars=DIAGNOSTIC_OUTPUT_LIMIT,
                ),
            }
        )
        current = current.__cause__ or current.__context__
    formatted = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    traceback_value = bounded_diagnostic(
        formatted,
        limit=TRACEBACK_LIMIT,
    )
    raw_detail = getattr(exc, "diagnostic", None)
    if raw_detail is None:
        raw_detail = getattr(exc, "diagnostics", None)
    detail: Any = None
    if isinstance(raw_detail, (Mapping, list, tuple)):
        try:
            sanitized_detail = sanitize_diagnostic(
                json.dumps(
                    raw_detail,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                max_chars=DIAGNOSTIC_OUTPUT_LIMIT,
            )
            detail = json.loads(sanitized_detail)
        except (TypeError, ValueError, json.JSONDecodeError):
            detail = sanitized_detail if "sanitized_detail" in locals() else None
    return {
        "occurred_at": _now(),
        "run_id": str(run_id or uuid.uuid4()),
        "operation": str(operation),
        "stage": str(stage),
        "db_name": str(db_name or ""),
        "source_name": str(source_name or ""),
        "source_key": str(source_key or ""),
        "provider": str(provider or ""),
        "exception_chain": chains,
        "can_resume": can_resume,
        "events_jsonl": str(events_jsonl or ""),
        "traceback": traceback_value,
        "operation_detail": detail,
    }


def process_diagnostic(
    *,
    arguments: Iterable[Any],
    cwd: str | Path,
    returncode: int | None,
    elapsed_seconds: float,
    stdout: Any = "",
    stderr: Any = "",
) -> dict[str, Any]:
    stdout_value = bounded_diagnostic(stdout)
    stderr_value = bounded_diagnostic(stderr)
    return {
        "command": _sanitized_command(arguments),
        "cwd": sanitize_diagnostic(cwd, max_chars=8_192),
        "returncode": returncode,
        "elapsed_seconds": max(0.0, float(elapsed_seconds)),
        "stdout": stdout_value,
        "stderr": stderr_value,
    }


def render_diagnostic(
    diagnostic: Mapping[str, Any],
    *,
    process: Mapping[str, Any] | None = None,
) -> list[str]:
    chain = diagnostic.get("exception_chain")
    chain = chain if isinstance(chain, list) else []
    first = chain[0] if chain and isinstance(chain[0], Mapping) else {}
    lines = [
        f"発生日時: {diagnostic.get('occurred_at') or '不明'}",
        f"run_id: {diagnostic.get('run_id') or '不明'}",
        f"操作名: {diagnostic.get('operation') or '不明'}",
        f"処理段階: {diagnostic.get('stage') or '不明'}",
        f"DB名: {diagnostic.get('db_name') or '未特定'}",
        f"Source名: {diagnostic.get('source_name') or '未特定'}",
        f"Source key: {diagnostic.get('source_key') or '未特定'}",
        f"Provider: {diagnostic.get('provider') or '未特定'}",
        (
            "例外: "
            f"{first.get('type') or '不明'}: "
            f"{first.get('message') or '詳細なし'}"
        ),
    ]
    if len(chain) > 1:
        lines.append("cause／context:")
        for item in chain[1:]:
            if isinstance(item, Mapping):
                lines.append(
                    "  - "
                    f"{item.get('type') or '不明'}: "
                    f"{item.get('message') or '詳細なし'}"
                )
    resumable = diagnostic.get("can_resume")
    lines.append(
        "再開可能: "
        + ("はい" if resumable is True else "いいえ" if resumable is False else "不明")
    )
    if diagnostic.get("events_jsonl"):
        lines.append(f"進捗ログ: {diagnostic['events_jsonl']}")
    if diagnostic.get("operation_detail") is not None:
        lines.append("詳細診断（秘密情報を伏せて表示）:")
        lines.append(
            json.dumps(
                diagnostic["operation_detail"],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    if process is not None:
        command = process.get("command")
        command = command if isinstance(command, list) else []
        lines.extend(
            [
                "command（秘密情報を伏せて表示）: "
                + " ".join(str(value) for value in command),
                f"cwd: {process.get('cwd') or '不明'}",
                f"終了コード: {process.get('returncode')}",
                f"経過時間: {float(process.get('elapsed_seconds') or 0.0):.3f}秒",
            ]
        )
        for key, label in (("stdout", "stdout"), ("stderr", "stderr")):
            value = process.get(key)
            value = value if isinstance(value, Mapping) else {}
            lines.append(
                f"{label}: {int(value.get('chars') or 0):,}文字"
                + (
                    f"（{int(value.get('omitted_chars') or 0):,}文字省略）"
                    if value.get("truncated")
                    else ""
                )
            )
            if value.get("text"):
                lines.append(f"{label}内容:")
                lines.append(str(value["text"]))
    trace = diagnostic.get("traceback")
    trace = trace if isinstance(trace, Mapping) else {}
    if trace.get("text"):
        lines.append("例外ログ（診断用）/ traceback:")
        lines.append(str(trace["text"]))
    return lines


def append_diagnostic_event(
    db_root: Path,
    source_key: str | None,
    diagnostic: Mapping[str, Any],
    *,
    process: Mapping[str, Any] | None = None,
) -> None:
    """Best-effort persistence to an existing Source events log."""
    key = str(source_key or "")
    if not key:
        return
    try:
        from .store import SourceStore

        details = {
            "occurred_at": diagnostic.get("occurred_at"),
            "run_id": diagnostic.get("run_id"),
            "operation": diagnostic.get("operation"),
            "stage": diagnostic.get("stage"),
            "exception_chain": diagnostic.get("exception_chain"),
            "can_resume": diagnostic.get("can_resume"),
            "operation_detail": diagnostic.get("operation_detail"),
        }
        if process is not None:
            details["process"] = dict(process)
        SourceStore(Path(db_root)).append_event(
            key,
            "diagnostic.error",
            details,
        )
    except Exception:
        # Logging failure must not mask the original operation failure.
        return


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitized_command(arguments: Iterable[Any]) -> list[str]:
    values = [str(value) for value in arguments]
    rendered: list[str] = []
    redact_next = False
    for value in values:
        if redact_next:
            rendered.append("<REDACTED>")
            redact_next = False
            continue
        option, separator, _assigned = value.partition("=")
        lowered = option.casefold()
        if lowered in _SECRET_ARGUMENTS:
            rendered.append(
                option + ("=<REDACTED>" if separator else "")
            )
            redact_next = not bool(separator)
            continue
        rendered.append(sanitize_diagnostic(value, max_chars=4_096))
    return rendered
