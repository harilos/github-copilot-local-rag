# RAG性能評価ダッシュボード 20260726-exact-positive-smoke

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
|1|Current compatible run|9ec0213cc4|8dcc5c0389|seq=default, explain=False, diag=basic, pure=False|1|1|0|0.748|PASS|PASS|

## Run 1: Current compatible run

- source/run_id: 20260726-exact-positive-smoke
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: 8dcc5c0389406c220603eaf843a9ea7a2f3e6fd80a43fe2bcf7243a3b5f8f65b
- daemon_code_fingerprint_expected: 97d35c1c8bfe6c7a82017ba2e5e0fbafbb57824e7a4ab11466c9c62ed9773fe3
- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- db_hash/db_snapshot_hash: ac-rag=6b7bb428e1/f26efbb61f
- explain_enabled: False
- diagnostics_level: basic
- identifier_diagnostics_enabled: True
- pure_profile: False
- sequence_plan: default
- timeout_seconds: 30
- warmup_runs: 0

### latency by compatible cell

|DB|db hash|snapshot|profile|execution|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|1|1|0|0|0.748|0.748|2.000|0.748|PASS|PASS|NO|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|PASS|1|1|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|1|1|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|1|1|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|PASS|1|1|0|0|fallback後の成功と分離|
|daemon build identity|PASS|1|1|0|0|daemon応答code fingerprintと測定側を比較|
|daemon timeout rate|PASS|1|1|0|0|limit=0.000%|
|latency_p95_slo|PASS|1|0|0|0|profile別target、low-NはINSUFFICIENT_N|
|hard_latency_limit|PASS|1|1|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|1|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|1|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|1|1|0|
|stdout JSON purity|1|1|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|1|1|0|
|Matched identifier + raw occurrence|1|1|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC-EXACT-P-001|ac-rag|L|daemon|0.748|ok|SOURCES.md|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

