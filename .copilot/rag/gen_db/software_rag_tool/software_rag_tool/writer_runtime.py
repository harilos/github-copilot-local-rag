from __future__ import annotations

import errno
import json
import os
import stat
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from .dbs import collection_name_for_db, require_db_name


DB_BUSY_EXIT_CODE = 75
_ENV_KEYS = (
    "RAG_DBS_ROOT", "RAG_DB_NAME", "RAG_OUTPUT_ROOT",
    "LOCALRAG_OUTPUT_ROOT", "CHROMA_DIR_V2", "CHROMA_COLLECTION",
)
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class DatabaseWriteError(RuntimeError):
    code = "DB_WRITE_UNSAFE"
    retryable = False


class DatabaseBusyError(DatabaseWriteError):
    code = "DB_BUSY"
    retryable = True


@dataclass(frozen=True)
class DatabaseWriteTarget:
    dbs_root: Path
    db_name: str
    db_root: Path
    chroma_dir: Path
    collection: str = ""


_ACTIVE_TARGET: ContextVar[DatabaseWriteTarget | None] = ContextVar(
    "local_rag_database_write_target", default=None
)


def resolve_database_write_target(
    dbs_root: Path, db_name: str
) -> DatabaseWriteTarget:
    name = require_db_name(db_name)
    try:
        root = Path(dbs_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise DatabaseWriteError("database root is unavailable") from exc
    _real_directory(root, "database root")
    db_root = root / name
    _real_directory(db_root, "database directory")
    db_root = db_root.resolve(strict=True)
    if (
        db_root.parent != root
        or os.path.normcase(db_root.name) != os.path.normcase(name)
    ):
        raise DatabaseWriteError("database directory escaped its root")
    name = db_root.name
    return DatabaseWriteTarget(
        root, name, db_root, db_root / "index" / "chroma"
    )


@contextmanager
def bind_database_runtime(
    dbs_root: Path, db_name: str
) -> Iterator[DatabaseWriteTarget]:
    target = resolve_database_write_target(dbs_root, db_name)
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ.update({
        "RAG_DBS_ROOT": str(target.dbs_root),
        "RAG_DB_NAME": target.db_name,
        "RAG_OUTPUT_ROOT": str(target.db_root),
        "CHROMA_DIR_V2": str(target.chroma_dir),
    })
    os.environ.pop("LOCALRAG_OUTPUT_ROOT", None)
    os.environ.pop("CHROMA_COLLECTION", None)
    try:
        yield target
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def database_writer_lock(target: DatabaseWriteTarget) -> Iterator[Path]:
    lock_dir = target.dbs_root / ".writer-locks"
    _internal_path(lock_dir, target.dbs_root, "directory", False)
    try:
        if os.name == "nt":
            lock_dir.mkdir(exist_ok=True)
        else:
            lock_dir.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise DatabaseWriteError("writer lock directory is unavailable") from exc
    _real_directory(lock_dir, "writer lock directory")
    lock_path = lock_dir / f"{target.db_name}.lock"
    key = os.path.normcase(str(lock_path.resolve(strict=False)))
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise DatabaseBusyError(f"database writer is busy: {target.db_name}")
    descriptor: int | None = None
    try:
        descriptor = _kernel_lock(lock_path, target.db_name)
        yield lock_path
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        thread_lock.release()


@contextmanager
def database_writer_session(
    dbs_root: Path, db_name: str
) -> Iterator[DatabaseWriteTarget]:
    with bind_database_runtime(dbs_root, db_name) as target:
        with database_writer_lock(target):
            _validate_database_storage(target)
            target = _collection_target(target)
            os.environ["CHROMA_COLLECTION"] = target.collection
            _ensure_layout(target)
            token = _ACTIVE_TARGET.set(target)
            try:
                yield target
            finally:
                _ACTIVE_TARGET.reset(token)


def active_database_write_target() -> DatabaseWriteTarget | None:
    return _ACTIVE_TARGET.get()


def busy_error_payload(
    error: DatabaseBusyError, *, operation: str, db_name: str
) -> dict[str, Any]:
    return {
        "status": "error", "operation": operation, "db": str(db_name),
        "code": error.code, "retryable": error.retryable,
        "error": str(error),
    }


def _collection_target(target: DatabaseWriteTarget) -> DatabaseWriteTarget:
    metadata = (
        ("db.json", _metadata(target.db_root / "db.json")),
        ("VERSION.json", _metadata(target.db_root / "VERSION.json")),
        ("manifest.json", _metadata(target.db_root / "index/manifest.json")),
    )
    collections: list[tuple[str, str]] = []
    for source, payload in metadata:
        identity = payload.get("db_name")
        if identity is not None and str(identity) != target.db_name:
            raise DatabaseWriteError(f"database identity mismatch in {source}")
        if "collection" not in payload or payload.get("collection") is None:
            continue
        value = payload.get("collection")
        if not isinstance(value, str) or not value.strip():
            raise DatabaseWriteError(f"invalid collection in {source}")
        collections.append((source, value.strip()))
    if len({value for _source, value in collections}) > 1:
        raise DatabaseWriteError(
            "database collection mismatch: "
            + json.dumps(dict(collections), ensure_ascii=False, sort_keys=True)
        )
    collection = (
        collections[0][1]
        if collections
        else collection_name_for_db(target.db_name)
    )
    return replace(target, collection=collection)


def _metadata(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise DatabaseWriteError(f"unreadable metadata: {path.name}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatabaseWriteError(f"invalid metadata: {path.name}") from exc
    if not isinstance(value, dict):
        raise DatabaseWriteError(f"metadata must be an object: {path.name}")
    return value


def _ensure_layout(target: DatabaseWriteTarget) -> None:
    for relative in ("data/raw", "data/clean", "index", "logs"):
        path = target.db_root / relative
        _internal_path(path, target.db_root, "directory", False)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DatabaseWriteError("database layout is unavailable") from exc
        _internal_path(path, target.db_root, "directory", True)


def _validate_database_storage(target: DatabaseWriteTarget) -> None:
    directories = (
        "data", "data/raw", "data/clean", "index", "index/chroma", "logs",
    )
    files = (
        "catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm",
        "catalog.sqlite-journal",
        "logs/index_state.json", "logs/progress.json", "logs/events.jsonl",
        "logs/prepare_errors.json", "index/manifest.json", "db.json",
        "VERSION.json", "DB_PROFILE.md", "source-links.json",
        "source-links.json.bak", "rag-wrapper.json",
    )
    for relative in directories:
        _internal_path(target.db_root / relative, target.db_root, "directory", False)
    for relative in files:
        _internal_path(target.db_root / relative, target.db_root, "file", False)
    for relative in ("data/clean", "index/chroma", "logs"):
        path = target.db_root / relative
        if path.exists():
            _validate_real_tree(path, target.db_root)


def _validate_real_tree(root: Path, boundary: Path) -> None:
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise DatabaseWriteError("database storage is unavailable") from exc
    for entry in entries:
        path = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise DatabaseWriteError("database storage is unavailable") from exc
        if _linked(info, path):
            relative = path.relative_to(boundary).as_posix()
            raise DatabaseWriteError(f"linked storage: {relative}")
        if stat.S_ISDIR(info.st_mode):
            _validate_real_tree(path, boundary)
        elif not stat.S_ISREG(info.st_mode):
            relative = path.relative_to(boundary).as_posix()
            raise DatabaseWriteError(f"invalid file: {relative}")


def _kernel_lock(path: Path, db_name: str) -> int:
    if path.exists() or path.is_symlink():
        _internal_path(path, path.parent, "file", True)
    flags = (
        os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.set_inheritable(descriptor, False)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DatabaseWriteError("writer lock is not a regular file")
        if info.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if not _try_lock(descriptor):
            raise DatabaseBusyError(f"database writer is busy: {db_name}")
        return descriptor
    except Exception:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _try_lock(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    import fcntl
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def _real_directory(path: Path, label: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise DatabaseWriteError(f"{label} is unavailable") from exc
    if _linked(info, path) or not stat.S_ISDIR(info.st_mode):
        raise DatabaseWriteError(f"{label} must be a real directory")


def _internal_path(
    path: Path, boundary: Path, expected: str, required: bool
) -> None:
    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise DatabaseWriteError("database storage escaped its root") from exc
    current = boundary
    final: os.stat_result | None = None
    for part in relative.parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if required:
                raise DatabaseWriteError(f"missing storage: {relative.as_posix()}")
            return
        except OSError as exc:
            raise DatabaseWriteError("database storage is unavailable") from exc
        if _linked(info, current):
            raise DatabaseWriteError(f"linked storage: {relative.as_posix()}")
        final = info
    if final is None:
        return
    valid = (
        stat.S_ISDIR(final.st_mode)
        if expected == "directory"
        else stat.S_ISREG(final.st_mode)
    )
    if not valid:
        raise DatabaseWriteError(f"invalid {expected}: {relative.as_posix()}")


def _linked(info: os.stat_result, path: Path) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )
