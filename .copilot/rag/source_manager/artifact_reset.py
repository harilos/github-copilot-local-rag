from __future__ import annotations
import os
import shutil
import stat
import sys
from pathlib import Path
from .errors import SourceManagerError
_ANCHORS = ("catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm")
_FILES = ("logs/index_state.json", "logs/progress.json", "logs/prepare_errors.json")
def reset_derived_artifacts(db_root: Path, *, daemon_status: str) -> dict[str, object]:
    root = Path(db_root)
    if str(daemon_status or "").strip() not in {"stopped", "not_running"}:
        raise SourceManagerError("search daemon stop was not confirmed", stage="artifact_reset.daemon_stop")
    tool_root = Path(__file__).resolve().parents[1] / "gen_db" / "software_rag_tool"
    if str(tool_root) not in sys.path:
        sys.path.insert(0, str(tool_root))
    from software_rag_tool.writer_runtime import (
        bind_database_runtime,
        database_writer_lock,
    )
    with bind_database_runtime(root.parent, root.name) as target:
        with database_writer_lock(target):
            config = target.db_root / "db.json"
            _safe(target.db_root, config)
            if not config.is_file():
                raise SourceManagerError("database root is unsafe")
            return _reset_derived_artifacts(root)


def _reset_derived_artifacts(root: Path) -> dict[str, object]:
    anchors = [root / name for name in _ANCHORS]
    targets = [root / "data" / "clean", root / "index"]
    targets += [root.joinpath(*name.split("/")) for name in _FILES]
    sources = root / "sources"
    _safe(root, sources)
    if sources.exists():
        for path in sources.iterdir():
            _safe(root, path)
            config = path / "source.json"
            _safe(root, config)
            if path.is_dir() and config.is_file():
                targets.append(path / "state.json")
    for path in anchors:
        _safe(root, path)
    for path in targets:
        _safe_tree(root, path)
    removed: list[str] = []
    for path in anchors:
        _remove(root, path, removed, "artifact_reset.anchor")
    if any(path.exists() for path in anchors):
        raise SourceManagerError("catalog readiness anchor could not be removed", stage="artifact_reset.anchor")
    for path in targets:
        _remove(root, path, removed, "artifact_reset.remove")
    return {"removed": removed}

def _safe(root: Path, path: Path) -> None:
    path.relative_to(root)
    current = path
    while current != root:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            current = current.parent
            continue
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)
            or (hasattr(current, "is_junction") and current.is_junction())
        ):
            raise SourceManagerError("artifact reset target contains a link")
        current = current.parent


def _safe_tree(root: Path, path: Path) -> None:
    _safe(root, path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise SourceManagerError("artifact reset target is unreadable") from exc
    for entry in entries:
        _safe_tree(root, Path(entry.path))


def _remove(root: Path, path: Path, removed: list[str], stage: str) -> None:
    try:
        if not path.exists():
            return
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    except OSError as exc:
        raise SourceManagerError(f"artifact removal failed: {path.relative_to(root)}", stage=stage) from exc
    removed.append(path.relative_to(root).as_posix())
