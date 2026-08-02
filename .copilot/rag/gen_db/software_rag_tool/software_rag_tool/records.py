from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

from .embeddings import (
    DocumentEmbeddingTokenLimitError,
    DocumentTokenBudget,
    get_document_token_budget,
)
from .chunking import (
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    TOKEN_SAFE_CHUNKER_VERSION,
)
from .extractors import SUPPORTED_EXTENSIONS, extract_sections
from .ingestion_paths import IngestionScope, resolve_ingestion_scope


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_content_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def chunker_config(
    *,
    chunk_max_chars: int,
    chunk_overlap: int,
    document_token_budget: DocumentTokenBudget,
) -> dict[str, Any]:
    return {
        "version": TOKEN_SAFE_CHUNKER_VERSION,
        "target_tokens": document_token_budget.target_tokens,
        "overlap_tokens": DEFAULT_CHUNK_OVERLAP_TOKENS,
        "hard_limit_tokens": document_token_budget.max_tokens,
        "max_chars": chunk_max_chars,
        "overlap_chars": chunk_overlap,
        "tokenizer": document_token_budget.tokenizer_name,
    }


def chunker_version(config: dict[str, Any]) -> str:
    keys = (
        "version",
        "target_tokens",
        "overlap_tokens",
        "hard_limit_tokens",
        "max_chars",
        "overlap_chars",
        "tokenizer",
    )
    return ":".join(f"{key}={config[key]}" for key in keys)


def iter_input_files(root: Path) -> Iterable[Path]:
    discovered: list[Path] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, child_directories, filenames in os.walk(
        root,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        child_directories.sort()
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                discovered.append(path)
    yield from sorted(discovered)


def _source_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cs", ".rb", ".php"}:
        return "code"
    if ext in {".md", ".txt"}:
        return "docs"
    if ext in {".log"}:
        return "log"
    if ext in {".doc", ".docx", ".ppt", ".pptx", ".pdf", ".xlsx"}:
        return "office"
    return "local"


def _language(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(ext, "mixed")


def build_records_for_file(
    root: Path,
    path: Path,
    source_id: str = "local",
    content_hash: str | None = None,
    chunk_max_chars: int = 1400,
    chunk_overlap: int = 160,
    ingestion_scope: IngestionScope | None = None,
    document_token_budget: DocumentTokenBudget | None = None,
) -> list[dict[str, Any]]:
    scope = ingestion_scope or resolve_ingestion_scope(root)
    stored = scope.file(path)
    token_budget = document_token_budget or get_document_token_budget()
    config = chunker_config(
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        document_token_budget=token_budget,
    )
    sections = extract_sections(
        stored.resolved_path,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        token_budget=token_budget,
        embedding_path=stored.stored_path,
    )
    content_hash = content_hash or file_content_hash(stored.resolved_path)
    doc_id = sha256_text(
        f"{source_id}:{stored.stored_path}:{content_hash}"
    )
    current_chunker_version = chunker_version(config)

    records: list[dict[str, Any]] = []
    chunk_index = 0
    for section in sections:
        text = section.text.strip()
        if not text:
            continue
        text_hash = sha256_text(text)
        chunk_id = sha256_text(
            f"{doc_id}:{current_chunker_version}:{chunk_index}"
        )
        embedding_text = (
            f"{stored.stored_path}\n{section.title}\n{text}"
        )
        embedding_token_count = token_budget.count_embedding_text(
            embedding_text
        )
        if embedding_token_count > token_budget.max_tokens:
            raise DocumentEmbeddingTokenLimitError(
                "record exceeds the hard document token limit: "
                f"path={stored.stored_path!r} section={section.title!r} "
                f"tokens={embedding_token_count} limit={token_budget.max_tokens}"
            )
        records.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "text": text,
                "embedding_text": embedding_text,
                "metadata": {
                    "source": source_id,
                    "source_id": source_id,
                    "source_type": _source_type(stored.resolved_path),
                    "path": stored.stored_path,
                    "uri": stored.stored_path,
                    "title": stored.resolved_path.name,
                    "section_path": section.title,
                    "language": _language(stored.resolved_path),
                    "root": scope.root_display_name,
                    "chunk_title": section.title,
                    "chunk_index": chunk_index,
                    "content_hash": content_hash,
                    "chunk_hash": text_hash,
                    "text_hash": text_hash,
                    "chunker_version": current_chunker_version,
                    "chunk_max_chars": chunk_max_chars,
                    "chunk_overlap": chunk_overlap,
                    "embedding_token_count": embedding_token_count,
                    "embedding_token_limit": token_budget.max_tokens,
                },
            }
        )
        chunk_index += 1
    return records


def build_records(
    root: Path,
    source_id: str = "local",
    chunk_max_chars: int = 1400,
    chunk_overlap: int = 160,
    document_token_budget: DocumentTokenBudget | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scope = resolve_ingestion_scope(root)
    token_budget = document_token_budget or get_document_token_budget()

    for path in iter_input_files(scope.scan_root):
        stored = scope.file(path)
        try:
            records.extend(
                build_records_for_file(
                    scope.logical_root,
                    path,
                    source_id=source_id,
                    chunk_max_chars=chunk_max_chars,
                    chunk_overlap=chunk_overlap,
                    ingestion_scope=scope,
                    document_token_budget=token_budget,
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "path": stored.stored_path,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

    return records, errors
