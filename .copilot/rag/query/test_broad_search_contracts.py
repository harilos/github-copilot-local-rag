from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.search_api import (  # noqa: E402
    _add_discovery_lane,
    compact_search_contract,
)
from software_rag_tool.search_request import (  # noqa: E402
    normalize_search_request,
    request_from_cli,
)


class DiscoveryStore:
    def __init__(self) -> None:
        self.exact_queries: list[str] = []
        self.dense_batches: list[list[str]] = []

    @staticmethod
    def _rows(signal: str) -> list[dict]:
        return [
            {
                "id": f"chunk-{index}",
                "text": f"関連資料 {index} の短い説明です。",
                "metadata": {
                    "path": f"docs/related-{index}.pdf",
                    "title": f"Related document {index}",
                    "page": index,
                },
                "signals": [signal],
            }
            for index in range(1, 9)
        ]

    def bm25_search(
        self,
        question: str,
        *,
        top_k: int,
        source: str = "any",
    ) -> list[dict]:
        del question, top_k, source
        return self._rows("lexical")

    def metadata_search(
        self,
        question: str,
        *,
        top_k: int,
        source: str = "any",
    ) -> list[dict]:
        del question, top_k, source
        return []

    def exact_search(
        self,
        question: str,
        *,
        top_k: int,
        source: str = "any",
    ) -> list[dict]:
        del top_k, source
        self.exact_queries.append(question)
        return []

    def vector_query_many(
        self,
        questions: list[str],
        top_k: int,
        source: str = "any",
    ) -> list[list[dict]]:
        del top_k, source
        self.dense_batches.append(list(questions))
        return [self._rows("dense") for _question in questions]


class StructuredRequestTests(unittest.TestCase):
    def test_repeated_windows_safe_arguments_match_json_request(self) -> None:
        question = 'A2W "rev-1" と C:\\資料\\仕様 を確認して'
        repeated = argparse.Namespace(
            request_json=False,
            stdin=False,
            answer_goal="evidence",
            literal_identifier=["A2W"],
            entity=["C:\\資料\\仕様"],
            facet=["A2W", "A2W の仕様・用途"],
            semantic_hypothesis=["air-to-water"],
        )
        json_args = argparse.Namespace(request_json=True, stdin=True)
        from_repeated = request_from_cli(
            repeated,
            positional_question=question,
        )
        from_json = request_from_cli(
            json_args,
            positional_question="",
            stdin_text=json.dumps(
                {
                    "schema_version": "rag-search-request-v1",
                    "original_question": question,
                    "answer_goal": "evidence",
                    "literal_identifiers": ["A2W"],
                    "entities": ["C:\\資料\\仕様"],
                    "facets": ["A2W", "A2W の仕様・用途"],
                    "inferred_concepts": ["air-to-water"],
                },
                ensure_ascii=False,
            ),
        )
        self.assertEqual(from_json, from_repeated)
        self.assertEqual(question, from_repeated["original_question"])

    def test_discovery_lane_returns_distinct_documents_after_exact_no_hit(self) -> None:
        store = DiscoveryStore()
        request = normalize_search_request(
            {
                "original_question": "A2Wについての関連資料や根拠を教えて",
                "literal_identifiers": ["A2W"],
                "facets": ["A2W", "A2Wの意味、役割、用途"],
                "inferred_concepts": ["air-to-water"],
            }
        )
        payload = {
            "status": "no_hit",
            "answerability": "none",
            "evidence": [],
            "warnings": [],
            "unmatched_identifiers": ["A2W"],
            "identifiers": {"anchors": ["A2W"]},
        }
        _add_discovery_lane(
            payload,
            store,
            request,
            source="any",
            use_dense=True,
        )
        paths = [item["path"] for item in payload["document_results"]]
        self.assertEqual("partial", payload["status"])
        self.assertEqual(8, len(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual([["A2Wについての関連資料や根拠を教えて", "A2Wの意味、役割、用途", "air-to-water"]], store.dense_batches)
        self.assertEqual(["A2W"], store.exact_queries)
        self.assertNotIn("air-to-water", store.exact_queries)

    def test_compact_output_keeps_document_cards_within_hard_limit(self) -> None:
        documents = [
            {
                "path": f"docs/{index}.pdf",
                "title": "関連資料" * 20,
                "section": "Section " + str(index),
                "preview": "日本語のプレビュー" * 100,
                "support_level": "weak",
                "authoritative": False,
                "matched_facets": ["facet"] * 4,
                "retrieval_signals": ["dense", "lexical"],
                "relationship": "Weak research lead. " * 20,
            }
            for index in range(10)
        ]
        compact = compact_search_contract(
            {
                "status": "partial",
                "answerability": "none",
                "evidence": [],
                "document_results": documents,
                "coverage": {"returned_distinct_documents": 10},
                "warnings": [],
            }
        )
        rendered = json.dumps(
            compact,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        self.assertLessEqual(len(rendered) + 1, 16_384)
        self.assertGreaterEqual(len(compact["document_results"]), 6)
        self.assertEqual(
            len(compact["document_results"]),
            compact["coverage"]["returned_distinct_documents"],
        )


if __name__ == "__main__":
    unittest.main()
