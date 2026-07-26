# RAG性能評価ダッシュボード 20260726-mac-short-postfix-coldwarm-incident

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
|1|Current pure-profile|9ec0213cc4|afcddd845d|seq=default, explain=False, diag=off, pure=True|2|2|0|0.457|INSUFFICIENT_N|PASS|

## Run 1: Current pure-profile

- source/run_id: 20260726-mac-short-postfix-coldwarm-incident
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: afcddd845d577dbcb07d4bd7878570c3e40573e84db3613ce6a03a5369f1743c
- daemon_code_fingerprint_expected: ba9b8b13f50d9aa1b136eb57b4eade6dcee610cd2f30fc024914bedf2cc0950c
- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- db_hash/db_snapshot_hash: incident-rag=7e3858deb3/f825401fbe
- explain_enabled: False
- diagnostics_level: off
- identifier_diagnostics_enabled: False
- identifier_diagnostics_requested: False
- pure_profile: True
- sequence_plan: default
- timeout_seconds: 15.0
- daemon_attempt_timeout_seconds: 5.0
- daemon_fallback_policy: off
- case_spec_fingerprint: ed65640f9248dff612673e55daa57658f9be7ce91fa7b9f9c32a9a94899b40db
- mixed_total/seed/time_buckets: 500/20260726/10
- warmup_runs: 1

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|incident-rag|7e3858deb3|f825401fbe|H|daemon|daemon|2|2|0|0|0.415|0.457|8.000|0.457|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|2|2|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|2|2|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|PASS|2|2|0|0|fallback後の成功と分離|
|final user-visible成功率|PASS|2|2|0|0|fallback後の最終結果|
|fallback rate|PASS|2|2|0|0|clean daemon runでは0|
|daemon build identity|PASS|2|2|0|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|PASS|2|2|0|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|2|2|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|1|0|0|0|profile別target、low-N cells=1|
|hard_latency_limit|PASS|2|2|0|0|max limit=15.000 sec|
|outer deadline adherence|PASS|2|2|0|0|成功・失敗を問わずwall time ≤ 15.000 sec|
|time degradation|NOT_APPLICABLE|2|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|2|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|2|2|0|
|stdout JSON purity|2|2|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|INC_SEM_001|incident-rag|H|daemon|0.457|ok|ntsb_aviation_report_67526.pdf|
|INC_SEM_001|incident-rag|H|daemon|0.415|ok|ntsb_aviation_report_67526.pdf|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

