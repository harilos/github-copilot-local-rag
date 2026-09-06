from __future__ import annotations

import errno
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from .errors import SourceManagerError


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_HELD_LOCKS: ContextVar[frozenset[str]] = ContextVar(
    "local_rag_source_operation_locks", default=frozenset()
)


@contextmanager
def database_operation_lock(db_root: Path) -> Iterator[Path]:
    """Serialize reset, Source fetch, rebuild, and configuration mutation."""
    root = Path(db_root).resolve(strict=True)
    lock_dir = root.parent / ".operation-locks"
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / f"{root.name}.lock"
    key = os.path.normcase(str(lock_path.resolve(strict=False)))
    held = _HELD_LOCKS.get()
    if key in held:
        yield lock_path
        return

    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise SourceManagerError(
            "database management operation is busy", stage="operation_lock.busy"
        )
    descriptor: int | None = None
    token = None
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.path.getsize(lock_path) == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                exc, "winerror", None
            ) in {32, 33}:
                raise SourceManagerError(
                    "database management operation is busy",
                    stage="operation_lock.busy",
                ) from exc
            raise
        token = _HELD_LOCKS.set(held | {key})
        yield lock_path
    finally:
        if token is not None:
            _HELD_LOCKS.reset(token)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        thread_lock.release()
