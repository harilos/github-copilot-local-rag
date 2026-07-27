from __future__ import annotations

import math
import re
from typing import Any, Protocol

from . import catalog
from .manifest import ConfigMismatchError
from .token_budget import conservative_token_count, truncate_to_token_limit
from .tokenize import (
    canonicalize,
    extract_anchors,
    identifier_match_keys,
    tokens_for_fts,
)


RRF_K = 60
DEFAULT_DENSE_K = 30
DEFAULT_LEXICAL_K = 30
DEFAULT_METADATA_K = 20
DEFAULT_EXACT_K = 20
DEFAULT_RRF_K = 24
DEFAULT_FAMILY_FLOOR_K = 3
DEFAULT_ANCHORED_MAX_PER_DOC = 4
DEFAULT_PRIMARY_PROTECTED_COUNT = 3
DEFAULT_PRIMARY_BUDGET_RATIO = 0.75
DEFAULT_CONTEXT_MAX_CHARS = 280
DEFAULT_CONTEXT_TOKEN_BUDGET = 240
_GENERIC_IDENTIFIER_LOOKUP_TERMS = {
    "about",
    "detail",
    "details",
    "document",
    "documents",
    "evidence",
    "explain",
    "information",
    "is",
    "me",
    "please",
    "source",
    "sources",
    "tell",
    "what",
    "アバウト",
    "つく",
    "だけ",
    "から",
    "こと",
    "ホワット",
    "ローカル",
    "居る",
    "教える",
    "書く",
    "根拠",
    "確認",
    "範囲",
    "資料",
}


class SearchBackend(Protocol):
    def vector_query(self, question: str, top_k: int, source: str = "any") -> list[dict[str, Any]]: ...
    def vector_query_many(self, questions: list[str], top_k: int, source: str = "any") -> list[list[dict[str, Any]]]: ...
    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]: ...
    def bm25_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]: ...
    def anchor_lexical_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]: ...
    def metadata_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]: ...
    def fetch_rows_by_ids(self, ids: Any) -> dict[str, dict[str, Any]]: ...
    def get_neighbor_rows(self, chunk_uid: str, *, window: int = 1) -> list[dict[str, Any]]: ...


