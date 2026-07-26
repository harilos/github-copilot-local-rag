# RAG Pilot Rerun Results (20260726-125347)

This is an operational pilot/smoke result, not a relevance-quality benchmark. No gold spans were used.

## Scope

- Queries: 9
- Retrieval modes: hybrid, lexical, dense
- Total executions: 27
- Command shape: `search.py --db <db> --retrieval-mode <mode> --format json --budget-tokens 1200 --max-chars 1200 --timeout 120 <question>`
- Raw results: `.copilot/rag/docs/tests/data/pilot-results-rerun-20260726-125347.jsonl`

## Mode Summary

| Mode | Runs | Exit 0 | JSON OK | Avg s | p95 s | Avg Evidence |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 9 | 9 | 9 | 3.353 | 6.248 | 2.7 |
| lexical | 9 | 9 | 9 | 0.662 | 0.778 | 2.7 |
| dense | 9 | 9 | 9 | 5.337 | 6.712 | 2.2 |

## DB x Mode Summary

| DB | Mode | Avg s | p95 s | Top1 paths by query |
|---|---|---:|---:|---|
| ac-rag | dense | 5.149 | 5.445 | AC_EXACT_001: `lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf`<br>AC_SEM_001: `sample_global_cooling_temperature_memo.docx`<br>AC_BROAD_001: `clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf` |
| ac-rag | hybrid | 3.468 | 5.020 | AC_EXACT_001: `lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf`<br>AC_SEM_001: `sample_global_cooling_temperature_memo.docx`<br>AC_BROAD_001: `sample_global_cooling_temperature_memo.docx` |
| ac-rag | lexical | 0.571 | 0.677 | AC_EXACT_001: `lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf`<br>AC_SEM_001: `sample_global_cooling_temperature_memo.docx`<br>AC_BROAD_001: `sample_global_cooling_temperature_memo.docx` |
| incident-rag | dense | 4.932 | 5.113 | INC_EXACT_001: `ntsb_aviation_report_67440.pdf`<br>INC_SEM_001: `ntsb_aviation_report_67440.pdf`<br>INC_BROAD_001: `ntsb_aviation_report_67671.pdf` |
| incident-rag | hybrid | 3.991 | 6.248 | INC_EXACT_001: `ntsb_aviation_report_67438.pdf`<br>INC_SEM_001: `ntsb_aviation_report_67526.pdf`<br>INC_BROAD_001: `ntsb_aviation_report_67648.pdf` |
| incident-rag | lexical | 0.680 | 0.742 | INC_EXACT_001: `ntsb_aviation_report_67438.pdf`<br>INC_SEM_001: `ntsb_aviation_report_67526.pdf`<br>INC_BROAD_001: `ntsb_aviation_report_67648.pdf` |
| rfc-full-20k-rag | dense | 5.929 | 6.712 | RFC_EXACT_001: `rfc9926.txt`<br>RFC_SEM_001: `rfc10026.txt`<br>RFC_BROAD_001: `rfc9743.txt` |
| rfc-full-20k-rag | hybrid | 2.600 | 5.967 | RFC_EXACT_001: `rfc10026.txt`<br>RFC_SEM_001: `rfc10026.txt`<br>RFC_BROAD_001: `rfc9706.txt` |
| rfc-full-20k-rag | lexical | 0.733 | 0.778 | RFC_EXACT_001: `rfc10026.txt`<br>RFC_SEM_001: `rfc10026.txt`<br>RFC_BROAD_001: `rfc9706.txt` |

## Observations

- All 27 commands exited successfully and returned parseable JSON.
- `lexical` is fastest in this pilot: avg 0.662s.
- `dense` is slowest: avg 5.337s. This includes embedding/vector path overhead.
- `hybrid` preserves exact/lexical-looking top1 results in the sampled exact queries, while still enabling dense rescue candidates.
- Dense quality impact cannot be concluded from this run because the test set has no gold spans. Use the gold-span evaluator before making ranking-quality claims.

## Review Points

- Confirm whether top1 paths are intuitively acceptable for each query.
- Add gold spans for questions where correctness matters, then compute Hit@k, MRR, Context Recall, Vector Rescue, and Vector Harm.
- Keep `--retrieval-mode` and chunk-size options as evaluation-only switches; default search behavior remains hybrid.
