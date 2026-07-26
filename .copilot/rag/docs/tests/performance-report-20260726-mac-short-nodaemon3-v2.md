# RAG性能評価ダッシュボード 20260726-mac-short-nodaemon3-v2

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
|NOT_RUN|0||0|||

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Current pure-profile|9ec0213cc4|a6c0d6f965|seq=default, explain=False, diag=off, pure=True|3|0|3||NOT_RUN|NOT_RUN|

## Run 1: Current pure-profile

- source/run_id: 20260726-mac-short-nodaemon3-v2
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: a6c0d6f96575d6df9a711770e84fc2a573893887fd7f40d2806cbd738f100762
- daemon_code_fingerprint_expected: dce1860d47f74e6b655454931b06de16a8e549984dc7aaf56543759cc70a5751
- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- db_hash/db_snapshot_hash: ac-rag=6b7bb428e1/f26efbb61f, incident-rag=7e3858deb3/f825401fbe, rfc-full-20k-rag=c7f4fba2fc/7c2a9d6f17
- explain_enabled: False
- diagnostics_level: off
- identifier_diagnostics_enabled: unknown
- identifier_diagnostics_requested: False
- pure_profile: True
- sequence_plan: default
- timeout_seconds: 15.0
- daemon_attempt_timeout_seconds: 5.0
- daemon_fallback_policy: off
- case_spec_fingerprint: 5f802c80fc0699e78f1a395189b2272cca80eefd4d55622018bb6bb95d59e331
- mixed_total/seed/time_buckets: 500/20260726/10
- warmup_runs: 0

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|H|no-daemon|unverified|1|0|1|1|||8.000||INSUFFICIENT_N|NOT_RUN|YES|
|incident-rag|7e3858deb3|f825401fbe|H|no-daemon|unverified|1|0|1|1|||8.000||INSUFFICIENT_N|NOT_RUN|YES|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|H|no-daemon|unverified|1|0|1|1|||8.000||INSUFFICIENT_N|NOT_RUN|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|NOT_RUN|0|0|0|0|完了行のみを分母にする|
|全検索の正常完了率|FAIL|3|0|3|3|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|NOT_RUN|0|0|0|0|fallback後の成功と分離|
|final user-visible成功率|NOT_RUN|0|0|0|0|fallback後の最終結果|
|fallback rate|NOT_RUN|0|0|0|0|clean daemon runでは0|
|daemon build identity|NOT_RUN|0|0|0|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|NOT_RUN|0|0|0|0|応答実値と要求値を照合|
|daemon timeout rate|NOT_RUN|0|0|0|0|limit=0.000%|
|latency_p95_slo|NOT_RUN|0|0|0|0|profile別target、low-N cells=0|
|hard_latency_limit|NOT_RUN|0|0|0|0|max limit=15.000 sec|
|outer deadline adherence|FAIL|3|0|3|3|成功・失敗を問わずwall time ≤ 15.000 sec|
|time degradation|NOT_APPLICABLE|0|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|0|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|FAIL|3|0|3|3|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|3|0|3|
|stdout JSON purity|3|0|3|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC_SEM_001|rfc-full-20k-rag|H|no-daemon|16.036|timeout||
|AC_SEM_001|ac-rag|H|no-daemon|16.019|timeout||
|INC_SEM_001|incident-rag|H|no-daemon|16.012|timeout||

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

