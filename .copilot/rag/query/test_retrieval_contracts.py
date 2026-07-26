from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import catalog
from software_rag_tool.retrieval import adaptive_hybrid_query, cold_lexical_fast_path, hybrid_query
from software_rag_tool.search_api import json_payload, normalize_search_contract
from software_rag_tool.tokenize import extract_anchors, identifier_match_keys, tokenize_for_fts


def result_row(
    row_id: str,
    text: str,
    *,
    signals: list[str] | None = None,
    test_fixture: object | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "path": f"{row_id}.txt",
        "section_path": "Section",
    }
    if test_fixture is not None:
        metadata["test_fixture"] = test_fixture
    return {
        "id": row_id,
        "text": text,
        "metadata": metadata,
        "signals": list(signals or []),
        "score": 0.0,
    }


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
        row = self.fetch_rows_by_ids([chunk_uid]).get(chunk_uid)
        return [row] if row else []


class HybridAnchorContractTests(unittest.TestCase):
    def test_adaptive_certified_anchor_collects_each_lexical_family_once(self) -> None:
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
        self.assertEqual("poland", rows[0]["id"])
        self.assertFalse(route["dense_used"])
        self.assertEqual("db_scope_full_query_lexical", route["dense_skipped_reason"])
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

    def test_cold_fast_path_keeps_rare_anchor_as_direct_evidence(self) -> None:
        backend = FakeBackend()
        backend.exact_rows = [result_row("rag", "RAG background", signals=["exact"])]
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
            "RAGでポーランドの空調について教えて",
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
        exact = result_row("exact", "A2L direct evidence", signals=["exact"])
        neighbor = result_row("neighbor", "Nearby explanation", signals=["exact"])
        backend.exact_rows = [exact]
        backend.get_neighbor_rows = (  # type: ignore[method-assign]
            lambda _chunk_uid, *, window=1: [exact, neighbor]
        )
        rows = hybrid_query("A2Lについて", top_k=3, backend=backend)
        selected = next(row for row in rows if row["id"] == "neighbor")
        self.assertEqual(["neighbor"], selected["signals"])

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
