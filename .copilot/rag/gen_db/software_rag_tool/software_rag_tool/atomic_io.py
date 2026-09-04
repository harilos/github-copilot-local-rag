from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar


_WINDOWS_REPLACE_RETRY_SECONDS = 2.0
_WINDOWS_REPLACE_INITIAL_DELAY_SECONDS = 0.01
_WINDOWS_REPLACE_MAX_DELAY_SECONDS = 0.1
_WINDOWS_REPLACE_RETRY_ERRORS = frozenset({5, 32, 33})
_T = TypeVar("_T")


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = True,
) -> None:
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=indent,
        sort_keys=sort_keys,
    ) + "\n"
    atomic_write_text(path, text)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.{os.getpid()}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, target)
        _fsync_directory(target.parent)
    finally:
        try:
            temporary.unlink()
        except OSError:
            # Best-effort cleanup must not hide a failed write/replacement.
            pass


def _replace_with_retry(source: Path, target: Path) -> None:
    retry_windows_sharing(lambda: os.replace(source, target))


def retry_windows_sharing(operation: Callable[[], _T]) -> _T:
    """Retry a single safe operation, never a possibly partially completed write."""
    if os.name != "nt":
        return operation()

    deadline = time.monotonic() + _WINDOWS_REPLACE_RETRY_SECONDS
    delay = _WINDOWS_REPLACE_INITIAL_DELAY_SECONDS
    while True:
        try:
            return operation()
        except OSError as exc:
            # Windows readers (and scanners) can briefly deny delete sharing.
            # Retrying replace preserves the old-or-new publication contract;
            # an append caller may retry opening only, never the append itself.
            if getattr(exc, "winerror", None) not in _WINDOWS_REPLACE_RETRY_ERRORS:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _WINDOWS_REPLACE_MAX_DELAY_SECONDS)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
