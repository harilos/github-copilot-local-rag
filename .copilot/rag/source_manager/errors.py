from __future__ import annotations

import re
from typing import Any


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_CREDENTIAL = re.compile(
    r"(?i)\b(https?://)([^/@\s]+)@"
)
_NAMED_SECRET = re.compile(
    r"(?i)([\"']?\b(?:"
    r"authorization|proxy-authorization|x-redmine-api-key|"
    r"api[_-]?key|access[_-]?token|oauth[_-]?token|token|"
    r"credential|private[_-]?key|password|passwd|secret|"
    r"cookie|set-cookie"
    r")\b[\"']?\s*[:=]\s*)([^\r\n,;}&]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:"
    r"api[_-]?key|access[_-]?token|token|password|passwd|secret|"
    r"authorization|cookie|sig|signature"
    r")=)([^&#\s]*)"
)


def sanitize_diagnostic(value: Any, *, max_chars: int = 8_000) -> str:
    """Return useful diagnostics without echoing common credential forms."""
    text = str(value or "").replace("\x00", "")
    text = _ANSI_ESCAPE.sub("", text)
    text = _URL_CREDENTIAL.sub(r"\1<REDACTED>@", text)
    text = _NAMED_SECRET.sub(r"\1<REDACTED>", text)
    text = _QUERY_SECRET.sub(r"\1<REDACTED>", text)
    text = text.strip()
    if len(text) > max_chars:
        marker_reserve = 120
        available = max(2, max_chars - marker_reserve)
        head = available // 2
        tail = available - head
        omitted = max(0, len(text) - head - tail)
        return (
            text[:head].rstrip()
            + f"\n...（診断ログを{omitted:,}文字省略）...\n"
            + text[-tail:].lstrip()
        )
    return text


def exception_summary(exc: BaseException) -> str:
    detail = sanitize_diagnostic(str(exc), max_chars=4_000)
    return (
        f"{type(exc).__name__}: {detail}"
        if detail
        else type(exc).__name__
    )


class SourceManagerError(ValueError):
    """A safe, user-presentable Source Manager contract failure."""

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
    ) -> None:
        super().__init__(sanitize_diagnostic(message))
        self.stage = str(stage or "").strip() or None
