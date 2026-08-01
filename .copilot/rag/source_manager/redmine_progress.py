"""Deterministic human progress cadence for Redmine Source updates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


_PROGRESS_MARK_INTERVAL = 5


class RedmineProgressCadence:
    """Render the quiet 5/10/50 cadence without changing progress events."""

    def __init__(
        self,
        output: Callable[[str], None],
        *,
        provider: str | None,
    ) -> None:
        self._output = output
        self._provider = str(provider or "")
        self._fragment_open = False
        self._batch_started_at: float | None = None

    def render(self, event: Mapping[str, Any], *, now: float) -> bool:
        provider = str(event.get("provider") or self._provider).casefold()
        if provider != "redmine":
            return False
        event_kind = str(event.get("event") or "")
        phase = str(event.get("phase") or "")
        status = str(event.get("status") or "").casefold()

        if event_kind == "redmine.http_attempt" and not bool(
            event.get("retry")
        ):
            status_code = _non_negative_int(event.get("status"))
            if (
                status_code is not None
                and 200 <= status_code < 300
            ):
                return True


        if event_kind == "redmine.item" and status == "started":
            current = _non_negative_int(event.get("current_index"))
            total = _non_negative_int(event.get("total"))
            target = str(event.get("current_item") or "").strip()
            if current is None or not target:
                return True
            denominator = f"/{total}" if total is not None else ""
            self._write_line(
                f"[処理開始 {current}{denominator}] {target}"
            )
            return True

        if phase == "redmine.detail" and status in {"running", "replayed"}:
            completed = _non_negative_int(event.get("completed"))
            if (
                status == "running"
                and completed is not None
                and completed > 0
                and completed % _PROGRESS_MARK_INTERVAL == 0
                and bool(event.get("checkpoint_saved"))
            ):
                self._write_fragment(".")
            return True

        if event_kind == "redmine.add_batch":
            completed = _non_negative_int(event.get("completed"))
            current = _non_negative_int(event.get("current_index"))
            total = _non_negative_int(event.get("total"))
            if status == "started" and completed is not None and current is not None:
                batch_count = max(0, current - completed)
                denominator = f"/{total}" if total is not None else ""
                self._batch_started_at = now
                self._write_line(
                    f"[DB反映開始 {completed + 1}-{current}{denominator}] "
                    f"{batch_count}件を検索DBへ反映"
                )
                return True
            if status == "success" and completed is not None:
                batch_count = _non_negative_int(event.get("documents")) or 0
                denominator = f"/{total}" if total is not None else ""
                percentage = _percentage(completed, total)
                elapsed = (
                    max(0.0, now - self._batch_started_at)
                    if self._batch_started_at is not None
                    else None
                )
                suffix = (
                    f"  {_format_duration(elapsed)}"
                    if elapsed is not None
                    else ""
                )
                self._write_line(
                    f"[{completed}{denominator}{percentage}] "
                    f"取得{batch_count}・MD生成{batch_count}・"
                    f"ADD成功（対象{batch_count}）・state保存済み{suffix}"
                )
                self._batch_started_at = None
                return True
            return True

        # The batch-specific events above replace the older generic reflect
        # lines in normal output. The callback stream itself remains intact.
        return phase == "redmine.reflect"

    def prefix_generic(self, message: str) -> str:
        if not self._fragment_open:
            return message
        self._fragment_open = False
        return " " + message

    def _write_fragment(self, value: str) -> None:
        if self._output is print:
            print(value, end="", flush=True)
        else:
            self._output(value)
        self._fragment_open = True

    def _write_line(self, value: str) -> None:
        prefix = " " if self._fragment_open else ""
        self._fragment_open = False
        if self._output is print:
            print(prefix + value, flush=True)
        else:
            self._output(prefix + value)


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _percentage(completed: int, total: int | None) -> str:
    if total is None or total <= 0:
        return ""
    value = min(100.0, completed * 100.0 / total)
    rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    return f" {rendered}%"


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, remaining = divmod(total, 60)
    if not minutes:
        return f"{remaining}秒"
    return f"{minutes}分{remaining:02d}秒"
