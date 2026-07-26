# RAG性能評価レポート 20260726-051622-timeout-retry

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: H, V
- daemon repeats: 1
- no-daemon repeats: 0
- budget_tokens: 1200
- max_chars: 1200

## list_dbs

- repeats: 0
- dbs: 
- JSON errors: 0
- p50:  sec
- p95:  sec

## latency by DB / profile / execution

|DB|profile|execution|N|errors|p50 sec|p95 sec|max sec|
|--|--|--|--:|--:|--:|--:|--:|
|ac-rag|H|daemon|1|0|0.599|0.599|0.599|
|ac-rag|V|daemon|1|0|3.500|3.500|3.500|
|incident-rag|H|daemon|1|0|0.453|0.453|0.453|
|incident-rag|V|daemon|1|0|3.700|3.700|3.700|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|4|4|0|
|stdout JSON purity|4|4|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|2|2|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|INC_EXACT_001|incident-rag|V|daemon|3.700|ok|ntsb_aviation_report_67440.pdf|
|AC_BROAD_001|ac-rag|V|daemon|3.500|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|H|daemon|0.599|ok|sample_global_cooling_temperature_memo.docx|
|INC_EXACT_001|incident-rag|H|daemon|0.453|ok|ntsb_aviation_report_67438.pdf|

