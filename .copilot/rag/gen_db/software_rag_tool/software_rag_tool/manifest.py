from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .embeddings import embedding_fingerprint
from .paths import default_collection_name, index_dir, output_root
from .tokenize import tokenizer_fingerprint


class ConfigMismatchError(RuntimeError):
    pass


def manifest_path() -> Path:
    return index_dir() / "manifest.json"


def build_manifest(record_count: int) -> dict[str, Any]:
    embedding = embedding_fingerprint()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_root": str(output_root()),
        "record_count": record_count,
        "collection": default_collection_name(),
        "chroma_space": "cosine",
        "chunker_version": "jp-sw-v1",
        "catalog_schema_version": 1,
        "tokenizer": tokenizer_fingerprint(),
        "retrieval": "hybrid-rrf-v1",
        **embedding,
    }


def write_manifest(record_count: int) -> Path:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_manifest(record_count), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_manifest() -> dict[str, Any]:
    path = manifest_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def validate_embedding_manifest() -> None:
    manifest = read_manifest()
    if not manifest:
        return
    expected = {
        **embedding_fingerprint(),
        "collection": default_collection_name(),
    }
    keys = [
        "embedding_model",
        "embedding_dimension",
        "embedding_backend",
        "quantization",
        "document_prefix",
        "query_prefix",
        "collection",
    ]
    mismatches = {
        key: {"manifest": manifest.get(key), "current": expected.get(key)}
        for key in keys
        if manifest.get(key) not in {None, expected.get(key)}
    }
    if mismatches:
        raise ConfigMismatchError(
            "Embedding fingerprint does not match this DB. "
            "Rebuild the vector index for the current model/backend. "
            f"mismatches={json.dumps(mismatches, ensure_ascii=False, sort_keys=True)}"
        )
