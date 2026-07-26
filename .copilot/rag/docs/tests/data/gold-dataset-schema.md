# Gold Dataset Schema

Gold data is span-based so that chunk length can vary without rewriting labels.

## JSONL Record

```json
{
  "id": "AC_SEM_001",
  "db_family": "ac",
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

For each span:

- `source_id`
- `document_id` or `path`
- `start_char`
- `end_char`
- `required`
- `source_text_hash` or stable context quote

