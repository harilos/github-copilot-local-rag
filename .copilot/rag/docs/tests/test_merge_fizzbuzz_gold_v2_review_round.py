from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("merge_fizzbuzz_gold_v2_review_round.py")
SPEC = importlib.util.spec_from_file_location("merge_fizzbuzz_gold_v2_review_round", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MergeReviewTests(unittest.TestCase):
    def test_addendum_replaces_every_item_for_revised_query(self) -> None:
        base = {
            "reviewer_id": "a",
            "rubric_version": "v1",
            "score_blind": True,
            "items": [
                {"query_id": "q1", "item_id": "A01"},
                {"query_id": "q1", "item_id": "A02"},
                {"query_id": "q2", "item_id": "A01"},
            ],
        }
        addendum = {
            "reviewer_id": "a",
            "rubric_version": "v1",
            "round": "redesign-1",
            "score_blind": True,
            "items": [{"query_id": "q1", "item_id": "A01"}],
        }
        result = MODULE.merged_review(base, addendum, {("q1", "A01"), ("q2", "A01")})
        self.assertEqual(2, len(result["items"]))
        self.assertEqual(["initial", "redesign-1"], result["review_rounds"])

    def test_incomplete_packet_is_rejected(self) -> None:
        base = {"reviewer_id": "a", "rubric_version": "v1", "score_blind": True, "items": []}
        addendum = {"reviewer_id": "a", "rubric_version": "v1", "score_blind": True, "items": []}
        with self.assertRaisesRegex(ValueError, "cover"):
            MODULE.merged_review(base, addendum, {("q", "A01")})

    def test_duplicate_addendum_item_is_rejected(self) -> None:
        item = {"query_id": "q", "item_id": "A01"}
        base = {"reviewer_id": "a", "rubric_version": "v1", "score_blind": True, "items": []}
        addendum = {"reviewer_id": "a", "rubric_version": "v1", "score_blind": True, "items": [item, dict(item)]}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            MODULE.merged_review(base, addendum, {("q", "A01")})

    def test_addendum_rubric_must_match_base(self) -> None:
        base = {"reviewer_id": "a", "rubric_version": "v1", "score_blind": True, "items": []}
        addendum = {"reviewer_id": "a", "rubric_version": "v2", "score_blind": True, "items": []}
        with self.assertRaisesRegex(ValueError, "rubric_version"):
            MODULE.merged_review(base, addendum, set())


if __name__ == "__main__":
    unittest.main()
