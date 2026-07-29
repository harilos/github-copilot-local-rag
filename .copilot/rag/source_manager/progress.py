"""Human-facing Source progress rendering.

Progress callbacks are observational: malformed events and rendering failures
must never fail a Source operation.  Exact percentages are shown only when a
producer supplied an exact total; unknown totals remain count/elapsed based.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any


_IMMEDIATE_STATUSES = frozenset(
    {"completed", "failed", "failure", "retry", "success"}
)
_PROVIDER_LABELS = {
    "github": "GitHub",
    "svn": "SVN",
    "redmine": "Redmine",
    "sharepoint": "SharePoint",
    "other": "Other",
}


def normalize_progress_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a protocol event without inventing progress information."""
    value = dict(event)
    payload = value.get("payload")
    if isinstance(payload, Mapping):
        merged = dict(payload)
        for key in ("event", "protocol", "elapsed_seconds"):
            if key in value and key not in merged:
                merged[key] = value[key]
        return merged
    return value


class ProgressRenderer:
    """Rate-limited Japanese renderer for Manager progress callbacks."""

    def __init__(
        self,
        output: Callable[[str], None],
        *,
        operation: str,
        provider: str | None = None,
        is_tty: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._output = output
        self._operation = str(operation)
        self._provider = str(provider or "")
        self._is_tty = (
            bool(getattr(sys.stdout, "isatty", lambda: False)())
            if is_tty is None
            else bool(is_tty)
        )
        self._clock = clock
        self._started_at = self._clock()
        self._last_rendered_at = float("-inf")
        self._last_phase = ""

    def __call__(self, event: Mapping[str, Any]) -> None:
        try:
            self._render(normalize_progress_event(event))
        except Exception:
            # A terminal/display problem must not turn a successful fetch into
            # a failed Source operation.
            return

    def _render(self, event: dict[str, Any]) -> None:
        now = self._clock()
        phase = str(event.get("phase") or event.get("event") or "processing")
        status = str(event.get("status") or "").lower()
        immediate = (
            phase != self._last_phase
            or status in _IMMEDIATE_STATUSES
            or event.get("event") == "heartbeat"
            or bool(event.get("retry"))
            or event.get("event") == "redmine.http_attempt"
        )
        minimum_interval = 0.25 if self._is_tty else 1.0
        if not immediate and now - self._last_rendered_at < minimum_interval:
            return

        label = str(
            event.get("label_ja")
            or {
                "fetch": "取得",
                "reflect": "検索反映",
                "metadata": "設定反映",
                "heartbeat": "処理継続中",
            }.get(phase, phase)
        )
        provider_value = str(event.get("provider") or self._provider)
        provider = _PROVIDER_LABELS.get(provider_value, provider_value)
        prefixes = [self._operation]
        if provider:
            prefixes.append(provider)
        prefixes.append(label)
        prefix = "".join(f"[{value}]" for value in prefixes)

        completed = _optional_non_negative_int(
            event.get(
                "completed",
                event.get("current", event.get("documents")),
            )
        )
        total = _optional_non_negative_int(event.get("total"))
        unit = str(event.get("unit") or "件")
        total_kind = str(event.get("total_kind") or "")
        elapsed = _optional_non_negative_number(event.get("elapsed_seconds"))
        if elapsed is None:
            elapsed = max(0.0, now - self._started_at)
        current_item = str(event.get("current_item") or "").strip()

        provider_percentage = _optional_non_negative_int(
            event.get("provider_percentage")
        )
        if provider_percentage is not None:
            detail = f"{min(100, provider_percentage)}%（Provider内部進捗）"
        elif total == 0 and total_kind == "exact":
            detail = "対象なし"
        elif (
            completed is not None
            and total is not None
            and total > 0
            and total_kind == "exact"
        ):
            percentage = min(100, int(completed * 100 / total))
            checkpoint_complete = bool(event.get("checkpoint_saved")) or status in {
                "completed",
                "success",
            }
            if percentage == 100 and not checkpoint_complete:
                percentage = 99
            detail = f"{percentage}%（{completed}/{total}{unit}）"
        elif completed is not None:
            detail = f"{completed}{unit}処理済み"
        else:
            detail = "処理中"
        if current_item:
            detail += f" {current_item}"
        message_detail = str(event.get("message") or "").strip()
        if message_detail and event.get("event") == "subprocess.log":
            detail += f" {message_detail}"
        if elapsed is not None:
            detail += f" 経過{int(elapsed)}秒"
        if status == "retry" or event.get("retry"):
            detail += "（再試行）"

        message = f"{prefix} {detail}"
        if event.get("event") == "redmine.http_attempt":
            message = _render_http_attempt(prefix, event)
        if self._is_tty and self._output is print:
            final_line = (
                status in _IMMEDIATE_STATUSES
                or event.get("event") == "redmine.http_attempt"
            )
            print(
                "\r" + message + "\033[K",
                end="\n" if final_line else "",
                flush=True,
            )
        else:
            if self._is_tty:
                message = "\r" + message
            self._output(message)
        self._last_rendered_at = now
        self._last_phase = phase


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _optional_non_negative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _render_http_attempt(prefix: str, event: Mapping[str, Any]) -> str:
    parts = [
        f"{event.get('method') or 'GET'} {event.get('url') or 'URL不明'}",
        (
            f"試行 {event.get('attempt') or '?'}"
            f"/{event.get('max_attempts') or '?'}"
        ),
        f"timeout={event.get('timeout_seconds')}秒",
    ]
    if event.get("status") is not None:
        parts.append(
            f"HTTP {event.get('status')} {event.get('reason') or ''}".rstrip()
        )
    elif event.get("error_kind"):
        parts.append(
            f"{event.get('error_kind')}: {event.get('reason') or ''}".rstrip()
        )
    parts.append(
        "再試行="
        + ("あり" if event.get("retry") else "なし")
    )
    if event.get("retry_after") is not None:
        parts.append(f"Retry-After={event.get('retry_after')}")
    if event.get("wait_seconds"):
        parts.append(f"待機={event.get('wait_seconds')}秒")
    if event.get("content_type"):
        parts.append(f"Content-Type={event.get('content_type')}")
    if event.get("body_bytes") is not None:
        parts.append(f"response={event.get('body_bytes')} bytes")
    rendered = f"{prefix} " + " / ".join(parts)
    for field, label in (
        ("request_headers", "request headers"),
        ("response_headers", "response headers"),
    ):
        headers = event.get(field)
        if isinstance(headers, Mapping) and headers:
            rendered += (
                f"\n{label}（秘密情報を伏せて表示）: "
                + json.dumps(
                    dict(headers),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    if event.get("body_preview"):
        rendered += "\nresponse body（秘密情報を伏せて表示）:\n"
        rendered += str(event["body_preview"])
        if event.get("body_truncated"):
            rendered += "\n...（64KiB以降を省略）"
    return rendered
