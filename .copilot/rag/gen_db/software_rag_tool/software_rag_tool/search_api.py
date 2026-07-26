from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .db_runtime import DbRegistry
from .dbs import require_db_name
from .env import load_env
from .paths import dbs_dir
from .retrieval import cold_lexical_fast_path, hybrid_query
from .tokenize import extract_anchors


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
    identifier_diagnostics: bool = True,
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
    if identifier_diagnostics:
        _add_identifier_diagnostics(payload, store, question, source=source)
        payload.setdefault("identifier_diagnostics_enabled", True)
    else:
        payload["identifier_diagnostics_enabled"] = False
    payload["retrieval_mode"] = mode
    return normalize_search_contract(payload)


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
    identifier_diagnostics: bool = True,
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
    if identifier_diagnostics:
        _add_identifier_diagnostics(payload, store, question, source=source)
        payload.setdefault("identifier_diagnostics_enabled", True)
    else:
        payload["identifier_diagnostics_enabled"] = False
    payload["fast_path"] = "cold_lexical"
    payload["retrieval_mode"] = "hybrid"
    return normalize_search_contract(payload)


def _normalize_retrieval_mode(mode: str, *, use_dense: bool = True) -> str:
    if not use_dense and mode == "hybrid":
        return "lexical"
    normalized = (mode or "hybrid").strip().lower()
    if normalized not in RETRIEVAL_MODES:
        raise ValueError(f"retrieval_mode must be one of {sorted(RETRIEVAL_MODES)}")
    return normalized


def json_payload(rows: list[dict[str, Any]], question: str, db_name: str, max_chars: int, *, db_hint: str = "") -> dict[str, Any]:
    converted: list[tuple[dict[str, Any], dict[str, Any], set[str]]] = []
    warnings: list[str] = []
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
        result = dict(row)
        result["text"] = text
        signals = set(str(value) for value in row.get("signals") or [])
        warnings.extend(_evidence_limit_warnings(text, meta))
        converted.append((item, result, signals))

    has_lexical_anchor = any("lexical_anchor" in signals for _item, _result, signals in converted)
    if has_lexical_anchor:
        evidence = [
            item
            for item, _result, signals in converted
            if "lexical_anchor" in signals or "exact" in signals
        ]
        background_context = [
            item
            for item, _result, signals in converted
            if "lexical_anchor" not in signals and "exact" not in signals
        ]
        background_ids = {item["id"] for item in background_context}
    else:
        evidence = [item for item, _result, _signals in converted]
        background_context = []
        background_ids = set()
    results = [result for _item, result, _signals in converted]
    background_results = [
        result
        for item, result, _signals in converted
        if item["id"] in background_ids
    ]
    limitation_warnings = sorted(set(warnings))
    if evidence:
        status = "partial" if limitation_warnings else "ok"
        answerability = "partial" if limitation_warnings else "full"
    else:
        status = "no_hit"
        answerability = "none"
    payload = {
        "schema": "local-rag.search.v1",
        "db": db_name,
        "selected_db": db_name,
        "db_hint": db_hint,
        "query": question,
        "generation": 1,
        "status": status,
        "answerability": answerability,
        "evidence": evidence,
        "contexts": evidence,
        "background_context": background_context,
        "related_context": [],
        "results": results,
        "background_results": background_results,
        "related_results": [],
        "warnings": limitation_warnings,
        "truncated": truncated or any(bool(row.get("truncated")) for row in rows),
    }
    if status == "no_hit":
        payload["legacy_status"] = "no_evidence"
    return payload


