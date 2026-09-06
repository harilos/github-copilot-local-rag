from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .jsonl import read_jsonl
from .paths import clean_dir, output_root
from .atomic_io import atomic_write_bytes


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[一-龯ぁ-んァ-ヴー]{2,}")
STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "can",
    "for",
    "from",
    "have",
    "into",
    "more",
    "not",
    "of",
    "that",
    "than",
    "the",
    "this",
    "use",
    "with",
    "which",
    "figure",
    "file",
    "page",
    "pdf",
    "sample",
    "source",
    "table",
    "この",
    "その",
    "ため",
    "について",
    "など",
    "また",
    "する",
    "ある",
    "いる",
    "及び",
}
AUTO_PROFILE_START = "<!-- local-rag:auto-profile:start -->"
AUTO_PROFILE_END = "<!-- local-rag:auto-profile:end -->"


def update_profile_from_clean(*, max_records: int = 2000, max_terms: int = 24) -> bool:
    records = _load_sample_records(max_records=max_records)
    if not records:
        return False

    terms = _top_terms(records, max_terms=max_terms)
    source_ids = _top_metadata_values(records, "source_id", limit=5)
    titles = _top_metadata_values(records, "title", limit=8)
    title = _db_title()
    profile_path = output_root() / "DB_PROFILE.md"
    auto_profile = (
        f"- Frequent terms: {', '.join(terms) if terms else 'n/a'}\n"
        f"- Source IDs: {', '.join(source_ids) if source_ids else 'n/a'}\n"
        f"- Representative files: {', '.join(titles) if titles else 'n/a'}\n"
    )
    if profile_path.exists():
        original = profile_path.read_bytes()
        bom = original.startswith(b"\xef\xbb\xbf")
        existing = original[3:].decode("utf-8", errors="strict") if bom else original.decode(
            "utf-8", errors="strict"
        )
    else:
        bom = False
        existing = (
            f"# {title}\n\n## Query Hint\n\n"
            f"{_default_query_hint(terms=terms, source_ids=source_ids, titles=titles)}\n"
        )
    newline = "\r\n" if "\r\n" in existing else "\n"
    replacement = newline.join(
        (
            AUTO_PROFILE_START,
            "## Auto Profile",
            "",
            auto_profile.strip().replace("\n", newline),
            AUTO_PROFILE_END,
        )
    ) + newline
    updated = _replace_owned_auto_profile(existing, replacement, newline)
    payload = updated.encode("utf-8")
    if bom:
        payload = b"\xef\xbb\xbf" + payload
    atomic_write_bytes(profile_path, payload)
    return True


def _replace_owned_auto_profile(text: str, replacement: str, newline: str) -> str:
    start_count = text.count(AUTO_PROFILE_START)
    end_count = text.count(AUTO_PROFILE_END)
    if start_count or end_count:
        if start_count != 1 or end_count != 1:
            raise ValueError("DB_PROFILE has ambiguous Local RAG owned blocks")
        start = text.index(AUTO_PROFILE_START)
        end = text.index(AUTO_PROFILE_END, start) + len(AUTO_PROFILE_END)
        if end < start:
            raise ValueError("DB_PROFILE Local RAG owned block is invalid")
        if text[end : end + 2] == "\r\n":
            end += 2
        elif text[end : end + 1] == "\n":
            end += 1
        return text[:start] + replacement + text[end:]

    headings = list(re.finditer(r"(?m)^## Auto Profile[ \t]*(?:\r?\n|$)", text))
    if len(headings) > 1:
        raise ValueError("DB_PROFILE has ambiguous Auto Profile sections")
    if headings:
        start = headings[0].start()
        next_heading = re.search(r"(?m)^## [^\r\n]+(?:\r?\n|$)", text[headings[0].end() :])
        end = (
            headings[0].end() + next_heading.start()
            if next_heading
            else len(text)
        )
        return text[:start] + replacement + text[end:]
    separator = "" if not text or text.endswith(("\n", "\r")) else newline
    if text and not text.endswith(newline * 2):
        separator += newline
    return text + separator + replacement


def _load_sample_records(*, max_records: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    directory = clean_dir() / "records"
    if not directory.exists():
        return records
    for path in sorted(directory.rglob("*.jsonl")):
        for record in read_jsonl(path):
            records.append(record)
            if len(records) >= max_records:
                return records
    return records


def _top_terms(records: list[dict[str, Any]], *, max_terms: int) -> list[str]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for record in records:
        metadata = record.get("metadata") or {}
        weighted_text = " ".join(
            [
                str(metadata.get("title") or ""),
                str(metadata.get("section_path") or ""),
                str(record.get("text") or "")[:2000],
            ]
        )
        for token in TOKEN_RE.findall(weighted_text):
            normalized = _normalize_token(token)
            if not _is_useful_token(normalized):
                continue
            counts[normalized] += 1
            display.setdefault(normalized, token)
    return [display[token] for token, _ in counts.most_common(max_terms)]


def _top_metadata_values(records: list[dict[str, Any]], key: str, *, limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    for record in records:
        metadata = record.get("metadata") or {}
        value = str(metadata.get(key) or "").strip()
        if value:
            counts[value] += 1
    return [value for value, _ in counts.most_common(limit)]


def _default_query_hint(*, terms: list[str], source_ids: list[str], titles: list[str]) -> str:
    parts: list[str] = []
    if terms:
        parts.append(f"このDBは、主に「{'、'.join(terms[:12])}」などに関する文書を検索するためのRAGです。")
    else:
        parts.append("このDBは、作成時に指定された文書を検索するためのRAGです。")
    if source_ids:
        parts.append(f"主なsource-id: {', '.join(source_ids)}。")
    if titles:
        parts.append(f"代表的なファイル: {', '.join(titles[:5])}。")
    parts.append("回答では検索結果の根拠IDとsource locationを引用してください。")
    parts.append("根拠が不足する場合は断定しないでください。")
    return " ".join(parts)


def _section_body(text: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _profile_title(text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    return match.group(1).strip() if match else fallback


def _db_title() -> str:
    config_path = output_root() / "db.json"
    if not config_path.exists():
        return output_root().name
    try:
        import json

        config = json.loads(config_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return output_root().name
    return str(config.get("title") or config.get("db_name") or output_root().name)


def _normalize_token(token: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_+-]+", token):
        return token.casefold()
    return token


def _is_useful_token(token: str) -> bool:
    if len(token) < 2:
        return False
    if token.casefold() in STOPWORDS:
        return False
    if "_" in token or "/" in token or "\\" in token:
        return False
    if token.isdigit():
        return False
    return True
