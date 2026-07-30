from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable


FILE_SELECTION_ENV = "LOCAL_RAG_FILE_SELECTION"
FILE_SELECTION_ALL = "all_supported"
FILE_SELECTION_DOCUMENTS = "documents_only"

# This is intentionally a subset of the full extractor allowlist.  It keeps
# ordinary office/text/design artefacts while excluding source code and config.
DOCUMENT_ONLY_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".log",
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xlsx",
        ".asta",
        ".pu",
        ".puml",
        ".plantuml",
    }
)

_EXTRA_TEXT_EXTENSIONS = frozenset({".pu", ".puml", ".plantuml"})
_EXTRA_EXTENSIONS = frozenset({".asta", *_EXTRA_TEXT_EXTENSIONS})
_INSTALL_MARKER = "_local_rag_document_extensions_installed"
_ASTAH_READ_LIMIT = 32 * 1024 * 1024
_ASTAH_TEXT_LIMIT = 500_000


def install_document_extension_runtime() -> None:
    """Extend extraction and optionally restrict discovery to document files."""

    from . import extractors, records

    if bool(getattr(records, _INSTALL_MARKER, False)):
        return

    extractors.SUPPORTED_EXTENSIONS.update(_EXTRA_EXTENSIONS)
    original_extract = extractors.extract_sections
    original_iter = records.iter_input_files
    original_source_type = records._source_type
    original_language = records._language

    def extract_sections(
        path: Path,
        *,
        chunk_max_chars: int = 1400,
        chunk_overlap: int = 160,
    ) -> list[Any]:
        extension = Path(path).suffix.lower()
        if extension in _EXTRA_TEXT_EXTENSIONS:
            return extractors._extract_plain(
                Path(path),
                chunk_max_chars=chunk_max_chars,
                chunk_overlap=chunk_overlap,
            )
        if extension == ".asta":
            return _extract_astah_project(
                Path(path),
                chunk_text=extractors.chunk_text,
                chunk_max_chars=chunk_max_chars,
                chunk_overlap=chunk_overlap,
            )
        return original_extract(
            Path(path),
            chunk_max_chars=chunk_max_chars,
            chunk_overlap=chunk_overlap,
        )

    def iter_input_files(root: Path) -> Iterable[Path]:
        values = original_iter(Path(root))
        selection = str(os.getenv(FILE_SELECTION_ENV) or FILE_SELECTION_ALL).strip()
        if selection != FILE_SELECTION_DOCUMENTS:
            yield from values
            return
        for path in values:
            if path.suffix.lower() in DOCUMENT_ONLY_EXTENSIONS:
                yield path

    def source_type(path: Path) -> str:
        if Path(path).suffix.lower() in _EXTRA_EXTENSIONS:
            return "docs"
        return original_source_type(Path(path))

    def language(path: Path) -> str:
        extension = Path(path).suffix.lower()
        if extension in _EXTRA_TEXT_EXTENSIONS:
            return "plantuml"
        if extension == ".asta":
            return "astah"
        return original_language(Path(path))

    extractors.extract_sections = extract_sections
    records.extract_sections = extract_sections
    records.SUPPORTED_EXTENSIONS = extractors.SUPPORTED_EXTENSIONS
    records.iter_input_files = iter_input_files
    records._source_type = source_type
    records._language = language
    setattr(records, _INSTALL_MARKER, True)


def _extract_astah_project(
    path: Path,
    *,
    chunk_text: Any,
    chunk_max_chars: int,
    chunk_overlap: int,
) -> list[Any]:
    """Extract bounded readable labels from an Astah binary project.

    Astah's supported interchange path requires an Astah installation.  Local
    RAG must remain copy-deployable, so this fallback indexes the file name and
    readable strings embedded in the project without adding an external tool
    dependency.  The original .asta file remains the Source of truth.
    """

    with path.open("rb") as stream:
        data = stream.read(_ASTAH_READ_LIMIT + 1)
    truncated = len(data) > _ASTAH_READ_LIMIT
    if truncated:
        data = data[:_ASTAH_READ_LIMIT]

    candidates: list[str] = []
    candidates.extend(
        match.decode("ascii", errors="ignore")
        for match in re.findall(rb"[\x20-\x7e]{4,}", data)
    )
    candidates.extend(
        match.decode("utf-16-le", errors="ignore")
        for match in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", data)
    )
    # Modified UTF-8 and ordinary UTF-8 labels can still be recovered this way.
    decoded = data.decode("utf-8", errors="ignore")
    candidates.extend(
        part
        for part in re.split(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", decoded)
        if len(part.strip()) >= 4
    )

    visible: list[str] = []
    seen: set[str] = set()
    used = 0
    for raw in candidates:
        text = " ".join(str(raw).split()).strip()
        if not _useful_astah_text(text) or text in seen:
            continue
        seen.add(text)
        if used + len(text) + 1 > _ASTAH_TEXT_LIMIT:
            truncated = True
            break
        visible.append(text)
        used += len(text) + 1

    heading = [
        f"Astah project file: {path.name}",
        "Format: .asta",
        "Extracted content: embedded readable model and diagram labels",
    ]
    if truncated:
        heading.append("Note: extraction was bounded; the original project may contain more data.")
    if not visible:
        heading.append("No readable model labels were recovered; the file name remains indexed.")
    text = "\n".join([*heading, *visible])
    return chunk_text(
        path.name,
        text,
        max_chars=chunk_max_chars,
        overlap=chunk_overlap,
    )


def _useful_astah_text(value: str) -> bool:
    if len(value) < 4 or len(value) > 4_096:
        return False
    if value.startswith(("PK\x03\x04", "java.")) and " " not in value:
        return False
    meaningful = sum(
        character.isalnum()
        or "\u3040" <= character <= "\u30ff"
        or "\u4e00" <= character <= "\u9fff"
        for character in value
    )
    return meaningful >= 2


__all__ = [
    "DOCUMENT_ONLY_EXTENSIONS",
    "FILE_SELECTION_ALL",
    "FILE_SELECTION_DOCUMENTS",
    "FILE_SELECTION_ENV",
    "install_document_extension_runtime",
]
