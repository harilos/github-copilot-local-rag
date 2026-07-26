# Semantic Gold v2

This is the frozen release-gate semantic retrieval dataset for version 1.0.1.

## Freeze identity

- Dataset: `semantic-gold-v2.jsonl`
- Dataset SHA-256: `fdcf70a137d091d6b453495d7ce1d20b44d28f691177d3b8aaf9a9e27c56eafd`
- Cases: 30 (10 per database)
- Languages: 24 Japanese, 6 English
- Required claim groups: 48
- Token budget: 1200
- Maximum characters per evidence item: 6000 (prevents a second, unintended
  1200-character cap from clipping a 1200-token evaluation)

| Database | Snapshot SHA-256 |
|---|---|
| `ac-rag` | `811c6d556abc020423b7e45c605637ea46690b02552869dc0327e1e1ac9acb56` |
| `incident-rag` | `f825401fbe70dac6ae5d41850a7e59d8db9e399a1a25b9f66fb9016c4aaa5ba6` |
| `rfc-full-20k-rag` | `7c2a9d6f17f7e0397f61bf64c8897d760141de0e1c8b515e4ceb7adb13ba62ef` |

## Construction protocol

1. The invalid v1 pilot was independently reviewed before this dataset was built.
2. Six AC ingestion-test documents without fixture metadata were removed from the
   production corpus and `ac-rag` was rebuilt before its snapshot was frozen.
3. Source documents and unused source sections were selected directly from the
   catalog without running retrieval.
4. Atomic source spans and claim groups were fixed before questions were written.
5. A separate high-capability agent received only the claim packet and wrote the
   questions without access to retrieval results.
6. Questions, spans, database snapshots, and thresholds were frozen before the
   first H/L/V retrieval run.

The set contains 6 uniquely scoped cases, 2 multi-evidence cases, and 2 broad or
rare-term cases per database. Alternatives inside a claim group are OR matches;
all required groups for a question are AND requirements. No passage returned by
the evaluation may be added as a post-hoc alternative.

## Release gates

- Dataset validation: 30/30
- Request success and pure JSON: 90/90
- Timeout: 0
- Hybrid H Hit@5 and Context Recall@1200: at least 80% overall
- Hybrid H Hit@5 and Context Recall@1200: at least 70% for each database
- Hybrid H recall must not be lower than lexical L recall
- Vector Harm Rate: at most 5%

Dense policy is decided once:

- H minus L recall at least 10 percentage points: keep Dense in the default route.
- Improvement from 3 to under 10 points: use L first and Dense only on failure.
- Improvement under 3 points: remove Dense from the default route.
- Harm over 10%: permit one fusion adjustment, then rerun the frozen set once.

`span_text` is the authoritative matching value. Evaluation uses only final
authoritative evidence, never `related_context`. A database snapshot change
invalidates the run until the dataset is independently relocated or versioned.
