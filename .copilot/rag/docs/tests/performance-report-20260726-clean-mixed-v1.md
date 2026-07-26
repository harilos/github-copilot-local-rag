# RAG性能評価ダッシュボード 20260726-clean-mixed-v1

異なるrun条件は統合せず、互換条件ごとのcellとして比較する。

## 判定基準

- H p95 target: 8.000 sec
- L p95 target: 2.000 sec
- V p95 target: 8.000 sec
- hard latency limit: 15.000 sec
- timeout rate limit: 0.000%
- p95 minimum N: 20

## list_dbs

list_dbs latency is shown per source/run and is never pooled.

|source/run|repeats|dbs|JSON errors|p50 sec|p95 sec|
|--|--:|--|--:|--:|--:|
|performance-results-20260726-clean-mixed-v1.jsonl|10|ac-rag, incident-rag, rfc-full-20k-rag|0|0.032|0.049|

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Current clean mixed|9ec0213cc4|8a4ba76131|seq=clean-mixed, explain=False, diag=off, pure=True|500|500|0|1.699|PASS|PASS|

## Run 1: Current clean mixed

- source/run_id: performance-results-20260726-clean-mixed-v1.jsonl
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: 8a4ba76131c8dd7adc796ffac3d8d1de7a845a7773350fce1bc6ee32a44279ae
- daemon_code_fingerprint_expected: b0d4458d9278e2d7307fae0cb3712d249d02e1bef93734114a5afcb18c23be4b
- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- db_hash/db_snapshot_hash: ac-rag=6b7bb428e1/f26efbb61f, incident-rag=7e3858deb3/f825401fbe, rfc-full-20k-rag=c7f4fba2fc/7c2a9d6f17
- explain_enabled: False
- diagnostics_level: off
- identifier_diagnostics_enabled: False
- identifier_diagnostics_requested: False
- pure_profile: True
- sequence_plan: clean-mixed
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: 15.0
- daemon_fallback_policy: off
- case_spec_fingerprint: e9f6abdfe5417b9f0df44fac06c968e82913a6acaf4c395ab148567894279fd8
- mixed_total/seed/time_buckets: 500/20260726/10
- warmup_runs: 5

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|H|daemon|daemon|120|120|0|0|0.659|0.890|8.000|1.528|PASS|PASS|NO|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|daemon|35|35|0|0|0.632|0.871|2.000|0.929|PASS|PASS|NO|
|ac-rag|6b7bb428e1|f26efbb61f|V|daemon|daemon|20|20|0|0|0.652|0.880|8.000|1.205|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|H|daemon|daemon|100|100|0|0|0.670|0.973|8.000|1.345|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|L|daemon|daemon|30|30|0|0|0.670|1.070|2.000|1.089|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|V|daemon|daemon|20|20|0|0|0.650|0.813|8.000|0.904|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|H|daemon|daemon|120|120|0|0|0.852|1.258|8.000|1.699|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|L|daemon|daemon|35|35|0|0|0.874|1.117|2.000|1.316|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|V|daemon|daemon|20|20|0|0|0.648|0.708|8.000|0.898|PASS|PASS|NO|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_APPLICABLE|0|0|0|0|identifier diagnostics無効のrun|
|Exact negative|NOT_APPLICABLE|0|0|0|0|identifier diagnostics無効のrun|
|JSON stdout purity|PASS|500|500|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|500|500|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|PASS|500|500|0|0|fallback後の成功と分離|
|final user-visible成功率|PASS|500|500|0|0|fallback後の最終結果|
|fallback rate|PASS|500|500|0|0|clean daemon runでは0|
|daemon build identity|PASS|500|500|0|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|PASS|500|500|0|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|500|500|0|0|limit=0.000%|
|latency_p95_slo|PASS|9|9|0|0|profile別target、low-N cells=0|
|hard_latency_limit|PASS|500|500|0|0|max limit=15.000 sec|
|time degradation|PASS|500|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|PASS|500|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_APPLICABLE|0|0|0|0|identifier diagnostics無効のrun|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|500|500|0|
|stdout JSON purity|500|500|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC_BROAD_001|rfc-full-20k-rag|H|daemon|1.699|ok|rfc10003.txt|
|AC_EXACT_NEG_COLLISION_001|ac-rag|H|daemon|1.528|ok|sample_global_cooling_temperature_memo.docx|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.449|ok|rfc10026.txt|
|RFC_BROAD_001|rfc-full-20k-rag|H|daemon|1.418|ok|rfc10003.txt|
|RFC_SEM_001|rfc-full-20k-rag|H|daemon|1.409|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.345|ok|rfc10026.txt|
|INC_EXACT_001|incident-rag|H|daemon|1.345|ok|ntsb_aviation_report_67438.pdf|
|RFC_SEM_001|rfc-full-20k-rag|L|daemon|1.316|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.306|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.258|ok|rfc10026.txt|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

