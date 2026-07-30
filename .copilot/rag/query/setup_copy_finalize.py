from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


PORTABLE_DB_PATH = "__local_rag_db_relative_path__"
PORTABLE_EXTERNAL_SOURCE = "__local_rag_sharepoint_source_key__"
PORTABLE_EXTERNAL_SUFFIX = "source_relative_suffix"
SHAREPOINT_ROOT_ENV = "LOCAL_RAG_SHAREPOINT_ROOT"
_MAX_STATE_BYTES = 64 * 1024 * 1024


def finalize_copied_install(rag_root: str | Path) -> dict[str, Any]:
    """Finalize DB-local paths after the .copilot directory was copied.

    Copying the package is the installation operation.  This setup-side pass is
    idempotent and only rewrites portable path markers in administrative state.
    Search-only distributions normally have no such markers.
    """

    root = Path(rag_root).expanduser().resolve(strict=False)
    dbs = root / "dbs"
    result = {
        "status": "ok",
        "databases": 0,
        "files_rewritten": 0,
        "markers_restored": 0,
        "external_markers_pending": 0,
    }
    if not dbs.is_dir() or dbs.is_symlink():
        return result
    for db_root in sorted(dbs.iterdir()):
        if db_root.is_symlink() or not db_root.is_dir():
            continue
        value = finalize_database(
            db_root,
            portable_root=db_root,
            rag_root=root,
        )
        result["databases"] += 1
        for key in (
            "files_rewritten",
            "markers_restored",
            "external_markers_pending",
        ):
            result[key] += int(value.get(key) or 0)
    return result


def finalize_database(
    db_root: str | Path,
    *,
    portable_root: str | Path | None = None,
    rag_root: str | Path | None = None,
) -> dict[str, int]:
    root = Path(db_root).expanduser().resolve(strict=False)
    logical_root = Path(portable_root or root).expanduser().resolve(strict=False)
    installation_root = Path(rag_root or root.parents[1]).expanduser().resolve(
        strict=False
    )
    result = {
        "files_rewritten": 0,
        "markers_restored": 0,
        "external_markers_pending": 0,
    }
    logs = root / "logs"
    if not logs.is_dir() or logs.is_symlink():
        return result
    for path in sorted(logs.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _MAX_STATE_BYTES or path.suffix not in {".json", ".jsonl"}:
            continue
        if path.suffix == ".json":
            payload = _read_json(path)
            restored, changed, restored_count, pending_count = _restore_value(
                payload,
                db_root=root,
                portable_root=logical_root,
                rag_root=installation_root,
            )
            if changed:
                _atomic_json(path, restored)
                result["files_rewritten"] += 1
        else:
            values = _read_jsonl(path)
            output: list[Any] = []
            changed = False
            restored_count = pending_count = 0
            for value in values:
                restored, item_changed, item_restored, item_pending = _restore_value(
                    value,
                    db_root=root,
                    portable_root=logical_root,
                    rag_root=installation_root,
                )
                output.append(restored)
                changed = changed or item_changed
                restored_count += item_restored
                pending_count += item_pending
            if changed:
                _atomic_jsonl(path, output)
                result["files_rewritten"] += 1
        result["markers_restored"] += restored_count
        result["external_markers_pending"] += pending_count
    return result


def _restore_value(
    value: Any,
    *,
    db_root: Path,
    portable_root: Path,
    rag_root: Path,
) -> tuple[Any, bool, int, int]:
    if isinstance(value, list):
        output: list[Any] = []
        changed = False
        restored_total = pending_total = 0
        for item in value:
            restored, item_changed, count, pending = _restore_value(
                item,
                db_root=db_root,
                portable_root=portable_root,
                rag_root=rag_root,
            )
            output.append(restored)
            changed = changed or item_changed
            restored_total += count
            pending_total += pending
        return output, changed, restored_total, pending_total
    if not isinstance(value, dict):
        return value, False, 0, 0
    if set(value) == {PORTABLE_DB_PATH}:
        relative = _safe_relative(value[PORTABLE_DB_PATH])
        candidate = portable_root.joinpath(*relative.parts)
        _require_inside(candidate, portable_root)
        return str(candidate), True, 1, 0
    if set(value) == {PORTABLE_EXTERNAL_SOURCE, PORTABLE_EXTERNAL_SUFFIX}:
        candidate = _external_candidate(
            db_root,
            rag_root,
            local_source_key=str(value[PORTABLE_EXTERNAL_SOURCE]),
            suffix=str(value[PORTABLE_EXTERNAL_SUFFIX] or ""),
        )
        if candidate is None:
            return dict(value), False, 0, 1
        return str(candidate), True, 1, 0

    output: dict[str, Any] = {}
    changed = False
    restored_total = pending_total = 0
    for key, item in value.items():
        restored, item_changed, count, pending = _restore_value(
            item,
            db_root=db_root,
            portable_root=portable_root,
            rag_root=rag_root,
        )
        output[str(key)] = restored
        changed = changed or item_changed
        restored_total += count
        pending_total += pending
    return output, changed, restored_total, pending_total


def _external_candidate(
    db_root: Path,
    rag_root: Path,
    *,
    local_source_key: str,
    suffix: str,
) -> Path | None:
    if not local_source_key or any(
        character in local_source_key for character in "\\/\x00\r\n"
    ):
        raise ValueError("portable external Source key is invalid")
    source_path = db_root / "sources" / local_source_key / "source.json"
    source = _read_json(source_path)
    source_type = str(source.get("source_type") or "").strip().lower()
    if source_type not in {"sharepoint", "teams"}:
        raise ValueError("portable external Source type is invalid")
    fetch = source.get("fetch")
    if not isinstance(fetch, dict):
        raise ValueError("portable external Source fetch is invalid")
    root = _configured_sharepoint_root(rag_root, fetch)
    if root is None:
        return None
    relative = str(fetch.get("relative_path") or "").strip()
    candidate = root
    if relative:
        value = _safe_relative(relative)
        candidate = candidate.joinpath(*value.parts)
    if suffix:
        value = _safe_relative(suffix)
        candidate = candidate.joinpath(*value.parts)
    _require_inside(candidate, root)
    return candidate


def _configured_sharepoint_root(rag_root: Path, fetch: dict[str, Any]) -> Path | None:
    config_path = rag_root / "config" / "source-connections.json"
    try:
        config = _read_json(config_path)
    except FileNotFoundError:
        config = {}
    stored = str(config.get("sharepoint_root") or "").strip()
    if stored:
        root = Path(stored).expanduser()
        if root.is_absolute():
            return root.resolve(strict=False)
    environment_name = str(fetch.get("root_env") or SHAREPOINT_ROOT_ENV).strip()
    inherited = str(os.environ.get(environment_name) or "").strip()
    if inherited:
        root = Path(inherited).expanduser()
        if root.is_absolute():
            return root.resolve(strict=False)
    return None


def _safe_relative(value: Any) -> PurePosixPath:
    text = str(value or "")
    relative = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("portable path is invalid")
    return relative


def _require_inside(candidate: Path, boundary: Path) -> None:
    root = boundary.resolve(strict=False)
    value = candidate.resolve(strict=False)
    if value != root and root not in value.parents:
        raise ValueError("portable path escaped its root")


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


def _read_jsonl(path: Path) -> list[Any]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )


def _atomic_jsonl(path: Path, payload: list[Any]) -> None:
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for item in payload
    )
    _atomic_bytes(path, ((text + "\n") if text else "").encode("utf-8"))


def _atomic_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


__all__ = ["finalize_copied_install", "finalize_database"]
