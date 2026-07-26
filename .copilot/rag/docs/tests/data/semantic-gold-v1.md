# Semantic Gold v1

This is the frozen, span-based semantic retrieval pilot for the 1.0.1 release.

## Diagnostic status

`INVALID_FOR_RELEASE_GATE`

The Windows H/L/V run completed 90/90 requests with pure JSON and no timeout,
but an independent review found that this dataset cannot decide release quality.
Several questions admit valid evidence outside the single labeled document,
some labels require long paragraph containment rather than atomic claim support,
and the evaluated AC snapshot included six unlabelled ingestion-test documents.
The raw result remains a frozen diagnostic artifact and must not be re-labeled
or used as a failed retrieval baseline.

## Freeze identity

- Dataset: `semantic-gold-v1.jsonl`
- Dataset SHA-256: `9db5d9a37afc08bad3c2131d0af120c10b85705bfa6d6d150d9097053daacc70`
- Cases: 30 (10 per database)
- Required spans: 32
- Token budget for the release comparison: 1200

| Database | Snapshot SHA-256 |
|---|---|
| `ac-rag` | `f26efbb61f5052c5665e64cc186e1478d225a36cd7b5369456eb5101f5e3d61f` |
| `incident-rag` | `f825401fbe70dac6ae5d41850a7e59d8db9e399a1a25b9f66fb9016c4aaa5ba6` |
| `rfc-full-20k-rag` | `7c2a9d6f17f7e0397f61bf64c8897d760141de0e1c8b515e4ceb7adb13ba62ef` |

## Construction protocol

1. Source documents were selected directly from the catalog without running retrieval.
2. Exact source spans and claims were recorded before questions were written.
3. A separate high-capability agent received only the claims and wrote the Japanese questions.
4. The questions were inserted without inspecting retrieval output.
5. This file and the JSONL dataset were frozen before the first H/L/V evaluation.

`start_char` and `end_char` use the `clean_chunk` coordinate space identified by
`chunk_uid`; `span_text` is the authoritative relocation and matching value. Context
quotes are relocation aids only. Retrieval scoring must use final authoritative
`evidence`, never `related_context`.

Questions and gold spans must not be simplified, shortened, or replaced after observing
retrieval failures. A changed database snapshot requires a new dataset version or an
independent, documented relocation review.