class _GlobalBackend:
    def vector_query(self, question: str, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        from .store import vector_query

        return vector_query(question, top_k=top_k, source=source)

    def vector_query_many(
        self,
        questions: list[str],
        top_k: int,
        source: str = "any",
    ) -> list[list[dict[str, Any]]]:
        from .store import vector_query_many

        return vector_query_many(
            questions,
            top_k=top_k,
            source=source,
        )

    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        return catalog.exact_search(question, top_k=top_k, source=source)

    def bm25_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        return catalog.bm25_search(question, top_k=top_k, source=source)

    def anchor_lexical_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        return catalog.anchor_lexical_search(question, top_k=top_k, source=source)

    def metadata_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        return catalog.metadata_search(question, top_k=top_k, source=source)

    def fetch_rows_by_ids(self, ids: Any) -> dict[str, dict[str, Any]]:
        return catalog.fetch_rows_by_ids(ids)

    def get_neighbor_rows(self, chunk_uid: str, *, window: int = 1) -> list[dict[str, Any]]:
        return catalog.get_neighbor_rows(chunk_uid, window=window)


def hybrid_query(
    question: str,
    *,
    top_k: int,
    source: str = "any",
    fetch_k: int | None = None,
    max_per_doc: int = 2,
    budget_tokens: int | None = None,
    explain: bool = False,
    use_dense: bool = True,
    use_lexical: bool = True,
    backend: SearchBackend | None = None,
) -> list[dict[str, Any]]:
    backend = backend or _GlobalBackend()
    dense_k = fetch_k or max(DEFAULT_DENSE_K, top_k * 4)
    family_rankings: list[tuple[str, float, list[dict[str, Any]]]] = []
    warnings: list[str] = []

    if use_dense:
        try:
            dense_rows = _without_test_fixtures(
                backend.vector_query(question, top_k=dense_k, source=source)
            )
            family_rankings.append(("dense", 1.0, dense_rows))
        except ConfigMismatchError:
            raise
        except Exception as exc:
            warnings.append(f"dense search unavailable: {type(exc).__name__}: {exc}")

    exact_rows: list[dict[str, Any]] = []
    lexical_rows: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    raw_exact_complete = False
    selected_exact_document = ""
    anchor_ids: list[str] = []
    if use_lexical:
        try:
            raw_exact_rows, lexical_rows, metadata_rows, raw_exact_complete = _lexical_candidates(
                question,
                source=source,
                backend=backend,
            )
            raw_exact_rows = _without_test_fixtures(raw_exact_rows)
            exact_rows = _matching_strong_exact_rows(question, raw_exact_rows)
            selected_exact_document = _mark_exact_evidence_eligibility(
                question,
                exact_rows,
                lexical_rows,
                exact_result_set_complete=raw_exact_complete,
            )
            lexical_rows = _without_test_fixtures(lexical_rows)
            metadata_rows = _without_test_fixtures(metadata_rows)
            if use_dense and not _has_strong_exact_anchor(question, exact_rows):
                anchor_rows = _anchor_candidates(question, source=source, backend=backend)
                lexical_rows, anchor_ids = _merge_anchor_rows(lexical_rows, anchor_rows)
            family_rankings.append(("lexical", 1.1, lexical_rows))
            family_rankings.append(("metadata", 0.7, metadata_rows))
            if exact_rows:
                family_rankings.append(("exact", 1.4, exact_rows))
        except Exception as exc:
            warnings.append(f"catalog search unavailable: {type(exc).__name__}: {exc}")
            exact_rows = []

    fused = _weighted_rrf(family_rankings)
    rows = _materialize(fused, family_rankings, backend=backend)
    rows = _without_test_fixtures(rows)
    rows = _anchor_rescue(rows, exact_rows, question, anchor_ids=anchor_ids)
    document_anchors = _verified_document_anchors(
        question,
        exact_rows,
        lexical_rows,
        metadata_rows,
        exact_result_set_complete=raw_exact_complete if use_lexical else False,
        selected_exact_document=selected_exact_document if use_lexical else "",
    )
    rows, _pool_diagnostics = _postprocess_candidate_pool(
        rows,
        family_rankings,
        verified_exact_rows=exact_rows,
        top_k=max(DEFAULT_RRF_K, top_k),
        protected_metadata_doc_keys=set(_relaxed_doc_limits(document_anchors)),
    )
    rows = _dedupe_and_diversify(
        rows,
        top_k=len(rows),
        max_per_doc=max_per_doc,
        relaxed_doc_limits=_relaxed_doc_limits(document_anchors),
    )
    rows = rows[:top_k]
    rows = _expand_and_pack(
        rows,
        question=question,
        family_rankings=family_rankings,
        backend=backend,
        budget_tokens=budget_tokens,
        document_anchors=document_anchors,
    )
    rows = _without_test_fixtures(rows)

    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        if explain:
            debug = dict(row.get("debug") or {})
            debug["warnings"] = warnings
            row["debug"] = debug
        else:
            row.pop("debug", None)
            row.pop("score", None)
    return rows


def adaptive_hybrid_query(
    question: str,
    *,
    top_k: int,
    source: str = "any",
    fetch_k: int | None = None,
    max_per_doc: int = 2,
    budget_tokens: int | None = None,
    explain: bool = False,
    db_scope_confirmed: bool = False,
    excluded_identifiers: set[str] | None = None,
    backend: SearchBackend | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one local retrieval operation with a single lexical collection.

    Exact, BM25, metadata, and low-DF anchor candidates are collected once.
    Dense is added exactly once only when the lexical bundle cannot produce a
    conservative certificate.
    """
    backend = backend or _GlobalBackend()
    dense_k = fetch_k or max(DEFAULT_DENSE_K, top_k * 4)
    raw_exact_rows, lexical_rows, metadata_rows, raw_exact_complete = _lexical_candidates(
        question,
        source=source,
        backend=backend,
    )
    raw_exact_rows = _without_test_fixtures(raw_exact_rows)
    lexical_rows = _without_test_fixtures(lexical_rows)
    metadata_rows = _without_test_fixtures(metadata_rows)
    anchor_rows = _anchor_candidates(question, source=source, backend=backend)
    verified_exact_rows = _matching_strong_exact_rows(question, raw_exact_rows)
    selected_exact_document = _mark_exact_evidence_eligibility(
        question,
        verified_exact_rows,
        lexical_rows,
        exact_result_set_complete=raw_exact_complete,
    )

    strong_anchors = _strong_query_anchors(
        question,
        excluded_identifiers=excluded_identifiers,
    )
    unmatched_anchors = [
        anchor
        for anchor in strong_anchors
        if not any(_raw_anchor_occurs(row, anchor) for row in raw_exact_rows)
    ]
    certificate: dict[str, Any] | None = None
    certified_anchor_rows: list[dict[str, Any]] = []
    if unmatched_anchors and verified_exact_rows:
        certificate = {
            **(_exact_certificate(question, verified_exact_rows) or {}),
            "kind": "verified_identifier_partial",
            "unmatched_identifiers": unmatched_anchors,
        }
        _mark_fast_path_exact_certificate(
            verified_exact_rows,
            certificate=certificate,
        )
    elif unmatched_anchors:
        certificate = {
            "kind": "verified_identifier_no_hit",
            "unmatched_identifiers": unmatched_anchors,
        }
    elif verified_exact_rows:
        certificate = _exact_certificate(question, verified_exact_rows)
        _mark_fast_path_exact_certificate(
            verified_exact_rows,
            certificate=certificate,
        )
    else:
        certified_anchor_rows = _certified_anchor_rows(
            anchor_rows,
            lexical_rows,
            metadata_rows,
            question=question,
            db_scope_confirmed=db_scope_confirmed,
        )
        if certified_anchor_rows:
            certificate = dict(
                ((certified_anchor_rows[0].get("debug") or {}).get("fast_path_certificate") or {})
            )

    direct_anchor_rows = certified_anchor_rows
    lexical_family, anchor_ids = _merge_anchor_rows(
        lexical_rows,
        direct_anchor_rows,
    )
    family_rankings: list[tuple[str, float, list[dict[str, Any]]]] = [
        ("lexical", 1.1, lexical_family),
        ("metadata", 0.7, metadata_rows),
    ]
    if verified_exact_rows:
        family_rankings.append(("exact", 1.4, verified_exact_rows))

    dense_rows: list[dict[str, Any]] = []
    dense_used = certificate is None
    warnings: list[str] = []
    if dense_used:
        try:
            dense_rows = _without_test_fixtures(
                backend.vector_query(question, top_k=dense_k, source=source)
            )
            family_rankings.insert(0, ("dense", 1.0, dense_rows))
        except ConfigMismatchError:
            raise
        except Exception as exc:
            warnings.append(f"dense search unavailable: {type(exc).__name__}: {exc}")
        # Keep the already-collected anchor candidate in fusion without
        # granting it a direct-evidence signal. The anchor search is another
        # lexical view, so do not give the same row a second RRF vote when it
        # is already present in the ordinary lexical family.
        if anchor_rows:
            lexical_ids = {
                str(row.get("id") or "")
                for row in lexical_family
                if row.get("id")
            }
            novel_anchor_rows = [
                row
                for row in anchor_rows[:1]
                if str(row.get("id") or "") not in lexical_ids
            ]
            if novel_anchor_rows:
                family_rankings.append(("anchor_candidate", 1.1, novel_anchor_rows))

    rows = _weighted_rrf(family_rankings)
    materialized = _materialize(rows, family_rankings, backend=backend)
    materialized = _without_test_fixtures(materialized)
    materialized = _anchor_rescue(
        materialized,
        verified_exact_rows,
        question,
        anchor_ids=anchor_ids,
    )
    fused_materialized = list(materialized)
    document_anchors = _verified_document_anchors(
        question,
        verified_exact_rows,
        lexical_rows,
        metadata_rows,
        exact_result_set_complete=raw_exact_complete,
        selected_exact_document=selected_exact_document,
        certified_anchor_rows=certified_anchor_rows,
        allow_anchored_neighbors=not bool(
            (certificate or {}).get("unmatched_identifiers")
        ),
    )
    materialized, pool_diagnostics = _postprocess_candidate_pool(
        materialized,
        family_rankings,
        verified_exact_rows=verified_exact_rows,
        top_k=max(DEFAULT_RRF_K, top_k),
        protected_metadata_doc_keys=set(_relaxed_doc_limits(document_anchors)),
    )
    postprocess_pool_rows = list(materialized)
    materialized = _dedupe_and_diversify(
        materialized,
        top_k=len(materialized),
        max_per_doc=max_per_doc,
        relaxed_doc_limits=_relaxed_doc_limits(document_anchors),
    )
    diversified_rows = list(materialized)
    materialized = materialized[:top_k]
    postprocess_primary_count = len(materialized)
    packing_diagnostics: dict[str, list[dict[str, Any]]] = {}
    materialized = _expand_and_pack(
        materialized,
        question=question,
        family_rankings=family_rankings,
        backend=backend,
        budget_tokens=budget_tokens,
        document_anchors=document_anchors,
        packing_diagnostics=packing_diagnostics,
    )
    materialized = _without_test_fixtures(materialized)
    for rank, row in enumerate(materialized, start=1):
        row["rank"] = rank
        if explain:
            debug = dict(row.get("debug") or {})
            debug["warnings"] = warnings
            row["debug"] = debug
        else:
            row.pop("debug", None)
            row.pop("score", None)

    reason = str((certificate or {}).get("kind") or "") or None
    return materialized, {
        "retrieval_route": (
            "adaptive_lexical_certified"
            if certificate is not None
            else "adaptive_hybrid_dense"
        ),
        "dense_used": dense_used,
        "dense_skipped_reason": reason,
        "certificate": certificate,
        "raw_exact_rows": raw_exact_rows,
        "verified_exact_rows": verified_exact_rows,
        "dense_rows": dense_rows,
        "lexical_rows": lexical_rows,
        "metadata_rows": metadata_rows,
        "anchor_rows": anchor_rows,
        "retrieval_funnel": {
            **pool_diagnostics,
            "postprocess_primary_count": postprocess_primary_count,
            "final_count": len(materialized),
            "verified_document_anchor_count": len(document_anchors),
            "stages": {
                "dense": _trace_rows(dense_rows),
                "lexical": _trace_rows(lexical_family),
                "exact": _trace_rows(verified_exact_rows),
                "metadata": _trace_rows(metadata_rows),
                "fused": _trace_rows(fused_materialized),
                "postprocess_pool": _trace_rows(postprocess_pool_rows),
                "diversified": _trace_rows(diversified_rows),
                "protected_primaries": _trace_rows(
                    packing_diagnostics.get("protected_primaries") or []
                ),
                "neighbor_candidates": _trace_rows(
                    packing_diagnostics.get("neighbor_candidates") or []
                ),
                "packed": _trace_rows(materialized),
            },
        },
    }


def cold_lexical_fast_path(
    question: str,
    *,
    top_k: int,
    source: str = "any",
    max_per_doc: int = 2,
    budget_tokens: int | None = None,
    explain: bool = False,
    db_scope_confirmed: bool = False,
    backend: SearchBackend | None = None,
) -> list[dict[str, Any]] | None:
    backend = backend or _GlobalBackend()
    exact_rows, lexical_rows, metadata_rows, raw_exact_complete = _lexical_candidates(
        question,
        source=source,
        backend=backend,
    )
    exact_rows = _without_test_fixtures(exact_rows)
    lexical_rows = _without_test_fixtures(lexical_rows)
    metadata_rows = _without_test_fixtures(metadata_rows)
    anchor_rows = _anchor_candidates(question, source=source, backend=backend)
    matching_exact_rows = _matching_strong_exact_rows(question, exact_rows)
    selected_exact_document = _mark_exact_evidence_eligibility(
        question,
        matching_exact_rows,
        lexical_rows,
        exact_result_set_complete=raw_exact_complete,
    )
    exact_certificate = (
        _exact_certificate(question, matching_exact_rows)
        if matching_exact_rows
        else None
    )
    _mark_fast_path_exact_certificate(
        matching_exact_rows,
        certificate=exact_certificate,
    )
    certified_anchor_rows = _certified_anchor_rows(
        anchor_rows,
        lexical_rows,
        metadata_rows,
        question=question,
        db_scope_confirmed=db_scope_confirmed,
    )
    lexical_rows, anchor_ids = _merge_anchor_rows(lexical_rows, certified_anchor_rows)
    if not anchor_ids and not matching_exact_rows:
        return None

    families: list[tuple[str, float, list[dict[str, Any]]]] = [
        ("lexical", 1.1, lexical_rows),
        ("metadata", 0.7, metadata_rows),
    ]
    if matching_exact_rows:
        families.append(("exact", 1.4, matching_exact_rows))
    fused = _weighted_rrf(families)
    rows = _materialize(fused, families, backend=backend)
    rows = _anchor_rescue(rows, matching_exact_rows, question, anchor_ids=anchor_ids)
    document_anchors = _verified_document_anchors(
        question,
        matching_exact_rows,
        lexical_rows,
        metadata_rows,
        exact_result_set_complete=raw_exact_complete,
        selected_exact_document=selected_exact_document,
        certified_anchor_rows=certified_anchor_rows,
    )
    rows, _pool_diagnostics = _postprocess_candidate_pool(
        rows,
        families,
        verified_exact_rows=matching_exact_rows,
        top_k=max(DEFAULT_RRF_K, top_k),
        protected_metadata_doc_keys=set(_relaxed_doc_limits(document_anchors)),
    )
    rows = _dedupe_and_diversify(
        rows,
        top_k=len(rows),
        max_per_doc=max_per_doc,
        relaxed_doc_limits=_relaxed_doc_limits(document_anchors),
    )
    rows = rows[:top_k]
    rows = _expand_and_pack(
        rows,
        question=question,
        family_rankings=families,
        backend=backend,
        budget_tokens=budget_tokens,
        document_anchors=document_anchors,
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        debug = dict(row.get("debug") or {})
        debug["cold_lexical_fast_path"] = True
        if explain:
            row["debug"] = debug
        else:
            row.pop("debug", None)
            row.pop("score", None)
    return rows


def _lexical_candidates(
    question: str,
    *,
    source: str,
    backend: SearchBackend,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    bool,
]:
    exact_rows_with_sentinel = backend.exact_search(
        question,
        top_k=DEFAULT_EXACT_K + 1,
        source=source,
    )
    exact_result_set_complete = len(exact_rows_with_sentinel) <= DEFAULT_EXACT_K
    exact_rows = exact_rows_with_sentinel[:DEFAULT_EXACT_K]
    lexical_rows = backend.bm25_search(question, top_k=DEFAULT_LEXICAL_K, source=source)
    metadata_rows = backend.metadata_search(question, top_k=DEFAULT_METADATA_K, source=source)
    return exact_rows, lexical_rows, metadata_rows, exact_result_set_complete


def _weighted_rrf(families: list[tuple[str, float, list[dict[str, Any]]]]) -> dict[str, dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for family, weight, rows in families:
        seen_in_family: set[str] = set()
        for rank, row in enumerate(rows, start=1):
            chunk_id = str(row.get("id") or "")
            if not chunk_id or chunk_id in seen_in_family:
                continue
            seen_in_family.add(chunk_id)
            item = fused.setdefault(
                chunk_id,
                {"id": chunk_id, "rrf_score": 0.0, "family_ranks": {}, "signals": set(), "best_row": row},
            )
            item["rrf_score"] += weight / (RRF_K + rank)
            item["family_ranks"][family] = rank
            item["signals"].add(family)
            if _prefer_row(row, item["best_row"]):
                item["best_row"] = row
    return fused


def _materialize(
    fused: dict[str, dict[str, Any]],
    families: list[tuple[str, float, list[dict[str, Any]]]],
    *,
    backend: SearchBackend,
) -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    retrieval_debug_by_id: dict[str, dict[str, Any]] = {}
    retrieval_attributes_by_id: dict[str, dict[str, Any]] = {}
    for _family, _weight, rows in families:
        for row in rows:
            chunk_id = str(row.get("id") or "")
            if chunk_id and chunk_id not in rows_by_id:
                rows_by_id[chunk_id] = row
            debug = row.get("debug") or {}
            if chunk_id and isinstance(debug, dict):
                selected = {
                    key: value
                    for key, value in debug.items()
                    if key in {"exact_match", "lexical_anchor", "fast_path_certificate"}
                }
                if selected:
                    retrieval_debug_by_id.setdefault(chunk_id, {}).update(selected)
            if chunk_id:
                attributes = {
                    key: row[key]
                    for key in (
                        "exact_evidence_eligible",
                        "exact_evidence_document_key",
                    )
                    if key in row
                }
                if attributes:
                    retrieval_attributes_by_id.setdefault(chunk_id, {}).update(
                        attributes
                    )

    catalog_rows = backend.fetch_rows_by_ids(fused.keys())
    output: list[dict[str, Any]] = []
    for chunk_id, item in fused.items():
        base = dict(catalog_rows.get(chunk_id) or item.get("best_row") or rows_by_id.get(chunk_id) or {})
        base["id"] = chunk_id
        base["signals"] = sorted(item["signals"])
        base["score"] = item["rrf_score"]
        base["debug"] = {
            "rrf_score": item["rrf_score"],
            "family_ranks": dict(item["family_ranks"]),
            **retrieval_debug_by_id.get(chunk_id, {}),
        }
        base.update(retrieval_attributes_by_id.get(chunk_id, {}))
        output.append(base)
    output.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    return output


def _postprocess_candidate_pool(
    rows: list[dict[str, Any]],
    families: list[tuple[str, float, list[dict[str, Any]]]],
    *,
    verified_exact_rows: list[dict[str, Any]],
    top_k: int,
    family_floor_k: int = DEFAULT_FAMILY_FLOOR_K,
    protected_metadata_doc_keys: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep a bounded RRF pool while preserving strong retriever candidates.

    A retriever floor only guarantees that a candidate reaches postprocessing.
    Diversification and budget packing remain responsible for the final
    context.
    """
    row_by_id = {
        str(row.get("id") or ""): row
        for row in rows
        if row.get("id")
    }
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    sources_by_id: dict[str, set[str]] = {}

    def add(row_id: str, source: str) -> None:
        if not row_id or row_id not in row_by_id:
            return
        sources_by_id.setdefault(row_id, set()).add(source)
        if row_id in selected_ids:
            return
        copy = dict(row_by_id[row_id])
        debug = dict(copy.get("debug") or {})
        debug["candidate_pool_sources"] = sorted(sources_by_id[row_id])
        copy["debug"] = debug
        selected.append(copy)
        selected_ids.add(row_id)

    for row in rows[:top_k]:
        add(str(row.get("id") or ""), "rrf")

    protected_family_ids: dict[str, list[str]] = {}
    for family, _weight, family_rows in families:
        if family not in {"dense", "lexical", "metadata"}:
            continue
        floor_rows = family_rows[:family_floor_k]
        if family == "metadata":
            floor_rows = [
                row
                for row in family_rows
                if _doc_key(row) in (protected_metadata_doc_keys or set())
            ][:family_floor_k]
        ids = [
            str(row.get("id") or "")
            for row in floor_rows
            if row.get("id")
        ]
        protected_family_ids[family] = ids
        for row_id in ids:
            add(row_id, f"{family}_floor")

    exact_ids = [
        str(row.get("id") or "")
        for row in verified_exact_rows
        if row.get("id")
    ]
    for row_id in exact_ids:
        add(row_id, "verified_exact")

    # A row may have acquired another protection source after it was copied.
    for row in selected:
        row_id = str(row.get("id") or "")
        debug = dict(row.get("debug") or {})
        debug["candidate_pool_sources"] = sorted(sources_by_id.get(row_id) or [])
        row["debug"] = debug

    return selected, {
        "rrf_pool_limit": top_k,
        "rrf_pool_count": min(len(rows), top_k),
        "postprocess_pool_count": len(selected),
        "family_floor_k": family_floor_k,
        "protected_family_ids": protected_family_ids,
        "verified_exact_ids": exact_ids,
    }


def _trace_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    traced: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        metadata = row.get("metadata") or {}
        debug = row.get("debug") or {}
        traced.append(
            {
                "chunk_uid": str(row.get("id") or ""),
                "path": str(metadata.get("path") or ""),
                "chunk_index": metadata.get("chunk_index"),
                "text": str(row.get("text") or ""),
                "rank": rank,
                "signals": list(row.get("signals") or []),
                "support_kind": row.get("support_kind"),
                "packing_phase": row.get("packing_phase"),
                "inclusion_reasons": list(
                    debug.get("candidate_pool_sources") or []
                ),
            }
        )
    return traced


def _mark_exact_evidence_eligibility(
    question: str,
    exact_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    *,
    exact_result_set_complete: bool,
) -> str:
    selected_document = _select_exact_evidence_document(
        question,
        exact_rows,
        lexical_rows,
        exact_result_set_complete=exact_result_set_complete,
    )
    exact_docs = {
        _doc_key(row)
        for row in exact_rows
        if row.get("id") and _doc_key(row)
    }
    allow_identifier_wide_exact = bool(
        exact_result_set_complete
        and len(exact_docs) > 1
        and not selected_document
        and not _exact_document_confirmations(
            question,
            exact_rows,
            lexical_rows,
        )
        and _is_identifier_only_lookup(question)
    )
    for row in exact_rows:
        row["exact_evidence_eligible"] = bool(
            allow_identifier_wide_exact
            or (
                selected_document
                and _doc_key(row) == selected_document
            )
        )
        row["exact_evidence_document_key"] = selected_document
    return selected_document


def _select_exact_evidence_document(
    question: str,
    exact_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    *,
    exact_result_set_complete: bool,
) -> str:
    if not exact_result_set_complete:
        return ""
    docs = {
        _doc_key(row)
        for row in exact_rows
        if row.get("id") and _doc_key(row)
    }
    if len(docs) == 1:
        return next(iter(docs))
    if len(docs) < 2:
        return ""

    confirmations = _exact_document_confirmations(
        question,
        exact_rows,
        lexical_rows,
    )
    if not confirmations:
        return ""
    ranked = sorted(confirmations.items(), key=lambda item: item[1])
    if len(ranked) == 1:
        only_doc, only_rank = ranked[0]
        return only_doc if only_rank == 1 else ""
    best_doc, best_rank = ranked[0]
    second_rank = ranked[1][1]
    return best_doc if best_rank == 1 and second_rank - best_rank >= 2 else ""


def _exact_document_confirmations(
    question: str,
    exact_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
) -> dict[str, int]:
    anchors = _strong_query_anchors(question)
    docs = {
        _doc_key(row)
        for row in exact_rows
        if row.get("id") and _doc_key(row)
    }
    confirmations: dict[str, int] = {}
    for doc_key in docs:
        exact_row = next(
            (row for row in exact_rows if _doc_key(row) == doc_key),
            None,
        )
        if exact_row is None:
            continue
        anchor_term = next(
            (
                anchor
                for anchor in anchors
                if _raw_anchor_occurs(exact_row, anchor)
            ),
            "",
        )
        if not anchor_term:
            continue
        lexical_match = next(
            (
                (rank, row)
                for rank, row in enumerate(lexical_rows[:5], start=1)
                if _doc_key(row) == doc_key
            ),
            None,
        )
        if lexical_match is None:
            continue
        rank, lexical_row = lexical_match
        coverage, non_anchor_confirmed = _query_term_coverage(
            question,
            exact_row,
            lexical_row,
            anchor_token=anchor_term,
        )
        if coverage > 0.0 and non_anchor_confirmed:
            confirmations[doc_key] = rank
    return confirmations


def _verified_document_anchors(
    question: str,
    exact_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    *,
    exact_result_set_complete: bool,
    selected_exact_document: str,
    certified_anchor_rows: list[dict[str, Any]] | None = None,
    allow_anchored_neighbors: bool = True,
) -> dict[str, dict[str, Any]]:
    """Return anchor chunks whose document identity is sufficiently bounded."""
    if not allow_anchored_neighbors:
        return {}

    anchors: dict[str, dict[str, Any]] = {}
    exact_docs = {
        _doc_key(row)
        for row in exact_rows
        if row.get("id") and _doc_key(row)
    }
    selected_exact_doc = (
        selected_exact_document
        if exact_result_set_complete
        and selected_exact_document in exact_docs
        else ""
    )
    exact_certificate = _exact_certificate(question, exact_rows)
    direct_document_identifier = str(
        (exact_certificate or {}).get("kind") or ""
    ) in {"verified_rfc_exact", "verified_path_exact"}
    strong_query_anchors = _strong_query_anchors(question)
    if selected_exact_doc:
        for row in exact_rows:
            row_id = str(row.get("id") or "")
            doc_key = _doc_key(row)
            if (
                not row_id
                or not doc_key
                or doc_key != selected_exact_doc
            ):
                continue
            exact_debug = (row.get("debug") or {}).get("exact_match") or {}
            matched_terms = [
                str(term)
                for term in exact_debug.get("matched_terms") or []
                if term and _strong_anchor(str(term))
            ]
            anchor_term = next(
                (
                    anchor
                    for anchor in [*matched_terms, *strong_query_anchors]
                    if _raw_anchor_occurs(row, anchor)
                ),
                "",
            )
            if not anchor_term:
                continue
            if not direct_document_identifier:
                lexical_row = next(
                    (
                        candidate
                        for candidate in lexical_rows[:3]
                        if _same_doc(row, candidate)
                    ),
                    None,
                )
                if lexical_row is None:
                    continue
                try:
                    lexical_score = float(lexical_row.get("score"))
                except (TypeError, ValueError):
                    continue
                coverage, non_anchor_confirmed = _query_term_coverage(
                    question,
                    row,
                    lexical_row,
                    anchor_token=anchor_term,
                )
                if (
                    not math.isfinite(lexical_score)
                    or coverage <= 0.0
                    or not non_anchor_confirmed
                ):
                    continue
            anchors[row_id] = {
                "anchor_chunk_uid": row_id,
                "anchor_term": anchor_term,
                "document_key": doc_key,
                "document_frequency": len(exact_docs),
                "exact_result_set_complete": True,
                "anchor_kind": "verified_exact",
            }

    for row in certified_anchor_rows or []:
        row_id = str(row.get("id") or "")
        doc_key = _doc_key(row)
        certificate = (row.get("debug") or {}).get("fast_path_certificate") or {}
        anchor_term = str(certificate.get("token") or "")
        try:
            document_frequency = int(
                certificate.get("document_frequency") or 0
            )
        except (TypeError, ValueError):
            continue
        if (
            not row_id
            or not doc_key
            or not anchor_term
            or document_frequency != 1
        ):
            continue
        anchors[row_id] = {
            "anchor_chunk_uid": row_id,
            "anchor_term": anchor_term,
            "document_key": doc_key,
            "document_frequency": document_frequency,
            "anchor_kind": "certified_low_df_anchor",
        }
    return anchors


def _relaxed_doc_limits(
    document_anchors: dict[str, dict[str, Any]],
) -> dict[str, int]:
    return {
        str(anchor["document_key"]): DEFAULT_ANCHORED_MAX_PER_DOC
        for anchor in document_anchors.values()
        if anchor.get("document_key")
    }


def _family_signals_by_id(
    families: list[tuple[str, float, list[dict[str, Any]]]],
) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}
    for family, _weight, rows in families:
        if family == "exact":
            continue
        normalized = "lexical" if family == "anchor_candidate" else family
        for row in rows:
            row_id = str(row.get("id") or "")
            if row_id:
                output.setdefault(row_id, set()).add(normalized)
    return output


def _same_semantic_section(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_section = str((left.get("metadata") or {}).get("section_path") or "").strip()
    right_section = str((right.get("metadata") or {}).get("section_path") or "").strip()
    if not left_section or not right_section:
        return False
    if left_section.casefold() == right_section.casefold():
        return True
    left_base = re.sub(r"\s+#\d+$", "", left_section).strip()
    right_base = re.sub(r"\s+#\d+$", "", right_section).strip()
    return bool(
        left_base
        and right_base
        and left_base.casefold() == right_base.casefold()
    )


def _neighbor_distance(primary: dict[str, Any], neighbor: dict[str, Any]) -> int | None:
    try:
        primary_index = int((primary.get("metadata") or {}).get("chunk_index"))
        neighbor_index = int((neighbor.get("metadata") or {}).get("chunk_index"))
    except (TypeError, ValueError):
        return None
    return abs(primary_index - neighbor_index)


def _decorate_anchored_neighbor(
    neighbor: dict[str, Any],
    primary: dict[str, Any],
    *,
    document_anchor: dict[str, Any] | None,
    family_signals: dict[str, set[str]],
) -> dict[str, Any]:
    copy = dict(neighbor)
    copy["signals"] = ["neighbor"]
    if not document_anchor or _doc_key(copy) != document_anchor.get("document_key"):
        return copy
    distance = _neighbor_distance(primary, copy)
    if distance is None or distance > 1:
        return copy
    independent_signals = sorted(
        family_signals.get(str(copy.get("id") or ""), set())
        & {"dense", "lexical", "metadata"}
    )
    if not independent_signals and not _same_semantic_section(primary, copy):
        return copy
    copy.update(
        {
            "support_kind": "anchored_neighbor",
            "anchor_chunk_uid": str(document_anchor.get("anchor_chunk_uid") or ""),
            "anchor_term": str(document_anchor.get("anchor_term") or ""),
            "neighbor_distance": distance,
            "independent_signals": independent_signals,
        }
    )
    return copy


def _expand_and_pack(
    rows: list[dict[str, Any]],
    *,
    question: str,
    family_rankings: list[tuple[str, float, list[dict[str, Any]]]],
    backend: SearchBackend,
    budget_tokens: int | None,
    document_anchors: dict[str, dict[str, Any]],
    packing_diagnostics: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Pack every selected primary before attaching optional structure context."""
    primary_rows = _without_test_fixtures(rows)
    if not primary_rows:
        return []

    prepared = [
        {**row, "packing_phase": "primary"}
        for row in primary_rows
    ]
    if budget_tokens and budget_tokens > 0:
        primary_budget = max(
            1,
            int(budget_tokens * DEFAULT_PRIMARY_BUDGET_RATIO),
        )
        packed_primaries = _pack_protected_rows(
            prepared,
            question=question,
            budget_tokens=primary_budget,
        )
        context_budget = max(
            0,
            budget_tokens - _packed_rows_token_count(packed_primaries),
        )
    else:
        packed_primaries = prepared
        context_budget = DEFAULT_CONTEXT_TOKEN_BUDGET

    packed_primaries, context_candidates = _attach_structure_context(
        packed_primaries,
        question=question,
        backend=backend,
        context_budget_tokens=context_budget,
        verified_anchor_ids=set(document_anchors),
    )
    if packing_diagnostics is not None:
        packing_diagnostics["protected_primaries"] = list(
            packed_primaries
        )
        packing_diagnostics["neighbor_candidates"] = list(
            context_candidates
        )
        packing_diagnostics["remaining_primaries"] = []
    return packed_primaries


def _attach_structure_context(
    primary_rows: list[dict[str, Any]],
    *,
    question: str,
    backend: SearchBackend,
    context_budget_tokens: int,
    verified_anchor_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if context_budget_tokens <= 0:
        return primary_rows, []
    primary_ids = {
        str(row.get("id") or "")
        for row in primary_rows
        if row.get("id")
    }
    identifier_only_lookup = _is_identifier_only_lookup(question)
    claimed_context_ids: set[str] = set()
    output: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    remaining = context_budget_tokens

    for primary in primary_rows:
        primary_id = str(primary.get("id") or "")
        copy = dict(primary)
        copy["matched_excerpt"] = str(copy.get("text") or "")
        copy["heading"] = str(
            (copy.get("metadata") or {}).get("section_path")
            or (copy.get("metadata") or {}).get("chunk_title")
            or ""
        )
        copy["source_ranges"] = [
            _source_range(primary, kind="matched")
        ]
        primary_signals = {
            str(value) for value in primary.get("signals") or []
        }
        if (
            (
                identifier_only_lookup
                and primary_id not in verified_anchor_ids
            )
            or (
                "exact" in primary_signals
                and primary.get("exact_evidence_eligible") is False
            )
        ):
            output.append(copy)
            continue

        before: tuple[dict[str, Any], str, str] | None = None
        after: tuple[dict[str, Any], str, str] | None = None
        neighbors = _without_test_fixtures(
            backend.get_neighbor_rows(primary_id, window=1)
        )
        for neighbor in neighbors:
            neighbor_id = str(neighbor.get("id") or "")
            if (
                not neighbor_id
                or neighbor_id in primary_ids
                or neighbor_id in claimed_context_ids
            ):
                continue
            relationship = _structural_context_relationship(
                primary,
                neighbor,
            )
            if not relationship:
                continue
            direction = _neighbor_direction(primary, neighbor)
            if direction not in {"before", "after"}:
                continue
            text = _trim_context_overlap(
                str(primary.get("text") or ""),
                str(neighbor.get("text") or ""),
                direction=direction,
            )
            if not text:
                continue
            text = (
                text[-DEFAULT_CONTEXT_MAX_CHARS:]
                if direction == "before"
                else text[:DEFAULT_CONTEXT_MAX_CHARS]
            )
            candidate = (neighbor, text, relationship)
            if direction == "before":
                before = candidate
            else:
                after = candidate

        attached_reasons: list[str] = []
        for direction, candidate in (
            ("before", before),
            ("after", after),
        ):
            if candidate is None or remaining <= 0:
                continue
            neighbor, text, relationship = candidate
            text_tokens = conservative_token_count(text)
            if text_tokens > remaining:
                text = truncate_to_token_limit(text, remaining)
                text_tokens = conservative_token_count(text)
            if not text or text_tokens <= 0:
                continue
            field = f"context_{direction}"
            copy[field] = text
            attached_reasons.append(relationship)
            source_range = _source_range(
                neighbor,
                kind=field,
                relationship=relationship,
            )
            copy["source_ranges"].append(source_range)
            neighbor_id = str(neighbor.get("id") or "")
            claimed_context_ids.add(neighbor_id)
            diagnostics.append(
                {
                    "id": neighbor_id,
                    "text": text,
                    "metadata": dict(neighbor.get("metadata") or {}),
                    "signals": ["structural_context"],
                    "support_kind": relationship,
                    "anchor_chunk_uid": primary_id,
                    "packing_phase": "context",
                }
            )
            remaining -= text_tokens
        if attached_reasons:
            copy["context_reason"] = (
                attached_reasons[0]
                if len(set(attached_reasons)) == 1
                else "structure_aware_neighbors"
            )
        if _looks_like_table_row(str(primary.get("text") or "")):
            has_header = any(
                reason == "table_header"
                for reason in attached_reasons
            ) or _contains_table_header(str(primary.get("text") or ""))
            if not has_header:
                copy["context_warnings"] = ["table_headers_incomplete"]
        output.append(copy)
    return output, diagnostics


def _neighbor_direction(
    primary: dict[str, Any],
    neighbor: dict[str, Any],
) -> str:
    try:
        primary_index = int(
            (primary.get("metadata") or {}).get("chunk_index")
        )
        neighbor_index = int(
            (neighbor.get("metadata") or {}).get("chunk_index")
        )
    except (TypeError, ValueError):
        return ""
    if neighbor_index < primary_index:
        return "before"
    if neighbor_index > primary_index:
        return "after"
    return ""


def _structural_context_relationship(
    primary: dict[str, Any],
    neighbor: dict[str, Any],
) -> str:
    if _doc_key(primary) != _doc_key(neighbor):
        return ""
    if _neighbor_distance(primary, neighbor) != 1:
        return ""
    if not _same_semantic_section(primary, neighbor):
        return ""
    primary_text = str(primary.get("text") or "")
    neighbor_text = str(neighbor.get("text") or "")
    direction = _neighbor_direction(primary, neighbor)
    source_type = str(
        (primary.get("metadata") or {}).get("source_type") or ""
    )
    path = str((primary.get("metadata") or {}).get("path") or "")
    if (
        _path_suffix(path) == ".md"
        and _crosses_markdown_heading_boundary(
            primary_text,
            neighbor_text,
            direction=direction,
        )
    ):
        return ""
    if _looks_like_table_row(primary_text):
        if direction == "before" and _contains_table_header(neighbor_text):
            return "table_header"
        if direction == "after" and _contains_table_footnote(neighbor_text):
            return "table_footnote"
    if source_type == "code" or _path_suffix(path) in {
        ".py",
        ".js",
        ".ts",
        ".java",
        ".go",
        ".rs",
        ".cs",
    }:
        if direction == "before" and re.search(
            r"(?m)^\s*(?:class|def|function|func|fn|public|private)\b",
            neighbor_text,
        ):
            return "enclosing_function"
    if _path_suffix(path) in {
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
    } and direction == "before":
        if re.search(
            r"(?m)^\s*(?:\[.+\]|[\"']?[\w.-]+[\"']?\s*[:={])",
            neighbor_text,
        ):
            return "enclosing_configuration"
    if not _context_is_useful(primary_text, primary):
        return ""
    return "same_section_neighbor"


def _path_suffix(path: str) -> str:
    match = re.search(r"(\.[A-Za-z0-9]+)$", path)
    return match.group(1).casefold() if match else ""


def _context_is_useful(text: str, row: dict[str, Any]) -> bool:
    signals = {str(value) for value in row.get("signals") or []}
    return bool(
        signals & {"exact", "lexical_anchor"}
        or _looks_like_table_row(text)
        or re.search(
            r"(?:this|that|these|those|the above|以下|以上|これ|それ|同上|前述)",
            text,
            re.IGNORECASE,
        )
        or (text and text[0].islower())
        or (text and text[-1] not in "。.!?！？）)]}\"'")
    )


def _crosses_markdown_heading_boundary(
    primary_text: str,
    neighbor_text: str,
    *,
    direction: str,
) -> bool:
    primary_headings = _markdown_headings(primary_text)
    neighbor_headings = _markdown_headings(neighbor_text)
    primary_first_is_heading = bool(
        re.match(r"^\s{0,3}#{1,6}\s+\S", primary_text)
    )
    neighbor_first_is_heading = bool(
        re.match(r"^\s{0,3}#{1,6}\s+\S", neighbor_text)
    )

    if (
        primary_headings
        and neighbor_headings
        and primary_headings[-1] != neighbor_headings[-1]
    ):
        return True
    if direction == "before" and primary_first_is_heading:
        return (
            not neighbor_headings
            or neighbor_headings[-1] != primary_headings[0]
        )
    if direction == "after" and neighbor_first_is_heading:
        return (
            not primary_headings
            or primary_headings[-1] != neighbor_headings[0]
        )
    return False


def _markdown_headings(text: str) -> list[str]:
    return [
        re.sub(r"\s+#+\s*$", "", match.group(1)).strip().casefold()
        for match in re.finditer(
            r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$",
            text,
        )
        if match.group(1).strip()
    ]


def _looks_like_table_row(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return any(
        (line.count("|") >= 2 or line.count("\t") >= 2)
        and bool(re.search(r"\d", line))
        for line in lines
    )


def _contains_table_header(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:4]:
        if line.count("|") < 2 and line.count("\t") < 2:
            continue
        cells = [
            cell.strip()
            for cell in re.split(r"[|\t]", line)
            if cell.strip()
        ]
        if len(cells) >= 2 and sum(
            bool(re.search(r"[A-Za-zぁ-んァ-ヶ一-龠]", cell))
            for cell in cells
        ) >= 2:
            return True
    return False


def _contains_table_footnote(text: str) -> bool:
    return bool(
        re.search(
            r"(?im)^\s*(?:note|notes|source|注|備考|出典|脚注)\s*[:：]",
            text,
        )
    )


def _trim_context_overlap(
    primary_text: str,
    context_text: str,
    *,
    direction: str,
) -> str:
    primary_text = primary_text.strip()
    context_text = context_text.strip()
    limit = min(320, len(primary_text), len(context_text))
    for size in range(limit, 19, -1):
        if (
            direction == "before"
            and context_text[-size:] == primary_text[:size]
        ):
            return context_text[:-size].strip()
        if (
            direction == "after"
            and primary_text[-size:] == context_text[:size]
        ):
            return context_text[size:].strip()
    return context_text


def _source_range(
    row: dict[str, Any],
    *,
    kind: str,
    relationship: str = "",
) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    output: dict[str, Any] = {
        "kind": kind,
        "chunk_uid": str(row.get("id") or ""),
        "chunk_index": metadata.get("chunk_index"),
        "section": str(
            metadata.get("section_path")
            or metadata.get("chunk_title")
            or ""
        ),
    }
    for key in ("page", "slide", "lines"):
        if metadata.get(key) not in (None, ""):
            output[key] = metadata[key]
    if relationship:
        output["relationship"] = relationship
    return output


def _pack_protected_rows(
    rows: list[dict[str, Any]],
    *,
    question: str,
    budget_tokens: int,
) -> list[dict[str, Any]]:
    """Share the protected budget so a large first chunk cannot evict peers."""
    selected = list(rows)
    if not selected:
        return []

    full_costs = [
        conservative_token_count(str(row.get("text") or ""))
        for row in selected
    ]
    allocations = [0] * len(selected)
    remaining_budget = budget_tokens
    pending = set(range(len(selected)))
    while pending:
        share = max(0, remaining_budget // len(pending))
        completed = {
            index
            for index in pending
            if full_costs[index] <= share
        }
        if not completed:
            for index in pending:
                allocations[index] = share
            break
        for index in completed:
            allocations[index] = full_costs[index]
            remaining_budget -= allocations[index]
        pending -= completed

    output: list[dict[str, Any]] = []
    for row, allocation in zip(selected, allocations):
        if allocation <= 0:
            break
        text = str(row.get("text") or "")
        copy = dict(row)
        if conservative_token_count(text) > allocation:
            if allocation <= 8:
                break
            copy["text"] = _truncate_packed_row_text(
                row,
                allocation,
                question=question,
            )
            copy["truncated"] = True
        output.append(copy)
    return output


def _anchor_candidates(
    question: str,
    *,
    source: str,
    backend: SearchBackend,
) -> list[dict[str, Any]]:
    search = getattr(backend, "anchor_lexical_search", None)
    if not callable(search):
        return []
    try:
        return _without_test_fixtures(search(question, top_k=1, source=source))
    except Exception:
        return []


def _merge_anchor_rows(
    lexical_rows: list[dict[str, Any]],
    anchor_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not anchor_rows:
        return lexical_rows, []
    anchor = dict(anchor_rows[0])
    anchor_id = str(anchor.get("id") or "")
    if not anchor_id:
        return lexical_rows, []
    anchor["signals"] = sorted(set(anchor.get("signals") or []) | {"lexical", "lexical_anchor"})
    merged = [anchor]
    merged.extend(row for row in lexical_rows if str(row.get("id") or "") != anchor_id)
    return merged, [anchor_id]


def _certified_anchor_rows(
    anchor_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    *,
    question: str,
    db_scope_confirmed: bool,
) -> list[dict[str, Any]]:
    """Accept one low-DF anchor only under a conservative certificate.

    A rare term by itself is not enough to skip dense retrieval. The complete
    query must cover the candidate, another informative term must match, and
    metadata must independently confirm the same document.
    """
    if not db_scope_confirmed or not anchor_rows:
        return []
    candidate = dict(anchor_rows[0])
    debug = dict(candidate.get("debug") or {})
    anchor_debug = debug.get("lexical_anchor") if isinstance(debug, dict) else {}
    token = str((anchor_debug or {}).get("token") or "")
    try:
        document_df = int((anchor_debug or {}).get("document_df") or 0)
        information_score = float((anchor_debug or {}).get("information_score") or 0.0)
    except (TypeError, ValueError):
        return []
    if (
        not token
        or document_df <= 0
        or information_score < 0.5
        or not _raw_anchor_occurs(candidate, token)
    ):
        return []
    lexical_match = next(
        (
            (rank, row)
            for rank, row in enumerate(lexical_rows[:3], start=1)
            if str(row.get("id") or "") == str(candidate.get("id") or "")
            or _same_doc(candidate, row)
        ),
        None,
    )
    if lexical_match is None:
        return []
    lexical_rank, lexical_row = lexical_match
    try:
        lexical_score = float(lexical_row.get("score"))
    except (TypeError, ValueError):
        return []
    if not math.isfinite(lexical_score):
        return []

    coverage, non_anchor_confirmed = _query_term_coverage(
        question,
        candidate,
        lexical_row,
        anchor_token=token,
    )
    metadata_rank = next(
        (
            rank
            for rank, row in enumerate(metadata_rows[:3], start=1)
            if str(row.get("id") or "") == str(candidate.get("id") or "")
            or _same_doc(candidate, row)
        ),
        None,
    )
    strict = coverage >= 0.5 and non_anchor_confirmed and metadata_rank is not None
    if not strict:
        return []
    kind = "certified_low_df_anchor"
    debug["fast_path_certificate"] = {
        "kind": kind,
        "token": token,
        "raw_occurrence_verified": True,
        "document_frequency": document_df,
        "information_score": round(information_score, 6),
        "lexical_rank": lexical_rank,
        "lexical_score_finite": True,
        "query_term_coverage": round(coverage, 6),
        "non_anchor_term_confirmed": non_anchor_confirmed,
        "metadata_rank": metadata_rank,
        "db_scope_confirmed": True,
    }
    candidate["debug"] = debug
    return [candidate]


def _mark_fast_path_exact_certificate(
    rows: list[dict[str, Any]],
    *,
    certificate: dict[str, Any] | None,
) -> None:
    if not certificate:
        return
    for row in rows:
        debug = dict(row.get("debug") or {})
        debug["fast_path_certificate"] = dict(certificate)
        row["debug"] = debug


def _anchor_rescue(
    rows: list[dict[str, Any]],
    exact_rows: list[dict[str, Any]],
    question: str,
    *,
    anchor_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    rescue_id = ""
    rescue_signal = ""
    matching_exact_rows = _matching_strong_exact_rows(question, exact_rows)
    if matching_exact_rows:
        rescue_id = str(matching_exact_rows[0].get("id") or "")
        rescue_signal = "exact"
    elif anchor_ids:
        rescue_id = str(anchor_ids[0] or "")
        rescue_signal = "lexical_anchor"
    if not rescue_id:
        return rows
    selected = next((row for row in rows if str(row.get("id") or "") == rescue_id), None)
    if selected is None:
        return rows
    rescued = dict(selected)
    rescued["signals"] = sorted(set(rescued.get("signals") or []) | {rescue_signal})
    rescued["score"] = 1_000_000_000.0
    rescued["debug"] = dict(rescued.get("debug") or {})
    rescued["debug"]["anchor_rescue"] = rescue_signal
    return [rescued] + [row for row in rows if str(row.get("id") or "") != rescue_id]


def _strong_lexical_hit(
    question: str,
    exact_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
) -> bool:
    anchors = extract_anchors(question, limit=5)
    if not anchors or not exact_rows:
        return False
    if any(_strong_anchor(anchor) for anchor in anchors):
        return True
    exact_top = exact_rows[0]
    lexical_top = lexical_rows[0] if lexical_rows else None
    metadata_top = metadata_rows[0] if metadata_rows else None
    if lexical_top and _same_doc(exact_top, lexical_top):
        return True
    if metadata_top and _same_doc(exact_top, metadata_top):
        return True
    exact_docs = {_doc_key(row) for row in exact_rows[:3]}
    lexical_docs = {_doc_key(row) for row in lexical_rows[:3]}
    return bool(exact_docs & lexical_docs)


def _strong_anchor(anchor: str) -> bool:
    if any(marker in anchor for marker in ["/", "\\", ".", ":", "_"]):
        return True
    uppercase_count = sum(1 for char in anchor if char.isupper())
    if "-" in anchor:
        return any(char.isdigit() for char in anchor) or uppercase_count >= 2
    if re.fullmatch(r"RFC ?\d{2,}", anchor, re.IGNORECASE):
        return True
    return bool(
        re.fullmatch(r"[A-Z]+\d{2,}[A-Z0-9]*", anchor)
        or re.fullmatch(r"[A-Z]+\d+[A-Z]+", anchor)
    )


def _has_strong_exact_anchor(question: str, exact_rows: list[dict[str, Any]]) -> bool:
    return bool(_matching_strong_exact_rows(question, exact_rows))


def _matching_strong_exact_rows(
    question: str,
    exact_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchors = [
        anchor
        for anchor in extract_anchors(question, limit=30)
        if _strong_anchor(anchor)
    ]
    if not anchors:
        return []
    matching: list[dict[str, Any]] = []
    for row in exact_rows:
        if any(_raw_anchor_occurs(row, anchor) for anchor in anchors):
            matching.append(row)
    return matching


def _strong_query_anchors(
    question: str,
    *,
    excluded_identifiers: set[str] | None = None,
) -> list[str]:
    excluded_keys = {
        key
        for identifier in (excluded_identifiers or set())
        if identifier
        for key in identifier_match_keys(identifier)
    }
    output: list[str] = []
    for anchor in extract_anchors(question, limit=30):
        if not _strong_anchor(anchor):
            continue
        if set(identifier_match_keys(anchor)) & excluded_keys:
            continue
        if anchor not in output:
            output.append(anchor)
    return output


def _is_identifier_only_lookup(question: str) -> bool:
    """Return whether the query adds only generic lookup language to anchors."""
    anchors = _strong_query_anchors(question)
    if not anchors:
        return False
    remainder = question or ""
    for anchor in sorted(anchors, key=len, reverse=True):
        remainder = re.sub(
            re.escape(anchor),
            " ",
            remainder,
            flags=re.IGNORECASE,
        )
    terms = {
        canonicalize(token)
        for token in [
            *tokens_for_fts(remainder),
            *re.findall(r"[A-Za-z][A-Za-z0-9_-]*", remainder),
        ]
        if canonicalize(token)
    }
    generic = {
        canonicalize(token)
        for token in _GENERIC_IDENTIFIER_LOOKUP_TERMS
    }
    return not terms or terms <= generic


def _exact_certificate(
    question: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not rows:
        return None
    anchors = _strong_query_anchors(question)
    matched = [
        anchor
        for anchor in anchors
        if any(_raw_anchor_occurs(row, anchor) for row in rows)
    ]
    if not matched:
        return None
    if any(re.fullmatch(r"RFC ?\d{2,}", anchor, re.IGNORECASE) for anchor in matched):
        kind = "verified_rfc_exact"
    elif any(any(marker in anchor for marker in ["/", "\\", ".", ":", "_"]) for anchor in matched):
        kind = "verified_path_exact"
    else:
        kind = "verified_identifier_exact"
    return {
        "kind": kind,
        "matched_identifiers": matched,
        "raw_occurrence_verified": True,
    }


def _query_term_coverage(
    question: str,
    candidate: dict[str, Any],
    lexical_row: dict[str, Any],
    *,
    anchor_token: str,
) -> tuple[float, bool]:
    # Sudachi can transliterate an ASCII query (for example, ``Poland`` to
    # ``ポーランド``). Keep the original ASCII terms as a second view so a
    # same-language source can still satisfy the conservative certificate.
    informative = [
        token
        for token in [
            *tokens_for_fts(question),
            *re.findall(r"[A-Za-z0-9][A-Za-z0-9_/-]*", question or ""),
        ]
        if len(canonicalize(token)) >= 2
    ]
    if not informative:
        return 0.0, False
    anchor_keys = set(identifier_match_keys(anchor_token))
    haystack = "\n".join(
        [
            _row_haystack(candidate),
            _row_haystack(lexical_row),
        ]
    )
    haystack_canonical = canonicalize(haystack)
    matched = 0
    non_anchor_confirmed = False
    seen: set[str] = set()
    for token in informative:
        key = canonicalize(token)
        if not key or key in seen:
            continue
        seen.add(key)
        token_matches = key in haystack_canonical
        if token_matches:
            matched += 1
            if not (set(identifier_match_keys(token)) & anchor_keys):
                non_anchor_confirmed = True
    return matched / max(1, len(seen)), non_anchor_confirmed


def _row_haystack(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    return "\n".join(
        [
            str(row.get("text") or ""),
            str(metadata.get("path") or ""),
            str(metadata.get("title") or ""),
            str(metadata.get("section_path") or ""),
        ]
    )


def _raw_anchor_occurs(row: dict[str, Any], anchor: str) -> bool:
    metadata = row.get("metadata") or {}
    haystack = "\n".join(
        [
            str(row.get("text") or ""),
            str(metadata.get("path") or ""),
            str(metadata.get("title") or ""),
            str(metadata.get("uri") or ""),
        ]
    )
    anchor_keys = set(identifier_match_keys(anchor))
    haystack_keys = {
        key
        for candidate in extract_anchors(haystack, limit=500)
        for key in identifier_match_keys(candidate)
    }
    if anchor_keys & haystack_keys:
        return True
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(anchor)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    return bool(pattern.search(haystack))


def _same_doc(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(_doc_key(left) and _doc_key(left) == _doc_key(right))


def _doc_key(row: dict[str, Any]) -> str:
    meta = row.get("metadata") or {}
    return str(meta.get("path") or meta.get("doc_id") or row.get("id") or "")


def _dedupe_and_diversify(
    rows: list[dict[str, Any]],
    *,
    top_k: int,
    max_per_doc: int,
    relaxed_doc_limits: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    seen_hashes: set[str] = set()
    unique_rows: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        chunk_hash = str(meta.get("chunk_hash") or meta.get("text_hash") or "")
        if chunk_hash and chunk_hash in seen_hashes:
            continue
        if chunk_hash:
            seen_hashes.add(chunk_hash)
        unique_rows.append(row)

    rows_by_doc: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in enumerate(unique_rows):
        meta = row.get("metadata") or {}
        doc_key = str(meta.get("path") or meta.get("doc_id") or row.get("id"))
        rows_by_doc.setdefault(doc_key, []).append((index, row))

    replacements: dict[int, dict[str, Any]] = {}
    for doc_key, group in rows_by_doc.items():
        allowed = (relaxed_doc_limits or {}).get(doc_key, max_per_doc)
        if any("exact" in set(row.get("signals") or []) for _index, row in group):
            allowed = max(allowed, 3)
        protected = [
            (index, row)
            for index, row in group
            if _is_protected_postprocess_candidate(row)
        ]
        protected.sort(key=lambda item: _protected_candidate_key(*item))
        chosen = protected[:allowed]
        chosen_indices = {index for index, _row in chosen}
        if len(chosen) < allowed:
            for index, row in group:
                if index in chosen_indices:
                    continue
                chosen.append((index, row))
                chosen_indices.add(index)
                if len(chosen) >= allowed:
                    break
        replacement_positions = sorted(index for index, _row in group)[: len(chosen)]
        for position, (_original_index, row) in zip(replacement_positions, chosen):
            replacements[position] = row

    output: list[dict[str, Any]] = []
    for index, _row in enumerate(unique_rows):
        row = replacements.get(index)
        if row is None:
            continue
        output.append(row)
        if len(output) >= top_k:
            break
    return output


def _is_protected_postprocess_candidate(row: dict[str, Any]) -> bool:
    if "exact" in set(row.get("signals") or []):
        return True
    sources = set((row.get("debug") or {}).get("candidate_pool_sources") or [])
    return bool(
        sources
        & {
            "dense_floor",
            "lexical_floor",
            "metadata_floor",
            "verified_exact",
        }
    )


def _protected_candidate_key(
    index: int,
    row: dict[str, Any],
) -> tuple[int, int, int]:
    exact_priority = 0 if "exact" in set(row.get("signals") or []) else 1
    family_ranks = (row.get("debug") or {}).get("family_ranks") or {}
    retriever_rank = min(
        [
            int(rank)
            for family, rank in family_ranks.items()
            if family in {"dense", "lexical", "metadata"}
        ]
        or [1_000_000]
    )
    # A family floor may replace an otherwise unprotected same-document row,
    # but a later floor must not displace an earlier protected RRF result.
    # Exact remains the only signal allowed to override fused order.
    return exact_priority, index, retriever_rank


def _expand_neighbors(rows: list[dict[str, Any]], *, backend: SearchBackend) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        row_id = str(row.get("id") or "")
        if row_id not in seen_ids:
            output.append(row)
            seen_ids.add(row_id)
        neighbors = _without_test_fixtures(
            backend.get_neighbor_rows(row_id, window=1)
        )
        for neighbor in neighbors:
            neighbor_id = str(neighbor.get("id") or "")
            if not neighbor_id or neighbor_id in seen_ids:
                continue
            copy = dict(neighbor)
            copy["signals"] = ["neighbor"]
            debug = dict(copy.get("debug") or {})
            debug.pop("exact_match", None)
            debug.pop("lexical_anchor", None)
            debug.pop("fast_path_certificate", None)
            if debug:
                copy["debug"] = debug
            else:
                copy.pop("debug", None)
            output.append(copy)
            seen_ids.add(neighbor_id)
    return output


def _without_test_fixtures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not _is_test_fixture(row)]


def _is_test_fixture(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata") or {}
    value = metadata.get("test_fixture", row.get("test_fixture"))
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value == 1


def _pack_budget(
    rows: list[dict[str, Any]],
    *,
    budget_tokens: int | None,
    question: str = "",
) -> list[dict[str, Any]]:
    if not budget_tokens or budget_tokens <= 0:
        return rows
    used = 0
    output: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text") or "")
        remaining = budget_tokens - used
        if remaining <= 0:
            break
        copy = dict(row)
        if conservative_token_count(text) > remaining:
            if remaining <= 8:
                break
            copy["text"] = _truncate_packed_row_text(
                row,
                remaining,
                question=question,
            )
            copy["truncated"] = True
        used += conservative_token_count(str(copy.get("text") or ""))
        output.append(copy)
    return output


def _truncate_packed_row_text(
    row: dict[str, Any],
    limit: int,
    *,
    question: str,
) -> str:
    text = str(row.get("text") or "")
    debug = row.get("debug") or {}
    exact_debug = debug.get("exact_match") or {}
    lexical_debug = debug.get("lexical_anchor") or {}
    anchors = [
        *[str(value) for value in exact_debug.get("matched_terms") or []],
        str(lexical_debug.get("token") or ""),
    ]
    exact_anchors = [
        value
        for value in anchors
        if value and value.casefold() in text.casefold()
    ]
    query_terms: list[str] = []
    for term in [
        *tokens_for_fts(question),
        *re.findall(r"[A-Za-z0-9][A-Za-z0-9_.:/-]+", question),
    ]:
        value = str(term).strip()
        if not value:
            continue
        if value.isascii() and len(value) < 3 and not any(char.isdigit() for char in value):
            continue
        if not value.isascii() and len(value) < 2:
            continue
        if value.casefold() not in text.casefold():
            continue
        if value.casefold() not in {item.casefold() for item in query_terms}:
            query_terms.append(value)
    candidate_terms = [*exact_anchors, *query_terms]
    if not candidate_terms:
        return truncate_to_token_limit(text, limit)

    best = ""
    best_score = -1
    folded_text = text.casefold()
    exact_keys = {value.casefold() for value in exact_anchors}
    for term in candidate_terms:
        start = 0
        occurrences = 0
        while occurrences < 5:
            anchor_start = folded_text.find(term.casefold(), start)
            if anchor_start < 0:
                break
            anchor_end = anchor_start + len(term)
            candidate = _bounded_anchor_excerpt(
                text,
                anchor_start=anchor_start,
                anchor_end=anchor_end,
                limit=limit,
            )
            candidate_folded = candidate.casefold()
            matched_terms = {
                value.casefold()
                for value in query_terms
                if value.casefold() in candidate_folded
            }
            score = (
                len(matched_terms) * 100
                + sum(len(value) for value in matched_terms)
                + (10_000 if term.casefold() in exact_keys else 0)
            )
            if score > best_score:
                best = candidate
                best_score = score
            start = anchor_end
            occurrences += 1
    return best or truncate_to_token_limit(text, limit)


def _bounded_anchor_excerpt(
    text: str,
    *,
    anchor_start: int,
    anchor_end: int,
    limit: int,
) -> str:
    low, high = 0, len(text)
    best = ""
    while low <= high:
        radius = (low + high) // 2
        start = max(0, anchor_start - radius)
        end = min(len(text), anchor_end + radius)
        candidate = text[start:end].strip()
        if start > 0:
            candidate = "[truncated]..." + candidate
        if end < len(text):
            candidate += "...[truncated]"
        if conservative_token_count(candidate) <= limit:
            best = candidate
            low = radius + 1
        else:
            high = radius - 1
    return best


def _packed_rows_token_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        conservative_token_count(
            "\n".join(
                str(row.get(key) or "")
                for key in ("context_before", "text", "context_after")
            )
        )
        for row in rows
    )


def _prefer_row(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    if not current:
        return True
    candidate_has_text = bool(candidate.get("text"))
    current_has_text = bool(current.get("text"))
    if candidate_has_text != current_has_text:
        return candidate_has_text
    candidate_meta = candidate.get("metadata") or {}
    current_meta = current.get("metadata") or {}
    return len(candidate_meta) > len(current_meta)
