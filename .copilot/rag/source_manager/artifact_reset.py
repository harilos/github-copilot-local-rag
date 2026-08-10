from __future__ import annotations
import shutil
from pathlib import Path
from .errors import SourceManagerError
_ANCHORS = ("catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm")
_FILES = ("logs/index_state.json", "logs/progress.json", "logs/prepare_errors.json")
def reset_derived_artifacts(db_root: Path, *, daemon_status: str) -> dict[str, object]:
    root = Path(db_root)
    if root.is_symlink() or not root.is_dir() or not (root / "db.json").is_file():
        raise SourceManagerError("database root is unsafe")
    if str(daemon_status or "").strip() not in {"stopped", "not_running"}:
        raise SourceManagerError("search daemon stop was not confirmed", stage="artifact_reset.daemon_stop")
    anchors = [root / name for name in _ANCHORS]
    targets = [root / "data" / "clean", root / "index"]
    targets += [root.joinpath(*name.split("/")) for name in _FILES]
    sources = root / "sources"
    if sources.exists():
        _safe(root, sources)
        targets += [path / "state.json" for path in sources.iterdir() if path.is_dir() and (path / "source.json").is_file()]
    for path in anchors + targets:
        _safe(root, path)
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
        if current.is_symlink():
            raise SourceManagerError("artifact reset target contains a link")
        current = current.parent


def _remove(root: Path, path: Path, removed: list[str], stage: str) -> None:
    try:
        if not path.exists():
            return
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    except OSError as exc:
        raise SourceManagerError(f"artifact removal failed: {path.relative_to(root)}", stage=stage) from exc
    removed.append(path.relative_to(root).as_posix())
