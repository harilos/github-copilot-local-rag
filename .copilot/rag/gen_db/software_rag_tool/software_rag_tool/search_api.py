from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .db_runtime import DbRegistry
from .dbs import require_db_name
from .env import load_env
from .paths import dbs_dir
from .retrieval import adaptive_hybrid_query, cold_lexical_fast_path, hybrid_query
from .token_budget import conservative_token_count, truncate_to_token_limit
from .tokenize import canonicalize, extract_anchors, identifier_match_keys


_REGISTRY: DbRegistry | None = None
RETRIEVAL_MODES = {"hybrid", "lexical", "dense"}
COMPACT_BACKGROUND_LIMIT = 2
COMPACT_RELATED_LIMIT = 2
COMPACT_MAX_UTF8_BYTES = 10_240
COMPACT_EVIDENCE_TOKEN_LIMIT = 1_200
COMPACT_AUXILIARY_TOKEN_LIMIT = 160
COMPACT_TRUNCATION_WARNING = "compact_output_truncated"
COMPACT_SEARCH_FIELDS = (
    "schema",
    "db",
    "selected_db",
    "query",
    "status",
    "legacy_status",
    "answerability",
    "evidence",
    "background_context",
    "related_context",
    "warnings",
    "unmatched_identifiers",
    "exact_candidate_count",
    "retrieval_mode",
    "retrieval_route",
    "dense_used",
    "dense_skipped_reason",
    "error",
    "message",
    "required_action",
    "execution_metadata",
)


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
        _add_identifier_diagnostics(
            payload,
            store,
            question,
            source=source,
            excluded_identifiers={name},
        )
        payload.setdefault("identifier_diagnostics_enabled", True)
    else:
        payload["identifier_diagnostics_enabled"] = False
    payload["retrieval_mode"] = mode
    payload["retrieval_route"] = mode
    payload["dense_used"] = mode in {"hybrid", "dense"}
    return normalize_search_contract(payload)


