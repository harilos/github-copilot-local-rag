from __future__ import annotations

from typing import Any, Protocol

from . import catalog
from .manifest import ConfigMismatchError
from .tokenize import extract_anchors


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
    backend: SearchBackend | None = None,
) -> list[dict[str, Any]]:
    backend = backend or _GlobalBackend()
    dense_k = fetch_k or max(DEFAULT_DENSE_K, top_k * 4)
    family_rankings: list[tuple[str, float, list[dict[str, Any]]]] = []
    warnings: list[str] = []

    if use_dense:
        try:
            family_rankings.append(("dense", 1.0, backend.vector_query(question, top_k=dense_k, source=source)))
        except ConfigMismatchError:
            raise
        except Exception as exc:
            warnings.append(f"dense search unavailable: {type(exc).__name__}: {exc}")

    try:
        exact_rows, lexical_rows, metadata_rows = _lexical_candidates(question, source=source, backend=backend)
        family_rankings.append(("lexical", 1.1, lexical_rows))
        family_rankings.append(("metadata", 0.7, metadata_rows))
        if exact_rows:
            family_rankings.append(("exact", 1.4, exact_rows))
    except Exception as exc:
        warnings.append(f"catalog search unavailable: {type(exc).__name__}: {exc}")
        exact_rows = []

    fused = _weighted_rrf(family_rankings)
    rows = _materialize(fused, family_rankings, backend=backend)
    rows = _anchor_rescue(rows, exact_rows, question)
    rows = rows[: max(DEFAULT_RRF_K, top_k)]
    rows = _dedupe_and_diversify(rows, top_k=max(top_k * 3, top_k), max_per_doc=max_per_doc)
    rows = _expand_neighbors(rows, backend=backend)
    rows = _pack_budget(rows, budget_tokens=budget_tokens)
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


def cold_lexical_fast_path(
    question: str,
    *,
    top_k: int,
    source: str = "any",
    max_per_doc: int = 2,
    budget_tokens: int | None = None,
    explain: bool = False,
    backend: SearchBackend | None = None,
) -> list[dict[str, Any]] | None:
    backend = backend or _GlobalBackend()
    exact_rows, lexical_rows, metadata_rows = _lexical_candidates(question, source=source, backend=backend)
    if not _strong_lexical_hit(question, exact_rows, lexical_rows, metadata_rows):
        return None

    families: list[tuple[str, float, list[dict[str, Any]]]] = [
        ("lexical", 1.1, lexical_rows),
        ("metadata", 0.7, metadata_rows),
    ]
    if exact_rows:
        families.append(("exact", 1.4, exact_rows))
    fused = _weighted_rrf(families)
    rows = _materialize(fused, families, backend=backend)
    rows = _anchor_rescue(rows, exact_rows, question)
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
    for _family, _weight, rows in families:
        for row in rows:
            chunk_id = str(row.get("id") or "")
            if chunk_id and chunk_id not in rows_by_id:
                rows_by_id[chunk_id] = row

    catalog_rows = backend.fetch_rows_by_ids(fused.keys())
    output: list[dict[str, Any]] = []
    for chunk_id, item in fused.items():
        base = dict(catalog_rows.get(chunk_id) or item.get("best_row") or rows_by_id.get(chunk_id) or {})
        base["id"] = chunk_id
        base["signals"] = sorted(item["signals"])
        base["score"] = item["rrf_score"]
        base["debug"] = {"rrf_score": item["rrf_score"], "family_ranks": dict(item["family_ranks"])}
        output.append(base)
    output.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    return output


def _anchor_rescue(rows: list[dict[str, Any]], exact_rows: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    if not extract_anchors(question, limit=5) or not exact_rows:
        return rows
    rescued = dict(exact_rows[0])
    rescued["signals"] = sorted(set(rescued.get("signals") or []) | {"exact"})
    rescued["score"] = 1_000_000_000.0
    rescued["debug"] = dict(rescued.get("debug") or {})
    rescued["debug"]["anchor_rescue"] = True
    return [rescued] + [row for row in rows if row.get("id") != rescued.get("id")]


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
    for row in rows:
        neighbors = backend.get_neighbor_rows(str(row.get("id") or ""), window=1)
        if len(neighbors) <= 1:
            output.append(row)
            continue
        texts: list[str] = []
        seen_text: set[str] = set()
        for neighbor in neighbors:
            text = str(neighbor.get("text") or "").strip()
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            texts.append(text)
        copy = dict(row)
        if texts:
            copy["text"] = "\n\n".join(texts)
            signals = set(copy.get("signals") or [])
            signals.add("neighbor")
            copy["signals"] = sorted(signals)
        output.append(copy)
    return output


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
