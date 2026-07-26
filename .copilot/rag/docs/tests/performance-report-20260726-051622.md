# RAG性能評価レポート 20260726-051622

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: H, L, V
- daemon repeats: 3
- no-daemon repeats: 1
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
|ac-rag|H|daemon|12|0|6.590|23.906|23.906|
|ac-rag|H|no-daemon|4|0|5.045|5.792|5.792|
|ac-rag|L|daemon|12|0|0.961|49.186|49.186|
|ac-rag|L|no-daemon|4|0|0.901|1.150|1.150|
|ac-rag|V|daemon|12|1|7.223|12.577|12.577|
|ac-rag|V|no-daemon|4|0|4.843|5.230|5.230|
|incident-rag|H|daemon|9|1|5.821|10.052|10.052|
|incident-rag|H|no-daemon|3|0|4.854|4.854|4.854|
|incident-rag|L|daemon|9|0|0.852|64.800|64.800|
|incident-rag|L|no-daemon|3|0|0.901|0.947|0.947|
|incident-rag|V|daemon|9|0|6.161|9.213|9.213|
|incident-rag|V|no-daemon|3|0|4.732|4.908|4.908|
|rfc-full-20k-rag|H|daemon|9|0|1.454|9.624|9.624|
|rfc-full-20k-rag|L|daemon|9|0|0.873|1.359|1.359|
|rfc-full-20k-rag|V|daemon|9|0|6.736|12.858|12.858|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|109|109|0|
|stdout JSON purity|109|109|0|
|Exact negative collision|12|12|0|
|Expected unmatched identifier|12|12|0|
|Exact positive candidate|32|32|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|V|daemon|193.393|timeout||
|INC_EXACT_001|incident-rag|H|daemon|186.949|timeout||
|INC_EXACT_001|incident-rag|L|daemon|64.800|ok|ntsb_aviation_report_67438.pdf|
|AC_BROAD_001|ac-rag|L|daemon|49.186|ok|sample_global_cooling_temperature_memo.docx|
|AC_SEM_001|ac-rag|H|daemon|23.906|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|22.436|ok|sample_global_cooling_temperature_memo.docx|
|RFC_BROAD_001|rfc-full-20k-rag|V|daemon|12.858|ok|rfc9743.txt|
|AC_EXACT_LOWDF_001|ac-rag|V|daemon|12.577|ok|lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf|
|AC_SEM_001|ac-rag|V|daemon|10.947|ok|sample_global_cooling_temperature_memo.docx|
|AC_EXACT_NEG_COLLISION_001|ac-rag|H|daemon|10.576|partial|sample_global_cooling_temperature_memo.docx|

