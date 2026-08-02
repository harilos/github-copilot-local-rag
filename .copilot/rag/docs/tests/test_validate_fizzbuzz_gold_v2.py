from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("validate_fizzbuzz_gold_v2.py")
SPEC = importlib.util.spec_from_file_location("validate_fizzbuzz_gold_v2", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def record(query_id: str, split: str, stratum: str) -> dict:
    value = {
        "schema_version": 2,
        "query_id": query_id,
        "gold_version": "fizzbuzz-gold-v2",
        "split": split,
        "corpus_id": MODULE.CORPUS_ID,
        "corpus_snapshot_sha256": "sha256:snapshot",
        "query_text": "question",
        "language": "ja",
        "stratum": stratum,
        "intent": "intent",
        "difficulty": 1,
        "answerability": "unanswerable",
        "fact_family": [f"{split}-{query_id}-fact"],
        "document_family": [f"{split}-{query_id}-doc"],
        "template_family": f"{split}-{query_id}-template",
        "canonical_fact_units": [],
        "acceptable_aliases": [],
        "excluded_interpretations": [],
        "required_evidence_groups": [],
        "evidence_anchor": [],
        "hard_negatives": [],
        "unsafe_or_false_assertions": ["unsupported"],
        "annotation_provenance": {},
        "adjudication": {},
        "rubric_version": "fizzbuzz-gold-v2-rubric-v2",
        "frozen_at": None,
    }
    value["artifact_sha256"] = MODULE.canonical_record_hash(value)
    return value


class GoldV2ValidatorTests(unittest.TestCase):
    def complete(self, split: str, per_stratum: int) -> list[dict]:
        return [
            record(f"{split}-{stratum}-{index}", split, stratum)
            for stratum in sorted(MODULE.STRATA)
            for index in range(per_stratum)
        ]

    def test_complete_disjoint_splits_pass(self) -> None:
        self.assertEqual([], MODULE.validate_dataset(self.complete("dev", 3), self.complete("holdout", 2)))

    def test_family_leakage_fails(self) -> None:
        dev = self.complete("dev", 3)
        holdout = self.complete("holdout", 2)
        holdout[0]["fact_family"] = list(dev[0]["fact_family"])
        holdout[0]["artifact_sha256"] = MODULE.canonical_record_hash(holdout[0])
        self.assertTrue(any("fact_family leaks" in error for error in MODULE.validate_dataset(dev, holdout)))

    def test_answerable_requires_anchor(self) -> None:
        value = record("id", "dev", "exact")
        value["answerability"] = "answerable"
        value["artifact_sha256"] = MODULE.canonical_record_hash(value)
        self.assertTrue(any("needs groups and anchors" in error for error in MODULE.validate_record(value, "dev")))

    def test_required_anchor_cannot_be_context_only(self) -> None:
        value = record("id", "dev", "exact")
        value["answerability"] = "answerable"
        value["required_evidence_groups"] = [
            {"group_id": "fact", "and_or": "OR", "alternatives": [{"anchor_id": "A01"}]}
        ]
        value["evidence_anchor"] = [
            {"anchor_id": "A01", "relevance_grade": "1-contextual", "derived_label": "Related"}
        ]
        value["artifact_sha256"] = MODULE.canonical_record_hash(value)
        errors = MODULE.validate_record(value, "dev")
        self.assertTrue(any("direct or essential" in error for error in errors))

    def test_manifest_rejects_sealed_content(self) -> None:
        errors = MODULE.validate_manifest({"sealed": True, "query_count": 12, "query_text": []}, None)
        self.assertTrue(any("unapproved" in error for error in errors))

    def test_manifest_rejects_nested_sealed_content(self) -> None:
        manifest = {
            "schema_version": 1,
            "gold_version": "fizzbuzz-gold-v2",
            "rubric_version": "fizzbuzz-gold-v2-rubric-v2",
            "split": "holdout",
            "sealed": True,
            "query_count": 12,
            "strata": {name: 2 for name in MODULE.STRATA},
            "corpus_commit": "commit",
            "corpus_snapshot_sha256": "sha256:snapshot",
            "holdout_artifact_sha256": "sha256:holdout",
            "disclosure": "Query text, labels, rationales, and anchors are sealed outside the product repository. Only this manifest is shared before final acceptance.",
            "frozen_at": "time",
            "annotation_gate": {"query_text": "secret"},
        }
        errors = MODULE.validate_manifest(manifest, None)
        self.assertTrue(any("annotation_gate has unapproved" in error for error in errors))

    def test_jsonl_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text("{bad}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                MODULE.load_jsonl(path)

    def test_anchor_uid_cannot_be_mixed_with_another_source_path(self) -> None:
        row = {
            "id": "uid-1",
            "text": "supporting span",
            "metadata": {
                "path": "src_issues-fixture/issues/1.md",
                "chunk_index": 0,
                "section_path": "1.md",
                "content_hash": "content",
            },
        }
        anchor = {
            "anchor_id": "A01",
            "source_kind": "github_issue",
            "source_relpath": "issues/2.md",
            "source_locator": "https://github.com/harilos/fizzbuzz-planet-docs/issues/2",
            "document_sha256": "sha256:content",
            "document_hash_basis": "provider_content_hash",
            "page_or_section": "1.md",
            "chunk_index": 0,
            "normalized_span_sha256": MODULE.sha256(b"supporting span"),
            "char_start": 0,
            "char_end": len("supporting span"),
            "derived_chunk_uid": ["uid-1"],
            "span_text": "supporting span",
        }
        with tempfile.TemporaryDirectory() as directory:
            errors = MODULE.validate_anchors(
                [{"query_id": "q", "evidence_anchor": [anchor]}],
                [row],
                Path(directory),
            )
        self.assertTrue(any("source path and span" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
