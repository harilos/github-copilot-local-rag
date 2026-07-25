from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .extractors import SUPPORTED_EXTENSIONS, extract_sections


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_content_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def iter_input_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


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
) -> list[dict[str, Any]]:
    root = root.resolve()
    path = path.resolve()
    rel = str(path.relative_to(root))
    sections = extract_sections(path)
    content_hash = content_hash or file_content_hash(path)
    doc_id = sha256_text(f"{source_id}:{rel}:{content_hash}")

    records: list[dict[str, Any]] = []
    chunk_index = 0
    for section in sections:
        text = section.text.strip()
        if not text:
            continue
        text_hash = sha256_text(text)
        chunk_id = sha256_text(f"{doc_id}:jp-sw-v1:{chunk_index}")
        embedding_text = f"{rel}\n{section.title}\n{text}"
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
                    "source_type": _source_type(path),
                    "path": rel,
                    "uri": str(path),
                    "title": path.name,
                    "section_path": section.title,
                    "language": _language(path),
                    "root": str(root),
                    "chunk_title": section.title,
                    "content_hash": content_hash,
                    "chunk_hash": text_hash,
                    "text_hash": text_hash,
                    "chunker_version": "jp-sw-v1",
                },
            }
        )
        chunk_index += 1
    return records


def build_records(root: Path, source_id: str = "local") -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    root = root.resolve()

    for path in iter_input_files(root):
        rel = str(path.relative_to(root))
        try:
            records.extend(build_records_for_file(root, path, source_id=source_id))
        except Exception as exc:
            errors.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue

    return records, errors
