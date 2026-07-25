from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TextSection:
    title: str
    text: str


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(title: str, text: str, max_chars: int = 1400, overlap: int = 160) -> list[TextSection]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [TextSection(title=title, text=text)]

    chunks: list[TextSection] = []
    start = 0
    index = 1
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            split = max(text.rfind("\n\n", start, end), text.rfind("。", start, end), text.rfind(". ", start, end))
            if split > start + max_chars // 2:
                end = split + 1
        part = text[start:end].strip()
        if part:
            suffix = "" if len(text) <= max_chars else f" #{index}"
            chunks.append(TextSection(title=f"{title}{suffix}", text=part))
            index += 1
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks
