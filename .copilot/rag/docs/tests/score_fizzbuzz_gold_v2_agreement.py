from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


GRADES = (0, 1, 2, 3)


def load_review(path: Path) -> dict:
    review = json.loads(path.read_text(encoding="utf-8"))
    if review.get("score_blind") is not True:
        raise ValueError(f"{path}: score_blind must be true")
    items = review.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path}: non-empty items list required")
    keys = [(item.get("query_id"), item.get("item_id")) for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path}: duplicate review item")
    for item in items:
        if item.get("grade") not in GRADES:
            raise ValueError(f"{path}: grade must be 0..3")
        if item.get("binary_relevant") is not (item["grade"] >= 2):
            raise ValueError(f"{path}: binary_relevant must equal grade >= 2")
    return review


def paired_grades(first: dict, second: dict) -> list[tuple[int, int]]:
    left = {(item["query_id"], item["item_id"]): item for item in first["items"]}
    right = {(item["query_id"], item["item_id"]): item for item in second["items"]}
    if set(left) != set(right):
        missing_left = sorted(set(right) - set(left))
        missing_right = sorted(set(left) - set(right))
        raise ValueError(
            f"review item sets differ; missing_first={missing_left}, missing_second={missing_right}"
        )
    return [(left[key]["grade"], right[key]["grade"]) for key in sorted(left)]


def quadratic_weighted_kappa(pairs: list[tuple[int, int]]) -> float:
    if not pairs:
        raise ValueError("at least one pair is required")
    count = len(pairs)
    left = Counter(a for a, _ in pairs)
    right = Counter(b for _, b in pairs)
    observed_disagreement = sum(((a - b) / 3) ** 2 for a, b in pairs) / count
    expected_disagreement = sum(
        ((a - b) / 3) ** 2 * left[a] * right[b]
        for a in GRADES
        for b in GRADES
    ) / (count * count)
    if expected_disagreement == 0:
        return 1.0 if observed_disagreement == 0 else 0.0
    return 1.0 - observed_disagreement / expected_disagreement


def binary_agreement(pairs: list[tuple[int, int]]) -> float:
    return sum((a >= 2) == (b >= 2) for a, b in pairs) / len(pairs)


def binary_kappa(pairs: list[tuple[int, int]]) -> float:
    binary = [(a >= 2, b >= 2) for a, b in pairs]
    observed = sum(a == b for a, b in binary) / len(binary)
    left_positive = sum(a for a, _ in binary) / len(binary)
    right_positive = sum(b for _, b in binary) / len(binary)
    expected = left_positive * right_positive + (1 - left_positive) * (1 - right_positive)
    return 1.0 if expected == 1 and observed == 1 else (observed - expected) / (1 - expected)


def agreement(first: dict, second: dict) -> dict:
    if not first.get("reviewer_id") or not second.get("reviewer_id"):
        raise ValueError("reviewer_id is required")
    if first["reviewer_id"] == second["reviewer_id"]:
        raise ValueError("reviewers must be independent identities")
    if not first.get("rubric_version") or first.get("rubric_version") != second.get("rubric_version"):
        raise ValueError("reviewers must use the same explicit rubric_version")
    pairs = paired_grades(first, second)
    exact = sum(a == b for a, b in pairs) / len(pairs)
    qwk = quadratic_weighted_kappa(pairs)
    binary = binary_agreement(pairs)
    return {
        "agreement_kind": "independent-agent agreement; not human IAA",
        "reviewer_1": first["reviewer_id"],
        "reviewer_2": second["reviewer_id"],
        "item_count": len(pairs),
        "exact_grade_agreement": exact,
        "quadratic_weighted_kappa": qwk,
        "binary_relevant_agreement": binary,
        "binary_relevant_kappa": binary_kappa(pairs),
        "thresholds": {
            "quadratic_weighted_kappa": 0.70,
            "binary_relevant_agreement": 0.75,
        },
        "gate_pass": qwk >= 0.70 and binary >= 0.75,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reviewer_1", type=Path)
    parser.add_argument("reviewer_2", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = agreement(load_review(args.reviewer_1), load_review(args.reviewer_2))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if result["gate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
