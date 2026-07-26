from __future__ import annotations

import math
import re
from typing import Any, Protocol

from . import catalog
from .manifest import ConfigMismatchError
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
DEFAULT_RRF_K = 12


class SearchBackend(Protocol):
    def vector_query(self, question: str, top_k: int, source: str = "any") -> list[dict[str, Any]]: ...
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
    anchor_ids: list[str] = []
    if use_lexical:
        try:
            raw_exact_rows, lexical_rows, metadata_rows = _lexical_candidates(question, source=source, backend=backend)
            raw_exact_rows = _without_test_fixtures(raw_exact_rows)
            exact_rows = _matching_strong_exact_rows(question, raw_exact_rows)
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
    rows = rows[: max(DEFAULT_RRF_K, top_k)]
    rows = _dedupe_and_diversify(rows, top_k=max(top_k * 3, top_k), max_per_doc=max_per_doc)
    rows = _expand_neighbors(rows, backend=backend)
    rows = _without_test_fixtures(rows)
    rows = _pack_budget(rows, budget_tokens=budget_tokens)
    rows = _without_test_fixtures(rows)
    rows = rows[:top_k]

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
    raw_exact_rows, lexical_rows, metadata_rows = _lexical_candidates(
        question,
        source=source,
        backend=backend,
    )
    raw_exact_rows = _without_test_fixtures(raw_exact_rows)
    lexical_rows = _without_test_fixtures(lexical_rows)
    metadata_rows = _without_test_fixtures(metadata_rows)
    anchor_rows = _anchor_candidates(question, source=source, backend=backend)
    verified_exact_rows = _matching_strong_exact_rows(question, raw_exact_rows)

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
    materialized = materialized[: max(DEFAULT_RRF_K, top_k)]
    materialized = _dedupe_and_diversify(
        materialized,
        top_k=max(top_k * 3, top_k),
        max_per_doc=max_per_doc,
    )
    materialized = _expand_neighbors(materialized, backend=backend)
    materialized = _without_test_fixtures(materialized)
    materialized = _pack_budget(materialized, budget_tokens=budget_tokens)
    materialized = _without_test_fixtures(materialized)[:top_k]
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
        "lexical_rows": lexical_rows,
        "metadata_rows": metadata_rows,
        "anchor_rows": anchor_rows,
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
    exact_rows, lexical_rows, metadata_rows = _lexical_candidates(question, source=source, backend=backend)
    exact_rows = _without_test_fixtures(exact_rows)
    lexical_rows = _without_test_fixtures(lexical_rows)
    metadata_rows = _without_test_fixtures(metadata_rows)
    anchor_rows = _anchor_candidates(question, source=source, backend=backend)
    matching_exact_rows = _matching_strong_exact_rows(question, exact_rows)
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
    rows = rows[: max(DEFAULT_RRF_K, top_k)]
    rows = _dedupe_and_diversify(rows, top_k=max(top_k * 3, top_k), max_per_doc=max_per_doc)
    rows = _expand_neighbors(rows, backend=backend)
    rows = _pack_budget(rows, budget_tokens=budget_tokens)
    rows = rows[:top_k]
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    exact_rows = backend.exact_search(question, top_k=DEFAULT_EXACT_K, source=source)
    lexical_rows = backend.bm25_search(question, top_k=DEFAULT_LEXICAL_K, source=source)
    metadata_rows = backend.metadata_search(question, top_k=DEFAULT_METADATA_K, source=source)
    return exact_rows, lexical_rows, metadata_rows


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
        output.append(base)
    output.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
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
    """Accept one low-DF anchor under a conservative generic certificate.

    The strict certificate requires topic confirmation from metadata. Some
    existing databases contain no metadata rows; for those databases the
    fallback certificate is limited to the selected DB and requires the
    candidate document to be a top-three result of the complete query's BM25
    ranking. The fallback still promotes only the verified seed chunk.
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
    strict = (
        coverage >= 0.5
        and non_anchor_confirmed
        and metadata_rank is not None
    )
    kind = "certified_low_df_anchor" if strict else "db_scope_full_query_lexical"
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
    if any(marker in anchor for marker in ["/", "\\", ".", ":", "_", "-"]):
        return True
    return any(char.isdigit() for char in anchor) and any(char.isalpha() for char in anchor)


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
    informative = [
        token
        for token in tokens_for_fts(question)
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


def _dedupe_and_diversify(rows: list[dict[str, Any]], *, top_k: int, max_per_doc: int) -> list[dict[str, Any]]:
    doc_counts: dict[str, int] = {}
    seen_hashes: set[str] = set()
    output: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        chunk_hash = str(meta.get("chunk_hash") or meta.get("text_hash") or "")
        if chunk_hash and chunk_hash in seen_hashes:
            continue
        if chunk_hash:
            seen_hashes.add(chunk_hash)
        doc_key = str(meta.get("path") or meta.get("doc_id") or row.get("id"))
        allowed = max_per_doc
        if "exact" in set(row.get("signals") or []):
            allowed = max(allowed, 3)
        count = doc_counts.get(doc_key, 0)
        if count >= allowed:
            continue
        doc_counts[doc_key] = count + 1
        output.append(row)
        if len(output) >= top_k:
            break
    return output


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


def _pack_budget(rows: list[dict[str, Any]], *, budget_tokens: int | None) -> list[dict[str, Any]]:
    if not budget_tokens or budget_tokens <= 0:
        return rows
    char_budget = max(500, budget_tokens * 4)
    used = 0
    output: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text") or "")
        overhead = 180
        remaining = char_budget - used - overhead
        if remaining <= 0:
            break
        copy = dict(row)
        if len(text) > remaining:
            copy["text"] = text[: max(0, remaining - 20)].rstrip() + "\n...[truncated]"
            copy["truncated"] = True
        used += len(str(copy.get("text") or "")) + overhead
        output.append(copy)
    return output


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
