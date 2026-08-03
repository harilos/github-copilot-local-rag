from __future__ import annotations

import hashlib
import json
from typing import Any

from .embeddings import embedding_fingerprint
from .structured_extraction import (
    DOCLING_OPTIONS_VERSION,
    DOCLING_PIN,
    EXTRACTION_STATUS_SCHEMA_VERSION,
    MARKDOWN_PARSER_VERSION,
    PLAIN_TEXT_ENCODING_POLICY_VERSION,
    XLSX_PARSER_VERSION,
    docling_pdf_artifacts_identity,
    docling_pdf_artifacts_path,
    package_version,
)
from .tokenize import tokenizer_fingerprint


PIPELINE_DESCRIPTOR_VERSION = "local-rag-pipeline-v1"
DEFAULT_EXTRACTOR_BACKEND_POLICY = "legacy"


def pipeline_descriptor(
    *,
    chunker: dict[str, Any],
    backend_policy: str = DEFAULT_EXTRACTOR_BACKEND_POLICY,
    lexical_tokenizer: str | None = None,
) -> dict[str, Any]:
    """Describe every setting that can change persisted retrieval content."""

    return {
        "schema": PIPELINE_DESCRIPTOR_VERSION,
        "extractor_backend_policy": backend_policy,
        "format_parsers": {
            "markdown": MARKDOWN_PARSER_VERSION,
            "plain_text": PLAIN_TEXT_ENCODING_POLICY_VERSION,
            "xlsx": XLSX_PARSER_VERSION,
            "pdf_docx_pptx": DOCLING_OPTIONS_VERSION,
            "legacy_office": "libreoffice-text-strict-source-range-v2",
        },
        "docling": {
            "required_version": DOCLING_PIN,
            "installed_version": package_version("docling"),
            "options": DOCLING_OPTIONS_VERSION,
            "device": "cpu",
            "ocr": False,
            "network_download": False,
            "pdf_artifacts_policy": "explicit_local_path_required",
            "pdf_artifacts_configured": (
                docling_pdf_artifacts_path() is not None
            ),
            "pdf_artifacts_identity": docling_pdf_artifacts_identity(),
        },
        "encoding_policy": PLAIN_TEXT_ENCODING_POLICY_VERSION,
        "extraction_status_schema": EXTRACTION_STATUS_SCHEMA_VERSION,
        "chunker": dict(chunker),
        "tokenizer": lexical_tokenizer or _runtime_tokenizer_fingerprint(),
        "embedding": embedding_fingerprint(),
    }


def pipeline_fingerprint(descriptor: dict[str, Any]) -> str:
    canonical = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def build_pipeline_contract(
    *,
    chunker: dict[str, Any],
    backend_policy: str = DEFAULT_EXTRACTOR_BACKEND_POLICY,
    lexical_tokenizer: str | None = None,
) -> dict[str, Any]:
    descriptor = pipeline_descriptor(
        chunker=chunker,
        backend_policy=backend_policy,
        lexical_tokenizer=lexical_tokenizer,
    )
    return {
        "fingerprint": pipeline_fingerprint(descriptor),
        "descriptor": descriptor,
    }


def _runtime_tokenizer_fingerprint() -> str:
    try:
        return tokenizer_fingerprint()
    except Exception:
        # Production ingestion resolves and passes the tokenizer before any
        # write.  This sentinel keeps pure/unit callers deterministic without
        # turning descriptor construction into a hidden dependency import.
        return "unavailable-unvalidated"
