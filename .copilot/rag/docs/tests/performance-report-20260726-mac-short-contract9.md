# RAG性能評価ダッシュボード 20260726-mac-short-contract9

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
|1|Current compatible run|9ec0213cc4|cd82614c79|seq=default, explain=False, diag=basic, pure=False|9|9|0|5.082|INSUFFICIENT_N|PASS|

## Run 1: Current compatible run

- source/run_id: 20260726-mac-short-contract9
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: cd82614c79caf12bd888aa193d5633238dbd0dfbf8955c6580bad1e52e69a860
- daemon_code_fingerprint_expected: dce1860d47f74e6b655454931b06de16a8e549984dc7aaf56543759cc70a5751
- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- db_hash/db_snapshot_hash: ac-rag=6b7bb428e1/f26efbb61f, incident-rag=7e3858deb3/f825401fbe, rfc-full-20k-rag=c7f4fba2fc/7c2a9d6f17
- explain_enabled: False
- diagnostics_level: basic
- identifier_diagnostics_enabled: True
- identifier_diagnostics_requested: True
- pure_profile: False
- sequence_plan: default
- timeout_seconds: 15.0
- daemon_attempt_timeout_seconds: 5.0
- daemon_fallback_policy: off
- case_spec_fingerprint: 88f091a627707ef46de8a9e52f4a828156376f83bf4322a9ef42137a1e61fa59
- mixed_total/seed/time_buckets: 500/20260726/10
- warmup_runs: 0

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|daemon|3|3|0|0|3.383|4.888|2.000|4.888|INSUFFICIENT_N|PASS|YES|
|incident-rag|7e3858deb3|f825401fbe|L|daemon|daemon|3|3|0|0|2.558|4.849|2.000|4.849|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|L|daemon|daemon|3|3|0|0|3.214|5.082|2.000|5.082|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|PASS|3|3|0|0|expectation対象行のみ|
|Exact negative|PASS|6|6|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|9|9|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|9|9|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|PASS|9|9|0|0|fallback後の成功と分離|
|final user-visible成功率|PASS|9|9|0|0|fallback後の最終結果|
|fallback rate|PASS|9|9|0|0|clean daemon runでは0|
|daemon build identity|PASS|9|9|0|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|PASS|9|9|0|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|9|9|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|3|0|0|0|profile別target、low-N cells=3|
|hard_latency_limit|PASS|9|9|0|0|max limit=15.000 sec|
|outer deadline adherence|PASS|9|9|0|0|成功・失敗を問わずwall time ≤ 15.000 sec|
|time degradation|NOT_APPLICABLE|9|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|9|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|PASS|6|6|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|9|9|0|
|stdout JSON purity|9|9|0|
|Exact negative collision|6|6|0|
|Expected unmatched identifier|6|6|0|
|Exact positive candidate|3|3|0|
|Matched identifier + raw occurrence|3|3|0|
|No-hit contract|6|6|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|MAC-RFC-NOHIT|rfc-full-20k-rag|L|daemon|5.082|partial|rfc10002.txt|
|MAC-AC-NOHIT|ac-rag|L|daemon|4.888|partial|sample_global_cooling_temperature_memo.docx|
|MAC-INC-EXACT-P|incident-rag|L|daemon|4.849|ok|ntsb_aviation_report_67438.pdf|
|MAC-AC-EXACT-N|ac-rag|L|daemon|3.383|partial|iea_2018_the_future_of_cooling.pdf|
|MAC-RFC-EXACT-P|rfc-full-20k-rag|L|daemon|3.214|ok|rfc10002.txt|
|MAC-RFC-EXACT-N|rfc-full-20k-rag|L|daemon|2.961|partial|rfc10002.txt|
|MAC-INC-EXACT-N|incident-rag|L|daemon|2.558|partial|ntsb_aviation_report_67438.pdf|
|MAC-AC-EXACT-P|ac-rag|L|daemon|2.275|ok|SOURCES.md|
|MAC-INC-NOHIT|incident-rag|L|daemon|1.828|partial|ntsb_aviation_report_67438.pdf|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

