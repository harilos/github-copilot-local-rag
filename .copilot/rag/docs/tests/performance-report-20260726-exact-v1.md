# RAG性能評価ダッシュボード 20260726-exact-v1

異なるrun条件は統合せず、互換条件ごとのcellとして比較する。

## 判定基準

- H p95 target: 8.000 sec
- L p95 target: 2.000 sec
- V p95 target: 8.000 sec
- hard latency limit: 15.000 sec
- timeout rate limit: 0.000%
- p95 minimum N: 5

## list_dbs

- repeats: 0
- dbs: 
- JSON errors: 0
- p50:  sec
- p95:  sec

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Current compatible run|9ec0213cc4|df4b1a49db|seq=default, explain=False, diag=basic, pure=False|30|30|0|1.145|PASS|PASS|

## Run 1: Current compatible run

- source/run_id: performance-results-20260726-exact-v1.jsonl
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: df4b1a49db2ed84d10dd0c242bc683f7be84ac8a3309cee53dbe6c0351db5db9
- daemon_code_fingerprint_expected: ccd8c495e0ab8ba13f82346f14f9686dc6dab7ce863ee4bb73e350aa45fde659
- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- db_hash/db_snapshot_hash: ac-rag=6b7bb428e1/f26efbb61f, incident-rag=7e3858deb3/f825401fbe, rfc-full-20k-rag=c7f4fba2fc/7c2a9d6f17
- explain_enabled: False
- diagnostics_level: basic
- identifier_diagnostics_enabled: True
- identifier_diagnostics_requested: True
- pure_profile: False
- sequence_plan: default
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: 15.0
- daemon_fallback_policy: off
- case_spec_fingerprint: 5c450417213045388717c4b24a584a9d409fbb9d67620335f757a3585e5ed017
- mixed_total/seed/time_buckets: 500/20260726/5
- warmup_runs: 1

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|daemon|10|10|0|0|0.623|0.671|2.000|0.671|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|L|daemon|daemon|10|10|0|0|0.652|0.739|2.000|0.739|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|L|daemon|daemon|10|10|0|0|1.066|1.145|2.000|1.145|PASS|PASS|NO|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|PASS|15|15|0|0|expectation対象行のみ|
|Exact negative|PASS|15|15|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|30|30|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|30|30|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|PASS|30|30|0|0|fallback後の成功と分離|
|final user-visible成功率|PASS|30|30|0|0|fallback後の最終結果|
|fallback rate|PASS|30|30|0|0|clean daemon runでは0|
|daemon build identity|PASS|30|30|0|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|PASS|30|30|0|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|30|30|0|0|limit=0.000%|
|latency_p95_slo|PASS|3|3|0|0|profile別target、low-N cells=0|
|hard_latency_limit|PASS|30|30|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|30|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|30|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|PASS|15|15|0|0|通常contexts空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|30|30|0|
|stdout JSON purity|30|30|0|
|Exact negative collision|15|15|0|
|Expected unmatched identifier|15|15|0|
|Exact positive candidate|15|15|0|
|Matched identifier + raw occurrence|15|15|0|
|No-hit contract|15|15|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC-EXACT-N-005|rfc-full-20k-rag|L|daemon|1.145|partial|rfc10026.txt|
|RFC-EXACT-N-003|rfc-full-20k-rag|L|daemon|1.119|partial|rfc10004.txt|
|RFC-EXACT-N-004|rfc-full-20k-rag|L|daemon|1.108|partial|rfc10005.txt|
|RFC-EXACT-P-005|rfc-full-20k-rag|L|daemon|1.070|ok|rfc10026.txt|
|RFC-EXACT-P-001|rfc-full-20k-rag|L|daemon|1.069|ok|rfc10002.txt|
|RFC-EXACT-N-002|rfc-full-20k-rag|L|daemon|1.066|partial|rfc10003.txt|
|RFC-EXACT-P-004|rfc-full-20k-rag|L|daemon|1.065|ok|rfc10005.txt|
|RFC-EXACT-N-001|rfc-full-20k-rag|L|daemon|1.063|partial|rfc10002.txt|
|RFC-EXACT-P-002|rfc-full-20k-rag|L|daemon|1.031|ok|rfc10003.txt|
|RFC-EXACT-P-003|rfc-full-20k-rag|L|daemon|0.998|ok|rfc10004.txt|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

