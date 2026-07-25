from __future__ import annotations

import os
from pathlib import Path

from .dbs import collection_name_for_db


def tool_root() -> Path:
    return Path(__file__).resolve().parents[1]


def rag_root() -> Path:
    return tool_root().parents[1]


def dbs_dir() -> Path:
    value = os.getenv("RAG_DBS_ROOT")
    if value:
        return _resolve_path(value)
    return rag_root() / "dbs"


def db_name() -> str:
    return os.getenv("RAG_DB_NAME", "software-rag")


def _resolve_path(value: str) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    return (Path.cwd() / p).resolve()


def output_root() -> Path:
    value = os.getenv("RAG_OUTPUT_ROOT")
    if value:
        return _resolve_path(value)
    value = os.getenv("LOCALRAG_OUTPUT_ROOT")
    if value:
        return _resolve_path(value)
    return dbs_dir() / db_name()


def clean_dir() -> Path:
    return output_root() / "data" / "clean"


def raw_dir() -> Path:
    return output_root() / "data" / "raw"


def index_dir() -> Path:
    return output_root() / "index"


def catalog_path() -> Path:
    return output_root() / "catalog.sqlite"


def logs_dir() -> Path:
    return output_root() / "logs"


def chroma_dir() -> Path:
    value = os.getenv("CHROMA_DIR_V2")
    if value:
        return _resolve_path(value)
    return index_dir() / "chroma"


def default_collection_name() -> str:
    return os.getenv("CHROMA_COLLECTION") or collection_name_for_db(db_name())
