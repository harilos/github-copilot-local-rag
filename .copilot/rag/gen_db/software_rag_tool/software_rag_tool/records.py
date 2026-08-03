from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
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
from .extractors import (
    SUPPORTED_EXTENSIONS,
    extract_document,
    sections_from_extraction,
)
from .ingestion_paths import IngestionScope, resolve_ingestion_scope
from .pipeline import build_pipeline_contract
from .structured_extraction import ExtractionResult


@dataclass(frozen=True)
class FileBuildResult:
    records: list[dict[str, Any]]
    extraction: ExtractionResult
    pipeline: dict[str, Any]


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
        "document_prefix": document_token_budget.document_prefix,
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
        "document_prefix",
    )
    return ":".join(f"{key}={config[key]}" for key in keys)


def is_office_temporary_file(path: Path | str) -> bool:
    """Return whether *path* is an Office owner/lock file.

    Office creates sibling files whose basename starts with the exact ``~$``
    prefix while a document is open.  They are not source documents and may
    be unreadable even when the real document is healthy.
    """

    return Path(path).name.startswith("~$")


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
            if is_office_temporary_file(filename):
                continue
            path = Path(directory) / filename
            # ``os.walk`` already classified this name as a non-directory.
            # Keep it as a candidate even if it disappears or becomes
            # unreadable immediately afterwards so incremental ingestion can
            # record a retryable per-file error instead of treating it as a
            # confirmed deletion.
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
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
    extraction_result: ExtractionResult | None = None,
    pipeline_contract: dict[str, Any] | None = None,
    backend_policy: str = "legacy",
    return_result: bool = False,
) -> list[dict[str, Any]] | FileBuildResult:
    result = build_file_result(
        root,
        path,
        source_id=source_id,
        content_hash=content_hash,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        ingestion_scope=ingestion_scope,
        document_token_budget=document_token_budget,
        extraction_result=extraction_result,
        pipeline_contract=pipeline_contract,
        backend_policy=backend_policy,
    )
    return result if return_result else result.records


def build_file_result(
    root: Path,
    path: Path,
    source_id: str = "local",
    content_hash: str | None = None,
    chunk_max_chars: int = 1400,
    chunk_overlap: int = 160,
    ingestion_scope: IngestionScope | None = None,
    document_token_budget: DocumentTokenBudget | None = None,
    extraction_result: ExtractionResult | None = None,
    pipeline_contract: dict[str, Any] | None = None,
    backend_policy: str = "legacy",
) -> FileBuildResult:
    scope = ingestion_scope or resolve_ingestion_scope(root)
    stored = scope.file(path)
    token_budget = document_token_budget or get_document_token_budget()
    config = chunker_config(
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        document_token_budget=token_budget,
    )
    active_pipeline = pipeline_contract or build_pipeline_contract(
        chunker=config,
        backend_policy=backend_policy,
    )
    extraction = extraction_result or extract_document(
        stored.resolved_path,
        backend_policy=backend_policy,
    )
    sections = sections_from_extraction(
        extraction,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        token_budget=token_budget,
        embedding_path=stored.stored_path,
    )
    content_hash = content_hash or file_content_hash(stored.resolved_path)
    fingerprint = str(active_pipeline["fingerprint"])
    doc_id = sha256_text(
        f"{source_id}:{stored.stored_path}:{content_hash}:{fingerprint}"
    )
    current_chunker_version = chunker_version(config)

    records: list[dict[str, Any]] = []
    chunk_index = 0
    for section in sections:
        text = section.text
        if not text.strip():
            continue
        text_hash = sha256_text(text)
        chunk_id = sha256_text(
            f"{doc_id}:{current_chunker_version}:{fingerprint}:{chunk_index}"
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
                    "source_format": section.source_format,
                    "structure_origin": section.structure_origin,
                    "structure_kind": section.structure_kind,
                    "structure_id": section.structure_id,
                    "parent_section_id": section.parent_section_id,
                    "breadcrumb": section.breadcrumb,
                    "source_start": section.source_start,
                    "source_end": section.source_end,
                    "page": section.page,
                    "slide": section.slide,
                    "sheet": section.sheet,
                    "extractor_backend": extraction.backend,
                    "extractor_version": extraction.backend_version,
                    "extraction_status": extraction.status,
                    "extraction_fallback_reason": extraction.fallback_reason,
                    "encoding": extraction.encoding,
                    "encoding_reason": extraction.encoding_reason,
                    "encoding_replacement_count": extraction.replacement_count,
                    "pipeline_fingerprint": fingerprint,
                    "embedding_token_count": embedding_token_count,
                    "embedding_token_limit": token_budget.max_tokens,
                },
            }
        )
        chunk_index += 1
    return FileBuildResult(
        records=records,
        extraction=extraction,
        pipeline=active_pipeline,
    )


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
            result = build_file_result(
                    scope.logical_root,
                    path,
                    source_id=source_id,
                    chunk_max_chars=chunk_max_chars,
                    chunk_overlap=chunk_overlap,
                    ingestion_scope=scope,
                    document_token_budget=token_budget,
                )
            if not result.extraction.is_indexed:
                errors.append(
                    {
                        "path": stored.stored_path,
                        "error": (
                            f"{result.extraction.status}:"
                            f"{result.extraction.reason}"
                        ),
                    }
                )
                continue
            records.extend(
                result.records
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
