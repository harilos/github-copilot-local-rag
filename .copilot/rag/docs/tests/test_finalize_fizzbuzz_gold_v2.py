from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("finalize_fizzbuzz_gold_v2.py")
SPEC = importlib.util.spec_from_file_location("finalize_fizzbuzz_gold_v2", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def base_record() -> dict:
    return {
        "query_id": "q1",
        "annotation_provenance": {"query_author": "author"},
        "adjudication": {},
        "evidence_anchor": [{"anchor_id": "A01", "relevance_grade": "3-essential", "derived_label": "Evidence"}],
        "frozen_at": None,
        "artifact_sha256": "old",
    }


class FinalizerTests(unittest.TestCase):
    def test_resolved_grade_freezes_record(self) -> None:
        adjudication = {
            "adjudicator_id": "c",
            "unresolved_disagreements": [],
            "items": [{"query_id": "q1", "item_id": "A01", "final_grade": 2}],
        }
        records = MODULE.finalized_records(
            [base_record()], {"reviewer_id": "a"}, {"reviewer_id": "b"}, adjudication, "time"
        )
        self.assertEqual("2-direct", records[0]["evidence_anchor"][0]["relevance_grade"])
        self.assertEqual("resolved", records[0]["adjudication"]["status"])
        self.assertNotEqual("old", records[0]["artifact_sha256"])

    def test_low_relevance_requires_redesign(self) -> None:
        adjudication = {
            "adjudicator_id": "c",
            "unresolved_disagreements": [],
            "items": [{"query_id": "q1", "item_id": "A01", "final_grade": 1}],
        }
        with self.assertRaisesRegex(ValueError, "redesign"):
            MODULE.finalized_records(
                [base_record()], {"reviewer_id": "a"}, {"reviewer_id": "b"}, adjudication, "time"
            )

    def test_unresolved_disagreement_blocks_freeze(self) -> None:
        with self.assertRaisesRegex(ValueError, "unresolved"):
            MODULE.finalized_records(
                [base_record()],
                {"reviewer_id": "a"},
                {"reviewer_id": "b"},
                {"unresolved_disagreements": ["q1"]},
                "time",
            )

    def test_adjudication_may_include_other_split_items(self) -> None:
        adjudication = {
            "adjudicator_id": "c",
            "unresolved_disagreements": [],
            "items": [
                {"query_id": "q1", "item_id": "A01", "final_grade": 3},
                {"query_id": "holdout", "item_id": "ABSTAIN", "final_grade": 3},
            ],
        }
        records = MODULE.finalized_records(
            [base_record()], {"reviewer_id": "a"}, {"reviewer_id": "b"}, adjudication, "time"
        )
        self.assertEqual(1, len(records))

    def test_duplicate_adjudication_item_is_rejected(self) -> None:
        item = {"query_id": "q1", "item_id": "A01", "final_grade": 3}
        adjudication = {
            "adjudicator_id": "c",
            "unresolved_disagreements": [],
            "items": [item, dict(item)],
        }
        with self.assertRaisesRegex(ValueError, "duplicate"):
            MODULE.finalized_records(
                [base_record()], {"reviewer_id": "a"}, {"reviewer_id": "b"}, adjudication, "time"
            )


if __name__ == "__main__":
    unittest.main()
