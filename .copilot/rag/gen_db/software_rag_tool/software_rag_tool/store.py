from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

try:
    import chromadb
except ModuleNotFoundError:
    chromadb = None  # type: ignore[assignment]

from .catalog import reset_catalog, upsert_records as upsert_catalog_records
from .embeddings import embedding_fingerprint, get_embedder
from .jsonl import read_jsonl
from .manifest import validate_embedding_manifest, write_manifest
from .paths import chroma_dir, clean_dir, default_collection_name


def collection_name() -> str:
    return default_collection_name()


def clean_jsonl_files() -> list[Path]:
    directory = clean_dir()
    if not directory.exists():
        return []
    return sorted(directory.rglob("*.jsonl"))


def load_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in clean_jsonl_files():
        records.extend(read_jsonl(path))
    return records


def build_index(reset: bool = True) -> int:
    records = load_records()
    if not records:
        raise RuntimeError(f"No clean jsonl records found under {clean_dir()}")

    if reset:
        reset_collection()
        reset_catalog()
    upsert_records(records)
    upsert_catalog_records(records)
    actual = collection_count()
    if reset and actual != len(records):
        raise RuntimeError(f"Index count mismatch: collection={actual}, records={len(records)}")
    write_manifest(actual)
    return actual


def reset_collection() -> None:
    _require_chromadb()
    cdir = chroma_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(cdir))
    name = collection_name()
    try:
        client.delete_collection(name)
    except Exception:
        pass


def _get_or_create_collection() -> Any:
    _require_chromadb()
    cdir = chroma_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(cdir))
    name = collection_name()
    metadata = {
        "hnsw:space": "cosine",
        **embedding_fingerprint(),
    }
    return client.get_or_create_collection(name=name, metadata=metadata)


def upsert_records(records: list[dict[str, Any]], progress_callback: Callable[[int, int], None] | None = None) -> int:
    if not records:
        return 0

    collection = _get_or_create_collection()
    embedder = get_embedder()
    batch_size = int(os.getenv("EMBED_BATCH_SIZE", "8"))
    if batch_size <= 0:
        raise ValueError("EMBED_BATCH_SIZE must be positive")

    total = 0
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        ids = [str(r["id"]) for r in batch]
        docs = [str(r.get("text") or "") for r in batch]
        embedding_docs = [str(r.get("embedding_text") or r.get("text") or "") for r in batch]
        metas = [_flat_metadata(dict(r.get("metadata") or {})) for r in batch]
        embeddings = embedder.encode(embedding_docs, mode="document")
        collection.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metas)
        total += len(batch)
        if progress_callback:
            progress_callback(total, len(records))
        print(f"Upserted batch {(start // batch_size) + 1}: +{len(batch)} (total={total})")
    return total


def delete_ids(ids: list[str]) -> int:
    ids = [value for value in ids if value]
    if not ids:
        return 0
    collection = _get_or_create_collection()
    collection.delete(ids=ids)
    return len(ids)


def collection_count() -> int:
    collection = _get_or_create_collection()
    return int(collection.count())


def vector_query(question: str, top_k: int, source: str = "any") -> list[dict[str, Any]]:
    _require_chromadb()
    validate_embedding_manifest()
    client = chromadb.PersistentClient(path=str(chroma_dir()))
    collection = client.get_collection(name=collection_name())
    embedder = get_embedder()
    q_embedding = embedder.encode([question], mode="query")[0]
    where = None if source == "any" else {"source": source}
    result = collection.query(
        query_embeddings=[q_embedding],
        n_results=top_k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    rows: list[dict[str, Any]] = []
    ids = result.get("ids", [[]])[0]
    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]
    for rank, (rid, doc, meta, distance) in enumerate(zip(ids, docs, metas, distances), start=1):
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
    return rows


def query(
    question: str,
    top_k: int,
    source: str = "any",
    fetch_k: int | None = None,
    max_per_doc: int = 2,
    budget_tokens: int | None = None,
    explain: bool = False,
    use_dense: bool = True,
) -> list[dict[str, Any]]:
    from .retrieval import hybrid_query

    return hybrid_query(
        question,
        top_k=top_k,
        source=source,
        fetch_k=fetch_k,
        max_per_doc=max_per_doc,
        budget_tokens=budget_tokens,
        explain=explain,
        use_dense=use_dense,
    )


def semantic_query(question: str, top_k: int, source: str = "any", fetch_k: int | None = None, max_per_doc: int = 2) -> list[dict[str, Any]]:
    rows = vector_query(question, top_k=fetch_k or max(top_k * 4, top_k), source=source)
    doc_counts: dict[str, int] = {}
    seen_chunk_hashes: set[str] = set()
    output: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        chunk_hash = str(meta.get("chunk_hash") or "")
        if chunk_hash and chunk_hash in seen_chunk_hashes:
            continue
        if chunk_hash:
            seen_chunk_hashes.add(chunk_hash)
        doc_key = str(meta.get("path") or row.get("id"))
        count = doc_counts.get(doc_key, 0)
        if count >= max_per_doc:
            continue
        doc_counts[doc_key] = count + 1
        row = dict(row)
        row["rank"] = len(output) + 1
        output.append(row)
        if len(output) >= top_k:
            break
    return output


def _flat_metadata(meta: dict[str, Any]) -> dict[str, str | int | float | bool]:
    flat: dict[str, str | int | float | bool] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            flat[key] = value
        else:
            flat[key] = str(value)
    return flat


def _require_chromadb() -> None:
    if chromadb is None:
        raise RuntimeError("chromadb is not installed. Run python ~/.copilot/rag/query/setup.py before dense search or DB generation.")
