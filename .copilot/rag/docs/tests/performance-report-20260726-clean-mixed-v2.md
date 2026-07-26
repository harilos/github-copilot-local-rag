# RAG性能評価ダッシュボード 20260726-clean-mixed-v2

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
|performance-results-20260726-clean-mixed-v2.jsonl|10|ac-rag, incident-rag, rfc-full-20k-rag|0|0.031|0.035|

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Current clean mixed|9ec0213cc4|2e2bb9a5b0|seq=clean-mixed, explain=False, diag=off, pure=True|500|500|0|1.219|PASS|PASS|

## Run 1: Current clean mixed

- source/run_id: performance-results-20260726-clean-mixed-v2.jsonl
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: 2e2bb9a5b04b2d61f339be35ef9cdd1d222f15943a779dd41534abe393e6d89a
- daemon_code_fingerprint_expected: dce1860d47f74e6b655454931b06de16a8e549984dc7aaf56543759cc70a5751
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
|ac-rag|6b7bb428e1|f26efbb61f|H|daemon|daemon|120|120|0|0|0.650|0.697|8.000|0.886|PASS|PASS|NO|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|daemon|35|35|0|0|0.634|0.839|2.000|0.848|PASS|PASS|NO|
|ac-rag|6b7bb428e1|f26efbb61f|V|daemon|daemon|20|20|0|0|0.655|0.852|8.000|0.862|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|H|daemon|daemon|100|100|0|0|0.668|0.862|8.000|1.219|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|L|daemon|daemon|30|30|0|0|0.658|0.835|2.000|0.837|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|V|daemon|daemon|20|20|0|0|0.639|0.696|8.000|0.853|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|H|daemon|daemon|120|120|0|0|0.800|1.035|8.000|1.092|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|L|daemon|daemon|35|35|0|0|0.771|1.095|2.000|1.121|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|V|daemon|daemon|20|20|0|0|0.659|0.715|8.000|0.859|PASS|PASS|NO|

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
|time degradation|PASS|500|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
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
|INC_BROAD_001|incident-rag|H|daemon|1.219|ok|ntsb_aviation_report_67648.pdf|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.121|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.095|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.092|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.085|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.061|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.060|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.050|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|daemon|1.048|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.040|ok|rfc10026.txt|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

