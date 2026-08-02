from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import chromadb
    from chromadb.config import Settings
except ModuleNotFoundError:
    chromadb = None  # type: ignore[assignment]
    Settings = None  # type: ignore[assignment,misc]

from . import catalog
from .dbs import collection_name_for_db, read_db_config, read_db_version, read_profile_hint, require_db_name
from .embeddings import embedding_fingerprint, get_embedder
from .manifest import ConfigMismatchError, read_manifest, validate_embedding_manifest
from .tokenize import TokenizerFingerprintError, validate_tokenizer_fingerprint


@dataclass(frozen=True)
class DbContext:
    name: str
    root: Path
    catalog_path: Path
    chroma_dir: Path
    collection_name: str
    db_config: dict[str, Any]
    version: dict[str, Any]
    manifest: dict[str, Any]
    profile_hint: str
    embedding_fingerprint: dict[str, Any]


class DbStore:
    def __init__(self, context: DbContext) -> None:
        self.context = context
        self._client: Any | None = None
        self._collection: Any | None = None
        self._collection_lock = threading.Lock()
        self._catalog_connection: sqlite3.Connection | None = None
        self._catalog_lock = threading.Lock()
        self._embed_lock = threading.Lock()
        self._last_used_at = time.monotonic()
        self._validate()

    @property
    def last_used_at(self) -> float:
        return self._last_used_at

    def vector_query(self, question: str, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        return self.vector_query_many([question], top_k=top_k, source=source)[0]

    def vector_query_many(
        self,
        questions: list[str],
        top_k: int,
        source: str = "any",
    ) -> list[list[dict[str, Any]]]:
        self._last_used_at = time.monotonic()
        if chromadb is None:
            raise RuntimeError("chromadb is not installed. Run python ~/.copilot/rag/query/setup.py before dense search or DB generation.")
        validate_embedding_manifest(self.context.manifest, collection=self.context.collection_name)
        if not questions:
            return []
        collection = self._get_collection()
        embedder = get_embedder()
        with self._embed_lock:
            query_embeddings = embedder.encode(questions, mode="query")
        where = None if source == "any" else {"source": source}
        result = collection.query(
            query_embeddings=query_embeddings,
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        output: list[list[dict[str, Any]]] = []
        all_ids = result.get("ids") or [[] for _ in questions]
        all_docs = result.get("documents") or [[] for _ in questions]
        all_metas = result.get("metadatas") or [[] for _ in questions]
        all_distances = result.get("distances") or [[] for _ in questions]
        for ids, docs, metas, distances in zip(
            all_ids,
            all_docs,
            all_metas,
            all_distances,
        ):
            rows: list[dict[str, Any]] = []
            for rank, (rid, doc, meta, distance) in enumerate(
                zip(ids, docs, metas, distances),
                start=1,
            ):
                rows.append(
                    {
                        "rank": rank,
                        "id": rid,
                        "distance": distance,
                        "text": doc,
                        "metadata": meta,
                        "signals": ["dense"],
                    }
                )
            output.append(rows)
        return output

    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        self._last_used_at = time.monotonic()
        return catalog.exact_search(
            question,
            top_k=top_k,
            source=source,
            path=self.context.catalog_path,
            conn=self._get_catalog_connection(),
        )

    def bm25_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        self._last_used_at = time.monotonic()
        self._validate_lexical_tokenizer()
        return catalog.bm25_search(
            question,
            top_k=top_k,
            source=source,
            path=self.context.catalog_path,
            conn=self._get_catalog_connection(),
        )

    def anchor_lexical_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        self._last_used_at = time.monotonic()
        self._validate_lexical_tokenizer()
        return catalog.anchor_lexical_search(
            question,
            top_k=top_k,
            source=source,
            path=self.context.catalog_path,
            conn=self._get_catalog_connection(),
        )

    def metadata_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        self._last_used_at = time.monotonic()
        self._validate_lexical_tokenizer()
        return catalog.metadata_search(
            question,
            top_k=top_k,
            source=source,
            path=self.context.catalog_path,
            conn=self._get_catalog_connection(),
        )

    def fetch_rows_by_ids(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        self._last_used_at = time.monotonic()
        return catalog.fetch_rows_by_ids(
            ids,
            path=self.context.catalog_path,
            conn=self._get_catalog_connection(),
        )

    def get_neighbor_rows(self, chunk_uid: str, *, window: int = 1) -> list[dict[str, Any]]:
        self._last_used_at = time.monotonic()
        return catalog.get_neighbor_rows(
            chunk_uid,
            window=window,
            path=self.context.catalog_path,
            conn=self._get_catalog_connection(),
        )

    def _get_catalog_connection(self) -> sqlite3.Connection:
        with self._catalog_lock:
            if self._catalog_connection is None:
                uri = catalog.readonly_uri(self.context.catalog_path)
                connection = sqlite3.connect(uri, uri=True)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA foreign_keys=ON")
                self._catalog_connection = connection
            return self._catalog_connection

    def _validate_lexical_tokenizer(self) -> None:
        runtime = validate_tokenizer_fingerprint(
            self.context.manifest.get("tokenizer")
        )
        connection = self._get_catalog_connection()
        row = connection.execute(
            "SELECT value FROM database_meta WHERE key = 'tokenizer'"
        ).fetchone()
        catalog_fingerprint = str(row[0]) if row else ""
        if catalog_fingerprint != runtime:
            raise TokenizerFingerprintError(
                "lexical_catalog_tokenizer_fingerprint_mismatch"
            )

    def close(self) -> None:
        with self._catalog_lock:
            connection = self._catalog_connection
            self._catalog_connection = None
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()
        self._collection = None
        self._client = None

    def _get_collection(self) -> Any:
        with self._collection_lock:
            if self._collection is not None:
                return self._collection
            if self._client is None:
                self._client = chromadb.PersistentClient(
                    path=str(self.context.chroma_dir),
                    settings=Settings(anonymized_telemetry=False),
                )
            self._collection = self._client.get_collection(name=self.context.collection_name)
            return self._collection

    def _validate(self) -> None:
        if not self.context.catalog_path.exists():
            raise FileNotFoundError(f"catalog.sqlite not found for {self.context.name}: {self.context.catalog_path}")
        if not self.context.chroma_dir.exists():
            raise FileNotFoundError(f"Chroma index not found for {self.context.name}: {self.context.chroma_dir}")
        expected_collection = self.context.collection_name
        mismatches = {}
        for source, payload in [
            ("db.json", self.context.db_config),
            ("VERSION.json", self.context.version),
            ("manifest.json", self.context.manifest),
        ]:
            value = payload.get("collection") if isinstance(payload, dict) else None
            if value and value != expected_collection:
                mismatches[source] = {"manifest": value, "current": expected_collection}
        if mismatches:
            raise ConfigMismatchError(f"DB collection metadata mismatch: {json.dumps(mismatches, ensure_ascii=False, sort_keys=True)}")
        validate_embedding_manifest(self.context.manifest, collection=expected_collection)


class DbRegistry:
    def __init__(self, dbs_root: Path, *, max_dbs: int = 3) -> None:
        self.dbs_root = dbs_root.expanduser().resolve()
        self.max_dbs = max_dbs
        self._stores: OrderedDict[str, tuple[tuple[Any, ...], DbStore]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, db_name: str) -> DbStore:
        name = require_db_name(db_name)
        root = (self.dbs_root / name).resolve()
        fingerprint = self._fingerprint(root, name)
        with self._lock:
            cached = self._stores.get(name)
            if cached and cached[0] == fingerprint:
                self._stores.move_to_end(name)
                return cached[1]
            if cached:
                cached[1].close()
            store = DbStore(_context_from_root(root, name))
            self._stores[name] = (fingerprint, store)
            self._stores.move_to_end(name)
            while len(self._stores) > self.max_dbs:
                _old_name, (_old_fingerprint, old_store) = (
                    self._stores.popitem(last=False)
                )
                old_store.close()
            return store

    def invalidate(self, db_name: str) -> None:
        with self._lock:
            cached = self._stores.pop(db_name, None)
        if cached:
            cached[1].close()

    def close(self) -> None:
        with self._lock:
            stores = [store for _fingerprint, store in self._stores.values()]
            self._stores.clear()
        for store in stores:
            store.close()

    @property
    def cached_count(self) -> int:
        with self._lock:
            return len(self._stores)

    def _fingerprint(self, root: Path, name: str) -> tuple[Any, ...]:
        catalog_path = root / "catalog.sqlite"
        manifest_path = root / "index" / "manifest.json"
        version_path = root / "VERSION.json"
        db_config_path = root / "db.json"
        return (
            name,
            str(root),
            _mtime(catalog_path),
            _mtime(manifest_path),
            _mtime(version_path),
            _mtime(db_config_path),
            collection_name_for_db(name),
        )


def _context_from_root(root: Path, name: str) -> DbContext:
    db_config = read_db_config(root)
    version = read_db_version(root)
    manifest = read_manifest(root / "index" / "manifest.json")
    collection = str(db_config.get("collection") or version.get("collection") or manifest.get("collection") or collection_name_for_db(name))
    return DbContext(
        name=name,
        root=root,
        catalog_path=root / "catalog.sqlite",
        chroma_dir=root / "index" / "chroma",
        collection_name=collection,
        db_config=db_config,
        version=version,
        manifest=manifest,
        profile_hint=read_profile_hint(root),
        embedding_fingerprint=embedding_fingerprint(),
    )


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0
