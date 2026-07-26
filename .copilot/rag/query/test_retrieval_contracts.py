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
from software_rag_tool.retrieval import hybrid_query
from software_rag_tool.search_api import json_payload, normalize_search_contract
from software_rag_tool.tokenize import tokenize_for_fts


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
    def test_low_chunk_frequency_token_is_selected_without_schema_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite"
            with catalog.connect(path) as connection:
                for index in range(20):
                    self._insert_record(
                        connection,
                        f"generic-{index}",
                        "空調 冷房 efficiency general",
                    )
                self._insert_record(
                    connection,
                    "poland",
                    "ポーランド 空調 market evidence",
                )
                connection.commit()
            rows = catalog.anchor_lexical_search(
                "RAGでポーランドの空調について教えて",
                top_k=1,
                path=path,
            )
            self.assertEqual("poland-chunk", rows[0]["id"])
            self.assertIn("ポーランド", rows[0]["text"])
            self.assertEqual(
                0,
                self._persistent_object_count(path, "fts_word_vocab"),
            )

    @staticmethod
    def _insert_record(connection: Any, record_id: str, text: str) -> None:
        cursor = connection.execute(
            """
            INSERT INTO document(doc_id, path, metadata_json, updated_at)
            VALUES(?, ?, '{}', 'now')
            """,
            (record_id, f"{record_id}.txt"),
        )
        doc_pk = int(cursor.lastrowid)
        cursor = connection.execute(
            """
            INSERT INTO chunk(
              chunk_uid, doc_pk, doc_id, text, location_json, metadata_json, updated_at
            )
            VALUES(?, ?, ?, ?, '{}', '{}', 'now')
            """,
            (f"{record_id}-chunk", doc_pk, record_id, text),
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
