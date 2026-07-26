# RAG Evaluation Plan

This document defines the evaluation design for the local RAG system. It separates retrieval-only evaluation from end-to-end answer evaluation.

## Scope

The target system is the local Copilot RAG pack:

- Dense search: Chroma + Ruri-v3-30m ONNX INT8
- Lexical search: SQLite FTS5 BM25 + Sudachi A
- Exact search: identifier dictionary + chunk postings
- Metadata search: file/path/title lookup
- Fusion: weighted RRF
- Postprocess: duplicate suppression, document diversity, neighbor expansion, context budget packing

Normal search behavior remains hybrid. Evaluation-only switches must be optional and must not change default execution.

## Evaluation Stages

| Stage | Goal | Primary output |
|---|---|---|
| Smoke/pilot | Verify CLI, daemon, DB, JSON I/F, and mode switches | Operational log |
| Retrieval evaluation | Measure whether correct evidence is retrieved | IR metrics by query |
| End-to-end evaluation | Measure whether an answer can be generated from retrieved evidence | Answer and citation scores |
| Regression | Detect retrieval behavior drift | Fixed query set results |

Smoke/pilot results are not quality certification.

## Retrieval Modes

The same search system is evaluated with three optional modes:

| ID | Mode | Meaning |
|---|---|---|
| L | `lexical` | BM25 + exact + file/metadata, no dense vector search |
| V | `dense` | Dense vector search only |
| H | `hybrid` | Dense + lexical + exact + metadata + RRF |

The main comparison is `H - L` at query level. This measures whether adding dense retrieval improves final context selection in the actual system. `V` is diagnostic.

## Chunk Variants

Chunk length experiments compare:

- 1,000
- 2,000
- 20,000

Record whether the unit is characters or model tokens. Current implementation options are character based:

- `--chunk-max-chars`
- `--chunk-overlap`

DB naming convention:

| Variant | DB name pattern |
---|---|
| 1k | `<base>-c1000-rag` |
| 2k | `<base>-c2000-rag` |
| 20k | `<base>-c20000-rag` |

Chunk length changes also change index size, latency, BM25 document length normalization, context packing behavior, and dense embedding granularity. Quality and cost metrics must be reported separately.

## Gold Data

Gold answers are defined against original source positions, not fixed chunk IDs. Chunk IDs change across chunk length variants.

Gold span requirements:

- Use `source_id + document_id/path + start_char + end_char`
- Store source text hash or a local context quote around the span
- Allow multiple gold spans per question
- Mark required spans for multi-evidence questions
- Include negative questions where no answer should be found

Relevance mapping per chunk variant:

| Relevance | Meaning |
|---:|---|
| 2 | chunk fully contains required gold span |
| 1 | chunk overlaps required gold span |
| 0 | no overlap |

Also record `same_document_only` matches separately; they are not sufficient evidence.

## Question Taxonomy

Pilot starts small. Full evaluation should use 150-250 questions after a 60-question pilot.

| Type | Target share | Purpose |
|---|---:|---|
| exact | 20% | IDs, acronyms, file names, RFC numbers, report IDs |
| lexical | 20% | Query uses important source terms |
| semantic | 25% | Query uses paraphrases and avoids copied source terms |
| multi_evidence | 15% | Requires multiple spans |
| cross_document | 10% | Comparison or synthesis across documents |
| negative | 10% | Answer is not in DB |

Each DB should contain at least exact, semantic, and broad/context-packing queries in smoke/pilot.

## Retrieval Metrics

Standard metrics:

- Hit@1/3/5/10
- MRR@10
- nDCG@10
- Recall@30/50

Budget-aware metrics:

- Context Recall@4k/8k
- All-evidence Success@budget
- Evidence Density
- Context Waste

Vector contribution metrics:

- Vector Rescue Rate: `L fails and H succeeds` over `L fails`
- Vector Harm Rate: `L succeeds and H fails` over `L succeeds`
- Dense Unique Evidence: correct evidence appears only in dense candidates

Record candidate-stage transitions:

1. Dense top 50 contains gold
2. RRF top 12 retains gold
3. Context budget retains gold

This separates model, fusion, and context-packing failures.

## End-to-End Metrics

Run end-to-end evaluation only after retrieval candidates are narrowed.

| Condition | Meaning |
|---|---|
| No RAG | model answers without retrieved context |
| Lexical RAG | `retrieval-mode lexical` |
| Hybrid RAG | `retrieval-mode hybrid` |
| Oracle RAG | gold evidence injected directly |

Evaluation dimensions:

- Answer Correctness
- Completeness
- Faithfulness
- Citation Correctness
- Abstention Accuracy

Human-reviewed gold claims and gold spans are primary. LLM-as-judge may be used as assistance, but should be spot-checked.

## Required Per-Query Log

Each query run records:

- run timestamp
- git commit and dirty state
- DB name
- query ID and type
- question
- command
- retrieval mode
- exit code
- latency
- JSON parse success
- status
- evidence count
- context character count
- top-1 source/path/title/section
- top-1 signals
- truncation flag
- stderr tail
- daemon transport when available

