from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Iterable


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]*")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")
_QUOTED_RE = re.compile(r"[`\"'「『](.{2,120}?)[`\"'」』]")
_ANCHOR_RE = re.compile(
    r"https?://[^\s)>\]}]+"
    r"|/[A-Za-z0-9_./:-]{2,}"
    r"|[A-Za-z0-9_.:/-]+\.(?:md|txt|log|pdf|docx?|pptx?|xlsx|json|ya?ml|toml|ini|py|js|jsx|ts|tsx|java|go|rs|cs|sql)"
    r"|[Rr][Ff][Cc] \d{2,}"
    r"|[A-Z]{2,}-\d{2,}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{12,}"
    r"|[A-Za-z]+[0-9][A-Za-z0-9_-]*"
    r"|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"|[A-Z]{2,}[A-Za-z0-9_]*"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:[.:/-][A-Za-z0-9_]+)+"
    r"|[A-Z]?[a-z]+(?:[A-Z][A-Za-z0-9]+){1,}"
)
_STOP_TOKENS = {
    "する",
    "した",
    "して",
    "され",
    "ある",
    "いる",
    "です",
    "ます",
    "れる",
    "られ",
    "こと",
    "もの",
    "ため",
    "これ",
    "それ",
    "どれ",
    "この",
    "その",
}


def canonicalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def identifier_match_keys(text: str) -> list[str]:
    """Return conservative formatting variants for identifier comparison."""
    canonical = canonicalize(text)
    keys = [canonical]
    rfc = re.fullmatch(r"rfc ?(\d{2,})", canonical)
    if rfc:
        keys.extend([f"rfc {rfc.group(1)}", f"rfc{rfc.group(1)}"])
    return _unique(key for key in keys if key)


def tokenizer_fingerprint() -> str:
    return "sudachi-a-v1" if _sudachi() else "fallback-cjk-ngram-v1"


def tokenize_for_fts(text: str, *, max_tokens: int | None = None) -> str:
    tokens = tokens_for_fts(text, max_tokens=max_tokens)
    return " ".join(tokens)


def tokens_for_fts(text: str, *, max_tokens: int | None = None) -> list[str]:
    tokens = _sudachi_tokens(text)
    if not tokens:
        tokens = _fallback_tokens(text)
    output = _unique(_clean_token_stream(tokens))
    if max_tokens is not None:
        return output[:max_tokens]
    return output


def extract_anchors(text: str, *, limit: int = 200) -> list[str]:
    found: list[str] = []
    for match in _QUOTED_RE.finditer(text or ""):
        value = match.group(1).strip()
        if _looks_anchor_like(value):
            found.append(value)
    for match in _ANCHOR_RE.finditer(text or ""):
        found.append(match.group(0).strip())
    return _unique(anchor for anchor in found if 2 <= len(anchor) <= 200)[:limit]


def identifier_aliases(identifier: str) -> list[str]:
    raw = identifier.strip()
    canonical = canonicalize(raw)
    parts: list[str] = [raw, canonical]
    parts.extend(re.split(r"[^A-Za-z0-9]+", raw))
    parts.extend(_split_camel(raw))
    parts.extend(_ASCII_WORD_RE.findall(canonical))
    return _unique(part for part in parts if 2 <= len(part) <= 120)


def fts_query_from_tokens(tokens: Iterable[str], *, operator: str = "OR", max_terms: int = 24) -> str:
    terms = [_quote_fts_term(token) for token in _unique(tokens) if token][:max_terms]
    if not terms:
        return ""
    glue = f" {operator} "
    return glue.join(terms)


def phrase_queries(text: str, *, max_phrases: int = 5) -> list[str]:
    phrases: list[str] = []
    for match in _QUOTED_RE.finditer(text or ""):
        tokens = tokens_for_fts(match.group(1), max_tokens=8)
        if len(tokens) >= 2:
            phrases.append(" ".join(_quote_fts_term(token) for token in tokens))
    return phrases[:max_phrases]


@lru_cache(maxsize=1)
def _sudachi() -> object | None:
    try:
        from sudachipy import dictionary, tokenizer

        return (dictionary.Dictionary().create(), tokenizer.Tokenizer.SplitMode.A)
    except Exception:
        try:
            from sudachipy import Dictionary, SplitMode

            return (Dictionary().create(), SplitMode.A)
        except Exception:
            return None


def _sudachi_tokens(text: str) -> list[str]:
    loaded = _sudachi()
    if not loaded:
        return []
    tokenizer_obj, mode = loaded
    tokens: list[str] = []
    for morpheme in tokenizer_obj.tokenize(text or "", mode):  # type: ignore[attr-defined]
        normalized = morpheme.normalized_form().strip()
        if normalized:
            tokens.append(normalized)
    return tokens


def _fallback_tokens(text: str) -> list[str]:
    canonical = canonicalize(text)
    tokens: list[str] = []
    tokens.extend(_ASCII_WORD_RE.findall(canonical))
    for segment in _CJK_RE.findall(canonical):
        if len(segment) <= 3:
            tokens.append(segment)
            continue
        tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        tokens.extend(segment[i : i + 3] for i in range(len(segment) - 2))
    return tokens


def _clean_token_stream(tokens: Iterable[str]) -> Iterable[str]:
    for token in tokens:
        for part in re.findall(r"[0-9A-Za-z_]+|[\u3040-\u30ff\u3400-\u9fff]+", canonicalize(token)):
            if len(part) >= 2 and part not in _STOP_TOKENS:
                yield part


def _quote_fts_term(token: str) -> str:
    safe = token.replace('"', '""')
    return f'"{safe}"'


def _split_camel(value: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return _ASCII_WORD_RE.findall(spaced)


def _looks_anchor_like(value: str) -> bool:
    if any(ch in value for ch in "/._:-"):
        return True
    if any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value):
        return True
    return bool(re.search(r"[A-Z]{2,}", value))


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        value = value.strip()
        if not value:
            continue
        key = canonicalize(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output
