# RAG性能評価レポート 20260726-daemon-transition-ac

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: L, V, H
- sequence_plan: profile-transition
- explain_mode: off
- diagnostics_level: off
- pure_profile: True
- daemon repeats: 5
- no-daemon repeats: 0
- warmup_runs: 1
- timeout_seconds: 30
- daemon_slo_p95: 15.0
- min_samples_for_p95: 10
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
|ac-rag|H|daemon|5|5|0|0|5.432|5.566|5.566|low-N(<10)|
|ac-rag|L|daemon|15|15|0|0|0.746|0.943|0.943|OK|
|ac-rag|V|daemon|10|10|0|0|5.306|7.360|7.360|OK|

## separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact修正のpilot確認|FAIL|0|0|0|0|A2W negativeとA2L positiveを分離|
|完了した処理のJSON純度|PASS|30|30|0|0|timeoutはJSON破損ではなく未完了として別計上|
|全検索の正常完了率|PASS|30|30|0|0|exit 0かつJSON parse成功のみ成功|
|daemon正常完了率|PASS|30|30|0|0|daemon経路だけを分離|
|daemon latency SLO|PASS|30|30|0|0|p95=5.729 sec / gate=15.000 sec。1件でもgate超過ならFAIL|
|no-daemon機能smoke|PASS|0|0|0|0|daemon問題との切り分け用|
|Vector有効性|未評価|0|0|0|0|gold span付きsemantic set未実行|
|no-hit応答品質|未確定|0|0|0|0|related_context隔離など最終契約が未固定|
|最終リリース|FAIL|0|0|0|0|Windows、soak、Vector、no-hit契約まで未充足|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|30|30|0|
|stdout JSON purity|30|30|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|V|daemon|7.360|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|5.729|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|5.630|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|5.578|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|H|daemon|5.566|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.465|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.432|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|V|daemon|5.392|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|5.306|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|5.073|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|

