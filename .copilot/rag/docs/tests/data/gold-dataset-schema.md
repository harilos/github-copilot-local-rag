# Gold Dataset Schema

Gold data is span-based so that chunk length can vary without rewriting labels.

## JSONL Record

```json
{
  "schema_version": 1,
  "id": "AC_SEM_001",
  "db": "ac-rag",
  "db_family": "ac",
  "db_snapshot_hash": "sha256:...",
  "query": "冷房需要が増える背景を資料から説明して",
  "query_type": "semantic",
  "answerable": true,
  "gold_spans": [
    {
      "source_id": "ac-corpus",
      "document_id": "iea_2018_the_future_of_cooling.pdf",
      "path": "iea_2018_the_future_of_cooling.pdf",
      "start_char": 18240,
      "end_char": 18810,
      "required": true,
      "span_text": "the exact short gold span contained by a current chunk",
      "span_text_sha256": "sha256:...",
      "source_text_hash": "sha256:...",
      "context_before": "short quote before span",
      "context_after": "short quote after span"
    }
  ],
  "gold_claims": [
    "income growth",
    "urbanization",
    "temperature rise"
  ],
  "negative_expectation": ""
}
```

## Matching Rules

| Label | Meaning |
|---:|---|
| 2 | retrieved chunk fully contains the gold span |
| 1 | retrieved chunk overlaps the gold span |
| 0 | no span overlap |

For negative questions, success means no unsupported answer is produced. Retrieval may still return related context, but answer evaluation must require abstention.

## Required Fields

- `id`
- `query`
- `query_type`
- `answerable`
- `gold_spans`
- `gold_claims`
- `db`
- `db_snapshot_hash`

For each span:

- `source_id`
- `document_id` or `path`
- `start_char`
- `end_char`
- `required`
- `span_text`
- `span_text_sha256`
- `source_text_hash` or stable context quote

`context_before` and `context_after` are relocation aids only. They must not
independently count as a retrieval hit. Evaluation matches `span_text` against
the final packed context and rejects a dataset whose DB snapshot differs from
`db_snapshot_hash`.

## Retrieval Trace

Semantic H/L/V comparison should persist the candidate lifecycle when the
search response exposes it:

```json
{
  "retrieval_trace": {
    "dense": [{"chunk_uid": "...", "rank": 1}],
    "lexical": [],
    "exact": [],
    "metadata": [],
    "rrf": [],
    "final": []
  }
}
```

This trace is required for Dense candidate → RRF → final gold survival. It is
not required to run the initial 30-question Hit@5 and Context Recall pilot.