def run_adaptive_search_payload(
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
) -> dict[str, Any]:
    """Run the default one-operation hybrid route without repeating lexical work."""
    load_env()
    name = require_db_name(db_name)
    store = registry().get(name)
    rows, route = adaptive_hybrid_query(
        question,
        top_k=top_k,
        source=source,
        budget_tokens=budget_tokens,
        explain=explain,
        db_scope_confirmed=True,
        excluded_identifiers={name},
        backend=store,
    )
    db_hint = store.context.profile_hint if include_db_hint else ""
    payload = json_payload(rows, question, name, max_chars, db_hint=db_hint)
    if identifier_diagnostics:
        _add_identifier_diagnostics(
            payload,
            store,
            question,
            source=source,
            excluded_identifiers={name},
            precomputed_exact_rows=list(route.get("raw_exact_rows") or []),
        )
        payload.setdefault("identifier_diagnostics_enabled", True)
    else:
        payload["identifier_diagnostics_enabled"] = False
    payload["retrieval_mode"] = "hybrid"
    payload["retrieval_route"] = route["retrieval_route"]
    payload["dense_used"] = bool(route["dense_used"])
    payload["dense_skipped_reason"] = route.get("dense_skipped_reason")
    if explain:
        payload["retrieval_funnel"] = dict(route.get("retrieval_funnel") or {})
    certificate = route.get("certificate") or {}
    if certificate.get("kind") == "db_scope_full_query_lexical":
        warnings = list(payload.get("warnings") or [])
        warnings.append(
            "Direct evidence is limited to one DB-scoped low-frequency anchor "
            "confirmed by the complete-query lexical ranking. Background context "
            "is not proof, and missing table headers or comparisons must not be inferred."
        )
        payload["warnings"] = sorted(set(warnings))
        if payload.get("evidence"):
            payload["status"] = "partial"
            payload["answerability"] = "partial"
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
        db_scope_confirmed=True,
        backend=store,
    )
    if rows is None:
        if not identifier_diagnostics:
            return None
        payload = json_payload([], question, name, max_chars, db_hint="")
        _add_identifier_diagnostics(
            payload,
            store,
            question,
            source=source,
            excluded_identifiers={name},
        )
        diagnostics = payload.get("identifiers") or {}
        if (
            not payload.get("unmatched_identifiers")
            or diagnostics.get("diagnostics_complete") is not True
        ):
            return None
        payload["fast_path"] = "cold_identifier_no_hit"
        payload["retrieval_mode"] = "hybrid"
        payload["retrieval_route"] = "cold_identifier_no_hit"
        payload["dense_used"] = False
        payload["dense_skipped_reason"] = "cold_lexical_fast_path"
        return normalize_search_contract(payload)
    db_hint = store.context.profile_hint if include_db_hint else ""
    payload = json_payload(rows, question, name, max_chars, db_hint=db_hint)
    if identifier_diagnostics:
        _add_identifier_diagnostics(
            payload,
            store,
            question,
            source=source,
            excluded_identifiers={name},
        )
        payload.setdefault("identifier_diagnostics_enabled", True)
    else:
        payload["identifier_diagnostics_enabled"] = False
    payload["fast_path"] = "cold_lexical"
    payload["retrieval_mode"] = "hybrid"
    payload["retrieval_route"] = "cold_lexical_fast_path"
    payload["dense_used"] = False
    payload["dense_skipped_reason"] = "cold_lexical_fast_path"
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
        for key in (
            "support_kind",
            "anchor_chunk_uid",
            "anchor_term",
            "neighbor_distance",
            "independent_signals",
        ):
            if row.get(key) not in (None, "", []):
                item[key] = row[key]
        if row.get("debug"):
            item["debug"] = row["debug"]
        result = dict(row)
        result["text"] = text
        signals = set(str(value) for value in row.get("signals") or [])
        warnings.extend(_evidence_limit_warnings(text, meta))
        converted.append((item, result, signals))

    has_lexical_anchor = any("lexical_anchor" in signals for _item, _result, signals in converted)
    strong_identifiers = [
        anchor
        for anchor in extract_anchors(question, limit=30)
        if _diagnostic_identifier_anchor(anchor)
    ]
    has_matched_strong_exact = any(
        "exact" in signals
        and _exact_result_is_direct(result)
        and _context_matches_identifier(item, strong_identifiers)
        for item, result, signals in converted
    )
    anchored_direct_ids = {
        item["id"]
        for item, result, _signals in converted
        if _anchored_neighbor_is_direct(
            item,
            result,
            converted=converted,
            strong_identifiers=strong_identifiers,
        )
    }
    if has_lexical_anchor or has_matched_strong_exact:
        evidence = [
            item
            for item, result, signals in converted
            if "lexical_anchor" in signals
            or (
                "exact" in signals
                and _exact_result_is_direct(result)
                and _context_matches_identifier(item, strong_identifiers)
            )
            or item["id"] in anchored_direct_ids
        ]
        background_context = [
            item
            for item, result, signals in converted
            if "lexical_anchor" not in signals
            and not (
                "exact" in signals
                and _exact_result_is_direct(result)
                and _context_matches_identifier(item, strong_identifiers)
            )
            and item["id"] not in anchored_direct_ids
        ]
        background_ids = {item["id"] for item in background_context}
    else:
        evidence = [
            item
            for item, result, signals in converted
            if not (
                "exact" in signals
                and not _exact_result_is_direct(result)
            )
        ]
        evidence_ids = {item["id"] for item in evidence}
        background_context = [
            item
            for item, _result, _signals in converted
            if item["id"] not in evidence_ids
        ]
        background_ids = {item["id"] for item in background_context}
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


