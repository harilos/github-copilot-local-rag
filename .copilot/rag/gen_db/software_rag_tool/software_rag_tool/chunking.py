from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


DEFAULT_CHUNK_TARGET_TOKENS = 320
DEFAULT_CHUNK_OVERLAP_TOKENS = 48


class DocumentTokenBudget(Protocol):
    target_tokens: int
    max_tokens: int

    def count_document(self, path: str, title: str, text: str) -> int: ...

    def count_body(self, text: str) -> int: ...


@dataclass(frozen=True)
class TextSection:
    title: str
    text: str
    source_start: int = 0
    source_end: int = 0


class DocumentTokenLimitError(RuntimeError):
    """Raised when document context leaves no room for source text."""


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    title: str,
    text: str,
    max_chars: int = 1400,
    overlap: int = 160,
    *,
    token_budget: DocumentTokenBudget | None = None,
    embedding_path: str = "",
    overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
) -> list[TextSection]:
    """Split normalized text without exceeding the final document token budget.

    ``max_chars`` and ``overlap`` retain their historical character units and
    act as additional ceilings. Production ingestion supplies ``token_budget``;
    the tokenizer-free path remains only for callers that do not create Dense
    document embeddings.
    """

    text = normalize_text(text)
    if not text:
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0:
        raise ValueError("overlap must be zero or positive")
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")
    if token_budget is None:
        return _chunk_text_by_characters(title, text, max_chars, overlap)
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must be zero or positive")
    return _chunk_text_by_tokens(
        title,
        text,
        max_chars=max_chars,
        overlap_chars=overlap,
        overlap_tokens=overlap_tokens,
        token_budget=token_budget,
        embedding_path=embedding_path,
    )


def _chunk_text_by_characters(
    title: str,
    text: str,
    max_chars: int,
    overlap: int,
) -> list[TextSection]:
    if len(text) <= max_chars:
        return [
            TextSection(
                title=title,
                text=text,
                source_start=0,
                source_end=len(text),
            )
        ]

    chunks: list[TextSection] = []
    start = 0
    index = 1
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            end = _select_semantic_boundary(text, start, end)
        part, part_start, part_end = _trimmed_slice(text, start, end)
        if part:
            chunks.append(
                TextSection(
                    title=f"{title} #{index}",
                    text=part,
                    source_start=part_start,
                    source_end=part_end,
                )
            )
            index += 1
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        start = next_start if next_start < end else end
    return chunks


def _chunk_text_by_tokens(
    title: str,
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
    overlap_tokens: int,
    token_budget: DocumentTokenBudget,
    embedding_path: str,
) -> list[TextSection]:
    if (
        len(text) <= max_chars
        and token_budget.count_document(embedding_path, title, text)
        <= token_budget.target_tokens
    ):
        return [
            TextSection(
                title=title,
                text=text,
                source_start=0,
                source_end=len(text),
            )
        ]

    chunks: list[TextSection] = []
    start = 0
    index = 1
    while start < len(text):
        chunk_title = f"{title} #{index}"
        char_end = min(len(text), start + max_chars)
        end = _furthest_fitting_end(
            text,
            start,
            char_end,
            path=embedding_path,
            title=chunk_title,
            limit=token_budget.target_tokens,
            token_budget=token_budget,
        )
        if end <= start:
            end = _furthest_fitting_end(
                text,
                start,
                char_end,
                path=embedding_path,
                title=chunk_title,
                limit=token_budget.max_tokens,
                token_budget=token_budget,
            )
        if end <= start:
            context_tokens = token_budget.count_document(
                embedding_path,
                chunk_title,
                "",
            )
            raise DocumentTokenLimitError(
                "document path and section title leave no room for body text: "
                f"context_tokens={context_tokens} limit={token_budget.max_tokens}"
            )
        if end < len(text):
            end = _select_semantic_boundary(text, start, end)

        part, part_start, part_end = _trimmed_slice(text, start, end)
        if not part:
            start = end
            continue
        token_count = token_budget.count_document(
            embedding_path,
            chunk_title,
            part,
        )
        if token_count > token_budget.max_tokens:
            raise DocumentTokenLimitError(
                "token-safe splitter produced an oversized document input: "
                f"tokens={token_count} limit={token_budget.max_tokens}"
            )
        chunks.append(
            TextSection(
                title=chunk_title,
                text=part,
                source_start=part_start,
                source_end=part_end,
            )
        )
        index += 1
        if end >= len(text):
            break
        start = _next_start_with_overlap(
            text,
            start,
            end,
            overlap_chars=overlap_chars,
            overlap_tokens=overlap_tokens,
            token_budget=token_budget,
        )

    if len(chunks) == 1:
        only = chunks[0]
        if (
            token_budget.count_document(embedding_path, title, only.text)
            <= token_budget.max_tokens
        ):
            chunks[0] = TextSection(
                title=title,
                text=only.text,
                source_start=only.source_start,
                source_end=only.source_end,
            )
    return chunks


def _furthest_fitting_end(
    text: str,
    start: int,
    end: int,
    *,
    path: str,
    title: str,
    limit: int,
    token_budget: DocumentTokenBudget,
) -> int:
    low = start
    high = end
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[start:middle].strip()
        if candidate and token_budget.count_document(path, title, candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    while low > start:
        candidate = text[start:low].strip()
        if candidate and token_budget.count_document(path, title, candidate) <= limit:
            break
        low -= 1
    return low


def _select_semantic_boundary(text: str, start: int, end: int) -> int:
    if end >= len(text):
        return end
    minimum = start + max(1, (end - start) // 2)

    paragraph = text.rfind("\n\n", minimum, end)
    if paragraph >= minimum:
        return paragraph + 2

    sentence_end = -1
    for match in re.finditer(r"[。！？.!?](?=\s|$)", text[minimum:end]):
        sentence_end = minimum + match.end()
    if sentence_end > minimum:
        return sentence_end

    line = text.rfind("\n", minimum, end)
    if line >= minimum:
        return line + 1
    return end


def _next_start_with_overlap(
    text: str,
    chunk_start: int,
    end: int,
    *,
    overlap_chars: int,
    overlap_tokens: int,
    token_budget: DocumentTokenBudget,
) -> int:
    if overlap_chars == 0 or overlap_tokens == 0:
        return end
    lower = max(chunk_start + 1, end - overlap_chars)
    low = lower
    high = end
    while low < high:
        middle = (low + high) // 2
        if token_budget.count_body(text[middle:end]) <= overlap_tokens:
            high = middle
        else:
            low = middle + 1
    next_start = low
    while (
        next_start < end
        and token_budget.count_body(text[next_start:end]) > overlap_tokens
    ):
        next_start += 1
    if next_start <= chunk_start:
        return end
    return next_start


def _trimmed_slice(text: str, start: int, end: int) -> tuple[str, int, int]:
    raw = text[start:end]
    left = len(raw) - len(raw.lstrip())
    right = len(raw.rstrip())
    return raw[left:right], start + left, start + right
