from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import default_collection_name, index_dir, output_root
from .tokenize import tokenizer_fingerprint


def manifest_path() -> Path:
    return index_dir() / "manifest.json"


def build_manifest(record_count: int) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_root": str(output_root()),
        "record_count": record_count,
        "embedding_model": os.getenv("EMBEDDING_MODEL", "cl-nagoya/ruri-v3-130m"),
        "embedding_dimension": 512,
        "document_prefix": os.getenv("EMBED_DOCUMENT_PREFIX", "検索文書: "),
        "query_prefix": os.getenv("EMBED_QUERY_PREFIX", "検索クエリ: "),
        "collection": default_collection_name(),
        "chroma_space": "cosine",
        "chunker_version": "jp-sw-v1",
        "catalog_schema_version": 1,
        "tokenizer": tokenizer_fingerprint(),
        "retrieval": "hybrid-rrf-v1",
    }


def write_manifest(record_count: int) -> Path:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_manifest(record_count), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
