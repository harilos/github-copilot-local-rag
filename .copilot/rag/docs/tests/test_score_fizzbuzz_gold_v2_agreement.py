from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("score_fizzbuzz_gold_v2_agreement.py")
SPEC = importlib.util.spec_from_file_location("score_fizzbuzz_gold_v2_agreement", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def review(reviewer_id: str, grades: list[int]) -> dict:
    return {
        "reviewer_id": reviewer_id,
        "rubric_version": "v2",
        "score_blind": True,
        "items": [
            {
                "query_id": f"q-{index}",
                "item_id": "A01",
                "grade": grade,
                "binary_relevant": grade >= 2,
            }
            for index, grade in enumerate(grades)
        ],
    }


class AgreementTests(unittest.TestCase):
    def test_identical_reviews_pass(self) -> None:
        result = MODULE.agreement(review("a", [0, 1, 2, 3]), review("b", [0, 1, 2, 3]))
        self.assertEqual(1.0, result["quadratic_weighted_kappa"])
        self.assertEqual(1.0, result["binary_relevant_agreement"])
        self.assertTrue(result["gate_pass"])

    def test_opposite_reviews_fail(self) -> None:
        result = MODULE.agreement(review("a", [0, 0, 3, 3]), review("b", [3, 3, 0, 0]))
        self.assertLess(result["quadratic_weighted_kappa"], 0.70)
        self.assertEqual(0.0, result["binary_relevant_agreement"])
        self.assertFalse(result["gate_pass"])

    def test_item_set_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "item sets differ"):
            MODULE.agreement(review("a", [1, 2]), review("b", [1]))

    def test_same_reviewer_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "independent"):
            MODULE.agreement(review("same", [3]), review("same", [3]))

    def test_rubric_mismatch_is_rejected(self) -> None:
        second = review("b", [3])
        second["rubric_version"] = "other"
        with self.assertRaisesRegex(ValueError, "rubric_version"):
            MODULE.agreement(review("a", [3]), second)


if __name__ == "__main__":
    unittest.main()
