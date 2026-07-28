from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .db_runtime import DbRegistry
from .dbs import require_db_name
from .env import load_env
from .paths import dbs_dir
from .retrieval import adaptive_hybrid_query, cold_lexical_fast_path, hybrid_query
from .search_request import normalize_search_request
from .source_paths import SourcePathError, canonical_stored_path
from .token_budget import conservative_token_count, truncate_to_token_limit
from .tokenize import canonicalize, extract_anchors, identifier_match_keys


_REGISTRY: DbRegistry | None = None
RETRIEVAL_MODES = {"hybrid", "lexical", "dense"}
COMPACT_BACKGROUND_LIMIT = 2
COMPACT_RELATED_LIMIT = 2
COMPACT_DOCUMENT_RESULT_LIMIT = 10
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
    "answer_goal",
    "evidence",
    "background_context",
    "related_context",
    "document_results",
    "coverage",
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
    search_request: dict[str, Any] | None = None,
    deadline_monotonic: float | None = None,
    dense_runtime_ready: bool = False,
) -> dict[str, Any]:
    load_env()
    name = require_db_name(db_name)
    store = registry().get(name)
    mode = _normalize_retrieval_mode(retrieval_mode, use_dense=use_dense)
    request = normalize_search_request(
        search_request
        or {
            "original_question": question,
            "answer_goal": "evidence",
        }
    )
    question = str(request["original_question"])
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
            additional_identifiers=list(
                request.get("literal_identifiers") or []
            ),
        )
        payload.setdefault("identifier_diagnostics_enabled", True)
    else:
        payload["identifier_diagnostics_enabled"] = False
    payload["retrieval_mode"] = mode
    payload["retrieval_route"] = mode
    payload["dense_used"] = mode in {"hybrid", "dense"}
    _apply_answer_goal_ranking(payload, str(request["answer_goal"]))
    _add_discovery_lane(
        payload,
        store,
        request,
        source=source,
        use_dense=mode in {"hybrid", "dense"},
        deadline_monotonic=deadline_monotonic,
        dense_runtime_ready=dense_runtime_ready,
    )
    return _finalize_search_payload(
        payload,
        store=store,
        db_name=name,
        explain=explain,
    )


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
    search_request: dict[str, Any] | None = None,
    deadline_monotonic: float | None = None,
    dense_runtime_ready: bool = False,
) -> dict[str, Any]:
    """Run the default one-operation hybrid route without repeating lexical work."""
    load_env()
    name = require_db_name(db_name)
    store = registry().get(name)
    request = normalize_search_request(
        search_request
        or {
            "original_question": question,
            "answer_goal": "evidence",
        }
    )
    question = str(request["original_question"])
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
            additional_identifiers=list(
                request.get("literal_identifiers") or []
            ),
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
    _apply_answer_goal_ranking(payload, str(request["answer_goal"]))
    _add_discovery_lane(
        payload,
        store,
        request,
        source=source,
        use_dense=True,
        deadline_monotonic=deadline_monotonic,
        dense_runtime_ready=(
            dense_runtime_ready or bool(route.get("dense_used"))
        ),
        precomputed={
            "dense": list(route.get("dense_rows") or []),
            "lexical": list(route.get("lexical_rows") or []),
            "metadata": list(route.get("metadata_rows") or []),
            "exact": list(route.get("verified_exact_rows") or []),
            "dense_ran": bool(route.get("dense_used")),
        },
    )
    return _finalize_search_payload(
        payload,
        store=store,
        db_name=name,
        explain=explain,
    )


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
        return _finalize_search_payload(
            payload,
            store=store,
            db_name=name,
            explain=explain,
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
    payload["fast_path"] = "cold_lexical"
    payload["retrieval_mode"] = "hybrid"
    payload["retrieval_route"] = "cold_lexical_fast_path"
    payload["dense_used"] = False
    payload["dense_skipped_reason"] = "cold_lexical_fast_path"
    return _finalize_search_payload(
        payload,
        store=store,
        db_name=name,
        explain=explain,
    )


def _finalize_search_payload(
    payload: dict[str, Any],
    *,
    store: Any,
    db_name: str,
    explain: bool,
) -> dict[str, Any]:
    """Finalize the retrieval payload without consulting Source Metadata.

    Browser URI generation belongs to the public wrapper one level above this
    search engine.  Keeping this layer path-only makes retrieval independent
    of the optional DB sidecar and prevents two independent URI resolvers from
    producing different links.
    """
    _strip_source_uri_fields(payload)
    _normalize_public_source_paths(payload)
    _strip_private_source_ids(payload)
    return normalize_search_contract(payload)


def _strip_source_uri_fields(payload: dict[str, Any]) -> None:
    """Remove legacy presentation URI fields from every result projection."""
    for key in (
        "evidence",
        "contexts",
        "background_context",
        "related_context",
        "document_results",
        "_result_detail_items",
    ):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            for field in (
                "uri",
                "source_provider",
                "source_url",
                "source_permalink",
                "source_link_status",
                "source_link_error",
            ):
                item.pop(field, None)
            source = item.get("source")
            if isinstance(source, dict):
                source.pop("uri", None)
    for key in ("results", "background_results", "related_results"):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                for field in (
                    "uri",
                    "root",
                    "resolved_root",
                    "source_id",
                    "source_type",
                    "source_url",
                    "source_permalink",
                ):
                    metadata.pop(field, None)


def _normalize_public_source_paths(payload: dict[str, Any]) -> None:
    """Expose only canonical DB-relative stored paths as source locations."""
    invalid_path = False
    for key in (
        "evidence",
        "contexts",
        "background_context",
        "related_context",
        "_result_detail_items",
    ):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if isinstance(source, dict) and "path" in source:
                normalized = _safe_public_stored_path(source.get("path"))
                invalid_path = invalid_path or normalized is None
                source["path"] = normalized or ""
            elif "path" in item:
                normalized = _safe_public_stored_path(item.get("path"))
                invalid_path = invalid_path or normalized is None
                item["path"] = normalized or ""
    for item in payload.get("document_results") or []:
        if not isinstance(item, dict) or "path" not in item:
            continue
        normalized = _safe_public_stored_path(item.get("path"))
        invalid_path = invalid_path or normalized is None
        item["path"] = normalized or ""
    for key in ("results", "background_results", "related_results"):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if not isinstance(metadata, dict) or "path" not in metadata:
                continue
            normalized = _safe_public_stored_path(metadata.get("path"))
            invalid_path = invalid_path or normalized is None
            metadata["path"] = normalized or ""
    if invalid_path:
        warnings = list(payload.get("warnings") or [])
        if "unsafe_stored_path_omitted" not in warnings:
            warnings.append("unsafe_stored_path_omitted")
        payload["warnings"] = warnings


def _safe_public_stored_path(value: object) -> str | None:
    try:
        return canonical_stored_path(value)
    except SourcePathError:
        return None


def _strip_private_source_ids(payload: dict[str, Any]) -> None:
    for key in (
        "evidence",
        "contexts",
        "background_context",
        "related_context",
        "document_results",
        "_result_detail_items",
    ):
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                item.pop("_source_id", None)


def _normalize_retrieval_mode(mode: str, *, use_dense: bool = True) -> str:
    if not use_dense and mode == "hybrid":
        return "lexical"
    normalized = (mode or "hybrid").strip().lower()
    if normalized not in RETRIEVAL_MODES:
        raise ValueError(f"retrieval_mode must be one of {sorted(RETRIEVAL_MODES)}")
    return normalized


def _add_discovery_lane(
    payload: dict[str, Any],
    store: Any,
    request: dict[str, Any],
    *,
    source: str,
    use_dense: bool,
    precomputed: dict[str, Any] | None = None,
    deadline_monotonic: float | None = None,
    dense_runtime_ready: bool = False,
) -> None:
    """Build a recall-first document lane independently from evidence packing."""
    question = str(request["original_question"])
    coverage_request = dict(request.get("coverage") or {})
    chunks: dict[str, dict[str, Any]] = {}
    dense_used = False
    discovery_warnings: list[str] = []
    requested_facets = [
        str(facet.get("query") or "")
        for facet in request.get("facets") or []
        if facet.get("query")
    ]
    answer_goal = str(request.get("answer_goal") or "evidence")

    def add_rows(
        rows: list[dict[str, Any]],
        *,
        signal: str,
        facet: str,
        weight: float,
        literal: str | None = None,
    ) -> None:
        for rank, row in enumerate(rows, start=1):
            if _is_test_fixture_row(row):
                continue
            chunk_id = str(row.get("id") or "")
            if not chunk_id:
                continue
            item = chunks.setdefault(
                chunk_id,
                {
                    "row": row,
                    "score": 0.0,
                    "signals": set(),
                    "facets": set(),
                    "ranks": {},
                    "literal_identifiers": set(),
                },
            )
            score = weight / (60.0 + rank)
            if score > float(item["score"]):
                item["score"] = score
                item["row"] = row
            item["signals"].add(signal)
            if facet:
                item["facets"].add(facet[:100])
            previous = item["ranks"].get(signal)
            item["ranks"][signal] = (
                rank if previous is None else min(int(previous), rank)
            )
            if literal and _raw_identifier_occurs(row, literal):
                item["literal_identifiers"].add(literal)

    original_label = question[:100]
    if precomputed:
        add_rows(
            list(precomputed.get("lexical") or []),
            signal="lexical",
            facet=original_label,
            weight=1.1,
        )
        add_rows(
            list(precomputed.get("metadata") or []),
            signal="metadata",
            facet=original_label,
            weight=0.7,
        )
        add_rows(
            list(precomputed.get("exact") or []),
            signal="exact",
            facet=original_label,
            weight=1.4,
        )
        if precomputed.get("dense_ran"):
            add_rows(
                list(precomputed.get("dense") or []),
                signal="dense",
                facet=original_label,
                weight=1.0,
            )
            dense_used = True

    lexical_queries: list[tuple[str, str, float]] = []
    if not precomputed:
        lexical_queries.append((question, original_label, 1.0))
    for facet_index, facet in enumerate(request.get("facets") or []):
        query = str(facet.get("query") or "")
        if query and query != question:
            lexical_queries.append(
                (
                    query,
                    query[:100],
                    0.85
                    * _answer_goal_facet_factor(
                        answer_goal,
                        kind=str(facet.get("kind") or "semantic"),
                        index=facet_index,
                    ),
                )
            )
    for entity in request.get("entities") or []:
        if entity and entity != question:
            lexical_queries.append((entity, str(entity)[:100], 0.65))
    for query, label, factor in lexical_queries:
        if (
            deadline_monotonic is not None
            and deadline_monotonic - time.monotonic() < 0.5
        ):
            discovery_warnings.append(
                "discovery_incomplete_insufficient_remaining_deadline"
            )
            break
        try:
            add_rows(
                store.bm25_search(
                    query,
                    top_k=30,
                    source=source,
                ),
                signal="lexical",
                facet=label,
                weight=1.1 * factor,
            )
            add_rows(
                store.metadata_search(
                    query,
                    top_k=20,
                    source=source,
                ),
                signal="metadata",
                facet=label,
                weight=0.7 * factor,
            )
        except Exception as exc:
            discovery_warnings.append(
                f"discovery lexical unavailable: {type(exc).__name__}"
            )

    literal_identifiers = list(request.get("literal_identifiers") or [])
    for anchor in (payload.get("identifiers") or {}).get("anchors") or []:
        if anchor not in literal_identifiers:
            literal_identifiers.append(str(anchor))
    literal_weight = 1.4 * _answer_goal_facet_factor(
        answer_goal,
        kind="literal",
        index=0,
    )
    for literal in literal_identifiers[:3]:
        try:
            verified = [
                row
                for row in store.exact_search(
                    literal,
                    top_k=20,
                    source=source,
                )
                if _raw_identifier_occurs(row, literal)
            ]
            add_rows(
                verified,
                signal="exact",
                facet=str(literal),
                weight=literal_weight,
                literal=str(literal),
            )
        except Exception as exc:
            discovery_warnings.append(
                f"discovery exact unavailable: {type(exc).__name__}"
            )

    dense_queries: list[tuple[str, str, float]] = []
    if use_dense and not bool((precomputed or {}).get("dense_ran")):
        dense_queries.append((question, original_label, 1.0))
    if use_dense:
        for facet_index, facet in enumerate(request.get("facets") or []):
            if facet.get("kind") != "semantic":
                continue
            query = str(facet.get("query") or "")
            if query and query != question:
                dense_queries.append(
                    (
                        query,
                        query[:100],
                        0.85
                        * _answer_goal_facet_factor(
                            answer_goal,
                            kind="semantic",
                            index=facet_index,
                        ),
                    )
                )
        for concept in request.get("inferred_concepts") or []:
            term = str(concept.get("term") or "")
            if term:
                dense_queries.append((term, term[:100], 0.45))
    unique_dense: list[tuple[str, str, float]] = []
    seen_dense: set[str] = set()
    for item in dense_queries:
        if item[0] in seen_dense:
            continue
        seen_dense.add(item[0])
        unique_dense.append(item)
    if dense_runtime_ready:
        dense_minimum_seconds = 3.0
    elif os.name == "nt":
        dense_minimum_seconds = 13.0
    elif sys.platform == "darwin":
        # A fresh ONNX session can exceed the complete user deadline on macOS.
        # Return lexical discovery first and let the persistent worker warm the
        # model after it has sent that bounded response.
        dense_minimum_seconds = 30.0
    else:
        dense_minimum_seconds = 15.0
    dense_remaining = (
        deadline_monotonic - time.monotonic()
        if deadline_monotonic is not None
        else None
    )
    dense_deadline_skipped = bool(
        unique_dense
        and dense_remaining is not None
        and dense_remaining < dense_minimum_seconds
    )
    if dense_deadline_skipped:
        discovery_warnings.append(
            "dense_discovery_unavailable_within_deadline"
        )
        payload["dense_skipped_reason"] = (
            "insufficient_remaining_deadline"
        )
    elif unique_dense:
        try:
            queries = [item[0] for item in unique_dense]
            if hasattr(store, "vector_query_many"):
                dense_batches = store.vector_query_many(
                    queries,
                    top_k=30,
                    source=source,
                )
            else:
                dense_batches = [
                    store.vector_query(query, top_k=30, source=source)
                    for query in queries
                ]
            for rows, (_query, label, factor) in zip(
                dense_batches,
                unique_dense,
            ):
                add_rows(
                    rows,
                    signal="dense",
                    facet=label,
                    weight=1.0 * factor,
                )
            dense_used = True
        except Exception as exc:
            discovery_warnings.append(
                f"dense_discovery_error:{type(exc).__name__}"
            )

    evidence_paths = {
        str((item.get("source") or {}).get("path") or "")
        for item in payload.get("evidence") or []
        if (item.get("source") or {}).get("path")
    }
    evidence_texts = {
        str(item.get("text") or "")
        for item in payload.get("evidence") or []
        if item.get("text")
    }
    documents: dict[str, dict[str, Any]] = {}
    for chunk_id, item in chunks.items():
        row = item["row"]
        metadata = row.get("metadata") or {}
        path = str(
            metadata.get("path")
            or metadata.get("source_path")
            or metadata.get("doc_id")
            or chunk_id
        )
        document = documents.setdefault(
            path,
            {
                "path": path,
                "title": str(
                    metadata.get("title")
                    or metadata.get("document_title")
                    or metadata.get("filename")
                    or Path(path).name
                ),
                "score": 0.0,
                "best": item,
                "signals": set(),
                "facets": set(),
                "literal_identifiers": set(),
                "ranks": {},
                "candidates": [],
                "_source_id": str(
                    metadata.get("source_id") or ""
                ),
            },
        )
        if all(
            str(
                (candidate.get("row") or {}).get("id") or ""
            )
            != chunk_id
            for candidate in document["candidates"]
        ):
            document["candidates"].append(item)
        score = float(item["score"])
        if score > float(document["score"]):
            document["score"] = score
            document["best"] = item
            document["_source_id"] = str(
                metadata.get("source_id") or ""
            )
        document["signals"].update(item["signals"])
        document["facets"].update(item["facets"])
        document["literal_identifiers"].update(
            item["literal_identifiers"]
        )
        for signal, rank in item["ranks"].items():
            previous = document["ranks"].get(signal)
            document["ranks"][signal] = (
                int(rank)
                if previous is None
                else min(int(previous), int(rank))
            )

    ranked_documents: list[dict[str, Any]] = []
    for document in documents.values():
        signals = set(document["signals"])
        facets = set(document["facets"])
        document["score"] = (
            float(document["score"])
            + min(0.006, 0.0015 * max(0, len(facets) - 1))
            + min(0.004, 0.0015 * max(0, len(signals) - 1))
        )
        is_direct = document["path"] in evidence_paths
        if is_direct:
            support_level = "direct"
        elif document["literal_identifiers"] or (
            {"dense", "lexical"}.issubset(signals)
            and max(
                int(document["ranks"].get("dense") or 10_000),
                int(document["ranks"].get("lexical") or 10_000),
            )
            <= 10
        ):
            support_level = "strong"
        elif len(facets) >= 2 or float(document["score"]) >= 0.017:
            support_level = "moderate"
        else:
            support_level = "weak"
        document["support_level"] = support_level
        ranked_documents.append(document)
    support_order = {"direct": 3, "strong": 2, "moderate": 1, "weak": 0}
    ranked_documents.sort(
        key=lambda item: (
            support_order[item["support_level"]],
            float(item["score"]),
        ),
        reverse=True,
    )

    maximum = int(coverage_request.get("maximum_distinct_documents") or 10)
    target = min(
        maximum,
        int(coverage_request.get("target_distinct_documents") or 8),
    )
    if not bool(coverage_request.get("allow_weak_related", True)):
        ranked_documents = [
            document
            for document in ranked_documents
            if document["support_level"] != "weak"
        ]
    selected: list[dict[str, Any]] = []
    selected_paths: set[str] = set()

    def select_document(document: dict[str, Any]) -> None:
        path = str(document["path"])
        if path in selected_paths or len(selected) >= target:
            return
        selected.append(document)
        selected_paths.add(path)

    for document in ranked_documents:
        if document["support_level"] == "direct":
            select_document(document)
    # Round-robin across requested facets before score-only fill so one
    # perspective cannot occupy the complete discovery list.
    made_progress = True
    while len(selected) < target and made_progress:
        made_progress = False
        for facet in requested_facets:
            candidate = next(
                (
                    document
                    for document in ranked_documents
                    if document["path"] not in selected_paths
                    and facet[:100] in document["facets"]
                ),
                None,
            )
            if candidate is not None:
                select_document(candidate)
                made_progress = True
            if len(selected) >= target:
                break
    for document in ranked_documents:
        select_document(document)
    cards: list[dict[str, Any]] = []
    cached_details: list[dict[str, Any]] = []
    for index, document in enumerate(selected, start=1):
        best = document["best"]
        row = best["row"]
        metadata = row.get("metadata") or {}
        preview = _document_preview(
            str(row.get("text") or ""),
            evidence_texts=evidence_texts,
        )
        support_level = str(document["support_level"])
        contains_literal = bool(document["literal_identifiers"])
        cards.append(
            {
                "path": document["path"],
                "title": document["title"],
                "section": str(
                    metadata.get("section_path")
                    or metadata.get("chunk_title")
                    or (
                        f"Page {metadata['page']}"
                        if metadata.get("page") not in (None, "")
                        else ""
                    )
                ),
                "preview": preview,
                "support_level": support_level,
                "authoritative": support_level == "direct",
                "contains_literal_identifier": contains_literal,
                "matched_facets": sorted(document["facets"])[:4],
                "retrieval_signals": sorted(document["signals"]),
                "relationship": _document_relationship(
                    support_level,
                    contains_literal=contains_literal,
                ),
                "rank": index,
                "_source_id": str(document.get("_source_id") or ""),
            }
        )
        cached_details.append(_cached_document_detail(document))

    counts = {
        level: sum(
            1 for card in cards if card["support_level"] == level
        )
        for level in ("direct", "strong", "moderate", "weak")
    }
    covered_facets = {
        facet
        for card in cards
        for facet in card.get("matched_facets") or []
        if facet in requested_facets
    }
    payload["document_results"] = cards
    payload["_result_detail_items"] = cached_details
    payload["coverage"] = {
        "policy": coverage_request.get("policy") or "wide",
        "exact_identifier_found": any(
            bool(card.get("contains_literal_identifier"))
            for card in cards
        ),
        "candidate_chunks": len(chunks),
        "candidate_documents": len(documents),
        "returned_distinct_documents": len(cards),
        "direct_documents": counts["direct"],
        "strong_documents": counts["strong"],
        "moderate_documents": counts["moderate"],
        "weak_documents": counts["weak"],
        "facets_requested": len(requested_facets),
        "facets_covered": len(covered_facets),
        "dense_discovery_used": dense_used,
    }
    payload["answer_goal"] = request.get("answer_goal")
    warnings = list(payload.get("warnings") or [])
    warnings.extend(discovery_warnings)
    minimum_desired = int(
        coverage_request.get("minimum_desired_documents") or 6
    )
    if len(cards) < minimum_desired:
        warnings.append("insufficient_distinct_related_documents")
    payload["warnings"] = sorted(set(warnings))
    if cards and not payload.get("evidence"):
        payload["status"] = "partial"
        payload["answerability"] = "none"
        payload.pop("legacy_status", None)


def _apply_answer_goal_ranking(
    payload: dict[str, Any],
    answer_goal: str,
) -> None:
    """Apply a small, stable signal preference without changing evidence."""
    priorities = {
        "definition": {
            "exact": 5,
            "lexical_anchor": 4,
            "lexical": 3,
            "metadata": 2,
            "dense": 1,
        },
        "evidence": {
            "exact": 5,
            "lexical_anchor": 4,
            "lexical": 3,
            "dense": 3,
            "metadata": 2,
        },
        "comparison": {"dense": 4, "lexical": 3, "metadata": 2},
        "procedure": {"lexical": 4, "dense": 3, "metadata": 2},
        "history": {"lexical": 4, "metadata": 3, "dense": 2},
        "survey": {"dense": 4, "lexical": 3, "metadata": 3},
    }.get(answer_goal, {})
    evidence = list(payload.get("evidence") or [])
    if len(evidence) < 2 or not priorities:
        payload["answer_goal"] = answer_goal
        return
    indexed = list(enumerate(evidence))
    indexed.sort(
        key=lambda entry: (
            -max(
                (
                    priorities.get(str(signal), 0)
                    for signal in entry[1].get("signals") or []
                ),
                default=0,
            ),
            entry[0],
        )
    )
    ranked = [item for _index, item in indexed]
    payload["evidence"] = ranked
    payload["contexts"] = list(ranked)
    payload["answer_goal"] = answer_goal


def _answer_goal_facet_factor(
    answer_goal: str,
    *,
    kind: str,
    index: int,
) -> float:
    if kind == "literal":
        return 1.15 if answer_goal in {"definition", "evidence"} else 1.0
    base = {
        "comparison": 1.12,
        "procedure": 1.08,
        "history": 1.08,
        "survey": 1.12,
        "definition": 1.0,
        "evidence": 1.04,
    }.get(answer_goal, 1.0)
    return max(0.85, base - (0.03 * max(0, index)))


def _is_test_fixture_row(row: dict[str, Any]) -> bool:
    value = (row.get("metadata") or {}).get("test_fixture")
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _document_preview(
    text: str,
    *,
    evidence_texts: set[str],
) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    preview = normalized[:220]
    if len(normalized) > 220:
        preview = preview.rstrip() + "…"
    if preview in evidence_texts:
        preview = preview[: max(1, min(180, len(preview) - 1))].rstrip() + "…"
    return preview


def _document_relationship(
    support_level: str,
    *,
    contains_literal: bool,
) -> str:
    if support_level == "direct":
        return "Contains direct evidence used in the answer."
    if contains_literal:
        return (
            "Contains a verified literal occurrence, but this card is not "
            "authoritative answer evidence."
        )
    if support_level == "strong":
        return "Matched multiple retrieval signals or a high-ranked facet."
    if support_level == "moderate":
        return "Provides useful surrounding material for one or more facets."
    return (
        "A weak research lead with a positive retrieval signal; it does not "
        "prove the answer."
    )


def _cached_document_detail(
    document: dict[str, Any],
) -> dict[str, Any]:
    best = document["best"]
    row = best["row"]
    metadata = row.get("metadata") or {}
    section = str(
        metadata.get("section_path")
        or metadata.get("chunk_title")
        or ""
    )
    ranked_candidates = sorted(
        list(document.get("candidates") or []),
        key=lambda item: float(item.get("score") or 0.0),
        reverse=True,
    )
    additional_sections: list[dict[str, Any]] = []
    seen_ids = {str(row.get("id") or "")}
    for candidate in ranked_candidates:
        candidate_row = candidate.get("row") or {}
        candidate_id = str(candidate_row.get("id") or "")
        if not candidate_id or candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        candidate_metadata = candidate_row.get("metadata") or {}
        additional_sections.append(
            {
                "chunk_uid": candidate_id,
                "heading": str(
                    candidate_metadata.get("section_path")
                    or candidate_metadata.get("chunk_title")
                    or ""
                )[:160],
                "text": str(candidate_row.get("text") or "")[:1_600],
            }
        )
        if len(additional_sections) >= 1:
            break
    source_range: dict[str, Any] = {
        "kind": "matched",
        "chunk_uid": str(row.get("id") or ""),
        "section": section,
    }
    if metadata.get("chunk_index") not in (None, ""):
        source_range["chunk_index"] = metadata["chunk_index"]
    for key in ("page", "slide", "lines"):
        if metadata.get(key) not in (None, ""):
            source_range[key] = metadata[key]
    return {
        "path": str(document.get("path") or ""),
        "_source_id": str(
            metadata.get("source_id")
            or document.get("_source_id")
            or ""
        ),
        "document_id": str(metadata.get("doc_id") or ""),
        "chunk_uid": str(row.get("id") or ""),
        "heading_path": [section] if section else [],
        "matched_excerpt": str(row.get("text") or "")[:2_400],
        "context_before": str(row.get("context_before") or "")[:1_000],
        "context_after": str(row.get("context_after") or "")[:1_000],
        "additional_sections": additional_sections,
        "table_context": row.get("table_context"),
        "source_ranges": [source_range],
        "context_reason": str(row.get("context_reason") or ""),
        "warnings": [
            str(value)[:160]
            for value in row.get("context_warnings") or []
        ],
    }


def json_payload(rows: list[dict[str, Any]], question: str, db_name: str, max_chars: int, *, db_hint: str = "") -> dict[str, Any]:
    converted: list[tuple[dict[str, Any], dict[str, Any], set[str]]] = []
    warnings: list[str] = []
    truncated = False
    for row in rows:
        warnings.extend((row.get("debug") or {}).get("warnings") or [])
        warnings.extend(row.get("context_warnings") or [])
        meta = row.get("metadata") or {}
        text = row.get("text") or ""
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        item: dict[str, Any] = {
            "id": f"R{row['rank']}",
            "_source_id": str(
                meta.get("source_id") or ""
            ),
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
        item["matched_excerpt"] = text
        heading = str(
            row.get("heading")
            or meta.get("section_path")
            or meta.get("chunk_title")
            or ""
        )
        if heading:
            item["heading"] = heading
        for key in ("context_before", "context_after", "context_reason"):
            if row.get(key) not in (None, ""):
                item[key] = row[key]
        if row.get("source_ranges"):
            item["source_ranges"] = list(row["source_ranges"])
        if row.get("context_warnings"):
            item["context_warnings"] = list(row["context_warnings"])
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
    additional_identifiers: list[str] | None = None,
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
    for identifier in additional_identifiers or []:
        if (
            identifier
            and canonicalize(identifier) not in excluded
            and identifier not in anchors
        ):
            anchors.append(identifier)
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
    document_results = list(payload.get("document_results") or [])
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
        source_link = _preferred_source_link(item)
        if source_link:
            lines.append(f"Source link: {source_link}")
        if explain and item.get("source_link_status"):
            lines.append(
                "Source link status: "
                + str(item["source_link_status"])
            )
        if item.get("context_before"):
            lines.append(
                "Context before: " + str(item["context_before"])
            )
        lines.append(
            str(item.get("matched_excerpt") or item.get("text") or "")
        )
        if item.get("context_after"):
            lines.append(
                "Context after: " + str(item["context_after"])
            )
        if explain and item.get("debug"):
            lines.append(f"signals={','.join(item.get('signals') or [])} debug={json.dumps(item['debug'], ensure_ascii=False)}")
        lines.append("")
    if background:
        lines.extend(["## Background context (not direct evidence)", ""])
        for item in background:
            source = item.get("source") or {}
            lines.append(f"[{item.get('id')}] {source.get('path') or ''}")
            source_link = _preferred_source_link(item)
            if source_link:
                lines.append(f"Source link: {source_link}")
            if explain and item.get("source_link_status"):
                lines.append(
                    "Source link status: "
                    + str(item["source_link_status"])
                )
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
    if document_results:
        lines.extend(["## Related documents (discovery results)", ""])
        for item in document_results:
            support = str(item.get("support_level") or "weak")
            path = str(item.get("path") or "")
            title = str(item.get("title") or "")
            heading = title if title and title != path else path
            lines.append(f"- [{support}] {heading} ({path})")
            source_link = _preferred_source_link(item)
            if source_link:
                lines.append(f"  Source link: {source_link}")
            if explain and item.get("source_link_status"):
                lines.append(
                    "  Source link status: "
                    + str(item["source_link_status"])
                )
            if item.get("relationship"):
                lines.append(f"  {item['relationship']}")
            if item.get("preview"):
                lines.append(f"  {item['preview']}")
        lines.extend(
            [
                "",
                "Related documents are discovery leads. Only direct evidence "
                "may support factual claims.",
                "",
            ]
        )
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


def _preferred_source_link(item: dict[str, Any]) -> str:
    return str(item.get("uri") or "")


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
    for key in (
        "evidence",
        "contexts",
        "background_context",
        "related_context",
        "document_results",
        "warnings",
    ):
        value = normalized.get(key)
        normalized[key] = list(value) if isinstance(value, list) else []
    if not isinstance(normalized.get("coverage"), dict):
        normalized["coverage"] = {}
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
    (
        compact["document_results"],
        documents_truncated,
    ) = _project_document_results(
        list(normalized.get("document_results") or []),
        limit=COMPACT_DOCUMENT_RESULT_LIMIT,
        explain=explain,
    )
    projection_truncated = (
        evidence_truncated
        or background_truncated
        or related_truncated
        or documents_truncated
    )
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
        "document_results",
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
    fitted = _finalize_compact_projection(
        compact,
        projection_truncated=projection_truncated,
    )
    if (
        isinstance(fitted.get("coverage"), dict)
        and "returned_distinct_documents" in fitted["coverage"]
    ):
        cards = list(fitted.get("document_results") or [])
        fitted["coverage"]["returned_distinct_documents"] = len(cards)
        for level in ("direct", "strong", "moderate", "weak"):
            fitted["coverage"][f"{level}_documents"] = sum(
                1
                for card in cards
                if card.get("support_level") == level
            )
    return fitted


def _project_document_results(
    results: list[dict[str, Any]],
    *,
    limit: int,
    explain: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    projected: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    truncated = len(results) > limit
    for result in results:
        path = str(result.get("path") or "")[:400]
        if not path or path in seen_paths:
            truncated = True
            continue
        seen_paths.add(path)
        item: dict[str, Any] = {
            "path": path,
            "title": str(result.get("title") or "")[:160],
            "section": str(result.get("section") or "")[:160],
            "preview": str(result.get("preview") or "")[:220],
            "support_level": str(
                result.get("support_level") or "weak"
            )[:20],
            "authoritative": bool(result.get("authoritative")),
            "contains_literal_identifier": bool(
                result.get("contains_literal_identifier")
            ),
            "matched_facets": [
                str(value)[:100]
                for value in (result.get("matched_facets") or [])[:4]
            ],
            "retrieval_signals": [
                str(value)[:30]
                for value in (result.get("retrieval_signals") or [])[:5]
            ],
            "relationship": str(
                result.get("relationship") or ""
            )[:220],
        }
        _copy_projected_uri(
            result,
            item,
            explain=explain,
        )
        projected.append(item)
        if len(projected) >= limit:
            truncated = truncated or len(results) > len(projected)
            break
    return projected, truncated


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
        for key in ("context_before", "context_after"):
            value = str(item.get(key) or "")
            if _conservative_token_count(value) > COMPACT_AUXILIARY_TOKEN_LIMIT:
                item[key] = _truncate_to_token_limit(
                    value,
                    COMPACT_AUXILIARY_TOKEN_LIMIT,
                )
                truncated = True
        overhead = dict(item)
        for key in (
            "text",
            "matched_excerpt",
            "context_before",
            "context_after",
            "uri",
        ):
            overhead[key] = ""
        overhead_tokens = _conservative_token_count(
            json.dumps(overhead, ensure_ascii=False, separators=(",", ":"))
        )
        if overhead_tokens >= remaining:
            truncated = True
            break
        text = str(item.get("text") or "")
        context_tokens = sum(
            _conservative_token_count(str(item.get(key) or ""))
            for key in ("context_before", "context_after")
        )
        # text and matched_excerpt intentionally carry the same primary
        # excerpt for backward compatibility and the additive API contract.
        excerpt_copies = 2 if item.get("matched_excerpt") else 1
        text_budget = max(
            0,
            (
                remaining
                - overhead_tokens
                - context_tokens
                - 30
            )
            // excerpt_copies,
        )
        if item_limit is not None:
            text_budget = min(
                text_budget,
                max(0, COMPACT_AUXILIARY_TOKEN_LIMIT - overhead_tokens - 20),
            )
        if _conservative_token_count(text) > text_budget:
            text = _truncate_to_token_limit(text, text_budget)
            item["text"] = text
            if item.get("matched_excerpt"):
                item["matched_excerpt"] = text
            item["truncated"] = True
            truncated = True
        budgeted_item = dict(item)
        for key in (
            "uri",
        ):
            budgeted_item.pop(key, None)
        used = _conservative_token_count(
            json.dumps(
                budgeted_item,
                ensure_ascii=False,
                separators=(",", ":"),
            )
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
    matched_excerpt = str(context.get("matched_excerpt") or "")
    if matched_excerpt:
        projected["matched_excerpt"] = matched_excerpt
    if context.get("heading"):
        projected["heading"] = str(context["heading"])[:160]
    for key in ("context_before", "context_after"):
        if context.get(key):
            projected[key] = str(context[key])[:300]
    if context.get("context_reason"):
        projected["context_reason"] = str(
            context["context_reason"]
        )[:80]
    if context.get("source_ranges"):
        projected["source_ranges"] = [
            dict(value)
            for value in list(context["source_ranges"])[:3]
            if isinstance(value, dict)
        ]
    if context.get("context_warnings"):
        projected["context_warnings"] = [
            str(value)[:80]
            for value in list(context["context_warnings"])[:3]
        ]
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
    _copy_projected_uri(
        context,
        projected,
        explain=explain,
    )
    if explain and context.get("debug"):
        projected["debug"] = context["debug"]
    return projected


def _copy_projected_uri(
    source: dict[str, Any],
    target: dict[str, Any],
    *,
    explain: bool,
) -> None:
    value = str(source.get("uri") or "")
    if value:
        target["uri"] = value


def _conservative_token_count(text: str) -> int:
    return conservative_token_count(text)


def _truncate_to_token_limit(text: str, limit: int) -> str:
    return truncate_to_token_limit(text, limit)


def _finalize_compact_projection(
    compact: dict[str, Any],
    *,
    projection_truncated: bool,
) -> dict[str, Any]:
    """Return the complete compact projection without content fitting.

    Compact output remains structurally concise, but byte size must not cause
    evidence, discovery cards, or resolved source links to disappear.
    """
    if projection_truncated:
        compact["warnings"] = sorted(
            set([*compact.get("warnings", []), COMPACT_TRUNCATION_WARNING])
        )
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
