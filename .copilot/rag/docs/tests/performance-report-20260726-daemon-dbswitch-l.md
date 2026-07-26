# RAG性能評価レポート 20260726-daemon-dbswitch-l

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: L
- sequence_plan: db-switch
- explain_mode: off
- diagnostics_level: off
- pure_profile: True
- daemon repeats: 5
- no-daemon repeats: 0
- warmup_runs: 1
- timeout_seconds: 30
- daemon_slo_p95: 15.0
- min_samples_for_p95: 5
- budget_tokens: 1200
- max_chars: 1200

## list_dbs

- repeats: 0
- dbs: 
- JSON errors: 0
- p50:  sec
- p95:  sec

## latency by DB / profile / execution

|DB|profile|execution|N|success|timeout|errors|p50 sec|p95 sec|max sec|p95判定|
|--|--|--|--:|--:|--:|--:|--:|--:|--:|--|
|ac-rag|L|daemon|10|10|0|0|0.742|0.804|0.804|OK|
|incident-rag|L|daemon|5|5|0|0|0.927|1.168|1.168|OK|
|rfc-full-20k-rag|L|daemon|5|5|0|0|1.290|1.569|1.569|OK|

## separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact修正のpilot確認|FAIL|10|10|0|0|A2W negativeとA2L positiveを分離|
|完了した処理のJSON純度|PASS|20|20|0|0|timeoutはJSON破損ではなく未完了として別計上|
|全検索の正常完了率|PASS|20|20|0|0|exit 0かつJSON parse成功のみ成功|
|daemon正常完了率|PASS|20|20|0|0|daemon経路だけを分離|
|daemon latency SLO|PASS|20|20|0|0|p95=1.446 sec / gate=15.000 sec。1件でもgate超過ならFAIL|
|no-daemon機能smoke|PASS|0|0|0|0|daemon問題との切り分け用|
|Vector有効性|未評価|0|0|0|0|gold span付きsemantic set未実行|
|no-hit応答品質|未確定|0|0|0|0|related_context隔離など最終契約が未固定|
|最終リリース|FAIL|0|0|0|0|Windows、soak、Vector、no-hit契約まで未充足|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|20|20|0|
|stdout JSON purity|20|20|0|
|Exact negative collision|10|10|0|
|Expected unmatched identifier|10|0|10|
|Exact positive candidate|10|10|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.569|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.446|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.290|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.277|ok|rfc10026.txt|
|INC_EXACT_001|incident-rag|L|daemon|1.168|ok|ntsb_aviation_report_67438.pdf|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.166|ok|rfc10026.txt|
|INC_EXACT_001|incident-rag|L|daemon|1.049|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.927|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.872|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.867|ok|ntsb_aviation_report_67438.pdf|

