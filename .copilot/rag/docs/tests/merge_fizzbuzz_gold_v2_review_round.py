from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def expected_keys(packet: Path) -> set[tuple[str, str]]:
    keys = set()
    for line in packet.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        anchors = record.get("evidence_anchor", [])
        if anchors:
            keys.update((record["query_id"], anchor["anchor_id"]) for anchor in anchors)
        else:
            keys.add((record["query_id"], "ABSTAIN"))
    return keys


def merged_review(base: dict, addendum: dict, expected: set[tuple[str, str]]) -> dict:
    if base.get("reviewer_id") != addendum.get("reviewer_id"):
        raise ValueError("reviewer_id differs between review rounds")
    if base.get("score_blind") is not True or addendum.get("score_blind") is not True:
        raise ValueError("every review round must be score-blind")
    if not base.get("rubric_version") or base.get("rubric_version") != addendum.get("rubric_version"):
        raise ValueError("review rounds must use the same explicit rubric_version")
    for name, review in (("base", base), ("addendum", addendum)):
        keys = [(item.get("query_id"), item.get("item_id")) for item in review.get("items", [])]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{name} review contains duplicate items")
    revised_queries = {item["query_id"] for item in addendum.get("items", [])}
    items = {
        (item["query_id"], item["item_id"]): item
        for item in base.get("items", [])
        if item["query_id"] not in revised_queries
    }
    for item in addendum.get("items", []):
        items[(item["query_id"], item["item_id"])] = item
    if set(items) != expected:
        raise ValueError("merged review does not exactly cover current packet")
    return {
        "reviewer_id": base["reviewer_id"],
        "rubric_version": base["rubric_version"],
        "score_blind": True,
        "review_rounds": ["initial", addendum.get("round", "redesign")],
        "items": [items[key] for key in sorted(items)],
        "concerns": list(base.get("concerns", [])) + list(addendum.get("concerns", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--addendum", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = merged_review(load_json(args.base), load_json(args.addendum), expected_keys(args.packet))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"reviewer_id": result["reviewer_id"], "items": len(result["items"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
