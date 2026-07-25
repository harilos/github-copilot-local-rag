from __future__ import annotations

import os
from pathlib import Path

DEFAULT_EMBEDDING_MODEL = "cl-nagoya/ruri-v3-30m"
DEFAULT_EMBEDDING_BACKEND = "onnx-int8"
DEFAULT_EMBEDDING_DIMENSION = 256
DEFAULT_DOCUMENT_PREFIX = "検索文書: "
DEFAULT_QUERY_PREFIX = "検索クエリ: "
DEFAULT_QUANTIZATION = "dynamic-int8"
DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS = 10_800


def rag_root() -> Path:
    return Path(__file__).resolve().parents[3]


def models_dir() -> Path:
    value = os.getenv("RAG_MODELS_DIR")
    if value:
        return Path(value).expanduser().resolve()
    return rag_root() / "models"


def default_onnx_model_dir() -> Path:
    return models_dir() / "ruri-v3-30m-onnx-int8"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