def _anchored_neighbor_is_direct(
    item: dict[str, Any],
    result: dict[str, Any],
    *,
    converted: list[tuple[dict[str, Any], dict[str, Any], set[str]]],
    strong_identifiers: list[str],
) -> bool:
    if item.get("support_kind") != "anchored_neighbor":
        return False
    anchor_uid = str(item.get("anchor_chunk_uid") or "")
    anchor_term = str(item.get("anchor_term") or "")
    try:
        distance = int(item.get("neighbor_distance"))
    except (TypeError, ValueError):
        return False
    if (
        not anchor_uid
        or not anchor_term
        or distance < 0
        or distance > 1
    ):
        return False
    anchor_entry = next(
        (
            (anchor_item, anchor_result, anchor_signals)
            for anchor_item, anchor_result, anchor_signals in converted
            if str(anchor_result.get("id") or "") == anchor_uid
        ),
        None,
    )
    if anchor_entry is None:
        return False
    anchor_item, anchor_result, anchor_signals = anchor_entry
    if not ({"exact", "lexical_anchor"} & anchor_signals):
        return False
    if "exact" in anchor_signals and not _exact_result_is_direct(anchor_result):
        return False
    if "lexical_anchor" not in anchor_signals and not (
        set(identifier_match_keys(anchor_term))
        & {
            key
            for identifier in strong_identifiers
            for key in identifier_match_keys(identifier)
        }
    ):
        return False
    if not _raw_identifier_occurs(anchor_result, anchor_term):
        return False
    source_path = str((item.get("source") or {}).get("path") or "")
    anchor_path = str((anchor_item.get("source") or {}).get("path") or "")
    if not source_path or source_path.casefold() != anchor_path.casefold():
        return False
    independent = set(str(value) for value in item.get("independent_signals") or [])
    same_section = _same_evidence_section(item, anchor_item)
    if not (independent & {"dense", "lexical", "metadata"}) and not same_section:
        return False
    return _raw_result_matches_context(result, item)


def _exact_result_is_direct(result: dict[str, Any]) -> bool:
    return result.get("exact_evidence_eligible") is not False


