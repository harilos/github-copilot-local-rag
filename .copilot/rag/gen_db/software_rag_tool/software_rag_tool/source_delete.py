from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from . import catalog
from .dbs import collection_name_for_db
from .jsonl import read_jsonl
from .paths import (
    catalog_path,
    clean_dir,
    db_name,
    index_dir,
    logs_dir,
    output_root,
)
from .store import delete_ids, source_records
from .writer_runtime import active_database_write_target


ProgressCallback = Callable[[Mapping[str, Any]], None]


def delete_source_data(
    source_id: str,
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Remove one Source from vector, catalog, clean records, and ADD state.

    The operation is deliberately idempotent.  A failed run can be repeated;
    each phase uses exact ``source_id`` equality and never removes another
    Source merely because a path or filename matches.
    """
    value = str(source_id or "")
    if not value or any(character in value for character in "\x00\r\n"):
        raise ValueError("source_id is required and must not contain controls")

    _validate_destructive_storage()
    catalog.ensure_source_delete_index()
    state = _read_json_object(logs_dir() / "index_state.json", default={})
    state_files = state.get("files")
    state_files = state_files if isinstance(state_files, dict) else {}
    matching_state_keys = [
        key
        for key, item in state_files.items()
        if isinstance(item, dict)
        and str(item.get("source_id") or "") == value
    ]
    state_record_ids = {
        str(record_id)
        for key in matching_state_keys
        for record_id in (
            state_files[key].get("record_ids") or []
            if isinstance(state_files[key].get("record_ids"), list)
            else []
        )
        if str(record_id)
    }

    catalog_ids = set(catalog.source_chunk_ids(value))
    vector_records = _inventory_vector_records(value, catalog_ids)
    vector_ids = {
        str(record.get("id") or "")
        for record in vector_records
        if str(record.get("id") or "")
    }
    clean_actions = _plan_clean_deletion(
        value,
        state=state,
        matching_state_keys=matching_state_keys,
        expected_record_ids=catalog_ids | state_record_ids | vector_ids,
    )
    # Chroma's exact metadata filter is canonical for vector deletion.  State
    # and catalog IDs remain recovery hints for their own stores, but never
    # authorize deletion of a vector owned by a sibling Source.
    record_ids = sorted(vector_ids)
    _emit_progress(
        progress_callback,
        phase="delete.verify",
        label_ja="削除対象確認",
        completed=len(record_ids),
        total=len(record_ids),
        unit="件",
        total_kind="exact",
        status="completed",
        checkpoint_saved=True,
    )

    # Vector deletion is first.  If a later phase fails, state and/or catalog
    # IDs allow an immediate retry and Chroma deletion is idempotent.
    def vector_progress(completed: int, total: int) -> None:
        _emit_progress(
            progress_callback,
            phase="delete.vector",
            label_ja="ベクトル削除",
            completed=completed,
            total=total,
            unit="件",
            total_kind="exact",
        )

    deleted_vector_records = (
        _delete_vector_ids(
            record_ids,
            progress_callback=vector_progress,
        )
        if record_ids and progress_callback is not None
        else _delete_vector_ids(record_ids)
        if record_ids
        else 0
    )
    _verify_vector_source_empty(value)
    _emit_progress(
        progress_callback,
        phase="delete.vector",
        label_ja="ベクトル削除",
        completed=len(record_ids),
        total=len(record_ids),
        unit="件",
        total_kind="exact",
        status="completed",
        checkpoint_saved=True,
    )
    _emit_progress(
        progress_callback,
        phase="delete.catalog",
        label_ja="SQLite・全文検索索引削除",
        total_kind="unknown",
    )
    catalog_result = catalog.delete_source_documents(value)
    _emit_progress(
        progress_callback,
        phase="delete.catalog",
        label_ja="SQLite・全文検索索引削除",
        completed=int(catalog_result["chunks"]),
        total=int(catalog_result["chunks"]),
        unit="件",
        total_kind="exact",
        status="completed",
        checkpoint_saved=True,
    )
    deleted_clean_files, rewritten_clean_files = _apply_clean_deletion(
        clean_actions,
        progress_callback=progress_callback,
    )

    _emit_progress(
        progress_callback,
        phase="delete.state",
        label_ja="state・manifest更新",
        total_kind="unknown",
    )
    for key in matching_state_keys:
        state_files.pop(key, None)
    if isinstance(state, dict) and state:
        state["files"] = state_files
        ingestion = state.get("ingestion")
        if (
            isinstance(ingestion, dict)
            and str(ingestion.get("source_id") or "") == value
        ):
            state["ingestion"] = {}
        state["updated_at"] = _now()
        _atomic_json_write(logs_dir() / "index_state.json", state)
    _remove_source_errors(value)
    _update_manifest_count(catalog.chunk_count())
    _emit_progress(
        progress_callback,
        phase="delete.state",
        label_ja="state・manifest更新",
        completed=1,
        total=1,
        unit="工程",
        total_kind="exact",
        status="completed",
        checkpoint_saved=True,
    )
    result = {
        "status": "deleted",
        "source_id": value,
        "documents_deleted": int(catalog_result["documents"]),
        "chunks_deleted": int(catalog_result["chunks"]),
        "vector_records_requested": len(record_ids),
        "vector_records_deleted": deleted_vector_records,
        "clean_files_deleted": deleted_clean_files,
        "clean_files_rewritten": rewritten_clean_files,
        "state_entries_deleted": len(matching_state_keys),
    }
    _emit_progress(
        progress_callback,
        phase="delete.complete",
        label_ja="完了",
        completed=1,
        total=1,
        unit="工程",
        total_kind="exact",
        status="completed",
        checkpoint_saved=True,
    )
    return result


def _plan_clean_deletion(
    source_id: str,
    *,
    state: Mapping[str, Any] | None = None,
    matching_state_keys: Iterable[str] = (),
    expected_record_ids: set[str] | None = None,
) -> list[tuple[Path, list[dict[str, Any]] | None]]:
    root = clean_dir()
    if not root.is_dir():
        return []
    _require_safe_directory(root, boundary=root)
    fast = _plan_current_state_clean_deletion(
        source_id,
        root=root,
        state=state,
        matching_state_keys=matching_state_keys,
        expected_record_ids=expected_record_ids,
    )
    if fast is not None:
        return fast
    return _plan_clean_deletion_fallback(source_id, root=root)


def _plan_current_state_clean_deletion(
    source_id: str,
    *,
    root: Path,
    state: Mapping[str, Any] | None,
    matching_state_keys: Iterable[str],
    expected_record_ids: set[str] | None,
) -> list[tuple[Path, list[dict[str, Any]] | None]] | None:
    """Use current complete state without opening unrelated JSONL files."""
    if not isinstance(state, Mapping) or state.get("version") != 2:
        return None
    state_files = state.get("files")
    if not isinstance(state_files, Mapping):
        return None
    keys = tuple(str(key) for key in matching_state_keys)
    if not keys:
        return None
    target_paths: dict[Path, dict[str, Any]] = {}
    other_paths: set[Path] = set()
    for key, raw_item in state_files.items():
        if not isinstance(raw_item, Mapping):
            return None
        path = _state_records_path(root, raw_item.get("records_path"))
        if path is None:
            if str(key) in keys:
                return None
            continue
        if str(key) in keys:
            if path in target_paths:
                return None
            target_paths[path] = dict(raw_item)
        else:
            other_paths.add(path)
    if not target_paths or set(target_paths) & other_paths:
        return None

    verified_ids: set[str] = set()
    actions: list[tuple[Path, list[dict[str, Any]] | None]] = []
    for path, item in sorted(
        target_paths.items(),
        key=lambda value: value[0].as_posix(),
    ):
        record_ids = item.get("record_ids")
        if not isinstance(record_ids, list):
            return None
        item_ids = {str(value) for value in record_ids if str(value)}
        if len(item_ids) != len(record_ids):
            return None
        if item.get("record_count") not in (None, len(item_ids)):
            return None
        verified_ids.update(item_ids)
        try:
            _require_safe_regular_file(path, boundary=root)
        except FileNotFoundError:
            return None
        records = list(read_jsonl(path))
        if not records or any(
            _record_source_id(record) != source_id for record in records
        ):
            return None
        actual_ids = {
            str(record.get("id") or "")
            for record in records
            if str(record.get("id") or "")
        }
        if actual_ids != item_ids or len(actual_ids) != len(records):
            return None
        actions.append((path, None))
    if (
        expected_record_ids is not None
        and verified_ids != set(expected_record_ids)
    ):
        return None
    return actions


def _plan_clean_deletion_fallback(
    source_id: str,
    *,
    root: Path,
) -> list[tuple[Path, list[dict[str, Any]] | None]]:
    actions: list[tuple[Path, list[dict[str, Any]] | None]] = []
    for path in _safe_jsonl_files(root):
        records = list(read_jsonl(path))
        matching = [
            record
            for record in records
            if _record_source_id(record) == source_id
        ]
        if not matching:
            continue
        remaining = [
            record
            for record in records
            if _record_source_id(record) != source_id
        ]
        actions.append((path, remaining or None))
    return actions


def _apply_clean_deletion(
    actions: Iterable[tuple[Path, list[dict[str, Any]] | None]],
    *,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, int]:
    deleted = 0
    rewritten = 0
    planned = list(actions)
    root = clean_dir()
    for index, (path, remaining) in enumerate(planned, start=1):
        _require_safe_regular_file(path, boundary=root)
        if remaining is None:
            path.unlink(missing_ok=True)
            deleted += 1
            _remove_empty_parents(path.parent, clean_dir())
        else:
            encoded = "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in remaining
            ).encode("utf-8")
            _atomic_bytes_write(path, encoded)
            rewritten += 1
        _emit_progress(
            progress_callback,
            phase="delete.clean",
            label_ja="clean削除",
            completed=index,
            total=len(planned),
            unit="ファイル",
            total_kind="exact",
        )
    _emit_progress(
        progress_callback,
        phase="delete.clean",
        label_ja="clean削除",
        completed=len(planned),
        total=len(planned),
        unit="ファイル",
        total_kind="exact",
        status="completed",
        checkpoint_saved=True,
    )
    return deleted, rewritten


def _record_source_id(record: dict[str, Any]) -> str:
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and metadata.get("source_id") is not None:
        return str(metadata.get("source_id") or "")
    return str(record.get("source_id") or "")


def _validate_destructive_storage() -> None:
    """Reject links/reparse points in every storage path we may mutate."""
    root = output_root()
    metadata = os.lstat(root)
    if _is_link_or_reparse(metadata, root) or not stat.S_ISDIR(
        metadata.st_mode
    ):
        raise RuntimeError("database root must be a real directory")
    for directory in (
        root / "data",
        clean_dir(),
        logs_dir(),
        index_dir(),
        index_dir() / "chroma",
    ):
        _validate_internal_component_path(
            directory,
            boundary=root,
            expected="directory",
        )
    for file_path in (
        catalog_path(),
        Path(str(catalog_path()) + "-wal"),
        Path(str(catalog_path()) + "-shm"),
        logs_dir() / "index_state.json",
        logs_dir() / "prepare_errors.json",
        index_dir() / "manifest.json",
    ):
        _validate_internal_component_path(
            file_path,
            boundary=root,
            expected="file",
        )
    vector_root = index_dir() / "chroma"
    if vector_root.is_dir():
        for directory, names, files in os.walk(
            vector_root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(directory)
            _validate_internal_component_path(
                current,
                boundary=root,
                expected="directory",
            )
            for name in [*names, *files]:
                candidate = current / name
                child = os.lstat(candidate)
                if _is_link_or_reparse(child, candidate):
                    raise RuntimeError(
                        "vector storage must not contain links or reparse points"
                    )


def _validate_internal_component_path(
    candidate: Path,
    *,
    boundary: Path,
    expected: str,
) -> None:
    try:
        relative = candidate.relative_to(boundary)
    except ValueError as exc:
        raise RuntimeError("destructive storage escaped database root") from exc
    current = boundary
    final_metadata: os.stat_result | None = None
    for component in relative.parts:
        current = current / component
        try:
            current_metadata = os.lstat(current)
        except FileNotFoundError:
            return
        if _is_link_or_reparse(current_metadata, current):
            raise RuntimeError(
                f"destructive storage must not contain links: {current.name}"
            )
        final_metadata = current_metadata
    if final_metadata is None:
        return
    if expected == "directory" and not stat.S_ISDIR(final_metadata.st_mode):
        raise RuntimeError("destructive storage directory is invalid")
    if expected == "file" and not stat.S_ISREG(final_metadata.st_mode):
        raise RuntimeError("destructive storage file is invalid")


def _delete_vector_ids(
    ids: list[str],
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    callback = delete_ids
    if progress_callback is None:
        return _with_target_vector_environment(callback, ids)
    return _with_target_vector_environment(
        callback,
        ids,
        progress_callback=progress_callback,
    )


def _inventory_vector_records(
    source_id: str,
    _known_catalog_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    return _with_target_vector_environment(source_records, source_id)


def _verify_vector_source_empty(source_id: str) -> None:
    residual = _inventory_vector_records(source_id)
    if residual:
        raise RuntimeError(
            "vector source deletion left residual records: "
            f"source_id={source_id!r} count={len(residual)}"
        )


def _with_target_vector_environment(
    callback: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    keys = ("CHROMA_DIR_V2", "CHROMA_COLLECTION")
    previous = {key: os.environ.get(key) for key in keys}
    active_target = active_database_write_target()
    target_collection = (
        active_target.collection
        if active_target is not None
        else collection_name_for_db(db_name())
    )
    os.environ["CHROMA_DIR_V2"] = str(index_dir() / "chroma")
    os.environ["CHROMA_COLLECTION"] = target_collection
    try:
        return callback(*args, **kwargs)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _state_records_path(root: Path, value: Any) -> Path | None:
    text = str(value or "")
    if (
        not text
        or "\\" in text
        or text.startswith("/")
        or "//" in text
        or "/./" in f"/{text}/"
        or "/../" in f"/{text}/"
    ):
        return None
    relative = PurePosixPath(text)
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        return None
    return root.joinpath(*relative.parts)


def _safe_jsonl_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        _require_safe_directory(directory_path, boundary=root)
        for name in list(names):
            child = directory_path / name
            metadata = os.lstat(child)
            if _is_link_or_reparse(metadata, child):
                raise RuntimeError(
                    f"clean record directory must not be a link: {name}"
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(
                    f"clean record directory is invalid: {name}"
                )
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            candidate = directory_path / name
            _require_safe_regular_file(candidate, boundary=root)
            paths.append(candidate)
    return sorted(paths)


def _require_safe_directory(path: Path, *, boundary: Path) -> None:
    _require_safe_path(path, boundary=boundary, expected_mode="directory")


def _require_safe_regular_file(path: Path, *, boundary: Path) -> None:
    _require_safe_path(path, boundary=boundary, expected_mode="file")


def _require_safe_path(
    path: Path,
    *,
    boundary: Path,
    expected_mode: str,
) -> None:
    boundary = Path(boundary)
    candidate = Path(path)
    boundary_metadata = os.lstat(boundary)
    if (
        _is_link_or_reparse(boundary_metadata, boundary)
        or not stat.S_ISDIR(boundary_metadata.st_mode)
    ):
        raise RuntimeError("clean root must be a real directory")
    try:
        relative = candidate.relative_to(boundary)
    except ValueError as exc:
        raise RuntimeError("clean record path escaped clean root") from exc
    current = boundary
    metadata = boundary_metadata
    for component in relative.parts:
        current = current / component
        metadata = os.lstat(current)
        if _is_link_or_reparse(metadata, current):
            raise RuntimeError(
                f"clean record path must not contain links: {current.name}"
            )
    if expected_mode == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("clean record directory is invalid")
    if expected_mode == "file" and not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("clean record path must be a regular file")


def _is_link_or_reparse(metadata: os.stat_result, path: Path) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


def _emit_progress(
    callback: ProgressCallback | None,
    **event: Any,
) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        # UI callbacks are observational and must not alter deletion.
        return


def _remove_source_errors(source_id: str) -> None:
    path = logs_dir() / "prepare_errors.json"
    payload = _read_json_value(path, default=[])
    if not isinstance(payload, list):
        return
    remaining = [
        value
        for value in payload
        if not isinstance(value, dict)
        or str(value.get("source_id") or "") != source_id
    ]
    if remaining != payload:
        _atomic_json_write(path, remaining)


def _update_manifest_count(record_count: int) -> None:
    path = index_dir() / "manifest.json"
    payload = _read_json_object(path, default={})
    if not payload:
        return
    payload["record_count"] = int(record_count)
    payload["generated_at"] = _now()
    _atomic_json_write(path, payload)


def _read_json_object(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    value = _read_json_value(path, default=default)
    return value if isinstance(value, dict) else dict(default)


def _read_json_value(path: Path, *, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _atomic_json_write(path: Path, payload: Any) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_bytes_write(path, encoded)


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _remove_empty_parents(path: Path, boundary: Path) -> None:
    boundary = boundary.resolve()
    current = path
    while current != boundary:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
