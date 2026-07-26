# RAG Evaluation Runbook

This runbook describes how to execute RAG evaluation without changing normal defaults.

## Preconditions

From repository root:

```bash
.copilot/rag/query/.venv/bin/python .copilot/rag/query/list_dbs.py
```

Expected current DBs:

- `ac-rag`
- `incident-rag`
- `rfc-full-20k-rag`

## Smoke/Pilot

Pilot verifies operation only. It is not a quality judgment.

Use three query types per DB:

- exact
- semantic
- broad/context-packing

Run all three retrieval modes:

```bash
.copilot/rag/query/.venv/bin/python .copilot/rag/query/search.py \
  --db ac-rag \
  --retrieval-mode hybrid \
  --format json \
  --budget-tokens 1200 \
  --max-chars 1200 \
  "A2Wに関する情報を教えて"
```

Modes:

```text
--retrieval-mode hybrid   # default, normal behavior
--retrieval-mode lexical  # evaluation-only, no dense vector search
--retrieval-mode dense    # evaluation-only, dense vector search only
```

Default behavior is unchanged when `--retrieval-mode` is omitted.

## Chunk Variant Builds

Chunk length options are evaluation-only. Defaults remain `1400` and `160`.

```bash
.copilot/rag/query/.venv/bin/python .copilot/rag/gen_db/create_db.py \
  --db project-c1000-rag \
  --title "Project 1k Chunk Evaluation"

.copilot/rag/query/.venv/bin/python .copilot/rag/gen_db/build_db.py \
  --db project-c1000-rag \
  --root /path/to/input \
  --source-id project \
  --force-rebuild \
  --chunk-max-chars 1000 \
  --chunk-overlap 120
```

Recommended variant names:

| Variant | `--chunk-max-chars` | Example DB |
|---|---:|---|
| 1k | 1000 | `project-c1000-rag` |
| 2k | 2000 | `project-c2000-rag` |
| 20k | 20000 | `project-c20000-rag` |

Use the same input corpus, embedding model, overlap policy, and query set across variants unless deliberately testing one factor.

## Retrieval Evaluation Matrix

For each chunk variant:

| Chunk | L | V | H |
|---|---|---|---|
| 1k | lexical | dense | hybrid |
| 2k | lexical | dense | hybrid |
| 20k | lexical | dense | hybrid |

The primary comparison is query-level `H - L`.

## Output Files

Store durable evaluation artifacts under:

```text
.copilot/rag/docs/tests/
  rag-evaluation-plan.md
  rag-evaluation-runbook.md
  rag-evaluation-pilot-results-YYYYMMDD.md
  data/
    pilot-queries-YYYYMMDD.jsonl
    pilot-results-YYYYMMDD.jsonl
    gold-dataset-schema.md
```

Raw DBs remain under `.copilot/rag/dbs/` and are gitignored.