def _same_evidence_section(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_section = str((left.get("location") or {}).get("section") or "").strip()
    right_section = str((right.get("location") or {}).get("section") or "").strip()
    if not left_section or not right_section:
        return False
    if left_section.casefold() == right_section.casefold():
        return True
    left_page = re.fullmatch(r"(Page\s+\d+)\s+#\d+", left_section, re.IGNORECASE)
    right_page = re.fullmatch(r"(Page\s+\d+)\s+#\d+", right_section, re.IGNORECASE)
    return bool(
        left_page
        and right_page
        and left_page.group(1).casefold() == right_page.group(1).casefold()
    )


def _raw_result_matches_context(
    result: dict[str, Any],
    item: dict[str, Any],
) -> bool:
    metadata = result.get("metadata") or {}
    source = item.get("source") or {}
    return bool(
        str(result.get("text") or "") == str(item.get("text") or "")
        and str(metadata.get("path") or "").casefold()
        == str(source.get("path") or "").casefold()
    )


def _add_identifier_diagnostics(
    payload: dict[str, Any],
    store: Any,
    question: str,
    *,
    source: str,
    excluded_identifiers: set[str] | None = None,
    precomputed_exact_rows: list[dict[str, Any]] | None = None,
) -> None:
    excluded = {
        canonicalize(identifier)
        for identifier in (excluded_identifiers or set())
        if identifier
    }
    anchors = [
        anchor
        for anchor in extract_anchors(question, limit=30)
        if _diagnostic_identifier_anchor(anchor)
        and canonicalize(anchor) not in excluded
    ]
    if not anchors:
        return
    unmatched = []
    matches = []
    diagnostic_errors = []
    for anchor in anchors:
        if precomputed_exact_rows is None:
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
        else:
            exact_rows = precomputed_exact_rows
        verified_rows = [row for row in exact_rows if _raw_identifier_occurs(row, anchor)]
        if not verified_rows:
            unmatched.append(anchor)
        matches.append(
            {
                "identifier": anchor,
                "matched": bool(verified_rows),
                "candidate_count": len(exact_rows),
                "verified_candidate_count": len(verified_rows),
                "raw_occurrence_verified": (
                    bool(exact_rows) and len(verified_rows) == len(exact_rows)
                ),
                "paths": sorted(
                    {
                        str((row.get("metadata") or {}).get("path") or "")
                        for row in verified_rows
                        if (row.get("metadata") or {}).get("path")
                    }
                ),
            }
        )
    if precomputed_exact_rows is not None:
        exact_candidate_count = len(precomputed_exact_rows)
    else:
        try:
            query_rows = store.exact_search(question, top_k=1000, source=source)
            exact_candidate_count = len(query_rows)
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
        matched_identifiers = [
            str(match.get("identifier") or "")
            for match in matches
            if match.get("matched") is True
        ]
        direct_evidence = [
            item
            for item in (payload.get("evidence") or [])
            if _context_matches_identifier(item, matched_identifiers)
        ]
        unsupported_evidence = [
            item
            for item in (payload.get("evidence") or [])
            if not _context_matches_identifier(item, matched_identifiers)
        ]
        related = _dedupe_contexts(
            [
                *unsupported_evidence,
                *(payload.get("background_context") or []),
            ]
        )
        payload["status"] = "partial" if (direct_evidence or related) else "no_hit"
        payload["answerability"] = "partial" if direct_evidence else "none"
        payload["related_context"] = related
        payload["contexts"] = direct_evidence
        payload["evidence"] = direct_evidence
        payload["background_context"] = []
        raw_results = list(payload.get("results") or [])
        direct_results = [
            row
            for row in raw_results
            if any(_raw_identifier_occurs(row, identifier) for identifier in matched_identifiers)
        ]
        payload["related_results"] = [
            row
            for row in raw_results
            if row not in direct_results
        ]
        payload["results"] = direct_results
        payload["background_results"] = []
        if payload["status"] == "no_hit":
            payload["legacy_status"] = "no_evidence"
        warnings = list(payload.get("warnings") or [])
        warnings.append(
            "Exact identifier match not found for: "
            + ", ".join(unmatched)
            + ". Direct evidence may support matched portions of the question, "
            "but it is not proof of the unmatched identifiers. Related context "
            "is never proof."
        )
        payload["warnings"] = sorted(set(warnings))
    elif diagnostic_errors:
        warnings = list(payload.get("warnings") or [])
        warnings.append("Identifier diagnostics did not complete; Exact/no-hit conclusions are unavailable.")
        payload["warnings"] = sorted(set(warnings))


def _diagnostic_identifier_anchor(anchor: str) -> bool:
    """Avoid treating ordinary all-letter acronyms as conclusive no-hit identifiers."""
    if any(marker in anchor for marker in ["/", "\\", ".", ":", "_", "-"]):
        return True
    return any(char.isdigit() for char in anchor) and any(char.isalpha() for char in anchor)


def _context_matches_identifier(item: dict[str, Any], anchors: list[str]) -> bool:
    if not anchors:
        return False
    canonical_anchors = {
        key
        for anchor in anchors
        for key in identifier_match_keys(anchor)
    }
    debug = item.get("debug") or {}
    exact_debug = debug.get("exact_match") if isinstance(debug, dict) else {}
    matched_terms = {
        key
        for term in (exact_debug or {}).get("matched_terms", [])
        if term
        for key in identifier_match_keys(str(term))
    }
    if canonical_anchors & matched_terms:
        return True
    source = item.get("source") or {}
    haystack = "\n".join(
        [
            str(item.get("text") or ""),
            str(source.get("path") or ""),
            str(source.get("title") or ""),
        ]
    )
    haystack_keys = {
        key
        for candidate in extract_anchors(haystack, limit=500)
        for key in identifier_match_keys(candidate)
    }
    if canonical_anchors & haystack_keys:
        return True
    return any(
        re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(anchor)}(?![A-Za-z0-9_-])",
            haystack,
            re.IGNORECASE,
        )
        for anchor in anchors
    )


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
    identifier_keys = set(identifier_match_keys(identifier))
    haystack_keys = {
        key
        for candidate in extract_anchors(haystack, limit=500)
        for key in identifier_match_keys(candidate)
    }
    if identifier_keys & haystack_keys:
        return True
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
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
    evidence = list(payload.get("evidence") or payload.get("contexts") or [])
    background = list(payload.get("background_context") or [])
    related = list(payload.get("related_context") or [])
    warnings = [str(value) for value in payload.get("warnings") or [] if value]
    lines = ["## Retrieved evidence", f"Database: {db_name}", ""]
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
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    if not evidence:
        lines.extend(
            [
                f"Status: {payload.get('status') or 'no_hit'}",
                "",
                "直接根拠が不足しているため、断定しないこと。",
                "",
            ]
        )
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
    if related:
        lines.extend(["## Related search candidates (not exact evidence)", ""])
        for item in related:
            source = item.get("source") or {}
            lines.append(f"[{item.get('id')}] {source.get('path') or ''}")
            lines.append(str(item.get("text") or ""))
            lines.append("")
        lines.append("Do not use related search candidates as direct proof.")
        lines.append("")
    if unmatched:
        lines.append("取得済みの直接根拠は、根拠がある部分の回答にだけ使用すること。")
        lines.append("背景情報や関連候補を、未一致識別子そのものの根拠として引用しないこと。")
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


