# FizzBuzz Gold v2 schema

FizzBuzz Gold v2 is a score-blind, span-grounded evaluation set for query-time
retrieval and reranking. It contains 30 newly authored questions: 18 public DEV
questions and 12 sealed holdout questions. The six strata are `exact`,
`paraphrase`, `procedure`, `multi-evidence`, `ambiguity`, and `unanswerable`.
Each stratum has three DEV and two holdout records.

The holdout JSONL is never committed. Only a manifest containing counts,
snapshot identity, and the sealed artifact hash is repository-safe.

## Record contract

Each JSONL record has these required fields:

- `schema_version`: integer `2`
- `query_id`: globally unique `FBG2-DEV-*` or `FBG2-HO-*` identifier
- `gold_version`: `fizzbuzz-gold-v2`
- `split`: `dev` or `holdout`
- `corpus_id` and `corpus_snapshot_sha256`: immutable source identity
- `query_text`, `language`, `stratum`, `intent`, and `difficulty`
- `answerability`: `answerable`, `ambiguous`, or `unanswerable`
- `fact_family`, `document_family`, and `template_family`: leakage controls
- `canonical_fact_units`, `acceptable_aliases`, `excluded_interpretations`
- `required_evidence_groups`: AND across groups, OR across alternatives
- `evidence_anchor`, `hard_negatives`, `unsafe_or_false_assertions`
- `annotation_provenance`, `adjudication`, `rubric_version`, `frozen_at`
- `artifact_sha256`: hash of the canonical record before this field is added

An evidence anchor identifies a frozen source span with `source_kind`,
`source_relpath`, `source_locator`, `document_sha256`, `document_hash_basis`,
`page_or_section`, `coordinate_space`, `chunk_index`,
`normalized_span_sha256`, `char_start`, `char_end`, `relevance_grade`,
`derived_label`, `derived_chunk_uid`, and `span_text`.

## Split and leakage rules

DEV and holdout must have disjoint `fact_family`, `document_family`, and
`template_family` values. Existing PoC questions are legacy material and may
not be reclassified as holdout. Holdout query text, labels, rationales, and
anchors remain outside the product repository.

`answerable` records require at least one evidence group and anchor.
`ambiguous` and `unanswerable` records require no positive evidence group;
their excluded interpretations and unsafe assertions define the abstention
boundary.

## Reproducibility

Validation receives explicit corpus and clean-record paths. It never resolves
or reads a user's real `%USERPROFILE%/.copilot` directory and never rebuilds a
database. Source bytes, provider content hashes, clean-chunk coordinates, span
hashes, and derived chunk UIDs are checked against the frozen corpus snapshot.