def _add_identifier_diagnostics(payload: dict[str, Any], store: Any, question: str, *, source: str) -> None:
    anchors = extract_anchors(question, limit=30)
    if not anchors:
        return
    unmatched = []
    matches = []
    diagnostic_errors = []
    for anchor in anchors:
        try:
            exact_rows = store.exact_search(anchor, top_k=1000, source=source)
        except Exception as exc:
            diagnostic_errors.append(
                {
                    "identifier": anchor,
                    "operation": "exact_search",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            matches.append(
                {
                    "identifier": anchor,
                    "matched": None,
                    "candidate_count": None,
                    "verified_candidate_count": None,
                    "raw_occurrence_verified": False,
                    "paths": [],
                    "diagnostic_error": True,
                }
            )
            continue
        if not exact_rows:
            unmatched.append(anchor)
        verified_rows = [row for row in exact_rows if _raw_identifier_occurs(row, anchor)]
        matches.append(
            {
                "identifier": anchor,
                "matched": bool(exact_rows),
                "candidate_count": len(exact_rows),
                "verified_candidate_count": len(verified_rows),
                "raw_occurrence_verified": bool(exact_rows) and len(verified_rows) == len(exact_rows),
                "paths": sorted(
                    {
                        str((row.get("metadata") or {}).get("path") or "")
                        for row in verified_rows
                        if (row.get("metadata") or {}).get("path")
                    }
                ),
            }
        )
    try:
        exact_candidate_count = len(store.exact_search(question, top_k=1000, source=source))
    except Exception as exc:
        exact_candidate_count = None
        diagnostic_errors.append(
            {
                "identifier": question,
                "operation": "query_exact_search",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    payload["identifiers"] = {
        "anchors": anchors,
        "unmatched_identifiers": unmatched,
        "exact_candidate_count": exact_candidate_count,
        "matches": matches,
        "diagnostics_complete": not diagnostic_errors,
        "diagnostic_errors": diagnostic_errors,
    }
    payload["identifier_diagnostics_enabled"] = True
    if diagnostic_errors:
        payload["identifier_diagnostics_error"] = {
            "kind": "identifier_diagnostics_failed",
            "count": len(diagnostic_errors),
            "errors": diagnostic_errors,
        }
    payload["unmatched_identifiers"] = unmatched
    payload["exact_candidate_count"] = exact_candidate_count
    if unmatched and not diagnostic_errors:
        related = _dedupe_contexts(
            [
                *(payload.get("evidence") or []),
                *(payload.get("background_context") or []),
            ]
        )
        payload["status"] = "partial" if related else "no_hit"
        payload["answerability"] = "none"
        payload["related_context"] = related
        payload["contexts"] = []
        payload["evidence"] = []
        payload["background_context"] = []
        payload["related_results"] = list(payload.get("results") or [])
        payload["results"] = []
        payload["background_results"] = []
        if payload["status"] == "no_hit":
            payload["legacy_status"] = "no_evidence"
        warnings = list(payload.get("warnings") or [])
        warnings.append(
            "Exact identifier match not found for: "
            + ", ".join(unmatched)
            + ". Returned evidence, if any, is related context and not proof of those identifiers."
        )
        payload["warnings"] = sorted(set(warnings))
    elif diagnostic_errors:
        warnings = list(payload.get("warnings") or [])
        warnings.append("Identifier diagnostics did not complete; Exact/no-hit conclusions are unavailable.")
        payload["warnings"] = sorted(set(warnings))


def _raw_identifier_occurs(row: dict[str, Any], identifier: str) -> bool:
    metadata = row.get("metadata") or {}
    haystack = "\n".join(
        [
            str(row.get("text") or ""),
            str(metadata.get("path") or ""),
            str(metadata.get("title") or ""),
            str(metadata.get("section_path") or ""),
        ]
    )
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])", re.IGNORECASE)
    return bool(pattern.search(haystack))


def payload_to_text(payload: dict[str, Any], output_format: str, *, explain: bool = False) -> str:
    payload = normalize_search_contract(payload)
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return payload_to_prompt(payload, explain=explain)


def payload_to_prompt(payload: dict[str, Any], *, explain: bool = False) -> str:
    question = str(payload.get("query") or "")
    db_name = str(payload.get("db") or "")
    db_hint = str(payload.get("db_hint") or "")
    unmatched = payload.get("unmatched_identifiers") or []
    background = [] if unmatched else list(payload.get("background_context") or [])
    evidence = (payload.get("related_context") or payload.get("evidence") or []) if unmatched else (
        payload.get("contexts") or payload.get("evidence") or []
    )
    heading = "## Related search candidates (not exact evidence)" if unmatched else "## Retrieved evidence"
    lines = [heading, f"Database: {db_name}", ""]
    if db_hint:
        lines.extend(["## DB hint", db_hint, ""])
    if payload.get("status") == "error":
        lines.extend(["Status: error", "", str(payload.get("error") or "unknown error"), "", "## Question", question])
        return "\n".join(lines)
    if unmatched:
        lines.extend(
            [
                "## Identifier notice",
                "DB内では次の識別子の完全一致を確認できませんでした: " + ", ".join(str(value) for value in unmatched),
                "以下の候補は関連検索結果であり、その識別子そのものの根拠とは限りません。",
                "",
            ]
        )
    if not evidence:
        lines.extend(["Status: no_hit", "", "根拠が不足している場合は断定しないこと。", "", "## Question", question])
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
    if background:
        lines.extend(["## Background context (not direct evidence)", ""])
        for item in background:
            source = item.get("source") or {}
            lines.append(f"[{item.get('id')}] {source.get('path') or ''}")
            lines.append(str(item.get("text") or ""))
            lines.append("")
        lines.append("Do not use background context as direct proof.")
        lines.append("")
    if unmatched:
        lines.append("上記候補を、未一致識別子そのものの根拠として引用しないこと。")
        lines.append("DB内では完全一致を確認できない旨を明示し、断定しないこと。")
    else:
        lines.append("回答では根拠IDとsource locationを引用すること。")
        lines.append("根拠が不足する場合は断定しないこと。")
    lines.append("\n# Question\n")
    lines.append(question)
    return "\n".join(lines)


def normalize_search_contract(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    status = str(normalized.get("status") or "error")
    if status == "no_evidence":
        normalized["legacy_status"] = "no_evidence"
        status = "no_hit"
    normalized["status"] = status
    db_name = str(normalized.get("selected_db") or normalized.get("db") or "")
    normalized.setdefault("db", db_name)
    normalized["selected_db"] = db_name
    for key in ("evidence", "contexts", "background_context", "related_context", "warnings"):
        value = normalized.get(key)
        normalized[key] = list(value) if isinstance(value, list) else []
    if status == "ok":
        normalized.setdefault("answerability", "full" if normalized["evidence"] else "none")
    elif status == "partial":
        normalized.setdefault("answerability", "partial" if normalized["evidence"] else "none")
    else:
        normalized.setdefault("answerability", "none")
    if status == "no_hit":
        normalized.setdefault("legacy_status", "no_evidence")
        normalized["evidence"] = []
        normalized["contexts"] = []
    return normalized


def _dedupe_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for context in contexts:
        key = str(context.get("id") or json.dumps(context, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        output.append(context)
    return output


def _evidence_limit_warnings(text: str, metadata: dict[str, Any]) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    numeric_rows = [
        line
        for line in lines
        if len(re.findall(r"(?<!\w)[+-]?(?:\d[\d,.]*|-)(?!\w)", line)) >= 3
    ]
    if len(numeric_rows) < 2:
        return []
    section = str(metadata.get("section_path") or metadata.get("chunk_title") or "")
    first_line = lines[0] if lines else ""
    appears_continued = bool(
        re.search(r"#(?:[2-9]|\d{2,})\s*$", section)
        or re.match(r"^[\d\s,.;:+%()/-]+$", first_line)
    )
    if not appears_continued:
        return []
    return [
        "A table-like excerpt appears to continue without verified column headers. "
        "Do not infer column meanings, comparisons, rankings, or qualitative size labels."
    ]