def compact_search_contract(
    payload: dict[str, Any],
    *,
    explain: bool = False,
) -> dict[str, Any]:
    """Return the evidence-first view used by ordinary assistant lookup.

    The full payload remains the default for backward compatibility. This
    additive view removes duplicate legacy result arrays so a single tool
    response stays small enough for lightweight assistants to consume.
    """
    normalized = normalize_search_contract(payload)
    compact = {
        key: normalized[key]
        for key in COMPACT_SEARCH_FIELDS
        if key in normalized
    }
    projection_truncated = False
    compact["evidence"], evidence_truncated = _project_contexts(
        list(normalized.get("evidence") or []),
        total_token_limit=COMPACT_EVIDENCE_TOKEN_LIMIT,
        item_limit=None,
        explain=explain,
    )
    compact["background_context"], background_truncated = _project_contexts(
        list(normalized.get("background_context") or []),
        total_token_limit=COMPACT_AUXILIARY_TOKEN_LIMIT * COMPACT_BACKGROUND_LIMIT,
        item_limit=COMPACT_BACKGROUND_LIMIT,
        explain=explain,
    )
    compact["related_context"], related_truncated = _project_contexts(
        list(normalized.get("related_context") or []),
        total_token_limit=COMPACT_AUXILIARY_TOKEN_LIMIT * COMPACT_RELATED_LIMIT,
        item_limit=COMPACT_RELATED_LIMIT,
        explain=explain,
    )
    projection_truncated = evidence_truncated or background_truncated or related_truncated
    if isinstance(compact.get("execution_metadata"), dict):
        metadata = compact["execution_metadata"]
        compact["execution_metadata"] = {
            key: metadata[key]
            for key in (
                "actual_execution",
                "first_attempt_success",
                "final_user_visible_success",
                "fallback_used",
                "total_latency_seconds",
            )
            if key in metadata
        }
    for key in (
        "evidence",
        "background_context",
        "related_context",
        "warnings",
    ):
        compact.setdefault(key, [])
    compact.setdefault("answerability", "none")
    compact["warnings"] = [
        str(value)[:160]
        for value in compact.get("warnings", [])[:6]
    ]
    if len(str(compact.get("query") or "")) > 2_000:
        compact["query"] = str(compact["query"])[:1_980] + "...[truncated]"
        projection_truncated = True
    return _fit_compact_utf8_limit(
        compact,
        projection_truncated=projection_truncated,
    )


def _project_contexts(
    contexts: list[dict[str, Any]],
    *,
    total_token_limit: int,
    item_limit: int | None,
    explain: bool,
) -> tuple[list[dict[str, Any]], bool]:
    projected: list[dict[str, Any]] = []
    remaining = total_token_limit
    truncated = item_limit is not None and len(contexts) > item_limit
    selected = contexts if item_limit is None else contexts[:item_limit]
    for context in selected:
        item = _project_context(context, explain=explain)
        overhead = dict(item)
        overhead["text"] = ""
        overhead_tokens = _conservative_token_count(
            json.dumps(overhead, ensure_ascii=False, separators=(",", ":"))
        )
        if overhead_tokens >= remaining:
            truncated = True
            break
        text = str(item.get("text") or "")
        text_budget = max(0, remaining - overhead_tokens - 30)
        if item_limit is not None:
            text_budget = min(
                text_budget,
                max(0, COMPACT_AUXILIARY_TOKEN_LIMIT - overhead_tokens - 20),
            )
        if _conservative_token_count(text) > text_budget:
            text = _truncate_to_token_limit(text, text_budget)
            item["text"] = text
            item["truncated"] = True
            truncated = True
        used = _conservative_token_count(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        )
        if used > remaining:
            truncated = True
            break
        projected.append(item)
        remaining -= used
    if len(projected) < len(selected):
        truncated = True
    return projected, truncated


