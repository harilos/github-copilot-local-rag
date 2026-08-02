from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Iterable


STRATA = {
    "exact",
    "paraphrase",
    "procedure",
    "multi-evidence",
    "ambiguity",
    "unanswerable",
}
CORPUS_COMMIT = "e936dae1fe79f5e8e885c768ffaf7b42dc6fb74b"
CORPUS_ID = f"harilos/fizzbuzz-planet-docs@{CORPUS_COMMIT}"
REQUIRED_FIELDS = {
    "schema_version",
    "query_id",
    "gold_version",
    "split",
    "corpus_id",
    "corpus_snapshot_sha256",
    "query_text",
    "language",
    "stratum",
    "intent",
    "difficulty",
    "answerability",
    "fact_family",
    "document_family",
    "template_family",
    "canonical_fact_units",
    "acceptable_aliases",
    "excluded_interpretations",
    "required_evidence_groups",
    "evidence_anchor",
    "hard_negatives",
    "unsafe_or_false_assertions",
    "annotation_provenance",
    "adjudication",
    "rubric_version",
    "frozen_at",
    "artifact_sha256",
}


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_record_hash(record: dict) -> str:
    payload = {key: value for key, value in record.items() if key != "artifact_sha256"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded)


def load_jsonl(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def clean_records(records_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(records_dir.rglob("*.jsonl")):
        rows.extend(load_jsonl(path))
    return rows


def corpus_snapshot(source_root: Path) -> str:
    digest = hashlib.sha256()
    excluded_roots = {".git", "_rag_eval", "_build_notes", "tools"}
    files = []
    for path in source_root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(source_root)
            if not relative.parts or relative.parts[0] not in excluded_roots:
                files.append((relative.as_posix(), path))
    for relative, path in sorted(files):
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_record(record: dict, expected_split: str) -> list[str]:
    errors = []
    query_id = record.get("query_id", "<missing>")
    missing = sorted(REQUIRED_FIELDS - set(record))
    if missing:
        errors.append(f"{query_id}: missing fields: {', '.join(missing)}")
    if record.get("schema_version") != 2:
        errors.append(f"{query_id}: schema_version must be 2")
    if record.get("gold_version") != "fizzbuzz-gold-v2":
        errors.append(f"{query_id}: wrong gold_version")
    if record.get("corpus_id") != CORPUS_ID:
        errors.append(f"{query_id}: wrong corpus_id")
    if record.get("rubric_version") != "fizzbuzz-gold-v2-rubric-v2":
        errors.append(f"{query_id}: wrong rubric_version")
    if record.get("split") != expected_split:
        errors.append(f"{query_id}: split must be {expected_split}")
    if record.get("stratum") not in STRATA:
        errors.append(f"{query_id}: unknown stratum")
    answerability = record.get("answerability")
    groups = record.get("required_evidence_groups", [])
    anchors = record.get("evidence_anchor", [])
    if answerability == "answerable" and (not groups or not anchors):
        errors.append(f"{query_id}: answerable record needs groups and anchors")
    if answerability in {"ambiguous", "unanswerable"} and (groups or anchors):
        errors.append(f"{query_id}: abstention record must not contain positive evidence")
    anchor_ids = {anchor.get("anchor_id") for anchor in anchors}
    referenced = {
        item.get("anchor_id")
        for group in groups
        for item in group.get("alternatives", [])
    }
    if referenced != anchor_ids:
        errors.append(f"{query_id}: evidence groups must reference every and only anchor")
    for anchor in anchors:
        if anchor.get("coordinate_space") != "clean_chunk":
            errors.append(f"{query_id}/{anchor.get('anchor_id')}: coordinate_space must be clean_chunk")
        grade = anchor.get("relevance_grade", "")
        if grade not in {"2-direct", "3-essential"}:
            errors.append(
                f"{query_id}/{anchor.get('anchor_id')}: required anchor must be direct or essential"
            )
        if anchor.get("derived_label") != "Evidence":
            errors.append(f"{query_id}/{anchor.get('anchor_id')}: required anchor must derive Evidence")
    if record.get("artifact_sha256") != canonical_record_hash(record):
        errors.append(f"{query_id}: artifact_sha256 mismatch")
    return errors


def validate_anchors(
    records: Iterable[dict], rows: list[dict], source_root: Path
) -> list[str]:
    errors = []
    by_uid = {row.get("id"): row for row in rows}
    if len(by_uid) != len(rows):
        errors.append("clean records contain duplicate chunk UIDs")
    for record in records:
        for anchor in record.get("evidence_anchor", []):
            query_id = record["query_id"]
            item = f"{query_id}/{anchor['anchor_id']}"
            span = anchor["span_text"]
            claimed_uids = anchor.get("derived_chunk_uid", [])
            if not claimed_uids or len(claimed_uids) != len(set(claimed_uids)):
                errors.append(f"{item}: derived_chunk_uid must be non-empty and unique")
                continue
            missing_uids = sorted(set(claimed_uids) - set(by_uid))
            if missing_uids:
                errors.append(f"{item}: unknown chunk UIDs: {missing_uids}")
                continue
            source_relpath = anchor["source_relpath"].replace("\\", "/")
            expected_prefix = "src_issues-" if anchor["source_kind"] == "github_issue" else "src_source-"
            source_rows = [
                row
                for row in rows
                if row.get("metadata", {}).get("path", "").startswith(expected_prefix)
                and row.get("metadata", {}).get("path", "").endswith("/" + source_relpath)
                and span in normalized(row.get("text", ""))
            ]
            expected_uids = {row["id"] for row in source_rows}
            if set(claimed_uids) != expected_uids:
                errors.append(f"{item}: chunk UIDs do not exactly match source path and span")
                continue
            matches = [by_uid[uid] for uid in claimed_uids]
            start, end = anchor["char_start"], anchor["char_end"]
            if any(normalized(row["text"])[start:end] != span for row in matches):
                errors.append(f"{item}: coordinate mismatch")
            if any(row.get("metadata", {}).get("chunk_index") != anchor["chunk_index"] for row in matches):
                errors.append(f"{item}: chunk_index mismatch")
            if any(
                (row.get("metadata", {}).get("section_path") or row.get("metadata", {}).get("chunk_title"))
                != anchor["page_or_section"]
                for row in matches
            ):
                errors.append(f"{item}: section mismatch")
            if anchor["normalized_span_sha256"] != sha256(span.encode("utf-8")):
                errors.append(f"{item}: span hash mismatch")
            if anchor["document_hash_basis"] == "raw_file_bytes":
                if anchor["source_kind"] != "repository_file" or anchor["source_locator"] != source_relpath:
                    errors.append(f"{item}: repository source locator mismatch")
                source = source_root / Path(anchor["source_relpath"])
                if not source.is_file() or sha256(source.read_bytes()) != anchor["document_sha256"]:
                    errors.append(f"{item}: source file hash mismatch")
            elif anchor["document_hash_basis"] == "provider_content_hash":
                expected_locator = "https://github.com/harilos/fizzbuzz-planet-docs/" + source_relpath.removesuffix(".md")
                if anchor["source_kind"] != "github_issue" or anchor["source_locator"] != expected_locator:
                    errors.append(f"{item}: provider source locator mismatch")
                content_hashes = {
                    "sha256:" + row.get("metadata", {}).get("content_hash", "")
                    for row in matches
                }
                if content_hashes != {anchor["document_sha256"]}:
                    errors.append(f"{item}: provider hash mismatch")
            else:
                errors.append(f"{item}: unknown document hash basis")
    return errors


def validate_dataset(dev: list[dict], holdout: list[dict] | None = None) -> list[str]:
    errors = []
    for record in dev:
        errors.extend(validate_record(record, "dev"))
    if len(dev) != 18:
        errors.append(f"DEV count must be 18, got {len(dev)}")
    if Counter(row.get("stratum") for row in dev) != Counter({name: 3 for name in STRATA}):
        errors.append("DEV must contain exactly three records per stratum")
    all_records = list(dev)
    if holdout is not None:
        for record in holdout:
            errors.extend(validate_record(record, "holdout"))
        if len(holdout) != 12:
            errors.append(f"holdout count must be 12, got {len(holdout)}")
        if Counter(row.get("stratum") for row in holdout) != Counter({name: 2 for name in STRATA}):
            errors.append("holdout must contain exactly two records per stratum")
        for field in ("fact_family", "document_family"):
            dev_values = {value for row in dev for value in row.get(field, [])}
            holdout_values = {value for row in holdout for value in row.get(field, [])}
            overlap = sorted(dev_values & holdout_values)
            if overlap:
                errors.append(f"{field} leaks across splits: {overlap}")
        dev_templates = {row.get("template_family") for row in dev}
        holdout_templates = {row.get("template_family") for row in holdout}
        overlap = sorted(dev_templates & holdout_templates)
        if overlap:
            errors.append(f"template_family leaks across splits: {overlap}")
        all_records.extend(holdout)
    identifiers = [row.get("query_id") for row in all_records]
    if len(identifiers) != len(set(identifiers)):
        errors.append("query_id values must be unique")
    return errors


def validate_manifest(
    manifest: dict,
    holdout_path: Path | None,
    holdout_records: list[dict] | None = None,
    holdout_bytes: bytes | None = None,
) -> list[str]:
    errors = []
    allowed_top_level = {
        "schema_version", "gold_version", "rubric_version", "split", "sealed",
        "query_count", "strata", "corpus_commit", "corpus_snapshot_sha256",
        "holdout_artifact_sha256", "disclosure", "frozen_at", "annotation_gate",
    }
    if set(manifest) != allowed_top_level:
        errors.append("holdout manifest has missing or unapproved top-level fields")
    allowed_annotation = {
        "agreement_kind", "reviewed_item_count", "frozen_item_count",
        "adjudicated_removal_count", "quadratic_weighted_kappa",
        "binary_relevant_agreement", "passed", "unresolved_disagreements",
    }
    if set(manifest.get("annotation_gate", {})) != allowed_annotation:
        errors.append("holdout manifest annotation_gate has unapproved fields")
    if manifest.get("sealed") is not True or manifest.get("query_count") != 12:
        errors.append("holdout manifest must describe 12 sealed questions")
    expected_identity = {
        "schema_version": 1,
        "gold_version": "fizzbuzz-gold-v2",
        "rubric_version": "fizzbuzz-gold-v2-rubric-v2",
        "split": "holdout",
        "corpus_commit": CORPUS_COMMIT,
    }
    for field, expected in expected_identity.items():
        if manifest.get(field) != expected:
            errors.append(f"holdout manifest has wrong {field}")
    if manifest.get("strata") != {name: 2 for name in STRATA}:
        errors.append("holdout manifest strata must be exactly two per stratum")
    if manifest.get("disclosure") != (
        "Query text, labels, rationales, and anchors are sealed outside the product repository. "
        "Only this manifest is shared before final acceptance."
    ):
        errors.append("holdout manifest disclosure text is not canonical")
    annotation_gate = manifest.get("annotation_gate", {})
    if annotation_gate.get("agreement_kind") != "independent-agent agreement; not human IAA":
        errors.append("holdout manifest has wrong agreement kind")
    if annotation_gate.get("passed") is not True or annotation_gate.get("unresolved_disagreements") != 0:
        errors.append("holdout manifest annotation gate is not resolved PASS")
    qwk = annotation_gate.get("quadratic_weighted_kappa")
    binary_agreement = annotation_gate.get("binary_relevant_agreement")
    if not isinstance(qwk, (int, float)) or qwk < 0.70:
        errors.append("holdout manifest weighted kappa is below gate")
    if not isinstance(binary_agreement, (int, float)) or binary_agreement < 0.75:
        errors.append("holdout manifest binary agreement is below gate")
    reviewed_count = annotation_gate.get("reviewed_item_count")
    frozen_count = annotation_gate.get("frozen_item_count")
    removal_count = annotation_gate.get("adjudicated_removal_count")
    if not all(isinstance(value, int) for value in (reviewed_count, frozen_count, removal_count)) or reviewed_count != frozen_count + removal_count:
        errors.append("holdout manifest annotation item counts are inconsistent")
    artifact_bytes = holdout_bytes
    if artifact_bytes is None and holdout_path is not None:
        artifact_bytes = holdout_path.read_bytes()
    if artifact_bytes is not None and sha256(artifact_bytes) != manifest.get("holdout_artifact_sha256"):
        errors.append("holdout artifact hash does not match manifest")
    if holdout_records is not None:
        if len(holdout_records) != manifest.get("query_count"):
            errors.append("holdout record count does not match manifest")
        if Counter(row.get("stratum") for row in holdout_records) != Counter(manifest.get("strata", {})):
            errors.append("holdout record strata do not match manifest")
        snapshots = {row.get("corpus_snapshot_sha256") for row in holdout_records}
        if snapshots != {manifest.get("corpus_snapshot_sha256")}:
            errors.append("holdout corpus snapshot does not match manifest")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--holdout-manifest", type=Path, required=True)
    parser.add_argument("--holdout-private", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--records-dir", type=Path)
    parser.add_argument("--require-private-holdout", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dev = load_jsonl(args.dev)
    holdout = load_jsonl(args.holdout_private) if args.holdout_private else None
    manifest = json.loads(args.holdout_manifest.read_text(encoding="utf-8"))
    errors = validate_dataset(dev, holdout)
    errors.extend(validate_manifest(manifest, args.holdout_private, holdout))
    records = dev + (holdout or [])
    if args.require_private_holdout and not args.holdout_private:
        errors.append("freeze validation requires --holdout-private")
    if args.require_private_holdout and (not args.source_root or not args.records_dir):
        errors.append("freeze validation requires --source-root and --records-dir")
    if args.source_root or args.records_dir:
        if not args.source_root or not args.records_dir:
            errors.append("--source-root and --records-dir must be supplied together")
        else:
            snapshot = corpus_snapshot(args.source_root)
            expected = {record.get("corpus_snapshot_sha256") for record in records}
            if expected != {snapshot}:
                errors.append(f"corpus snapshot mismatch: computed {snapshot}, records {sorted(expected)}")
            errors.extend(validate_anchors(records, clean_records(args.records_dir), args.source_root))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    state = "PASS_FREEZE" if holdout is not None and args.source_root and args.records_dir else "PASS_MANIFEST_ONLY"
    print(f"{state}: dev={len(dev)} holdout={len(holdout or [])} sealed={manifest['sealed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
