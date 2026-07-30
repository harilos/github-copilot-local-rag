from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .errors import SourceManagerError
from .database_copy_storage import (
    copy_catalog_snapshot,
    copy_chroma_snapshot,
    delete_excluded_sources,
    validate_excluded_vectors,
)

_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_MAX_JSON_BYTES = 1024 * 1024


class DatabaseCopyError(SourceManagerError):
    pass


def copy_database(
    source_root: str | Path,
    destination_root: str | Path,
    *,
    destination_name: str,
    title: str,
    query_hint: str,
    excluded_sources: Iterable[Mapping[str, Any]] = (),
    rag_root: str | Path,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Copy a DB independently, preserving Source locations and removing exclusions."""

    source = required_real_directory(source_root, label="copy source DB")
    destination = Path(destination_root).expanduser()
    name = require_db_name(destination_name)
    if destination.name != name:
        raise DatabaseCopyError("copy destination does not match the new DB name")
    if destination.parent.resolve() != source.parent.resolve():
        raise DatabaseCopyError("copy destination must use the same DB root")
    if destination.exists() or destination.is_symlink():
        raise DatabaseCopyError("copy destination already exists")
    validate_tree_for_copy(source)

    excluded = [dict(item) for item in excluded_sources]
    temporary_parent = Path(
        tempfile.mkdtemp(prefix=f".copy-{name}-", dir=str(destination.parent))
    )
    staging = temporary_parent / name
    try:
        emit(progress_callback, "copy.files", "DBファイルのコピー", 0, None)
        shutil.copytree(
            source,
            staging,
            copy_function=shutil.copy2,
            ignore=live_database_ignore(source),
        )
        emit(progress_callback, "copy.files", "DBファイルのコピー", 1, 1)
        copy_catalog_snapshot(source, staging, error_type=DatabaseCopyError)

        old_collection = configured_collection(source, source.name)
        new_collection = collection_name_for_db(name)
        vector_count = copy_chroma_snapshot(
            source,
            staging,
            old_collection=old_collection,
            new_collection=new_collection,
            progress_callback=progress_callback,
            error_type=DatabaseCopyError,
        )
        rewrite_database_identity(
            staging,
            source_name=source.name,
            destination_name=name,
            destination_title=title,
            query_hint=query_hint,
            collection=new_collection,
        )
        rewrite_db_local_paths(
            staging,
            source_root=Path(source_root),
            destination_root=destination,
        )
        deletion_results = delete_excluded_sources(
            staging,
            excluded,
            destination_name=name,
            collection=new_collection,
            rag_root=Path(rag_root),
            progress_callback=progress_callback,
            error_type=DatabaseCopyError,
        )
        write_copy_marker(staging)
        validate_copied_database(
            staging,
            destination_name=name,
            collection=new_collection,
            excluded_sources=excluded,
        )
        os.replace(staging, destination)
        fsync_directory(destination.parent)
        return {
            "status": "copied",
            "source_db": source.name,
            "destination_db": name,
            "excluded_source_count": len(excluded),
            "excluded_sources": deletion_results,
            "copied_vector_records": vector_count,
            "destination": str(destination),
        }
    except Exception:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if temporary_parent.exists() and not temporary_parent.is_symlink():
            shutil.rmtree(temporary_parent, ignore_errors=True)


def live_database_ignore(source_root: Path):
    source = source_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory).resolve()
        excluded: set[str] = set()
        if current == source:
            excluded.update(
                name
                for name in names
                if name in {
                    "catalog.sqlite",
                    "catalog.sqlite-wal",
                    "catalog.sqlite-shm",
                }
            )
        if current == source / "index":
            excluded.add("chroma")
        return excluded

    return ignore


def rewrite_database_identity(
    root: Path,
    *,
    source_name: str,
    destination_name: str,
    destination_title: str,
    query_hint: str,
    collection: str,
) -> None:
    now = utc_now()
    config_path = root / "db.json"
    config = read_json_object(config_path, required=True)
    config.update(
        {
            "db_name": destination_name,
            "collection": collection,
            "title": str(destination_title).strip() or destination_name,
        }
    )
    atomic_json(config_path, config)

    version_path = root / "VERSION.json"
    if version_path.is_file():
        version = read_json_object(version_path, required=True)
        version.update(
            {
                "db_name": destination_name,
                "collection": collection,
                "created_at": now,
            }
        )
        version["db_hash"] = hashlib.sha256(
            json.dumps(
                {
                    "source_db": source_name,
                    "source_hash": version.get("db_hash"),
                    "destination_db": destination_name,
                    "created_at": now,
                    "collection": collection,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        atomic_json(version_path, version)

    manifest_path = root / "index" / "manifest.json"
    if manifest_path.is_file():
        manifest = read_json_object(manifest_path, required=True)
        manifest.update({"collection": collection, "generated_at": now})
        atomic_json(manifest_path, manifest)

    profile_path = root / str(config.get("profile") or "DB_PROFILE.md")
    if profile_path.parent.resolve() != root.resolve():
        raise DatabaseCopyError("copied DB profile path is unsafe")
    current = (
        profile_path.read_text(encoding="utf-8", errors="strict")
        if profile_path.is_file()
        else ""
    )
    atomic_text(
        profile_path,
        updated_profile_text(
            current,
            title=str(destination_title).strip() or destination_name,
            query_hint=str(query_hint).strip(),
        ),
    )


def rewrite_db_local_paths(
    root: Path,
    *,
    source_root: Path,
    destination_root: Path,
) -> None:
    source_texts = tuple(
        dict.fromkeys(
            (
                str(source_root.expanduser().absolute()),
                str(source_root.resolve()),
            )
        )
    )
    destination_text = str(destination_root.expanduser().absolute())
    for path in (
        root / "logs" / "index_state.json",
        root / "logs" / "progress.json",
    ):
        if path.is_file():
            payload = read_json_object(path, required=True)
            for source_text in source_texts:
                payload = replace_path_prefix(
                    payload,
                    source_text,
                    destination_text,
                )
            atomic_json(path, payload)


def replace_path_prefix(value: Any, source: str, destination: str) -> Any:
    if isinstance(value, dict):
        return {
            key: replace_path_prefix(item, source, destination)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace_path_prefix(item, source, destination) for item in value]
    if isinstance(value, str) and (value == source or value.startswith(source + os.sep)):
        return destination + value[len(source) :]
    return value


def write_copy_marker(root: Path) -> None:
    atomic_json(
        root / "rag-wrapper.json",
        {
            "schema_version": "local-rag.wrapper.v1",
            "content_snapshot_at": utc_now().replace("+00:00", "Z"),
            "reason": "database_copy",
        },
    )


def validate_copied_database(
    root: Path,
    *,
    destination_name: str,
    collection: str,
    excluded_sources: list[dict[str, Any]],
) -> None:
    config = read_json_object(root / "db.json", required=True)
    if config.get("db_name") != destination_name or config.get("collection") != collection:
        raise DatabaseCopyError("copied DB identity is invalid")
    excluded_ids = {
        str(source.get("source_id") or "").strip()
        for source in excluded_sources
        if str(source.get("source_id") or "").strip()
    }
    excluded_keys = {
        str(source.get("_local_source_key") or "").strip()
        for source in excluded_sources
        if str(source.get("_local_source_key") or "").strip()
    }
    catalog_path = root / "catalog.sqlite"
    if catalog_path.is_file():
        import sqlite3

        connection = sqlite3.connect(catalog_path)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).casefold() != "ok":
                raise DatabaseCopyError("copied catalog is invalid")
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(document)")
            }
            if "source_id" in columns:
                for source_id in excluded_ids:
                    if connection.execute(
                        "SELECT 1 FROM document WHERE source_id = ? LIMIT 1",
                        (source_id,),
                    ).fetchone() is not None:
                        raise DatabaseCopyError(
                            "excluded Source remains in copied catalog"
                        )
        finally:
            connection.close()

    _validate_excluded_clean_records(root, excluded_ids)
    _validate_excluded_json(
        root / "logs" / "index_state.json",
        excluded_ids,
        label="ADD state",
    )
    _validate_excluded_json(
        root / "source-links.json",
        excluded_ids,
        label="Source Metadata",
    )
    _validate_excluded_json(
        root / "source-links.json.bak",
        excluded_ids,
        label="Source Metadata backup",
    )
    for local_key in excluded_keys:
        if (root / "sources" / local_key).exists():
            raise DatabaseCopyError(
                "excluded Source management directory remains in copied DB"
            )
    validate_excluded_vectors(
        root,
        collection=collection,
        source_ids=excluded_ids,
        error_type=DatabaseCopyError,
    )


def _validate_excluded_clean_records(
    root: Path,
    excluded_ids: set[str],
) -> None:
    if not excluded_ids:
        return
    clean_root = root / "data" / "clean"
    if not clean_root.is_dir():
        return
    for path in sorted(clean_root.rglob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            raise DatabaseCopyError("copied clean storage is unsafe")
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if (
                        isinstance(value, dict)
                        and _source_id_from_record(value) in excluded_ids
                    ):
                        raise DatabaseCopyError(
                            "excluded Source remains in copied clean records"
                        )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DatabaseCopyError("copied clean records are invalid") from exc


def _source_id_from_record(value: Mapping[str, Any]) -> str:
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("source_id") is not None:
        return str(metadata.get("source_id") or "")
    return str(value.get("source_id") or "")


def _validate_excluded_json(
    path: Path,
    excluded_ids: set[str],
    *,
    label: str,
) -> None:
    if not excluded_ids or not path.is_file():
        return
    payload = read_json_object(path, required=True)
    if _contains_source_id(payload, excluded_ids):
        raise DatabaseCopyError(f"excluded Source remains in copied {label}")


def _contains_source_id(value: Any, excluded_ids: set[str]) -> bool:
    if isinstance(value, dict):
        if str(value.get("source_id") or "") in excluded_ids:
            return True
        return any(_contains_source_id(item, excluded_ids) for item in value.values())
    if isinstance(value, list):
        return any(_contains_source_id(item, excluded_ids) for item in value)
    return False


def configured_collection(root: Path, db_name: str) -> str:
    for path in (
        root / "db.json",
        root / "VERSION.json",
        root / "index" / "manifest.json",
    ):
        if path.is_file():
            collection = str(
                read_json_object(path, required=False).get("collection") or ""
            ).strip()
            if collection:
                return collection
    return collection_name_for_db(db_name)


def collection_name_for_db(db_name: str) -> str:
    safe = re.sub(
        r"[^A-Za-z0-9_]+",
        "_",
        db_name.strip("-").replace("-", "_"),
    ).strip("_")
    return f"{safe}_ruri3_30m_int8_v1"


def require_db_name(value: str) -> str:
    name = str(value or "").strip()
    if not _DB_NAME.fullmatch(name):
        raise DatabaseCopyError("DB name must match '<name>-rag'")
    return name


def required_real_directory(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise DatabaseCopyError(f"{label} is unavailable") from exc
    if is_link_or_reparse(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise DatabaseCopyError(f"{label} must be a real directory")
    return path.resolve(strict=True)


def validate_tree_for_copy(root: Path) -> None:
    for directory, child_directories, filenames in os.walk(
        root, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        for name in list(child_directories):
            candidate = directory_path / name
            metadata = os.lstat(candidate)
            if is_link_or_reparse(candidate, metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise DatabaseCopyError("DB copy does not allow linked directories")
        for name in filenames:
            candidate = directory_path / name
            metadata = os.lstat(candidate)
            if is_link_or_reparse(candidate, metadata) or not stat.S_ISREG(metadata.st_mode):
                raise DatabaseCopyError("DB copy does not allow links or special files")


def is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


def read_json_object(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise DatabaseCopyError(f"required DB file is missing: {path.name}")
        return {}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            raise OSError("unsafe JSON file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatabaseCopyError(f"DB JSON is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise DatabaseCopyError(f"DB JSON must be an object: {path.name}")
    return value


def updated_profile_text(text: str, *, title: str, query_hint: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines[0] = f"# {title}"
        value = "\n".join(lines).rstrip() + "\n"
    elif text.strip():
        value = f"# {title}\n\n{text.lstrip()}"
    else:
        value = f"# {title}\n"
    marker = "## Query Hint"
    replacement = f"{marker}\n\n{query_hint}".rstrip() + "\n"
    if marker not in value:
        return value.rstrip() + "\n\n" + replacement
    before, after = value.split(marker, 1)
    next_section = re.search(r"(?m)^##\s+", after)
    suffix = after[next_section.start() :] if next_section else ""
    return before.rstrip() + "\n\n" + replacement + (
        "\n" + suffix.lstrip() if suffix else ""
    )


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_text(
        path,
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def emit(
    callback: Callable[[Mapping[str, Any]], None] | None,
    phase: str,
    label: str,
    completed: int,
    total: int | None,
    *,
    current_item: str | None = None,
) -> None:
    if callback is None:
        return
    payload: dict[str, Any] = {
        "event": "database.copy.progress",
        "phase": phase,
        "label_ja": label,
        "status": "completed" if total is not None and completed >= total else "running",
        "completed": max(0, int(completed)),
        "unit": "件",
        "total_kind": "exact" if total is not None else "unknown",
    }
    if total is not None:
        payload["total"] = max(0, int(total))
    if current_item:
        payload["current_item"] = str(current_item)
    try:
        callback(payload)
    except Exception:
        pass


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
