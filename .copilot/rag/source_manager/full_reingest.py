from __future__ import annotations
import json
import os
import shutil
import stat
import uuid
import ctypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from .errors import SourceManagerError
from .store import SourceStore
MARKER_NAME = "full-reingest-required.json"
MARKER_SCHEMA = "local-rag.full-reingest-required.v1"
def full_reingest_required(db_root: Path) -> bool:
    return (Path(db_root) / MARKER_NAME).is_file()
def request_full_reingest(db_root: Path) -> dict[str, Any]:
    """Invalidate generated search state without fetching or rebuilding."""
    store = SourceStore(Path(db_root))
    root = store.db_root
    source_keys = _validate_preserved_content(store)
    _reject_active_operation(store, source_keys)
    marker = root / MARKER_NAME
    if not marker.exists():
        _write_marker(marker, {"schema_version": MARKER_SCHEMA, "requested_at": _now(),
                               "source_keys": source_keys, "status": "invalidating", "deleted": []})
    else:
        _read_marker(marker)
    deleted: list[str] = []
    targets = [
        root / "data" / "clean", root / "index", root / "catalog.sqlite",
        root / "catalog.sqlite-wal", root / "catalog.sqlite-shm",
        root / "logs" / "index_state.json", root / "logs" / "progress.json",
        *(store.paths(key).absolute(root, store.paths(key).state_json) for key in source_keys),
    ]
    try:
        for target in targets:
            if _remove_generated_target(root, target):
                deleted.append(target.relative_to(root).as_posix())
    except Exception as exc:
        _write_marker(marker, {
            "schema_version": MARKER_SCHEMA,
            "requested_at": _read_marker(marker).get("requested_at") or _now(),
            "source_keys": source_keys, "status": "invalidation_failed",
            "deleted": deleted, "failure": type(exc).__name__,
        })
        raise SourceManagerError("full reingest invalidation was incomplete",
                                 stage="full_reingest.invalidate") from exc
    result = {
        "schema_version": MARKER_SCHEMA,
        "requested_at": _read_marker(marker).get("requested_at") or _now(),
        "source_keys": source_keys, "status": "required", "deleted": deleted,
    }
    _write_marker(marker, result)
    return result
def finish_full_reingest(db_root: Path, result: Mapping[str, Any], *, artifacts_complete: bool) -> bool:
    marker = Path(db_root) / MARKER_NAME
    if not marker.is_file() or not artifacts_complete:
        return False
    expected = sorted(str(value) for value in _read_marker(marker).get("source_keys") or [])
    completed = sorted(str(row.get("local_source_key") or "")
                       for row in list(result.get("results") or [])
                       if row.get("status") in {"updated", "complete", "success"})
    if not expected or completed != expected:
        return False
    marker.unlink()
    return True
def _validate_preserved_content(store: SourceStore) -> list[str]:
    root = store.db_root
    for required in (root / "db.json", root / "DB_PROFILE.md"):
        if required.is_symlink() or not required.is_file():
            raise SourceManagerError("database identity is unreadable")
        required.read_bytes()
    sources = root / "sources"
    if sources.exists() and (sources.is_symlink() or not sources.is_dir()):
        raise SourceManagerError("Source root is unsafe")
    keys = store.list_keys()
    entries = list(sources.iterdir()) if sources.is_dir() else []
    if len(keys) != len(entries):
        raise SourceManagerError("Source configuration is incomplete")
    for key in keys:
        store.read_source(key)
        paths = store.paths(key)
        for relative in (paths.events_jsonl, paths.work_directory):
            path = paths.absolute(root, relative)
            if path.exists():
                _validate_preserved_tree(path)
    for name in ("source-links.json", "source-links.json.bak"):
        path = root / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise SourceManagerError("Source links are unsafe")
            json.loads(path.read_text(encoding="utf-8"))
    return keys
def _validate_preserved_tree(path: Path) -> None:
    candidates = [path, *path.rglob("*")] if path.is_dir() else [path]
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for candidate in candidates:
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse):
            raise SourceManagerError("preserved Source content is unsafe")
def _reject_active_operation(store: SourceStore, source_keys: list[str]) -> None:
    progress = _read_json(store.db_root / "logs" / "progress.json")
    if str(progress.get("status") or "").casefold() == "running":
        pid = int(progress.get("operation_pid") or 0)
        if not pid or _process_exists(pid):
            raise SourceManagerError("database build is active")
    terminal = {"", "complete", "completed", "configured", "failed", "error",
                "interrupted", "metadata_sync_pending", "not_started", "ready"}
    for key in source_keys:
        state = store.read_state(key).payload
        status = str(state.get("status") or state.get("phase") or "").casefold()
        if status not in terminal:
            raise SourceManagerError("Source update is active")
def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong); kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)); kernel.GetExitCodeProcess.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,); kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
def _remove_generated_target(root: Path, target: Path) -> bool:
    target.relative_to(root)
    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse):
        raise SourceManagerError("generated target contains a link")
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(target)
    elif stat.S_ISREG(metadata.st_mode):
        target.unlink()
    else:
        raise SourceManagerError("generated target is unsafe")
    return True
def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}
def _read_marker(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("schema_version") != MARKER_SCHEMA:
        raise SourceManagerError("full reingest marker is invalid")
    return value
def _write_marker(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