def _project_context(context: dict[str, Any], *, explain: bool) -> dict[str, Any]:
    source = context.get("source") or {}
    projected: dict[str, Any] = {
        "id": str(context.get("id") or "")[:120],
        "text": str(context.get("text") or ""),
    }
    source_limits = {"path": 400, "title": 160, "revision": 100}
    filtered_source = {}
    for key, limit in source_limits.items():
        if source.get(key):
            value = str(source[key])
            filtered_source[key] = (
                value
                if len(value) <= limit
                else value[: limit - 15] + "...[truncated]"
            )
    if filtered_source:
        projected["source"] = filtered_source
    location = context.get("location") or {}
    filtered_location = {}
    for key in ("section", "lines", "page", "slide"):
        if location.get(key) in (None, ""):
            continue
        value = location[key]
        if isinstance(value, str) and len(value) > 160:
            value = value[:145] + "...[truncated]"
        filtered_location[key] = value
    if filtered_location:
        projected["location"] = filtered_location
    if context.get("signals"):
        projected["signals"] = list(context["signals"])
    for key in (
        "support_kind",
        "anchor_chunk_uid",
        "anchor_term",
        "neighbor_distance",
        "independent_signals",
    ):
        if context.get(key) not in (None, "", []):
            projected[key] = context[key]
    if explain and context.get("debug"):
        projected["debug"] = context["debug"]
    return projected


def _conservative_token_count(text: str) -> int:
    return conservative_token_count(text)


def _truncate_to_token_limit(text: str, limit: int) -> str:
    return truncate_to_token_limit(text, limit)


def _compact_utf8_size(payload: dict[str, Any]) -> int:
    # CLI print() appends one newline; include it in the stdout purity cap.
    return len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")) + 1


def _fit_compact_utf8_limit(
    compact: dict[str, Any],
    *,
    projection_truncated: bool,
) -> dict[str, Any]:
    if projection_truncated:
        compact["warnings"] = sorted(
            set([*compact.get("warnings", []), COMPACT_TRUNCATION_WARNING])
        )
    if _compact_utf8_size(compact) <= COMPACT_MAX_UTF8_BYTES:
        return compact

    compact["warnings"] = sorted(
        set([*compact.get("warnings", []), COMPACT_TRUNCATION_WARNING])
    )
    for key in ("related_context", "background_context"):
        while compact.get(key) and _compact_utf8_size(compact) > COMPACT_MAX_UTF8_BYTES:
            compact[key].pop()
    for key in ("query", "error", "message"):
        if _compact_utf8_size(compact) <= COMPACT_MAX_UTF8_BYTES:
            return compact
        if key in compact:
            compact[key] = str(compact[key])[:500] + "...[truncated]"
    if _compact_utf8_size(compact) > COMPACT_MAX_UTF8_BYTES:
        compact["warnings"] = [
            COMPACT_TRUNCATION_WARNING,
            *[
                str(value)[:120]
                for value in compact.get("warnings", [])
                if value != COMPACT_TRUNCATION_WARNING
            ][:3],
        ]
    while len(compact.get("evidence") or []) > 1 and _compact_utf8_size(compact) > COMPACT_MAX_UTF8_BYTES:
        compact["evidence"].pop()
    for contexts_key in ("evidence", "background_context", "related_context"):
        for item in compact.get(contexts_key) or []:
            if _compact_utf8_size(compact) <= COMPACT_MAX_UTF8_BYTES:
                return compact
            item.pop("debug", None)
            item.pop("signals", None)
            item.pop("location", None)
            text = str(item.get("text") or "")
            item["text"] = _truncate_to_token_limit(
                text,
                max(32, _conservative_token_count(text) // 2),
            )
    if _compact_utf8_size(compact) > COMPACT_MAX_UTF8_BYTES:
        compact["warnings"] = [COMPACT_TRUNCATION_WARNING]
        compact.pop("execution_metadata", None)
        compact["background_context"] = []
        compact["related_context"] = []
    while compact.get("evidence") and _compact_utf8_size(compact) > COMPACT_MAX_UTF8_BYTES:
        item = compact["evidence"][-1]
        text = str(item.get("text") or "")
        if len(text) <= 64:
            compact["evidence"].pop()
        else:
            item["text"] = text[: max(32, len(text) // 2)] + "...[truncated]"
    if _compact_utf8_size(compact) > COMPACT_MAX_UTF8_BYTES:
        compact = {
            "schema": str(compact.get("schema") or "local-rag.search.v1"),
            "selected_db": str(compact.get("selected_db") or compact.get("db") or ""),
            "status": str(compact.get("status") or "error"),
            "answerability": str(compact.get("answerability") or "none"),
            "evidence": [],
            "background_context": [],
            "related_context": [],
            "warnings": [COMPACT_TRUNCATION_WARNING],
        }
    return compact


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
