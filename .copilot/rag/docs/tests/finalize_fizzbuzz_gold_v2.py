from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from score_fizzbuzz_gold_v2_agreement import (
    agreement as calculate_agreement,
    load_review,
)
from validate_fizzbuzz_gold_v2 import (
    clean_records,
    corpus_snapshot,
    validate_anchors,
    validate_dataset,
    validate_manifest,
)


GRADE_NAMES = {
    0: "0-irrelevant",
    1: "1-contextual",
    2: "2-direct",
    3: "3-essential",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def jsonl_payload(records: list[dict]) -> bytes:
    text = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    return text.encode("utf-8")


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_bytes(jsonl_payload(records))


def canonical_hash(record: dict) -> str:
    payload = {key: value for key, value in record.items() if key != "artifact_sha256"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def expected_items(records: list[dict]) -> set[tuple[str, str]]:
    result = set()
    for record in records:
        anchors = record.get("evidence_anchor", [])
        if anchors:
            result.update((record["query_id"], anchor["anchor_id"]) for anchor in anchors)
        else:
            result.add((record["query_id"], "ABSTAIN"))
    return result


def finalized_records(
    records: list[dict], reviewer_1: dict, reviewer_2: dict, adjudication: dict, frozen_at: str
) -> list[dict]:
    if adjudication.get("unresolved_disagreements"):
        raise ValueError("unresolved disagreements remain")
    adjudication_items = adjudication.get("items", [])
    decision_keys = [(item["query_id"], item["item_id"]) for item in adjudication_items]
    if len(decision_keys) != len(set(decision_keys)):
        raise ValueError("adjudication contains duplicate items")
    decisions = {
        (item["query_id"], item["item_id"]): item
        for item in adjudication_items
    }
    expected = expected_items(records)
    if not expected.issubset(decisions):
        raise ValueError("adjudication does not cover Gold records")
    for key in expected:
        item = decisions[key]
        grade = item.get("final_grade")
        if grade not in GRADE_NAMES:
            raise ValueError(f"{key}: final_grade must be 0..3")
        if grade < 2:
            raise ValueError(f"{key}: final grade below relevance gate; redesign Gold before freeze")
    output = []
    for source in records:
        record = json.loads(json.dumps(source, ensure_ascii=False))
        record["annotation_provenance"] = {
            "query_author": source["annotation_provenance"]["query_author"],
            "grader_1": reviewer_1["reviewer_id"],
            "grader_2": reviewer_2["reviewer_id"],
            "adjudicator": adjudication["adjudicator_id"],
            "agreement_kind": "independent-agent agreement; not human IAA",
        }
        record["adjudication"] = {
            "status": "resolved",
            "unresolved_disagreements": 0,
        }
        for anchor in record.get("evidence_anchor", []):
            decision = decisions[(record["query_id"], anchor["anchor_id"])]
            anchor["relevance_grade"] = GRADE_NAMES[decision["final_grade"]]
            anchor["derived_label"] = "Evidence" if decision["final_grade"] >= 2 else "Related"
        record["frozen_at"] = frozen_at
        record["artifact_sha256"] = canonical_hash(record)
        output.append(record)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-provisional", type=Path, required=True)
    parser.add_argument("--holdout-provisional", type=Path, required=True)
    parser.add_argument("--reviewer-1", type=Path, required=True)
    parser.add_argument("--reviewer-2", type=Path, required=True)
    parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--agreement", type=Path, required=True)
    parser.add_argument("--dev-output", type=Path, required=True)
    parser.add_argument("--holdout-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--frozen-at")
    args = parser.parse_args()

    reviewer_1 = load_review(args.reviewer_1)
    reviewer_2 = load_review(args.reviewer_2)
    adjudication = load_json(args.adjudication)
    agreement_artifact = load_json(args.agreement)
    recomputed_agreement = calculate_agreement(reviewer_1, reviewer_2)
    if agreement_artifact != recomputed_agreement:
        raise ValueError("agreement artifact does not match the supplied reviewer files")
    if recomputed_agreement.get("gate_pass") is not True:
        raise ValueError("independent-review agreement gate did not pass")
    frozen_at = args.frozen_at or datetime.now(timezone.utc).isoformat()
    dev_source = load_jsonl(args.dev_provisional)
    holdout_source = load_jsonl(args.holdout_provisional)
    rows = clean_records(args.records_dir)
    preflight_errors = validate_dataset(dev_source, holdout_source)
    snapshot = corpus_snapshot(args.source_root)
    expected_snapshots = {
        record.get("corpus_snapshot_sha256")
        for record in dev_source + holdout_source
    }
    if expected_snapshots != {snapshot}:
        preflight_errors.append(
            f"corpus snapshot mismatch: computed {snapshot}, records {sorted(expected_snapshots)}"
        )
    preflight_errors.extend(
        validate_anchors(dev_source + holdout_source, rows, args.source_root)
    )
    if preflight_errors:
        raise ValueError("Gold preflight failed: " + "; ".join(preflight_errors))
    raw_adjudication_keys = [
        (item["query_id"], item["item_id"])
        for item in adjudication.get("items", [])
    ]
    if len(raw_adjudication_keys) != len(set(raw_adjudication_keys)):
        raise ValueError("adjudication contains duplicate items")
    adjudication_keys = {
        (item["query_id"], item["item_id"])
        for item in adjudication.get("items", [])
    }
    reviewer_1_items = {
        (item["query_id"], item["item_id"]): item
        for item in reviewer_1["items"]
    }
    reviewer_2_items = {
        (item["query_id"], item["item_id"]): item
        for item in reviewer_2["items"]
    }
    if set(reviewer_1_items) != adjudication_keys or set(reviewer_2_items) != adjudication_keys:
        raise ValueError("reviewer and adjudication item sets differ")
    for item in adjudication["items"]:
        key = (item["query_id"], item["item_id"])
        if item.get("reviewer_1_grade") != reviewer_1_items[key]["grade"]:
            raise ValueError(f"{key}: adjudication reviewer_1_grade mismatch")
        if item.get("reviewer_2_grade") != reviewer_2_items[key]["grade"]:
            raise ValueError(f"{key}: adjudication reviewer_2_grade mismatch")
    current_keys = expected_items(dev_source + holdout_source)
    removed_keys = {
        (action["query_id"], action["item_id"])
        for action in adjudication.get("redesign_actions", [])
        if action.get("action") == "remove_anchor_and_group"
    }
    if not current_keys.issubset(adjudication_keys):
        raise ValueError("adjudication does not cover the combined Gold dataset")
    if adjudication_keys - current_keys != removed_keys:
        raise ValueError("extra adjudication items must exactly match approved redesign removals")
    dev = finalized_records(dev_source, reviewer_1, reviewer_2, adjudication, frozen_at)
    holdout = finalized_records(holdout_source, reviewer_1, reviewer_2, adjudication, frozen_at)
    dev_bytes = jsonl_payload(dev)
    holdout_bytes = jsonl_payload(holdout)
    prior_manifest = load_json(args.manifest_output)
    prior_manifest.update(
        {
            "rubric_version": "fizzbuzz-gold-v2-rubric-v2",
            "frozen_at": frozen_at,
            "holdout_artifact_sha256": "sha256:" + hashlib.sha256(holdout_bytes).hexdigest(),
            "annotation_gate": {
                "agreement_kind": recomputed_agreement["agreement_kind"],
                "reviewed_item_count": recomputed_agreement["item_count"],
                "frozen_item_count": len(current_keys),
                "adjudicated_removal_count": len(removed_keys),
                "quadratic_weighted_kappa": recomputed_agreement["quadratic_weighted_kappa"],
                "binary_relevant_agreement": recomputed_agreement["binary_relevant_agreement"],
                "passed": True,
                "unresolved_disagreements": 0,
            },
        }
    )
    postflight_errors = validate_dataset(dev, holdout)
    postflight_errors.extend(validate_anchors(dev + holdout, rows, args.source_root))
    postflight_errors.extend(validate_manifest(prior_manifest, None, holdout, holdout_bytes))
    if postflight_errors:
        raise ValueError("Gold postflight failed: " + "; ".join(postflight_errors))
    args.dev_output.write_bytes(dev_bytes)
    args.holdout_output.write_bytes(holdout_bytes)
    args.manifest_output.write_text(
        json.dumps(prior_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"dev": len(dev), "holdout": len(holdout), "frozen_at": frozen_at}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
