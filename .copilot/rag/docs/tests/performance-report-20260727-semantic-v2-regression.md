# RAG性能評価ダッシュボード 249f803-semantic-v2

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
|249f803-semantic-v2|10|ac-rag, incident-rag, rfc-full-20k-rag|0|0.095|0.102|

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Current compatible run|unknown|1274b518cb|seq=default, explain=False, diag=off, pure=False|90|90|0|0.831|INSUFFICIENT_N|PASS|

## Run 1: Current compatible run

- source/run_id: 249f803-semantic-v2
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: 1274b518cbc73455f157e003f19569c7445f5b9f14b040eaef8b5fa65b4dea1f
- daemon_code_fingerprint_expected: 501d94996312ae6bae2dfb7976a247b4410b76ffe4b32fad9865d3ff4bdff633
- OS: Windows-11-10.0.26200-SP0
- Python: 3.13.1
- db_hash/db_snapshot_hash: ac-rag=6b7bb428e1/811c6d556a, incident-rag=7e3858deb3/f825401fbe, rfc-full-20k-rag=c7f4fba2fc/7c2a9d6f17
- explain_enabled: False
- diagnostics_level: off
- identifier_diagnostics_enabled: unknown
- identifier_diagnostics_requested: False
- pure_profile: False
- sequence_plan: default
- timeout_seconds: 15.0
- daemon_attempt_timeout_seconds: 5.0
- daemon_fallback_policy: off
- case_spec_fingerprint: ac48d7c5223df09580adae8ef8b00ab629d95a86e5ac113894392757569cf1fc
- case_file_path: C:\Users\harilos\.copilot\rag\docs\tests\data\semantic-gold-v2.jsonl
- case_file_sha256: fdcf70a137d091d6b453495d7ce1d20b44d28f691177d3b8aaf9a9e27c56eafd
- mixed_total/seed/time_buckets: 500/20260726/10
- warmup_runs: 1

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|811c6d556a|H|daemon|daemon|10|10|0|0|0.368|0.514|8.000|0.514|INSUFFICIENT_N|PASS|YES|
|ac-rag|6b7bb428e1|811c6d556a|L|daemon|daemon|10|10|0|0|0.367|0.441|2.000|0.441|INSUFFICIENT_N|PASS|YES|
|ac-rag|6b7bb428e1|811c6d556a|V|daemon|daemon|10|10|0|0|0.374|0.514|8.000|0.514|INSUFFICIENT_N|PASS|YES|
|incident-rag|7e3858deb3|f825401fbe|H|daemon|daemon|10|10|0|0|0.367|0.559|8.000|0.559|INSUFFICIENT_N|PASS|YES|
|incident-rag|7e3858deb3|f825401fbe|L|daemon|daemon|10|10|0|0|0.341|0.554|2.000|0.554|INSUFFICIENT_N|PASS|YES|
|incident-rag|7e3858deb3|f825401fbe|V|daemon|daemon|10|10|0|0|0.341|0.503|8.000|0.503|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|H|daemon|daemon|10|10|0|0|0.527|0.831|8.000|0.831|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|L|daemon|daemon|10|10|0|0|0.407|0.626|2.000|0.626|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|V|daemon|daemon|10|10|0|0|0.448|0.595|8.000|0.595|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|90|90|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|90|90|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|PASS|90|90|0|0|fallback後の成功と分離|
|final user-visible成功率|PASS|90|90|0|0|fallback後の最終結果|
|fallback rate|PASS|90|90|0|0|clean daemon runでは0|
|daemon build identity|PASS|90|90|0|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|UNVERIFIED|90|0|90|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|90|90|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|9|0|0|0|profile別target、low-N cells=9|
|hard_latency_limit|PASS|90|90|0|0|max limit=15.000 sec|
|outer deadline adherence|PASS|90|90|0|0|成功・失敗を問わずwall time ≤ 15.000 sec|
|time degradation|NOT_APPLICABLE|90|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|90|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|90|90|0|
|stdout JSON purity|90|90|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC_V2_009|rfc-full-20k-rag|H|daemon|0.831|ok|rfc9924.txt|
|RFC_V2_010|rfc-full-20k-rag|H|daemon|0.808|partial|rfc9944.txt|
|RFC_V2_004|rfc-full-20k-rag|H|daemon|0.642|ok|rfc9740.txt|
|RFC_V2_009|rfc-full-20k-rag|L|daemon|0.626|partial|rfc9924.txt|
|RFC_V2_002|rfc-full-20k-rag|H|daemon|0.617|ok|rfc9644.txt|
|RFC_V2_009|rfc-full-20k-rag|V|daemon|0.595|ok|rfc9924.txt|
|RFC_V2_002|rfc-full-20k-rag|L|daemon|0.559|ok|rfc9644.txt|
|INC_V2_010|incident-rag|H|daemon|0.559|ok|ntsb_aviation_report_67648.pdf|
|RFC_V2_008|rfc-full-20k-rag|H|daemon|0.557|ok|rfc9892.txt|
|INC_V2_010|incident-rag|L|daemon|0.554|ok|ntsb_aviation_report_67648.pdf|

### Semantic gold

|profile|N|Hit@5|Document Recall|Claim Chunk Recall|Authoritative Evidence Recall|
|--|--:|--:|--:|--:|--:|
|H|30|0.3|0.0|0.0|0.28888888888888886|
|L|30|0.13333333333333333|0.0|0.0|0.09444444444444444|
|V|30|0.13333333333333333|0.0|0.0|0.13333333333333333|

- comparable H/L cases: 30
- Vector Rescue Rate: 0.19230769230769232 (5/26 L misses)
- Vector Harm Rate: 0.0 (0/4 L hits)
