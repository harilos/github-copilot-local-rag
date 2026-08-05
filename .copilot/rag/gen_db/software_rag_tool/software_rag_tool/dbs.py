from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_EMBEDDING_MODEL
from .embeddings import embedding_fingerprint
from .tokenize import tokenizer_fingerprint, tokenizer_runtime_descriptor

DB_NAME_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]*-rag)(?![A-Za-z0-9_.-])")


@dataclass(frozen=True)
class DbResolution:
    db_name: str | None
    triggered: bool
    reason: str
    candidates: list[str]


def is_db_name(value: str) -> bool:
    return bool(DB_NAME_RE.fullmatch(value.strip()))


def require_db_name(value: str) -> str:
    name = value.strip()
    if not is_db_name(name):
        raise ValueError("DB name must match '<name>-rag'")
    return name


def list_db_names(dbs_root: Path) -> list[str]:
    if not dbs_root.exists():
        return []
    return sorted(
        p.name
        for p in dbs_root.iterdir()
        if p.is_dir() and not p.is_symlink() and is_db_name(p.name)
    )


def extract_db_name(text: str) -> str | None:
    match = DB_NAME_RE.search(text)
    if not match:
        return None
    return match.group(1)


def is_natural_rag_request(text: str) -> bool:
    normalized = text.lower()
    triggers = [
        "rag",
        "ローカル資料",
        "資料から",
        "ドキュメントから",
        "ナレッジ",
        "過去の",
        "設計書",
        "運用手順",
        "根拠",
        "検索して",
        "参照して",
        "関連情報",
    ]
    return any(trigger.lower() in normalized for trigger in triggers)


def resolve_db_name(text: str, explicit_db: str | None, dbs_root: Path, auto: bool) -> DbResolution:
    candidates = list_db_names(dbs_root)
    if explicit_db:
        return DbResolution(require_db_name(explicit_db), True, "explicit --db", candidates)

    embedded = extract_db_name(text)
    if embedded:
        return DbResolution(embedded, True, "db name in request", candidates)

    if not auto or not is_natural_rag_request(text):
        return DbResolution(None, False, "no explicit db name or natural-language RAG trigger", candidates)

    if len(candidates) == 1:
        return DbResolution(candidates[0], True, "natural-language trigger with a single available db", candidates)

    if not candidates:
        return DbResolution(None, True, "natural-language trigger but no db exists", candidates)

    return DbResolution(None, True, "natural-language trigger but multiple dbs exist", candidates)


DEFAULT_QUERY_HINT = (
    "このDBの内容は作成時に指定された文書に依存します。"
    "回答では検索結果の根拠IDとsource locationを引用してください。"
    "根拠が不足する場合は断定しないでください。"
)


