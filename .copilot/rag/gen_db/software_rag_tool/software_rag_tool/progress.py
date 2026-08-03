from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import logs_dir


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def progress_path() -> Path:
    return logs_dir() / "progress.json"


def events_path() -> Path:
    return logs_dir() / "events.jsonl"


def read_progress() -> dict[str, Any]:
    return _read_progress_file(progress_path())


def _read_progress_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def write_progress(**updates: Any) -> dict[str, Any]:
    """Legacy one-shot writer retained for non-ADD callers and rollback."""
    data = read_progress()
    data.update({key: value for key, value in updates.items() if value is not None})
    data["updated_at"] = utc_now()
    path = progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


class ProgressWriterInUseError(RuntimeError):
    """Raised instead of silently allowing two writers for one progress path."""


class ProgressWriter:
    """One ADD-run writer scoped to a run id and resolved progress path.

    The instance owns a non-blocking process lock for its full lifetime.  Its
    in-memory snapshot advances only after a durable temporary write and atomic
    replace both succeed.  No module-global progress cache is used.
    """

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        compact: bool = True,
    ) -> None:
        value = str(run_id).strip()
        if not value:
            raise ValueError("progress writer run_id is required")
        self.run_id = value
        self.path = Path(path).expanduser().resolve(strict=False)
        self.compact = bool(compact)
        self._closed = False
        self._write_count = 0
        self._lock_descriptor: int | None = None
        self._lock_path = self.path.parent / f".{self.path.name}.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._cached_data = _read_progress_file(self.path)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "ProgressWriter":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def write_count(self) -> int:
        return self._write_count

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._cached_data)

    def write(self, **updates: Any) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("progress writer is closed")
        base = (
            self._cached_data
            if self.compact
            else _read_progress_file(self.path)
        )
        data = copy.deepcopy(base)
        data.update(
            {key: value for key, value in updates.items() if value is not None}
        )
        data["updated_at"] = utc_now()
        encoded = self._serialize(data)
        temporary = self._write_temporary(encoded)
        try:
            self._replace(temporary)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        # This assignment is deliberately after atomic replace.  Failed
        # serialization, write/flush/close, or replace keeps the old snapshot.
        self._cached_data = data
        self._write_count += 1
        return copy.deepcopy(data)

    def close(self) -> None:
        if self._closed:
            return
        descriptor = self._lock_descriptor
        self._lock_descriptor = None
        self._closed = True
        if descriptor is None:
            return
        try:
            self._unlock(descriptor)
        finally:
            os.close(descriptor)

    def _serialize(self, data: dict[str, Any]) -> bytes:
        if self.compact:
            text = json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            text = json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        # Path.write_text() in the legacy writer uses platform newline
        # translation.  Preserve that byte contract while compacting only
        # insignificant JSON whitespace.
        return (text.replace("\n", os.linesep) + os.linesep).encode("utf-8")

    def _write_temporary(self, encoded: bytes) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.{os.getpid()}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return temporary

    def _replace(self, temporary: Path) -> None:
        os.replace(temporary, self.path)

    def _acquire_lock(self) -> None:
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise ProgressWriterInUseError(
                f"progress writer already active for {self.path}"
            ) from exc
        self._lock_descriptor = descriptor

    @staticmethod
    def _unlock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)


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
