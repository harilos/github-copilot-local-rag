from __future__ import annotations

import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from .metadata import remove_source_metadata
from .store import SourceStore

_LOCAL_SOURCE_KEY = re.compile(r"^src_[a-z0-9][a-z0-9-]{0,39}-[0-9a-f]{12}$")
_COPY_BATCH_SIZE = 500


def copy_catalog_snapshot(source: Path, staging: Path, *, error_type: type[Exception]) -> None:
    source_catalog = source / "catalog.sqlite"
    if not source_catalog.is_file():
        return
    destination_catalog = staging / "catalog.sqlite"
    source_connection = sqlite3.connect(
        source_catalog.resolve().as_uri() + "?mode=ro", uri=True
    )
    destination_connection = sqlite3.connect(destination_catalog)
    try:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).casefold() != "ok":
            raise error_type("copied catalog failed SQLite integrity_check")
    finally:
        destination_connection.close()
        source_connection.close()


def copy_chroma_snapshot(
    source: Path,
    staging: Path,
    *,
    old_collection: str,
    new_collection: str,
    progress_callback: Callable[[Mapping[str, Any]], None] | None,
    error_type: type[Exception],
) -> int:
    source_chroma = source / "index" / "chroma"
    if not source_chroma.is_dir():
        return 0
    try:
        import chromadb
        from chromadb.config import Settings
    except ModuleNotFoundError as exc:
        raise error_type("ChromaDB runtime is required to copy an indexed DB") from exc

    destination_chroma = staging / "index" / "chroma"
    destination_chroma.mkdir(parents=True, exist_ok=True)
    settings = Settings(anonymized_telemetry=False)
    source_client = chromadb.PersistentClient(path=str(source_chroma), settings=settings)
    destination_client = chromadb.PersistentClient(
        path=str(destination_chroma), settings=settings
    )
    try:
        source_collection = source_client.get_collection(name=old_collection)
    except Exception as exc:
        raise error_type(
            f"source Chroma collection is unavailable: {old_collection}"
        ) from exc
    try:
        destination_client.delete_collection(new_collection)
    except Exception:
        pass
    destination_collection = destination_client.create_collection(
        name=new_collection, metadata=source_collection.metadata
    )
    total = int(source_collection.count())
    copied = 0
    while copied < total:
        batch = source_collection.get(
            limit=min(_COPY_BATCH_SIZE, total - copied),
            offset=copied,
            include=["embeddings", "documents", "metadatas"],
        )
        ids = [str(value) for value in batch.get("ids") or []]
        if not ids:
            raise error_type("Chroma snapshot ended before the expected count")
        arguments: dict[str, Any] = {"ids": ids}
        embeddings = batch.get("embeddings")
        if embeddings is not None:
            arguments["embeddings"] = (
                embeddings.tolist() if hasattr(embeddings, "tolist") else embeddings
            )
        if batch.get("documents") is not None:
            arguments["documents"] = batch["documents"]
        if batch.get("metadatas") is not None:
            arguments["metadatas"] = batch["metadatas"]
        destination_collection.add(**arguments)
        copied += len(ids)
        _emit(progress_callback, "copy.vector", "ベクトルDBのコピー", copied, total)
    if int(destination_collection.count()) != total:
        raise error_type("copied Chroma collection count does not match")
    return total


def delete_excluded_sources(
    staging: Path,
    excluded_sources: list[dict[str, Any]],
    *,
    destination_name: str,
    collection: str,
    rag_root: Path,
    progress_callback: Callable[[Mapping[str, Any]], None] | None,
    error_type: type[Exception],
) -> list[dict[str, Any]]:
    if not excluded_sources:
        return []
    tool_root = rag_root / "gen_db" / "software_rag_tool"
    if str(tool_root) not in sys.path:
        sys.path.insert(0, str(tool_root))
    try:
        from software_rag_tool.source_delete import delete_source_data
    except Exception as exc:
        raise error_type("Source deletion runtime is unavailable") from exc

    results: list[dict[str, Any]] = []
    with temporary_environment(
        {
            "RAG_DB_NAME": destination_name,
            "RAG_OUTPUT_ROOT": str(staging),
            "CHROMA_COLLECTION": collection,
        }
    ):
        for position, source in enumerate(excluded_sources, start=1):
            source_id = str(source.get("source_id") or "").strip()
            local_key = str(source.get("_local_source_key") or "").strip()
            display_name = str(
                source.get("display_name") or source_id or local_key or "Source"
            )
            indexed_result: dict[str, Any] | None = None
            if source_id:
                indexed_result = dict(delete_source_data(source_id))
                try:
                    remove_source_metadata(staging, source_id, rag_root)
                except Exception as exc:
                    raise error_type(
                        f"failed to remove copied Source Metadata: {display_name}"
                    ) from exc
            if local_key:
                delete_management_source(staging, local_key, error_type=error_type)
            results.append(
                {
                    "display_name": display_name,
                    "source_id": source_id or None,
                    "local_source_key": local_key or None,
                    "indexed": indexed_result,
                }
            )
            _emit(
                progress_callback,
                "copy.exclude",
                "除外Sourceの削除",
                position,
                len(excluded_sources),
                current_item=display_name,
            )
    return results


def delete_management_source(
    db_root: Path, local_key: str, *, error_type: type[Exception]
) -> None:
    if not _LOCAL_SOURCE_KEY.fullmatch(local_key):
        raise error_type("excluded Source has an invalid management key")
    store = SourceStore(db_root)
    loaded = store.read_source(local_key)
    if loaded.payload:
        store.delete_source(
            local_key,
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )


@contextmanager
def temporary_environment(updates: Mapping[str, str]):
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            os.environ[key] = str(value)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _emit(
    callback: Callable[[Mapping[str, Any]], None] | None,
    phase: str,
    label: str,
    completed: int,
    total: int,
    *,
    current_item: str | None = None,
) -> None:
    if callback is None:
        return
    event: dict[str, Any] = {
        "event": "database.copy.progress",
        "phase": phase,
        "label_ja": label,
        "status": "completed" if completed >= total else "running",
        "completed": completed,
        "total": total,
        "unit": "件",
        "total_kind": "exact",
    }
    if current_item:
        event["current_item"] = current_item
    try:
        callback(event)
    except Exception:
        pass