def ensure_db_layout(dbs_root: Path, db_name: str, title: str | None = None, query_hint: str | None = None) -> Path:
    name = require_db_name(db_name)
    root = dbs_root / name
    for rel in ["data/raw", "data/clean", "index", "logs"]:
        (root / rel).mkdir(parents=True, exist_ok=True)

    config_path = root / "db.json"
    if not config_path.exists():
        config = {
            "db_name": name,
            "title": title or name,
            "collection": collection_name_for_db(name),
            "model": DEFAULT_EMBEDDING_MODEL,
            "profile": "DB_PROFILE.md",
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    profile_path = root / "DB_PROFILE.md"
    if not profile_path.exists():
        profile_path.write_text(
            f"# {title or name}\n\n"
            "## Query Hint\n\n"
            f"{query_hint or DEFAULT_QUERY_HINT}\n",
            encoding="utf-8",
        )
    ensure_db_version(root, name)
    return root


def ensure_db_version(db_root: Path, db_name: str) -> dict[str, Any]:
    path = db_root / "VERSION.json"
    if path.exists():
        return read_db_version(db_root)

    created_at = datetime.now(timezone.utc).isoformat()
    tool_hash = _tool_hash()
    embedding = embedding_fingerprint()
    tokenizer = tokenizer_fingerprint()
    tokenizer_config = tokenizer_runtime_descriptor()
    seed = {
        "db_name": db_name,
        "created_at": created_at,
        "collection": collection_name_for_db(db_name),
        "tool_hash": tool_hash,
        "embedding": embedding,
        "tokenizer": tokenizer,
        "tokenizer_config": tokenizer_config,
    }
    db_hash = hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    payload = {
        "schema": "local-rag.db-version.v1",
        "db_name": db_name,
        "created_at": created_at,
        "hash_algorithm": "sha256",
        "db_hash": db_hash,
        "collection": collection_name_for_db(db_name),
        "embedding": embedding,
        "tokenizer": tokenizer,
        "tokenizer_config": tokenizer_config,
        "tool": {
            "name": "software-rag-tool",
            "version": "0.1.0",
            "hash": tool_hash,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def collection_name_for_db(db_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", db_name.strip("-").replace("-", "_")).strip("_")
    return f"{safe}_ruri3_30m_int8_v1"


def read_db_config(db_root: Path) -> dict:
    path = db_root / "db.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def read_db_version(db_root: Path) -> dict[str, Any]:
    path = db_root / "VERSION.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def read_profile_hint(db_root: Path, max_chars: int = 500) -> str:
    path = db_root / "DB_PROFILE.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = "## Query Hint"
    if marker in text:
        text = text.split(marker, 1)[1]
        text = re.split(r"\n##\s+", text, maxsplit=1)[0]
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return " ".join(lines)[:max_chars]


def update_db_metadata(
    db_root: Path,
    db_name: str,
    *,
    title: str,
    query_hint: str,
) -> dict[str, str]:
    """Update presentation metadata without changing DB or index identity."""

    name = require_db_name(db_name)
    root = Path(db_root).resolve()
    if root.name != name or not root.is_dir():
        raise ValueError("DB root does not match DB name")
    normalized_title = title.strip() or name
    normalized_hint = query_hint.strip()
    if "\n" in normalized_title or "\r" in normalized_title:
        raise ValueError("DB title must be one line")
    if len(normalized_title) > 200:
        raise ValueError("DB title exceeds 200 characters")
    if len(normalized_hint) > 2_000:
        raise ValueError("DB query hint exceeds 2000 characters")

    config_path = root / "db.json"
    config = read_db_config(root)
    if not config or str(config.get("db_name") or "") != name:
        raise ValueError("DB configuration is missing or has a mismatched name")
    config["title"] = normalized_title

    profile_path = root / str(config.get("profile") or "DB_PROFILE.md")
    if profile_path.parent.resolve() != root:
        raise ValueError("DB profile path must be inside the DB root")
    profile = (
        profile_path.read_text(encoding="utf-8", errors="strict")
        if profile_path.is_file()
        else ""
    )
    updated_profile = _updated_profile_text(
        profile,
        title=normalized_title,
        query_hint=normalized_hint,
    )

    _atomic_write_text(
        config_path,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write_text(profile_path, updated_profile)
    return {
        "db_name": name,
        "title": normalized_title,
        "query_hint": normalized_hint,
    }


def _updated_profile_text(
    text: str,
    *,
    title: str,
    query_hint: str,
) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines[0] = f"# {title}"
        text = "\n".join(lines).rstrip() + "\n"
    elif text.strip():
        text = f"# {title}\n\n{text.lstrip()}"
    else:
        text = f"# {title}\n"

    marker = "## Query Hint"
    replacement = f"{marker}\n\n{query_hint}".rstrip() + "\n"
    if marker not in text:
        return text.rstrip() + "\n\n" + replacement
    before, after = text.split(marker, 1)
    next_section = re.search(r"(?m)^##\s+", after)
    suffix = after[next_section.start() :] if next_section else ""
    return before.rstrip() + "\n\n" + replacement + (
        "\n" + suffix.lstrip() if suffix else ""
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _tool_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    suffixes = {".py", ".toml", ".txt", ".md"}
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
