from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db_runtime import DbRegistry
from .dbs import require_db_name
from .env import load_env
from .paths import dbs_dir
from .retrieval import cold_lexical_fast_path, hybrid_query


_REGISTRY: DbRegistry | None = None
RETRIEVAL_MODES = {"hybrid", "lexical", "dense"}


def registry() -> DbRegistry:
    global _REGISTRY
    root = dbs_dir()
    if _REGISTRY is None or _REGISTRY.dbs_root != root.expanduser().resolve():
        _REGISTRY = DbRegistry(root)
    return _REGISTRY


def run_search_payload(
    *,
    db_name: str,
    question: str,
    top_k: int,
    source: str = "any",
    max_chars: int = 900,
    budget_tokens: int | None = None,
    explain: bool = False,
    include_db_hint: bool = False,
    use_dense: bool = True,
    retrieval_mode: str = "hybrid",
) -> dict[str, Any]:
    load_env()
    name = require_db_name(db_name)
    store = registry().get(name)
    mode = _normalize_retrieval_mode(retrieval_mode, use_dense=use_dense)
    rows = hybrid_query(
        question,
        top_k=top_k,
        source=source,
        budget_tokens=budget_tokens,
        explain=explain,
        use_dense=mode in {"hybrid", "dense"},
        use_lexical=mode in {"hybrid", "lexical"},
        backend=store,
    )
    db_hint = store.context.profile_hint if include_db_hint else ""
    payload = json_payload(rows, question, name, max_chars, db_hint=db_hint)
    payload["retrieval_mode"] = mode
    return payload


def try_cold_lexical_fast_path(
    *,
    db_name: str,
    question: str,
    top_k: int,
    source: str = "any",
    max_chars: int = 900,
    budget_tokens: int | None = None,
    explain: bool = False,
    include_db_hint: bool = False,
) -> dict[str, Any] | None:
    load_env()
    name = require_db_name(db_name)
    store = registry().get(name)
    rows = cold_lexical_fast_path(
        question,
        top_k=top_k,
        source=source,
        budget_tokens=budget_tokens,
        explain=explain,
        backend=store,
    )
    if rows is None:
        return None
    db_hint = store.context.profile_hint if include_db_hint else ""
    payload = json_payload(rows, question, name, max_chars, db_hint=db_hint)
    payload["fast_path"] = "cold_lexical"
    payload["retrieval_mode"] = "hybrid"
    return payload


def _normalize_retrieval_mode(mode: str, *, use_dense: bool = True) -> str:
    if not use_dense and mode == "hybrid":
        return "lexical"
    normalized = (mode or "hybrid").strip().lower()
    if normalized not in RETRIEVAL_MODES:
        raise ValueError(f"retrieval_mode must be one of {sorted(RETRIEVAL_MODES)}")
    return normalized


def json_payload(rows: list[dict[str, Any]], question: str, db_name: str, max_chars: int, *, db_hint: str = "") -> dict[str, Any]:
    evidence = []
    results = []
    warnings = []
    truncated = False
    for row in rows:
        warnings.extend((row.get("debug") or {}).get("warnings") or [])
        meta = row.get("metadata") or {}
        text = row.get("text") or ""
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        item: dict[str, Any] = {
            "id": f"R{row['rank']}",
            "source": {
                "path": meta.get("path") or "",
                "title": meta.get("title") or meta.get("chunk_title") or "",
                "revision": f"sha256:{meta.get('content_hash')}" if meta.get("content_hash") else "",
            },
            "location": {
                "section": meta.get("section_path") or meta.get("chunk_title") or "",
                "lines": meta.get("lines") or None,
                "page": meta.get("page") or None,
                "slide": meta.get("slide") or None,
            },
            "text": text,
            "signals": row.get("signals") or [],
        }
        if row.get("debug"):
            item["debug"] = row["debug"]
        evidence.append(item)
        result = dict(row)
        result["text"] = text
        results.append(result)
    return {
        "schema": "local-rag.search.v1",
        "db": db_name,
        "db_hint": db_hint,
        "query": question,
        "generation": 1,
        "status": "ok" if evidence else "no_evidence",
        "evidence": evidence,
        "results": results,
        "warnings": sorted(set(warnings)),
        "truncated": truncated or any(bool(row.get("truncated")) for row in rows),
    }


def payload_to_text(payload: dict[str, Any], output_format: str, *, explain: bool = False) -> str:
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return payload_to_prompt(payload, explain=explain)


def payload_to_prompt(payload: dict[str, Any], *, explain: bool = False) -> str:
    question = str(payload.get("query") or "")
    db_name = str(payload.get("db") or "")
    db_hint = str(payload.get("db_hint") or "")
    lines = ["## Retrieved evidence", f"Database: {db_name}", ""]
    if db_hint:
        lines.extend(["## DB hint", db_hint, ""])
    if payload.get("status") == "error":
        lines.extend(["Status: error", "", str(payload.get("error") or "unknown error"), "", "## Question", question])
        return "\n".join(lines)
    evidence = payload.get("evidence") or []
    if not evidence:
        lines.extend(["Status: no_evidence", "", "根拠が不足している場合は断定しないこと。", "", "## Question", question])
        return "\n".join(lines)
    if payload.get("fast_path"):
        lines.extend([f"Fast path: {payload['fast_path']}", ""])
    for item in evidence:
        source = item.get("source") or {}
        location = item.get("location") or {}
        section = location.get("section") or ""
        suffix = f" - {section}" if section else ""
        lines.append(f"[{item.get('id')}] {source.get('path') or ''}{suffix}")
        lines.append(str(item.get("text") or ""))
        if explain and item.get("debug"):
            lines.append(f"signals={','.join(item.get('signals') or [])} debug={json.dumps(item['debug'], ensure_ascii=False)}")
        lines.append("")
    lines.append("回答では根拠IDとsource locationを引用すること。")
    lines.append("根拠が不足する場合は断定しないこと。")
    lines.append("\n# Question\n")
    lines.append(question)
    return "\n".join(lines)
