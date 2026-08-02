from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .embeddings import embedding_fingerprint
from .paths import default_collection_name, index_dir, output_root
from .tokenize import (
    TokenizerFingerprintError,
    require_index_tokenizer,
    tokenizer_fingerprint,
    tokenizer_runtime_descriptor,
    validate_tokenizer_fingerprint,
)
from .chunking import TOKEN_SAFE_CHUNKER_VERSION


class ConfigMismatchError(RuntimeError):
    pass


def manifest_path() -> Path:
    return index_dir() / "manifest.json"


def build_manifest(
    record_count: int,
    *,
    chunker_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    embedding = embedding_fingerprint()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_root": str(output_root()),
        "record_count": record_count,
        "collection": default_collection_name(),
        "chroma_space": "cosine",
        "chunker_version": TOKEN_SAFE_CHUNKER_VERSION,
        "chunker": chunker_config or {"version": TOKEN_SAFE_CHUNKER_VERSION},
        "catalog_schema_version": 2,
        "tokenizer": tokenizer_fingerprint(),
        "tokenizer_config": tokenizer_runtime_descriptor(),
        "retrieval": "hybrid-rrf-v1",
        **embedding,
    }


def write_manifest(
    record_count: int,
    *,
    chunker_config: dict[str, Any] | None = None,
) -> Path:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            build_manifest(record_count, chunker_config=chunker_config),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def read_manifest(path: Path | None = None) -> dict[str, Any]:
    path = path or manifest_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def validate_existing_index_tokenizer() -> str:
    """Validate an existing lexical index without mutating it."""
    runtime = require_index_tokenizer()
    payload = read_manifest()
    from .catalog import connect_readonly
    from .paths import catalog_path

    path = catalog_path()
    if payload:
        validate_tokenizer_fingerprint(payload.get("tokenizer"))
    elif path.is_file():
        raise TokenizerFingerprintError(
            "lexical_manifest_tokenizer_fingerprint_missing"
        )
    if path.is_file():
        with connect_readonly(path) as connection:
            row = connection.execute(
                "SELECT value FROM database_meta WHERE key = 'tokenizer'"
            ).fetchone()
        stored = str(row[0]) if row else ""
        if stored != runtime:
            raise TokenizerFingerprintError(
                "lexical_catalog_tokenizer_fingerprint_mismatch"
            )
    return runtime


def validate_embedding_manifest(manifest: dict[str, Any] | None = None, *, collection: str | None = None) -> None:
    manifest = manifest if manifest is not None else read_manifest()
    if not manifest:
        return
    expected = {
        **embedding_fingerprint(),
        "collection": collection or default_collection_name(),
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
