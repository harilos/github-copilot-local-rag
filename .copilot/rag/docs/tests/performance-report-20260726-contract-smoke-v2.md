# RAG性能評価ダッシュボード 20260726-contract-smoke-v2

異なるrun条件は統合せず、互換条件ごとのcellとして比較する。

## 判定基準

- H p95 target: 8.000 sec
- L p95 target: 2.000 sec
- V p95 target: 8.000 sec
- hard latency limit: 15.000 sec
- timeout rate limit: 0.000%
- p95 minimum N: 1

## list_dbs

- repeats: 0
- dbs: 
- JSON errors: 0
- p50:  sec
- p95:  sec

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Current compatible run|9ec0213cc4|c0a0477ed5|seq=default, explain=False, diag=basic, pure=False|2|2|0|0.661|PASS|PASS|

## Run 1: Current compatible run

- source/run_id: performance-results-20260726-contract-smoke-v2.jsonl
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: c0a0477ed51215f3d83228c88d30ca966c291921b5830099758c2705e6d0a546
- daemon_code_fingerprint_expected: ccd8c495e0ab8ba13f82346f14f9686dc6dab7ce863ee4bb73e350aa45fde659
- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- db_hash/db_snapshot_hash: ac-rag=6b7bb428e1/f26efbb61f
- explain_enabled: False
- diagnostics_level: basic
- identifier_diagnostics_enabled: True
- identifier_diagnostics_requested: True
- pure_profile: False
- sequence_plan: default
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: 15.0
- daemon_fallback_policy: off
- case_spec_fingerprint: 4c7d693f9b191a582b98176a04c285fa122729997187762d312c5d29a3d4d3c1
- mixed_total/seed/time_buckets: 500/20260726/5
- warmup_runs: 1

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|daemon|2|2|0|0|0.616|0.661|2.000|0.661|PASS|PASS|NO|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|PASS|1|1|0|0|expectation対象行のみ|
|Exact negative|PASS|1|1|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|2|2|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|2|2|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|PASS|2|2|0|0|fallback後の成功と分離|
|final user-visible成功率|PASS|2|2|0|0|fallback後の最終結果|
|fallback rate|PASS|2|2|0|0|clean daemon runでは0|
|daemon build identity|PASS|2|2|0|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|PASS|2|2|0|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|2|2|0|0|limit=0.000%|
|latency_p95_slo|PASS|2|0|0|0|profile別target、low-NはINSUFFICIENT_N|
|hard_latency_limit|PASS|2|2|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|2|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|2|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|PASS|1|1|0|0|通常contexts空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|2|2|0|
|stdout JSON purity|2|2|0|
|Exact negative collision|1|1|0|
|Expected unmatched identifier|1|1|0|
|Exact positive candidate|1|1|0|
|Matched identifier + raw occurrence|1|1|0|
|No-hit contract|1|1|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC-EXACT-P-001|ac-rag|L|daemon|0.661|ok|SOURCES.md|
|AC-EXACT-N-001|ac-rag|L|daemon|0.616|partial|iea_2018_the_future_of_cooling.pdf|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

