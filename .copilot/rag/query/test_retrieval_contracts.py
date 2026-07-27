from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import catalog
from software_rag_tool.retrieval import (
    _decorate_anchored_neighbor,
    _dedupe_and_diversify,
    _expand_and_pack,
    _is_identifier_only_lookup,
    _packed_rows_token_count,
    _postprocess_candidate_pool,
    adaptive_hybrid_query,
    cold_lexical_fast_path,
    hybrid_query,
)
from software_rag_tool.search_api import json_payload, normalize_search_contract
from software_rag_tool.tokenize import extract_anchors, identifier_match_keys, tokenize_for_fts


def result_row(
    row_id: str,
    text: str,
    *,
    signals: list[str] | None = None,
    test_fixture: object | None = None,
    path: str | None = None,
    section: str = "Section",
    chunk_index: int | None = None,
    debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "path": path or f"{row_id}.txt",
        "section_path": section,
    }
    if chunk_index is not None:
        metadata["chunk_index"] = chunk_index
    if test_fixture is not None:
        metadata["test_fixture"] = test_fixture
    row = {
        "id": row_id,
        "text": text,
        "metadata": metadata,
        "signals": list(signals or []),
        "score": 0.0,
    }
    if debug:
        row["debug"] = debug
    return row


class FakeBackend:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {
            "dense": 0,
            "exact": 0,
            "lexical": 0,
            "anchor": 0,
            "metadata": 0,
        }
        self.dense_rows = [result_row("generic", "General cooling context", signals=["dense"])]
        self.exact_rows: list[dict[str, Any]] = []
        self.lexical_rows = [result_row("generic", "General cooling context", signals=["lexical"])]
        self.anchor_rows = [
            result_row(
                "poland",
                "Poland air-conditioning evidence",
                signals=["lexical_anchor"],
            )
        ]
        self.metadata_rows: list[dict[str, Any]] = []
        self.neighbor_rows: dict[str, list[dict[str, Any]]] = {}

    def vector_query(self, question: str, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        del question, top_k, source
        self.calls["dense"] += 1
        return list(self.dense_rows)

    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        del question, top_k, source
        self.calls["exact"] += 1
        return list(self.exact_rows)

    def bm25_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        del question, top_k, source
        self.calls["lexical"] += 1
        return list(self.lexical_rows)

    def anchor_lexical_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        del question, top_k, source
        self.calls["anchor"] += 1
        return list(self.anchor_rows)

    def metadata_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
        del question, top_k, source
        self.calls["metadata"] += 1
        return list(self.metadata_rows)

    def fetch_rows_by_ids(self, ids: Any) -> dict[str, dict[str, Any]]:
        all_rows = [
            *self.dense_rows,
            *self.exact_rows,
            *self.lexical_rows,
            *self.anchor_rows,
            *self.metadata_rows,
        ]
        wanted = {str(value) for value in ids}
        return {str(row["id"]): dict(row) for row in all_rows if str(row["id"]) in wanted}

    def get_neighbor_rows(self, chunk_uid: str, *, window: int = 1) -> list[dict[str, Any]]:
        del window
        if chunk_uid in self.neighbor_rows:
            return list(self.neighbor_rows[chunk_uid])
        row = self.fetch_rows_by_ids([chunk_uid]).get(chunk_uid)
        return [row] if row else []


class SourceIdentityContractTests(unittest.TestCase):
    def test_legacy_source_metadata_is_not_a_source_link_identity(
        self,
    ) -> None:
        row = result_row(
            "legacy-source",
            "Synthetic evidence.",
            path="Root/document.txt",
        )
        row["rank"] = 1
        row["metadata"]["source"] = "source-a"
        payload = json_payload(
            [row],
            "Synthetic question",
            "fixture-rag",
            6_000,
        )
        self.assertEqual("", payload["evidence"][0]["_source_id"])


class HybridAnchorContractTests(unittest.TestCase):
    def test_postprocess_pool_preserves_retriever_floors_and_exact_rows(self) -> None:
        fused_rows = [
            result_row(f"rrf-{index}", f"rrf {index}")
            for index in range(30)
        ]
        dense_rows = [
            result_row("dense-floor", "dense floor"),
            result_row("dense-2", "dense two"),
            result_row("dense-3", "dense three"),
        ]
        lexical_rows = [
            result_row("lexical-floor", "lexical floor"),
            result_row("lexical-2", "lexical two"),
            result_row("lexical-3", "lexical three"),
        ]
        exact = result_row(
            "verified-exact",
            "A2L",
            signals=["exact"],
        )
        all_rows = [*fused_rows, *dense_rows, *lexical_rows, exact]
        families = [
            ("dense", 1.0, dense_rows),
            ("lexical", 1.1, lexical_rows),
            ("metadata", 0.7, []),
            ("exact", 1.4, [exact]),
        ]
        pool, diagnostics = _postprocess_candidate_pool(
            all_rows,
            families,
            verified_exact_rows=[exact],
            top_k=24,
        )
        pool_ids = [row["id"] for row in pool]
        self.assertIn("dense-floor", pool_ids)
        self.assertIn("lexical-floor", pool_ids)
        self.assertIn("verified-exact", pool_ids)
        self.assertEqual(
            ["dense-floor", "dense-2", "dense-3"],
            diagnostics["protected_family_ids"]["dense"],
        )
        self.assertEqual(["verified-exact"], diagnostics["verified_exact_ids"])

    def test_family_floors_survive_global_diversification_cutoff(self) -> None:
        rrf_rows = [
            result_row(f"rrf-{index}", f"rrf {index}", path=f"rrf-{index}.txt")
            for index in range(24)
        ]
        dense_rows = [
            result_row(f"dense-{index}", f"dense {index}", path=f"dense-{index}.txt")
            for index in range(3)
        ]
        all_rows = [*rrf_rows, *dense_rows]
        pool, _diagnostics = _postprocess_candidate_pool(
            all_rows,
            [("dense", 1.0, dense_rows)],
            verified_exact_rows=[],
            top_k=24,
        )
        selected = _dedupe_and_diversify(
            pool,
            top_k=len(pool),
            max_per_doc=2,
        )
        selected_ids = {row["id"] for row in selected}
        self.assertTrue({row["id"] for row in dense_rows} <= selected_ids)

    def test_dense_only_hybrid_does_not_require_lexical_locals(self) -> None:
        backend = FakeBackend()
        backend.dense_rows = [
            result_row("dense-only", "semantic evidence", signals=["dense"])
        ]
        rows = hybrid_query(
            "semantic question",
            top_k=1,
            use_dense=True,
            use_lexical=False,
            backend=backend,
        )
        self.assertEqual(["dense-only"], [row["id"] for row in rows])
        self.assertEqual(
            {"dense": 1, "exact": 0, "lexical": 0, "anchor": 0, "metadata": 0},
            backend.calls,
        )

    def test_only_final_primaries_share_the_answer_context_budget(self) -> None:
        backend = FakeBackend()
        backend.dense_rows = [
            result_row(
                f"dense-{index}",
                chr(65 + index % 26) * 1_000,
                signals=["dense"],
                path=f"document-{index}.txt",
            )
            for index in range(24)
        ]
        rows = hybrid_query(
            "semantic question",
            top_k=2,
            budget_tokens=400,
            use_dense=True,
            use_lexical=False,
            backend=backend,
        )
        self.assertEqual(2, len(rows))
        self.assertTrue(all(len(row["text"]) > 300 for row in rows))

    def test_primary_protection_prevents_neighbors_from_evicting_top_primary_rows(self) -> None:
        backend = FakeBackend()
        primaries = [
            result_row(
                f"primary-{index}",
                str(index) * 900,
                path="same.txt",
                chunk_index=index,
            )
            for index in range(3)
        ]
        neighbor = result_row(
            "large-neighbor",
            "n" * 3_000,
            path="same.txt",
            chunk_index=3,
        )
        backend.neighbor_rows = {
            "primary-0": [primaries[0], neighbor],
            "primary-1": [primaries[1]],
            "primary-2": [primaries[2]],
        }
        rows = _expand_and_pack(
            primaries,
            question="primary evidence",
            family_rankings=[("dense", 1.0, primaries)],
            backend=backend,
            budget_tokens=1_200,
            document_anchors={},
        )
        self.assertEqual(
            ["primary-0", "primary-1", "primary-2"],
            [row["id"] for row in rows[:3]],
        )
        self.assertLessEqual(_packed_rows_token_count(rows), 1_200)

    def test_table_row_uses_available_header_as_context(self) -> None:
        backend = FakeBackend()
        header = result_row(
            "header",
            "Region | Metric | Year",
            path="table.pdf",
            section="Page 3 #1",
            chunk_index=0,
        )
        row = result_row(
            "row",
            "North | 25 | 31",
            path="table.pdf",
            section="Page 3 #2",
            chunk_index=1,
        )
        backend.neighbor_rows = {"row": [header, row]}
        selected = _expand_and_pack(
            [row],
            question="regional values",
            family_rankings=[("dense", 1.0, [row])],
            backend=backend,
            budget_tokens=400,
            document_anchors={},
        )
        self.assertEqual("Region | Metric | Year", selected[0]["context_before"])
        self.assertEqual("table_header", selected[0]["context_reason"])
        self.assertNotIn("context_warnings", selected[0])

    def test_missing_table_header_adds_machine_readable_warning(self) -> None:
        backend = FakeBackend()
        row = result_row(
            "row",
            "North | 25 | 31",
            path="table.pdf",
            section="Page 3 #2",
            chunk_index=1,
        )
        backend.neighbor_rows = {"row": [row]}
        selected = _expand_and_pack(
            [row],
            question="regional values",
            family_rankings=[("dense", 1.0, [row])],
            backend=backend,
            budget_tokens=400,
            document_anchors={},
        )
        self.assertEqual(
            ["table_headers_incomplete"],
            selected[0]["context_warnings"],
        )

    def test_structure_context_does_not_cross_a_heading_boundary(self) -> None:
        backend = FakeBackend()
        primary = result_row(
            "primary",
            "this result continues",
            path="guide.md",
            section="Results #2",
            chunk_index=1,
        )
        other_heading = result_row(
            "other",
            "Unrelated introduction",
            path="guide.md",
            section="Introduction #1",
            chunk_index=0,
        )
        backend.neighbor_rows = {"primary": [other_heading, primary]}
        selected = _expand_and_pack(
            [primary],
            question="result explanation",
            family_rankings=[("dense", 1.0, [primary])],
            backend=backend,
            budget_tokens=400,
            document_anchors={},
        )
        self.assertNotIn("context_before", selected[0])

    def test_visible_markdown_heading_blocks_blind_numbered_neighbor(self) -> None:
        backend = FakeBackend()
        primary = result_row(
            "primary",
            "# Results\nThis result continues",
            path="guide.md",
            section="guide.md #2",
            chunk_index=1,
        )
        other_heading = result_row(
            "other",
            "# Introduction\nUnrelated introduction",
            path="guide.md",
            section="guide.md #1",
            chunk_index=0,
        )
        backend.neighbor_rows = {"primary": [other_heading, primary]}
        selected = _expand_and_pack(
            [primary],
            question="result explanation",
            family_rankings=[("dense", 1.0, [primary])],
            backend=backend,
            budget_tokens=400,
            document_anchors={},
        )
        self.assertNotIn("context_before", selected[0])

    def test_numbered_plain_chunks_do_not_expand_without_a_reason(self) -> None:
        backend = FakeBackend()
        before = result_row(
            "before",
            "A complete preceding paragraph.",
            path="guide.txt",
            section="guide.txt #1",
            chunk_index=0,
        )
        primary = result_row(
            "primary",
            "A complete matched paragraph.",
            path="guide.txt",
            section="guide.txt #2",
            chunk_index=1,
        )
        backend.neighbor_rows = {"primary": [before, primary]}
        selected = _expand_and_pack(
            [primary],
            question="matched paragraph",
            family_rankings=[("dense", 1.0, [primary])],
            backend=backend,
            budget_tokens=400,
            document_anchors={},
        )
        self.assertNotIn("context_before", selected[0])

    def test_overlapping_text_is_not_repeated_in_context(self) -> None:
        backend = FakeBackend()
        overlap = "shared overlap between neighboring chunks"
        before = result_row(
            "before",
            "Earlier explanation. " + overlap,
            path="guide.md",
            section="Topic #1",
            chunk_index=0,
        )
        primary = result_row(
            "primary",
            overlap + " and this result continues",
            path="guide.md",
            section="Topic #2",
            chunk_index=1,
        )
        backend.neighbor_rows = {"primary": [before, primary]}
        selected = _expand_and_pack(
            [primary],
            question="result explanation",
            family_rankings=[("dense", 1.0, [primary])],
            backend=backend,
            budget_tokens=400,
            document_anchors={},
        )
        self.assertEqual(
            "Earlier explanation.",
            selected[0]["context_before"],
        )

    def test_code_hit_uses_enclosing_function_context(self) -> None:
        backend = FakeBackend()
        function = result_row(
            "function",
            "def calculate_total(items):",
            path="src/calculate.py",
            section="calculate.py #1",
            chunk_index=0,
        )
        function["metadata"]["source_type"] = "code"
        body = result_row(
            "body",
            "    return sum(items)",
            path="src/calculate.py",
            section="calculate.py #2",
            chunk_index=1,
        )
        body["metadata"]["source_type"] = "code"
        backend.neighbor_rows = {"body": [function, body]}
        selected = _expand_and_pack(
            [body],
            question="calculate total behavior",
            family_rankings=[("lexical", 1.0, [body])],
            backend=backend,
            budget_tokens=400,
            document_anchors={},
        )
        self.assertEqual(
            "def calculate_total(items):",
            selected[0]["context_before"],
        )
        self.assertEqual(
            "enclosing_function",
            selected[0]["context_reason"],
        )

    def test_verified_document_anchor_relaxes_only_its_document_limit(self) -> None:
        anchored = [
            result_row(
                f"anchored-{index}",
                f"anchored {index}",
                path="anchored.txt",
                chunk_index=index,
            )
            for index in range(4)
        ]
        ordinary = [
            result_row(
                f"ordinary-{index}",
                f"ordinary {index}",
                path="ordinary.txt",
                chunk_index=index,
            )
            for index in range(4)
        ]
        selected = _dedupe_and_diversify(
            [*anchored, *ordinary],
            top_k=8,
            max_per_doc=2,
            relaxed_doc_limits={"anchored.txt": 4},
        )
        self.assertEqual(4, sum(row["metadata"]["path"] == "anchored.txt" for row in selected))
        self.assertEqual(2, sum(row["metadata"]["path"] == "ordinary.txt" for row in selected))

    def test_late_family_floor_replaces_only_unprotected_same_document_row(self) -> None:
        first = result_row(
            "first-floor",
            "first protected result",
            path="same.txt",
            chunk_index=0,
            debug={"candidate_pool_sources": ["dense_floor", "rrf"]},
        )
        unprotected = result_row(
            "unprotected",
            "ordinary fused result",
            path="same.txt",
            chunk_index=1,
            debug={"candidate_pool_sources": ["rrf"]},
        )
        late_floor = result_row(
            "late-floor",
            "late lexical rescue",
            path="same.txt",
            chunk_index=2,
            debug={"candidate_pool_sources": ["lexical_floor", "rrf"]},
        )
        selected = _dedupe_and_diversify(
            [first, unprotected, late_floor],
            top_k=3,
            max_per_doc=2,
        )
        self.assertEqual(
            ["first-floor", "late-floor"],
            [row["id"] for row in selected],
        )

    def test_later_family_floor_does_not_displace_earlier_protected_rrf_row(self) -> None:
        rows = [
            result_row(
                "dense-and-lexical",
                "top protected result",
                path="same.txt",
                chunk_index=0,
                debug={"candidate_pool_sources": ["dense_floor", "lexical_floor", "rrf"]},
            ),
            result_row(
                "early-lexical",
                "second protected result",
                path="same.txt",
                chunk_index=1,
                debug={"candidate_pool_sources": ["lexical_floor", "rrf"]},
            ),
            result_row(
                "late-lexical",
                "later protected result",
                path="same.txt",
                chunk_index=2,
                debug={"candidate_pool_sources": ["lexical_floor", "rrf"]},
            ),
        ]
        selected = _dedupe_and_diversify(
            rows,
            top_k=3,
            max_per_doc=2,
        )
        self.assertEqual(
            ["dense-and-lexical", "early-lexical"],
            [row["id"] for row in selected],
        )

    def test_same_section_verified_exact_neighbor_becomes_attached_context(self) -> None:
        backend = FakeBackend()
        anchor = result_row(
            "anchor",
            "M-4 accident",
            signals=["exact"],
            path="report.pdf",
            section="Page 1 #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        neighbor = result_row(
            "cause",
            "The probable cause was loss of directional control.",
            path="report.pdf",
            section="Page 1 #2",
            chunk_index=1,
        )
        backend.exact_rows = [anchor]
        backend.lexical_rows = [anchor]
        backend.dense_rows = []
        backend.anchor_rows = []
        backend.metadata_rows = [anchor]
        backend.neighbor_rows = {"anchor": [anchor, neighbor]}
        rows, route = adaptive_hybrid_query(
            "What caused the M-4 accident?",
            top_k=3,
            budget_tokens=1_200,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertNotIn("cause", [row["id"] for row in rows])
        primary = next(row for row in rows if row["id"] == "anchor")
        self.assertEqual(
            "The probable cause was loss of directional control.",
            primary["context_after"],
        )
        context_range = next(
            item
            for item in primary["source_ranges"]
            if item["kind"] == "context_after"
        )
        self.assertEqual("cause", context_range["chunk_uid"])
        self.assertEqual(
            "same_section_neighbor",
            context_range["relationship"],
        )
        payload = json_payload(rows, "What caused the M-4 accident?", "incident-rag", 6_000)
        self.assertEqual(["R1"], [item["id"] for item in payload["evidence"]])
        self.assertEqual(
            "The probable cause was loss of directional control.",
            payload["evidence"][0]["context_after"],
        )
        self.assertEqual(1, route["retrieval_funnel"]["verified_document_anchor_count"])

    def test_unrelated_neighbor_is_not_promoted_by_verified_exact(self) -> None:
        backend = FakeBackend()
        anchor = result_row(
            "anchor",
            "M-4 accident",
            signals=["exact"],
            path="report.pdf",
            section="Page 1 #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        unrelated = result_row(
            "unrelated",
            "Other report",
            path="other.pdf",
            section="Page 1 #2",
            chunk_index=1,
        )
        backend.exact_rows = [anchor]
        backend.lexical_rows = [anchor]
        backend.dense_rows = []
        backend.anchor_rows = []
        backend.metadata_rows = [anchor]
        backend.neighbor_rows = {"anchor": [anchor, unrelated]}
        rows, _route = adaptive_hybrid_query(
            "What caused the M-4 accident?",
            top_k=3,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertNotIn("unrelated", [row["id"] for row in rows])
        selected = next(row for row in rows if row["id"] == "anchor")
        self.assertNotIn("context_after", selected)
        payload = json_payload(rows, "What caused the M-4 accident?", "incident-rag", 6_000)
        self.assertEqual([], payload["background_context"])

    def test_ambiguous_exact_documents_do_not_promote_a_neighbor(self) -> None:
        backend = FakeBackend()
        first = result_row(
            "first",
            "M-4 accident",
            signals=["exact"],
            path="first.pdf",
            section="Page 1 #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        second = result_row(
            "second",
            "M-4 accident",
            signals=["exact"],
            path="second.pdf",
            section="Page 1 #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        neighbor = result_row(
            "neighbor",
            "The probable cause was documented here.",
            path="first.pdf",
            section="Page 1 #2",
            chunk_index=1,
        )
        backend.exact_rows = [first, second]
        backend.lexical_rows = [
            result_row("generic", "General accident background")
        ]
        backend.metadata_rows = [first]
        backend.neighbor_rows = {"first": [first, neighbor]}
        rows, route = adaptive_hybrid_query(
            "What caused the M-4 accident?",
            top_k=4,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertEqual(0, route["retrieval_funnel"]["verified_document_anchor_count"])
        self.assertNotIn("neighbor", [row["id"] for row in rows])
        self.assertTrue(
            all("context_after" not in row for row in rows)
        )
        exact_rows = [row for row in rows if "exact" in row["signals"]]
        self.assertTrue(exact_rows)
        self.assertTrue(
            all(not row["exact_evidence_eligible"] for row in exact_rows)
        )

    def test_document_specific_query_without_confirmation_keeps_exact_in_background(
        self,
    ) -> None:
        backend = FakeBackend()
        backend.exact_rows = [
            result_row(
                "jerome",
                "M-4 Jerome accident",
                signals=["exact"],
                path="jerome.pdf",
                debug={"exact_match": {"matched_terms": ["M-4"]}},
            ),
            result_row(
                "bishop",
                "M-4 Bishop accident",
                signals=["exact"],
                path="bishop.pdf",
                debug={"exact_match": {"matched_terms": ["M-4"]}},
            ),
        ]
        backend.lexical_rows = []
        rows, route = adaptive_hybrid_query(
            "What caused the Unknownville M-4 accident?",
            top_k=6,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertEqual(
            0,
            route["retrieval_funnel"]["verified_document_anchor_count"],
        )
        exact_rows = [row for row in rows if "exact" in row["signals"]]
        self.assertEqual(2, len(exact_rows))
        self.assertTrue(
            all(not row["exact_evidence_eligible"] for row in exact_rows)
        )
        payload = json_payload(
            rows,
            "What caused the Unknownville M-4 accident?",
            "incident-rag",
            6_000,
        )
        self.assertEqual([], payload["evidence"])
        self.assertEqual(
            {"jerome.pdf", "bishop.pdf"},
            {
                item["source"]["path"]
                for item in payload["background_context"]
            },
        )

    def test_multi_document_exact_exposes_only_query_corroborated_document(self) -> None:
        backend = FakeBackend()
        correct = result_row(
            "correct",
            "M-4 Jerome accident",
            signals=["exact"],
            path="correct.pdf",
            section="Page 1 #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        wrong = result_row(
            "wrong",
            "M-4 Bishop accident",
            signals=["exact"],
            path="wrong.pdf",
            section="Page 1 #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        neighbor = result_row(
            "cause",
            "The probable cause was loss of control.",
            path="correct.pdf",
            section="Page 1 #2",
            chunk_index=1,
        )
        backend.exact_rows = [correct, wrong]
        backend.lexical_rows = [
            correct,
            result_row("other", "Other accident", path="other.pdf"),
            wrong,
        ]
        backend.metadata_rows = [correct]
        backend.neighbor_rows = {"correct": [correct, neighbor]}
        rows, _route = adaptive_hybrid_query(
            "What caused the Jerome M-4 accident?",
            top_k=4,
            budget_tokens=1_200,
            db_scope_confirmed=True,
            backend=backend,
        )
        payload = json_payload(
            rows,
            "What caused the Jerome M-4 accident?",
            "incident-rag",
            6_000,
        )
        evidence_paths = {
            item["source"]["path"]
            for item in payload["evidence"]
        }
        background_paths = {
            item["source"]["path"]
            for item in payload["background_context"]
        }
        self.assertEqual({"correct.pdf"}, evidence_paths)
        self.assertIn("wrong.pdf", background_paths)

    def test_rank_five_single_document_confirmation_does_not_select_exact_document(
        self,
    ) -> None:
        backend = FakeBackend()
        correct = result_row(
            "correct",
            "M-4 Jerome accident",
            signals=["exact"],
            path="correct.pdf",
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        wrong = result_row(
            "wrong",
            "M-4 Bishop accident",
            signals=["exact"],
            path="wrong.pdf",
            debug={"exact_match": {"matched_terms": ["M-4"]}},
        )
        backend.exact_rows = [correct, wrong]
        backend.lexical_rows = [
            result_row(f"other-{index}", "Other accident", path=f"other-{index}.pdf")
            for index in range(1, 5)
        ] + [correct]
        rows, route = adaptive_hybrid_query(
            "What caused the Jerome M-4 accident?",
            top_k=8,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertEqual(
            0,
            route["retrieval_funnel"]["verified_document_anchor_count"],
        )
        exact_rows = [row for row in rows if "exact" in row["signals"]]
        self.assertEqual(2, len(exact_rows))
        self.assertTrue(
            all(not row["exact_evidence_eligible"] for row in exact_rows)
        )

    def test_truncated_raw_exact_set_cannot_certify_a_unique_document(self) -> None:
        backend = FakeBackend()
        verified = result_row(
            "verified",
            "A2L evidence",
            signals=["exact"],
            path="verified.pdf",
            debug={"exact_match": {"matched_terms": ["A2L"]}},
        )
        backend.exact_rows = [
            verified,
            *[
                result_row(
                    f"other-{index}",
                    f"unrelated identifier {index}",
                    signals=["exact"],
                    path=f"other-{index}.pdf",
                )
                for index in range(20)
            ],
        ]
        backend.lexical_rows = [verified]
        rows, route = adaptive_hybrid_query(
            "A2L evidence",
            top_k=3,
            db_scope_confirmed=True,
            backend=backend,
        )
        selected = next(row for row in rows if row["id"] == "verified")
        self.assertFalse(selected["exact_evidence_eligible"])
        self.assertEqual(
            0,
            route["retrieval_funnel"]["verified_document_anchor_count"],
        )

    def test_identifier_only_query_can_use_complete_multi_document_exact_set(self) -> None:
        backend = FakeBackend()
        first = result_row(
            "first-a2l",
            "A2L refrigerant evidence",
            signals=["exact"],
            path="first.pdf",
            debug={"exact_match": {"matched_terms": ["A2L"]}},
        )
        second = result_row(
            "second-a2l",
            "A2L efficiency evidence",
            signals=["exact"],
            path="second.pdf",
            debug={"exact_match": {"matched_terms": ["A2L"]}},
        )
        backend.exact_rows = [first, second]
        backend.lexical_rows = [first]
        rows, _route = adaptive_hybrid_query(
            "A2Lについて教えて",
            top_k=3,
            db_scope_confirmed=True,
            backend=backend,
        )
        exact_rows = [row for row in rows if "exact" in row["signals"]]
        self.assertTrue(exact_rows)
        self.assertTrue(
            all(row["exact_evidence_eligible"] for row in exact_rows)
        )

    def test_identifier_only_lookup_is_stable_with_fallback_cjk_tokens(
        self,
    ) -> None:
        fallback_tokens = [
            "につ",
            "つい",
            "いて",
            "て教",
            "教え",
            "えて",
            "につい",
            "ついて",
            "いて教",
            "て教え",
            "教えて",
        ]
        with patch(
            "software_rag_tool.retrieval.tokens_for_fts",
            side_effect=lambda text: (
                [] if not str(text).strip() else fallback_tokens
            ),
        ):
            self.assertTrue(
                _is_identifier_only_lookup("A2Lについて教えて")
            )

    def test_different_section_neighbor_requires_an_independent_signal(self) -> None:
        primary = result_row(
            "anchor",
            "OH-58A accident",
            path="report.pdf",
            section="Page 1 #1",
            chunk_index=0,
        )
        neighbor = result_row(
            "cause",
            "The probable cause.",
            path="report.pdf",
            section="Page 2 #1",
            chunk_index=1,
        )
        anchor = {
            "anchor_chunk_uid": "anchor",
            "anchor_term": "OH-58A",
            "document_key": "report.pdf",
        }
        unrelated = _decorate_anchored_neighbor(
            neighbor,
            primary,
            document_anchor=anchor,
            family_signals={},
        )
        self.assertNotIn("support_kind", unrelated)
        promoted = _decorate_anchored_neighbor(
            neighbor,
            primary,
            document_anchor=anchor,
            family_signals={"cause": {"dense"}},
        )
        self.assertEqual("anchored_neighbor", promoted["support_kind"])
        self.assertEqual(["dense"], promoted["independent_signals"])

    def test_adaptive_uncorroborated_anchor_adds_dense_once(self) -> None:
        backend = FakeBackend()
        backend.lexical_rows.insert(
            0,
            result_row("poland", "Poland air-conditioning evidence", signals=["lexical"]),
        )
        backend.anchor_rows[0]["debug"] = {
            "lexical_anchor": {
                "token": "Poland",
                "document_df": 1,
                "information_score": 2.0,
            }
        }
        rows, route = adaptive_hybrid_query(
            "RAGでポーランドの空調について教えて",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertIn("poland", [row["id"] for row in rows])
        poland = next(row for row in rows if row["id"] == "poland")
        self.assertNotIn("lexical_anchor", poland["signals"])
        self.assertTrue(route["dense_used"])
        self.assertIsNone(route["dense_skipped_reason"])
        self.assertEqual(
            {"dense": 1, "exact": 1, "lexical": 1, "anchor": 1, "metadata": 1},
            backend.calls,
        )

    def test_adaptive_certified_anchor_requires_full_query_and_metadata(self) -> None:
        backend = FakeBackend()
        poland = result_row(
            "poland",
            "Poland air conditioning market evidence",
            signals=["lexical"],
        )
        backend.lexical_rows.insert(0, poland)
        backend.anchor_rows[0] = {
            **poland,
            "debug": {
                "lexical_anchor": {
                    "token": "Poland",
                    "document_df": 1,
                    "information_score": 2.0,
                }
            },
        }
        backend.metadata_rows = [poland]
        rows, route = adaptive_hybrid_query(
            "Poland air conditioning market",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertEqual("poland", rows[0]["id"])
        self.assertFalse(route["dense_used"])
        self.assertEqual("certified_low_df_anchor", route["dense_skipped_reason"])
        self.assertEqual(
            {"dense": 0, "exact": 1, "lexical": 1, "anchor": 1, "metadata": 1},
            backend.calls,
        )

    def test_adaptive_semantic_miss_adds_dense_once_without_repeating_lexical(self) -> None:
        backend = FakeBackend()
        backend.anchor_rows = []
        rows, route = adaptive_hybrid_query(
            "How does seasonal efficiency influence household demand?",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertTrue(rows)
        self.assertTrue(route["dense_used"])
        self.assertEqual("adaptive_hybrid_dense", route["retrieval_route"])
        self.assertIsNone(route["dense_skipped_reason"])
        self.assertEqual(
            {"dense": 1, "exact": 1, "lexical": 1, "anchor": 1, "metadata": 1},
            backend.calls,
        )

    def test_semantic_ipv6_term_does_not_skip_dense_as_an_identifier(self) -> None:
        backend = FakeBackend()
        backend.anchor_rows = []
        backend.exact_rows = [
            result_row("ipv6", "General IPv6 background", signals=["exact"])
        ]
        _rows, route = adaptive_hybrid_query(
            "How are IPv6 extension headers exported by IPFIX?",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertTrue(route["dense_used"])
        self.assertIsNone(route["dense_skipped_reason"])

    def test_hyphenated_common_word_does_not_skip_dense_as_an_identifier(self) -> None:
        backend = FakeBackend()
        backend.anchor_rows = []
        backend.exact_rows = [
            result_row("common", "air-conditioner background", signals=["exact"])
        ]
        _rows, route = adaptive_hybrid_query(
            "Why can an air-conditioner motor use a neodymium magnet?",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertTrue(route["dense_used"])
        self.assertIsNone(route["dense_skipped_reason"])

    def test_adaptive_anchor_does_not_double_vote_an_existing_lexical_row(self) -> None:
        backend = FakeBackend()
        backend.anchor_rows = [
            result_row("generic", "General cooling context", signals=["lexical_anchor"])
        ]
        rows, route = adaptive_hybrid_query(
            "How does seasonal efficiency influence household demand?",
            top_k=2,
            db_scope_confirmed=True,
            explain=True,
            backend=backend,
        )
        self.assertTrue(route["dense_used"])
        generic = next(row for row in rows if row["id"] == "generic")
        family_ranks = generic["debug"]["family_ranks"]
        self.assertIn("lexical", family_ranks)
        self.assertNotIn("anchor_candidate", family_ranks)

    def test_adaptive_false_exact_is_verified_before_fusion_and_certifies_no_hit(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [
            {
                **result_row("false", "A2L only", signals=["exact"]),
                "debug": {"exact_match": {"matched_terms": ["A2W"]}},
            }
        ]
        _rows, route = adaptive_hybrid_query(
            "A2Wについて教えて",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertEqual([], route["verified_exact_rows"])
        self.assertFalse(route["dense_used"])
        self.assertEqual("verified_identifier_no_hit", route["dense_skipped_reason"])
        self.assertEqual(
            {"dense": 0, "exact": 1, "lexical": 1, "anchor": 1, "metadata": 1},
            backend.calls,
        )

    def test_adaptive_mixed_identifiers_preserve_verified_exact_as_partial(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [
            {
                **result_row("supported", "A2L evidence", signals=["exact"]),
                "debug": {"exact_match": {"matched_terms": ["A2L"]}},
            }
        ]
        rows, route = adaptive_hybrid_query(
            "A2LとA2Wについて教えて",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertEqual("supported", rows[0]["id"])
        self.assertFalse(route["dense_used"])
        self.assertEqual(
            "verified_identifier_partial",
            route["dense_skipped_reason"],
        )
        self.assertEqual(
            ["A2W"],
            route["certificate"]["unmatched_identifiers"],
        )

    def test_exact_raw_verification_rejects_identifier_continuations(self) -> None:
        for near_collision in ("A2W-extra", "A2W_extra"):
            backend = FakeBackend()
            backend.exact_rows = [
                {
                    **result_row("near", near_collision, signals=["exact"]),
                    "debug": {"exact_match": {"matched_terms": ["A2W"]}},
                }
            ]
            _rows, route = adaptive_hybrid_query(
                "A2Wについて教えて",
                top_k=2,
                db_scope_confirmed=True,
                backend=backend,
            )
            self.assertEqual([], route["verified_exact_rows"])
            self.assertEqual(
                "verified_identifier_no_hit",
                route["dense_skipped_reason"],
            )

    def test_near_collision_no_hit_cannot_promote_a_neighbor(self) -> None:
        backend = FakeBackend()
        near = result_row(
            "near",
            "A2W-extra",
            signals=["exact"],
            path="near.txt",
            section="Page 1 #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["A2W"]}},
        )
        neighbor = result_row(
            "neighbor",
            "Nearby text",
            path="near.txt",
            section="Page 1 #2",
            chunk_index=1,
        )
        backend.exact_rows = [near]
        backend.lexical_rows = [near]
        backend.anchor_rows = []
        backend.metadata_rows = [near]
        backend.neighbor_rows = {"near": [near, neighbor]}
        rows, route = adaptive_hybrid_query(
            "A2Wについて教えて",
            top_k=3,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertEqual("verified_identifier_no_hit", route["dense_skipped_reason"])
        self.assertFalse(any(row.get("support_kind") for row in rows))

    def test_cold_fast_path_keeps_rare_anchor_as_direct_evidence(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [result_row("rag", "RAG background", signals=["exact"])]
        poland = result_row(
            "poland",
            "Poland air conditioning market evidence",
            signals=["lexical"],
        )
        backend.lexical_rows.insert(
            0,
            poland,
        )
        backend.metadata_rows = [poland]
        backend.anchor_rows[0] = {
            **poland,
            "debug": {
                "lexical_anchor": {
                    "token": "Poland",
                    "document_df": 1,
                    "information_score": 2.0,
                }
            },
        }
        rows = cold_lexical_fast_path(
            "Poland air conditioning market",
            top_k=2,
            db_scope_confirmed=True,
            backend=backend,
        )
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual("poland", rows[0]["id"])
        self.assertIn("lexical_anchor", rows[0]["signals"])
        self.assertEqual(0, backend.calls["dense"])

    def test_cold_fast_path_rejects_low_df_anchor_without_scope_confirmation(self) -> None:
        backend = FakeBackend()
        backend.lexical_rows.insert(
            0,
            result_row("poland", "Poland air-conditioning evidence", signals=["lexical"]),
        )
        backend.anchor_rows[0]["debug"] = {
            "lexical_anchor": {
                "token": "Poland",
                "document_df": 1,
                "information_score": 2.0,
            }
        }
        rows = cold_lexical_fast_path(
            "Tell me about Poland",
            top_k=2,
            db_scope_confirmed=False,
            backend=backend,
        )
        self.assertIsNone(rows)

    def test_cold_fast_path_rejects_weak_acronym_without_rare_anchor(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [result_row("rag", "RAG background", signals=["exact"])]
        backend.anchor_rows = []
        rows = cold_lexical_fast_path(
            "RAGで一般的な概要を教えて",
            top_k=2,
            backend=backend,
        )
        self.assertIsNone(rows)
        self.assertEqual(0, backend.calls["dense"])

    def test_one_anchor_candidate_survives_final_hybrid_budget(self) -> None:
        backend = FakeBackend()
        rows = hybrid_query(
            "RAGでポーランドの空調について教えて",
            top_k=1,
            budget_tokens=200,
            backend=backend,
        )
        self.assertEqual(["poland"], [row["id"] for row in rows])
        self.assertIn("lexical_anchor", rows[0]["signals"])
        self.assertEqual(1, backend.calls["dense"])
        self.assertEqual(1, backend.calls["lexical"])
        self.assertEqual(1, backend.calls["anchor"])

    def test_exact_candidate_takes_priority_and_skips_anchor_rescue(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [result_row("exact", "A2L", signals=["exact"])]
        rows = hybrid_query("A2Lについて", top_k=1, backend=backend)
        self.assertEqual("exact", rows[0]["id"])
        self.assertEqual(0, backend.calls["anchor"])

    def test_plain_acronym_exact_candidate_does_not_block_rare_anchor(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [result_row("rag", "RAG background", signals=["exact"])]
        rows = hybrid_query(
            "RAGでポーランドの空調について教えて",
            top_k=1,
            backend=backend,
        )
        self.assertEqual("poland", rows[0]["id"])
        self.assertEqual(1, backend.calls["anchor"])

    def test_unmatched_strong_identifier_does_not_promote_weak_exact_row(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [
            {
                **result_row("rag", "RAG background", signals=["exact"]),
                "debug": {
                    "exact_match": {
                        "matched_terms": ["rag"],
                    }
                },
            }
        ]
        rows = hybrid_query(
            "RAGでA2Wとポーランドの空調について教えて",
            top_k=1,
            backend=backend,
        )
        self.assertEqual("poland", rows[0]["id"])
        self.assertEqual(1, backend.calls["anchor"])

    def test_anchor_text_precedes_neighbors_before_budget_truncation(self) -> None:
        backend = FakeBackend()
        previous = result_row("previous", "x" * 2_000)
        anchor = backend.anchor_rows[0]
        following = result_row("following", "y" * 2_000)
        backend.get_neighbor_rows = (  # type: ignore[method-assign]
            lambda _chunk_uid, *, window=1: [previous, anchor, following]
        )
        rows = hybrid_query(
            "RAGでポーランドの空調について教えて",
            top_k=1,
            budget_tokens=200,
            backend=backend,
        )
        self.assertIn("Poland", rows[0]["text"])

    def test_exact_text_precedes_neighbors_before_budget_truncation(self) -> None:
        backend = FakeBackend()
        exact = result_row("exact", "A2L direct evidence", signals=["exact"])
        backend.exact_rows = [exact]
        previous = result_row("previous", "x" * 2_000)
        following = result_row("following", "y" * 2_000)
        backend.get_neighbor_rows = (  # type: ignore[method-assign]
            lambda _chunk_uid, *, window=1: [previous, exact, following]
        )
        rows = hybrid_query(
            "A2Lについて",
            top_k=1,
            budget_tokens=200,
            backend=backend,
        )
        self.assertIn("A2L", rows[0]["text"])

    def test_neighbor_chunks_do_not_inherit_direct_evidence_signals(self) -> None:
        backend = FakeBackend()
        exact = result_row(
            "exact",
            "A2L direct evidence",
            signals=["exact"],
            path="same.txt",
            section="Section #1",
            chunk_index=0,
            debug={"exact_match": {"matched_terms": ["A2L"]}},
        )
        neighbor = result_row(
            "neighbor",
            "Nearby explanation",
            signals=["exact"],
            path="same.txt",
            section="Section #2",
            chunk_index=1,
        )
        backend.exact_rows = [exact]
        backend.lexical_rows = [exact]
        backend.get_neighbor_rows = (  # type: ignore[method-assign]
            lambda _chunk_uid, *, window=1: [exact, neighbor]
        )
        rows = _expand_and_pack(
            [exact],
            question="A2Lについて",
            family_rankings=[("exact", 1.0, [exact])],
            backend=backend,
            budget_tokens=400,
            document_anchors={"exact": {"anchor_chunk_uid": "exact"}},
        )
        self.assertNotIn("neighbor", [row["id"] for row in rows])
        selected = next(row for row in rows if row["id"] == "exact")
        self.assertEqual("Nearby explanation", selected["context_after"])
        context_range = next(
            item
            for item in selected["source_ranges"]
            if item["kind"] == "context_after"
        )
        self.assertNotIn("signals", context_range)

    def test_anchor_rescue_is_disabled_for_lexical_only_evaluation(self) -> None:
        backend = FakeBackend()
        rows = hybrid_query(
            "RAGでポーランドの空調について教えて",
            top_k=1,
            use_dense=False,
            backend=backend,
        )
        self.assertEqual("generic", rows[0]["id"])
        self.assertEqual(0, backend.calls["anchor"])

    def test_anchor_failure_does_not_remove_normal_hybrid_results(self) -> None:
        backend = FakeBackend()

        def fail(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("fts5vocab unavailable")

        backend.anchor_lexical_search = fail  # type: ignore[method-assign]
        rows = hybrid_query("ordinary cooling question", top_k=1, backend=backend)
        self.assertEqual("generic", rows[0]["id"])

    def test_truthy_test_fixture_is_removed_before_fusion(self) -> None:
        backend = FakeBackend()
        fixture = result_row("fixture", "fixture", test_fixture="true")
        backend.dense_rows = [fixture]
        backend.exact_rows = [fixture]
        backend.lexical_rows = [fixture, result_row("real", "real evidence")]
        backend.anchor_rows = [fixture]
        backend.metadata_rows = [fixture]
        rows = hybrid_query("fixture-id", top_k=3, backend=backend)
        self.assertEqual(["real"], [row["id"] for row in rows])

    def test_false_test_fixture_string_is_not_removed(self) -> None:
        backend = FakeBackend()
        backend.dense_rows = [result_row("real", "real evidence", test_fixture="false")]
        backend.lexical_rows = []
        backend.anchor_rows = []
        rows = hybrid_query("ordinary question", top_k=1, backend=backend)
        self.assertEqual("real", rows[0]["id"])


class CatalogAnchorContractTests(unittest.TestCase):
    def test_acronym_number_identifier_accepts_spacing_without_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite"
            with catalog.connect(path) as connection:
                catalog._insert_record(
                    connection,
                    {
                        "id": "rfc10026",
                        "text": "RFC10026 Operational Recommendations",
                        "metadata": {"path": "rfc10026.txt"},
                    },
                    "now",
                )
                connection.commit()
            rows = catalog.exact_search("RFC 10026", top_k=1, path=path)
            self.assertEqual("rfc10026.txt", rows[0]["metadata"]["path"])

    def test_rfc_spacing_equivalence_is_bidirectional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite"
            with catalog.connect(path) as connection:
                catalog._insert_record(
                    connection,
                    {
                        "id": "rfc-spaced",
                        "text": "RFC 10026 Operational Recommendations",
                        "metadata": {"path": "rfc-spaced.txt"},
                    },
                    "now",
                )
                connection.commit()
            rows = catalog.exact_search("RFC10026", top_k=1, path=path)
            self.assertEqual("rfc-spaced.txt", rows[0]["metadata"]["path"])
            self.assertEqual(
                [],
                catalog.exact_search("RFC-10026", top_k=1, path=path),
            )

    def test_acronym_number_with_space_is_one_strong_anchor(self) -> None:
        self.assertIn("RFC 10026", extract_anchors("Explain RFC 10026"))
        self.assertIn("rfc 10026", extract_anchors("Explain rfc 10026"))

    def test_only_rfc_spacing_is_equivalent(self) -> None:
        self.assertIn("rfc10026", identifier_match_keys("RFC 10026"))
        self.assertIn("rfc10026", identifier_match_keys("RFC10026"))
        self.assertNotIn("rfc10026", identifier_match_keys("RFC-10026"))
        self.assertNotIn("ac123", identifier_match_keys("AC 123"))

    def test_low_document_frequency_token_is_selected_without_schema_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite"
            with catalog.connect(path) as connection:
                for index in range(20):
                    self._insert_record(
                        connection,
                        f"generic-{index}",
                        "空調 冷房 efficiency general",
                    )
                poland_doc_pk = self._insert_record(
                    connection,
                    "poland",
                    "ポーランド 空調 market evidence",
                )
                for index in range(1, 7):
                    self._insert_chunk(
                        connection,
                        poland_doc_pk,
                        f"poland-chunk-{index}",
                        "poland",
                        "ポーランド additional evidence",
                    )
                self._insert_record(
                    connection,
                    "poland-second-document",
                    "ポーランド regional evidence",
                )
                connection.commit()
            rows = catalog.anchor_lexical_search(
                "RAGでポーランドの空調について教えて",
                top_k=1,
                path=path,
            )
            self.assertTrue(str(rows[0]["id"]).startswith("poland-chunk"))
            self.assertIn("ポーランド", rows[0]["text"])
            self.assertEqual(
                0,
                self._persistent_object_count(path, "fts_word_vocab"),
            )

    @staticmethod
    def _insert_record(connection: Any, record_id: str, text: str) -> int:
        cursor = connection.execute(
            """
            INSERT INTO document(doc_id, path, metadata_json, updated_at)
            VALUES(?, ?, '{}', 'now')
            """,
            (record_id, f"{record_id}.txt"),
        )
        doc_pk = int(cursor.lastrowid)
        CatalogAnchorContractTests._insert_chunk(
            connection,
            doc_pk,
            f"{record_id}-chunk",
            record_id,
            text,
        )
        return doc_pk

    @staticmethod
    def _insert_chunk(
        connection: Any,
        doc_pk: int,
        chunk_uid: str,
        doc_id: str,
        text: str,
    ) -> None:
        cursor = connection.execute(
            """
            INSERT INTO chunk(
              chunk_uid, doc_pk, doc_id, text, location_json, metadata_json, updated_at
            )
            VALUES(?, ?, ?, ?, '{}', '{}', 'now')
            """,
            (chunk_uid, doc_pk, doc_id, text),
        )
        chunk_pk = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO fts_word(rowid, heading_tokens, body_tokens) VALUES(?, '', ?)",
            (chunk_pk, tokenize_for_fts(text)),
        )

    @staticmethod
    def _persistent_object_count(path: Path, name: str) -> int:
        with catalog.connect(path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM sqlite_master WHERE name = ?",
                (name,),
            ).fetchone()
        return int(row["count"] if row else 0)


class LightweightJsonContractTests(unittest.TestCase):
    def test_anchor_evidence_is_separated_from_background_and_warns_on_table_fragment(self) -> None:
        rows = [
            {
                **result_row(
                    "poland",
                    "5 18 17 5 22\nポーランド 18 5 24 16 5 21\nドイツ 29 14 43 29 12 41",
                    signals=["lexical", "lexical_anchor"],
                ),
                "rank": 1,
                "metadata": {
                    "path": "market.pdf",
                    "section_path": "Page 6 #2",
                },
            },
            {
                **result_row("general", "General cooling context", signals=["dense"]),
                "rank": 2,
            },
        ]
        payload = json_payload(rows, "question", "ac-rag", 900)
        self.assertEqual("partial", payload["status"])
        self.assertEqual("partial", payload["answerability"])
        self.assertEqual("ac-rag", payload["selected_db"])
        self.assertEqual(["R1"], [item["id"] for item in payload["evidence"]])
        self.assertEqual(["R2"], [item["id"] for item in payload["background_context"]])
        self.assertTrue(payload["warnings"])

    def test_plain_acronym_exact_result_is_background_when_anchor_exists(self) -> None:
        rows = [
            {
                **result_row("poland", "ポーランド evidence", signals=["lexical_anchor"]),
                "rank": 1,
            },
            {
                **result_row("rag", "RAG background", signals=["exact"]),
                "rank": 2,
            },
        ]
        payload = json_payload(
            rows,
            "RAGでポーランドの空調について教えて",
            "ac-rag",
            900,
        )
        self.assertEqual(["R1"], [item["id"] for item in payload["evidence"]])
        self.assertEqual(["R2"], [item["id"] for item in payload["background_context"]])

    def test_unmatched_strong_identifier_does_not_make_weak_exact_direct_evidence(self) -> None:
        rows = [
            {
                **result_row("poland", "ポーランド evidence", signals=["lexical_anchor"]),
                "rank": 1,
            },
            {
                **result_row("rag", "RAG background", signals=["exact"]),
                "rank": 2,
                "debug": {"exact_match": {"matched_terms": ["rag"]}},
            },
        ]
        payload = json_payload(
            rows,
            "RAGでA2Wとポーランドの空調について教えて",
            "ac-rag",
            900,
        )
        self.assertEqual(["R1"], [item["id"] for item in payload["evidence"]])
        self.assertEqual(["R2"], [item["id"] for item in payload["background_context"]])

    def test_matching_strong_exact_is_separated_from_dense_background(self) -> None:
        rows = [
            {
                **result_row("exact", "A2L evidence", signals=["exact"]),
                "rank": 1,
                "debug": {"exact_match": {"matched_terms": ["a2l"]}},
            },
            {
                **result_row("general", "General cooling background", signals=["dense"]),
                "rank": 2,
            },
        ]
        payload = json_payload(rows, "A2Lについて", "ac-rag", 900)
        self.assertEqual(["R1"], [item["id"] for item in payload["evidence"]])
        self.assertEqual(["R2"], [item["id"] for item in payload["background_context"]])

    def test_empty_result_is_no_hit_with_legacy_status(self) -> None:
        payload = json_payload([], "question", "ac-rag", 900)
        self.assertEqual("no_hit", payload["status"])
        self.assertEqual("no_evidence", payload["legacy_status"])
        self.assertEqual("none", payload["answerability"])
        self.assertEqual([], payload["evidence"])

    def test_error_and_setup_contracts_have_all_lightweight_fields(self) -> None:
        for status in ("error", "setup_required"):
            payload = normalize_search_contract(
                {
                    "schema": "local-rag.search.v1",
                    "status": status,
                    "db": "ac-rag",
                }
            )
            self.assertEqual("ac-rag", payload["selected_db"])
            self.assertEqual("none", payload["answerability"])
            self.assertEqual([], payload["evidence"])
            self.assertEqual([], payload["background_context"])
            self.assertEqual([], payload["related_context"])
            self.assertEqual([], payload["warnings"])


if __name__ == "__main__":
    unittest.main()
