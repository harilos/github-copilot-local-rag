# RAG性能評価レポート 20260726-rejudged

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: H, L, V
- sequence_plan: default
- explain_mode: on
- diagnostics_level: basic
- pure_profile: False
- daemon repeats: 3
- no-daemon repeats: 1
- warmup_runs: 1
- timeout_seconds: 180
- daemon_slo_p95: 15.0
- min_samples_for_p95: 20
- budget_tokens: 1200
- max_chars: 1200

## list_dbs

- repeats: 10
- dbs: ac-rag, incident-rag, rfc-full-20k-rag
- JSON errors: 0
- p50: 0.029 sec
- p95: 0.031 sec

## latency by DB / profile / execution

|DB|profile|execution|N|success|timeout|errors|p50 sec|p95 sec|max sec|p95判定|
|--|--|--|--:|--:|--:|--:|--:|--:|--:|--|
|ac-rag|H|daemon|12|12|0|0|6.590|23.906|23.906|low-N(<20)|
|ac-rag|H|no-daemon|4|4|0|0|5.045|5.792|5.792|low-N(<20)|
|ac-rag|L|daemon|12|12|0|0|0.961|49.186|49.186|low-N(<20)|
|ac-rag|L|no-daemon|4|4|0|0|0.901|1.150|1.150|low-N(<20)|
|ac-rag|V|daemon|12|11|1|1|7.223|12.577|12.577|low-N(<20)|
|ac-rag|V|no-daemon|4|4|0|0|4.843|5.230|5.230|low-N(<20)|
|incident-rag|H|daemon|9|8|1|1|5.821|10.052|10.052|low-N(<20)|
|incident-rag|H|no-daemon|3|3|0|0|4.854|4.854|4.854|low-N(<20)|
|incident-rag|L|daemon|9|9|0|0|0.852|64.800|64.800|low-N(<20)|
|incident-rag|L|no-daemon|3|3|0|0|0.901|0.947|0.947|low-N(<20)|
|incident-rag|V|daemon|9|9|0|0|6.161|9.213|9.213|low-N(<20)|
|incident-rag|V|no-daemon|3|3|0|0|4.732|4.908|4.908|low-N(<20)|
|rfc-full-20k-rag|H|daemon|9|9|0|0|1.454|9.624|9.624|low-N(<20)|
|rfc-full-20k-rag|H|no-daemon|3|3|0|0|4.313|4.634|4.634|low-N(<20)|
|rfc-full-20k-rag|L|daemon|9|9|0|0|0.873|1.359|1.359|low-N(<20)|
|rfc-full-20k-rag|L|no-daemon|3|3|0|0|0.852|0.970|0.970|low-N(<20)|
|rfc-full-20k-rag|V|daemon|9|9|0|0|6.736|12.858|12.858|low-N(<20)|
|rfc-full-20k-rag|V|no-daemon|3|3|0|0|4.297|4.481|4.481|low-N(<20)|

## separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact修正のpilot確認|PASS|20|20|0|0|A2W negativeとA2L positiveを分離|
|完了した処理のJSON純度|PASS|118|118|0|0|timeoutはJSON破損ではなく未完了として別計上|
|全検索の正常完了率|FAIL|120|118|2|2|exit 0かつJSON parse成功のみ成功|
|daemon正常完了率|FAIL|90|88|2|2|daemon経路だけを分離|
|daemon latency SLO|FAIL|88|84|4|0|p95=12.858 sec / gate=15.000 sec。1件でもgate超過ならFAIL|
|no-daemon機能smoke|PASS|30|30|0|0|daemon問題との切り分け用|
|Vector有効性|未評価|0|0|0|0|gold span付きsemantic set未実行|
|no-hit応答品質|未確定|0|0|0|0|related_context隔離など最終契約が未固定|
|最終リリース|FAIL|0|0|0|0|Windows、soak、Vector、no-hit契約まで未充足|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|120|118|2|
|stdout JSON purity|120|118|2|
|Exact negative collision|12|12|0|
|Expected unmatched identifier|12|12|0|
|Exact positive candidate|24|23|1|

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

