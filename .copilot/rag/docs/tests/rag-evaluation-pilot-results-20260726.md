# RAG Evaluation Pilot Results 2026-07-26

This execution is not a search quality pass/fail judgment. It verifies existing DBs, daemon behavior, CLI options, JSON I/F, retrieval-mode switching, and context packing. Gold spans were not available for this pilot, so Hit/MRR/nDCG/Vector Rescue are not computed here.

## Environment

| Item | Value |
|---|---|
| Git base commit | `dc26cf7` |
| Dirty during run | yes, evaluation options and docs in progress |
| Runtime | `.copilot/rag/query/.venv/bin/python` |
| Query date | 2026-07-26 |
| Query count | 27 |
| DBs | `ac-rag`, `incident-rag`, `rfc-full-20k-rag` |
| Retrieval modes | `hybrid`, `lexical`, `dense` |
| Context budget | `--budget-tokens 1200` |
| Max evidence chars | `--max-chars 1200` |

## DB State

| DB | Documents | Chunks | Size |
|---|---:|---:|---:|
| `ac-rag` | 21 | 1,205 | 27MB |
| `incident-rag` | 201 | 2,144 | 45MB |
| `rfc-full-20k-rag` | 386 | 19,518 | 480MB |

## Mode Summary

| Mode | Runs | OK | JSON OK | Avg Latency | p95 Latency |
|---|---:|---:|---:|---:|---:|
| `hybrid` | 9 | 9 | 9 | 2.901s | 4.477s |
| `lexical` | 9 | 9 | 9 | 0.672s | 0.792s |
| `dense` | 9 | 9 | 9 | 4.451s | 4.719s |

## DB And Mode Summary

| DB | Mode | Avg Latency | Top-1 Sources |
|---|---|---:|---|
| `ac-rag` | `hybrid` | 2.904s | `lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf`; `sample_global_cooling_temperature_memo.docx`; `sample_global_cooling_temperature_memo.docx` |
| `ac-rag` | `lexical` | 0.706s | `lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf`; `sample_global_cooling_temperature_memo.docx`; `sample_global_cooling_temperature_memo.docx` |
| `ac-rag` | `dense` | 4.024s | `lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf`; `sample_global_cooling_temperature_memo.docx`; `clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf` |
| `incident-rag` | `hybrid` | 3.079s | `ntsb_aviation_report_67438.pdf`; `ntsb_aviation_report_67526.pdf`; `ntsb_aviation_report_67648.pdf` |
| `incident-rag` | `lexical` | 0.655s | `ntsb_aviation_report_67438.pdf`; `ntsb_aviation_report_67526.pdf`; `ntsb_aviation_report_67648.pdf` |
| `incident-rag` | `dense` | 4.392s | `ntsb_aviation_report_67440.pdf`; `ntsb_aviation_report_67440.pdf`; `ntsb_aviation_report_67671.pdf` |
| `rfc-full-20k-rag` | `hybrid` | 2.720s | `rfc10026.txt`; `rfc10026.txt`; `rfc9706.txt` |
| `rfc-full-20k-rag` | `lexical` | 0.654s | `rfc10026.txt`; `rfc10026.txt`; `rfc9706.txt` |
| `rfc-full-20k-rag` | `dense` | 4.936s | `rfc9926.txt`; `rfc10026.txt`; `rfc9743.txt` |

## Observations

- Optional `--retrieval-mode hybrid|lexical|dense` worked through the top-level `search.py`.
- All 27 runs exited with code 0 and produced parseable JSON.
- `lexical` was fastest in this pilot because it avoids dense embedding.
- `dense` often returned different top-1 sources for exact/identifier questions. This is expected and is the reason `dense` is diagnostic rather than the main production mode.
- `hybrid` preserved exact/lexical top-1 results for exact RFC and incident queries in this pilot.
- This pilot cannot claim retrieval correctness because no gold spans were used.

## Raw Data

Raw JSONL files:

- `data/pilot-queries-20260726.jsonl`
- `data/pilot-results-20260726.jsonl`

## Next Steps

1. Build span-based gold data using `data/gold-dataset-schema.md`.
2. Build chunk variants with `--chunk-max-chars 1000`, `2000`, and `20000`.
3. Run the full 1k/2k/20k x L/V/H matrix.
4. Compute Hit@k, MRR@10, nDCG@10, Context Recall@budget, Vector Rescue, and Vector Harm.
