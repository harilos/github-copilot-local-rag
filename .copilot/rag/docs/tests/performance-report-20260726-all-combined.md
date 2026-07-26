# RAG performance detailed reports — combined

Generated: 2026-07-26

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-final-diagnostic.md`

# RAG性能評価ダッシュボード 20260726-final-diagnostic

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
|performance-results-20260726-053700-clean.jsonl|10|ac-rag, incident-rag, rfc-full-20k-rag|0|0.045|0.093|
|performance-results-20260726-clean-mixed-v2.jsonl|10|ac-rag, incident-rag, rfc-full-20k-rag|0|0.031|0.035|

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Historical baseline|unknown|unknown|seq=legacy, explain=unknown, diag=unknown, pure=unknown|111|109|2|64.800|INSUFFICIENT_N|FAIL|
|2|Historical baseline|unknown|unknown|seq=legacy, explain=unknown, diag=unknown, pure=unknown|9|6|3|8.515|INSUFFICIENT_N|PASS|
|3|Current pure-profile (identity incomplete)|unknown|unknown|seq=default, explain=False, diag=off, pure=True|30|30|0|6.183|INSUFFICIENT_N|PASS|
|4|Profile transition (identity incomplete)|unknown|unknown|seq=profile-transition, explain=False, diag=off, pure=True|30|30|0|7.360|INSUFFICIENT_N|PASS|
|5|DB switch (identity incomplete)|unknown|unknown|seq=db-switch, explain=False, diag=off, pure=True|20|20|0|1.569|INSUFFICIENT_N|PASS|
|6|Explain comparison (identity incomplete)|unknown|unknown|seq=explain-compare, explain=False, diag=basic, pure=False|10|10|0|7.196|INSUFFICIENT_N|PASS|
|7|Explain comparison (identity incomplete)|unknown|unknown|seq=explain-compare, explain=True, diag=basic, pure=False|10|10|0|7.418|INSUFFICIENT_N|PASS|
|8|Current clean mixed|9ec0213cc4|2e2bb9a5b0|seq=clean-mixed, explain=False, diag=off, pure=True|500|500|0|1.219|PASS|PASS|
|9|Current compatible run|9ec0213cc4|78eeda2d49|seq=default, explain=False, diag=basic, pure=False|30|30|0|1.080|INSUFFICIENT_N|PASS|

## Run 1: Historical baseline

- source/run_id: performance-results-20260726-051622.jsonl
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: unknown
- daemon_code_fingerprint_expected: unknown
- OS: unknown
- Python: unknown
- db_hash/db_snapshot_hash: ac-rag=unknown/unknown, incident-rag=unknown/unknown, rfc-full-20k-rag=unknown/unknown
- explain_enabled: unknown
- diagnostics_level: unknown
- identifier_diagnostics_enabled: unknown
- identifier_diagnostics_requested: unknown
- pure_profile: unknown
- sequence_plan: legacy
- timeout_seconds: unknown
- daemon_attempt_timeout_seconds: unknown
- daemon_fallback_policy: unknown
- case_spec_fingerprint: unknown
- mixed_total/seed/time_buckets: unknown/unknown/unknown
- warmup_runs: unknown

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|unknown|unknown|H|daemon|unverified|12|12|0|0|6.590|23.906|8.000|23.906|INSUFFICIENT_N|FAIL|YES|
|ac-rag|unknown|unknown|H|no-daemon|unverified|4|4|0|0|5.045|5.792|8.000|5.792|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|L|daemon|unverified|12|12|0|0|0.961|49.186|2.000|49.186|INSUFFICIENT_N|FAIL|YES|
|ac-rag|unknown|unknown|L|no-daemon|unverified|4|4|0|0|0.901|1.150|2.000|1.150|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|V|daemon|unverified|12|11|1|1|7.223|12.577|8.000|12.577|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|V|no-daemon|unverified|4|4|0|0|4.843|5.230|8.000|5.230|INSUFFICIENT_N|PASS|YES|
|incident-rag|unknown|unknown|H|daemon|unverified|9|8|1|1|5.821|10.052|8.000|10.052|INSUFFICIENT_N|PASS|YES|
|incident-rag|unknown|unknown|H|no-daemon|unverified|3|3|0|0|4.854|4.854|8.000|4.854|INSUFFICIENT_N|PASS|YES|
|incident-rag|unknown|unknown|L|daemon|unverified|9|9|0|0|0.852|64.800|2.000|64.800|INSUFFICIENT_N|FAIL|YES|
|incident-rag|unknown|unknown|L|no-daemon|unverified|3|3|0|0|0.901|0.947|2.000|0.947|INSUFFICIENT_N|PASS|YES|
|incident-rag|unknown|unknown|V|daemon|unverified|9|9|0|0|6.161|9.213|8.000|9.213|INSUFFICIENT_N|PASS|YES|
|incident-rag|unknown|unknown|V|no-daemon|unverified|3|3|0|0|4.732|4.908|8.000|4.908|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|unknown|unknown|H|daemon|unverified|9|9|0|0|1.454|9.624|8.000|9.624|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|unknown|unknown|L|daemon|unverified|9|9|0|0|0.873|1.359|2.000|1.359|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|unknown|unknown|V|daemon|unverified|9|9|0|0|6.736|12.858|8.000|12.858|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|109|109|0|0|完了行のみを分母にする|
|全検索の正常完了率|FAIL|111|109|2|2|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|90|88|2|2|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|90|88|2|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|90|90|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|90|0|90|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|UNVERIFIED|109|0|109|0|応答実値と要求値を照合|
|daemon timeout rate|FAIL|90|88|2|2|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|9|0|0|0|profile別target、low-N cells=9|
|hard_latency_limit|FAIL|88|84|4|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|90|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|90|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|PASS|21|21|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|111|109|2|
|stdout JSON purity|111|109|2|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|V|daemon|193.393|timeout||
|INC_EXACT_001|incident-rag|H|daemon|186.949|timeout||
|INC_EXACT_001|incident-rag|L|daemon|64.800|ok|ntsb_aviation_report_67438.pdf|
|AC_BROAD_001|ac-rag|L|daemon|49.186|ok|sample_global_cooling_temperature_memo.docx|
|AC_SEM_001|ac-rag|H|daemon|23.906|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|22.436|ok|sample_global_cooling_temperature_memo.docx|
|RFC_BROAD_001|rfc-full-20k-rag|V|daemon|12.858|ok|rfc9743.txt|
|AC_EXACT_LOWDF_001|ac-rag|V|daemon|12.577|ok|lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf|
|AC_SEM_001|ac-rag|V|daemon|10.947|ok|sample_global_cooling_temperature_memo.docx|
|AC_EXACT_NEG_COLLISION_001|ac-rag|H|daemon|10.576|partial|sample_global_cooling_temperature_memo.docx|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

## Run 2: Historical baseline

- source/run_id: performance-results-20260726-053700-clean.jsonl
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: unknown
- daemon_code_fingerprint_expected: unknown
- OS: unknown
- Python: unknown
- db_hash/db_snapshot_hash: ac-rag=unknown/unknown
- explain_enabled: unknown
- diagnostics_level: unknown
- identifier_diagnostics_enabled: unknown
- identifier_diagnostics_requested: unknown
- pure_profile: unknown
- sequence_plan: legacy
- timeout_seconds: unknown
- daemon_attempt_timeout_seconds: unknown
- daemon_fallback_policy: unknown
- case_spec_fingerprint: unknown
- mixed_total/seed/time_buckets: unknown/unknown/unknown
- warmup_runs: unknown

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|unknown|unknown|H|daemon|unverified|3|2|1|1|1.043|1.068|8.000|1.068|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|L|daemon|unverified|3|2|1|1|0.833|1.214|2.000|1.214|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|V|daemon|unverified|3|2|1|1|7.833|8.515|8.000|8.515|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|6|6|0|0|完了行のみを分母にする|
|全検索の正常完了率|FAIL|9|6|3|3|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|9|6|3|3|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|9|6|3|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|9|9|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|9|0|9|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|UNVERIFIED|6|0|6|0|応答実値と要求値を照合|
|daemon timeout rate|FAIL|9|6|3|3|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|3|0|0|0|profile別target、low-N cells=3|
|hard_latency_limit|PASS|6|6|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|9|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|9|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|9|6|3|
|stdout JSON purity|9|6|3|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_SEM_001|ac-rag|V|daemon|202.485|timeout||
|AC_SEM_001|ac-rag|L|daemon|185.520|timeout||
|AC_SEM_001|ac-rag|H|daemon|185.147|timeout||
|AC_EXACT_NEG_COLLISION_001|ac-rag|V|daemon|8.515|partial|lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf|
|AC_EXACT_LOWDF_001|ac-rag|V|daemon|7.833|ok|lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf|
|AC_EXACT_LOWDF_001|ac-rag|L|daemon|1.214|ok|jraia_2025_inverter_refrigerant_ratio.pdf|
|AC_EXACT_NEG_COLLISION_001|ac-rag|H|daemon|1.068|partial|sample_global_cooling_temperature_memo.docx|
|AC_EXACT_LOWDF_001|ac-rag|H|daemon|1.043|ok|jraia_2025_inverter_refrigerant_ratio.pdf|
|AC_EXACT_NEG_COLLISION_001|ac-rag|L|daemon|0.833|partial|sample_global_cooling_temperature_memo.docx|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

## Run 3: Current pure-profile (identity incomplete)

- source/run_id: performance-results-20260726-daemon-pure-ac.jsonl
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: unknown
- daemon_code_fingerprint_expected: unknown
- OS: unknown
- Python: unknown
- db_hash/db_snapshot_hash: ac-rag=unknown/unknown
- explain_enabled: False
- diagnostics_level: off
- identifier_diagnostics_enabled: False
- identifier_diagnostics_requested: unknown
- pure_profile: True
- sequence_plan: default
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: unknown
- daemon_fallback_policy: unknown
- case_spec_fingerprint: unknown
- mixed_total/seed/time_buckets: unknown/unknown/unknown
- warmup_runs: unknown

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|unknown|unknown|H|daemon|unverified|10|10|0|0|4.445|6.101|8.000|6.101|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|L|daemon|unverified|10|10|0|0|0.669|0.848|2.000|0.848|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|V|daemon|unverified|10|10|0|0|4.514|6.183|8.000|6.183|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|30|30|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|30|30|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|30|30|0|0|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|30|30|0|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|30|30|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|30|0|30|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|FAIL|30|0|30|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|30|30|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|3|0|0|0|profile別target、low-N cells=3|
|hard_latency_limit|PASS|30|30|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|30|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|30|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|30|30|0|
|stdout JSON purity|30|30|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|V|daemon|6.183|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|H|daemon|6.101|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|V|daemon|5.600|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|5.334|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|H|daemon|5.206|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.067|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.044|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|V|daemon|4.813|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|4.610|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|4.514|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

## Run 4: Profile transition (identity incomplete)

- source/run_id: performance-results-20260726-daemon-transition-ac.jsonl
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: unknown
- daemon_code_fingerprint_expected: unknown
- OS: unknown
- Python: unknown
- db_hash/db_snapshot_hash: ac-rag=unknown/unknown
- explain_enabled: False
- diagnostics_level: off
- identifier_diagnostics_enabled: False
- identifier_diagnostics_requested: unknown
- pure_profile: True
- sequence_plan: profile-transition
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: unknown
- daemon_fallback_policy: unknown
- case_spec_fingerprint: unknown
- mixed_total/seed/time_buckets: unknown/unknown/unknown
- warmup_runs: unknown

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|unknown|unknown|H|daemon|unverified|5|5|0|0|5.432|5.566|8.000|5.566|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|L|daemon|unverified|15|15|0|0|0.746|0.943|2.000|0.943|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|V|daemon|unverified|10|10|0|0|5.306|7.360|8.000|7.360|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|30|30|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|30|30|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|30|30|0|0|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|30|30|0|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|30|30|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|30|0|30|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|FAIL|30|0|30|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|30|30|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|3|0|0|0|profile別target、low-N cells=3|
|hard_latency_limit|PASS|30|30|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|30|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|30|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|30|30|0|
|stdout JSON purity|30|30|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

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

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

## Run 5: DB switch (identity incomplete)

- source/run_id: performance-results-20260726-daemon-dbswitch-l.jsonl
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: unknown
- daemon_code_fingerprint_expected: unknown
- OS: unknown
- Python: unknown
- db_hash/db_snapshot_hash: ac-rag=unknown/unknown, incident-rag=unknown/unknown, rfc-full-20k-rag=unknown/unknown
- explain_enabled: False
- diagnostics_level: off
- identifier_diagnostics_enabled: False
- identifier_diagnostics_requested: unknown
- pure_profile: True
- sequence_plan: db-switch
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: unknown
- daemon_fallback_policy: unknown
- case_spec_fingerprint: unknown
- mixed_total/seed/time_buckets: unknown/unknown/unknown
- warmup_runs: unknown

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|unknown|unknown|L|daemon|unverified|10|10|0|0|0.742|0.804|2.000|0.804|INSUFFICIENT_N|PASS|YES|
|incident-rag|unknown|unknown|L|daemon|unverified|5|5|0|0|0.927|1.168|2.000|1.168|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|unknown|unknown|L|daemon|unverified|5|5|0|0|1.290|1.569|2.000|1.569|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|20|20|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|20|20|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|20|20|0|0|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|20|20|0|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|20|20|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|20|0|20|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|FAIL|20|0|20|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|20|20|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|3|0|0|0|profile別target、low-N cells=3|
|hard_latency_limit|PASS|20|20|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|20|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|20|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|20|20|0|
|stdout JSON purity|20|20|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.569|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.446|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.290|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.277|ok|rfc10026.txt|
|INC_EXACT_001|incident-rag|L|daemon|1.168|ok|ntsb_aviation_report_67438.pdf|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.166|ok|rfc10026.txt|
|INC_EXACT_001|incident-rag|L|daemon|1.049|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.927|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.872|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.867|ok|ntsb_aviation_report_67438.pdf|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

## Run 6: Explain comparison (identity incomplete)

- source/run_id: performance-results-20260726-daemon-explain-ac.jsonl
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: unknown
- daemon_code_fingerprint_expected: unknown
- OS: unknown
- Python: unknown
- db_hash/db_snapshot_hash: ac-rag=unknown/unknown
- explain_enabled: False
- diagnostics_level: basic
- identifier_diagnostics_enabled: True
- identifier_diagnostics_requested: unknown
- pure_profile: False
- sequence_plan: explain-compare
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: unknown
- daemon_fallback_policy: unknown
- case_spec_fingerprint: unknown
- mixed_total/seed/time_buckets: unknown/unknown/unknown
- warmup_runs: unknown

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|unknown|unknown|H|daemon|unverified|5|5|0|0|6.809|7.196|8.000|7.196|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|L|daemon|unverified|5|5|0|0|0.875|1.007|2.000|1.007|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|10|10|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|10|10|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|10|10|0|0|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|10|10|0|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|10|10|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|10|0|10|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|FAIL|10|0|10|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|10|10|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|2|0|0|0|profile別target、low-N cells=2|
|hard_latency_limit|PASS|10|10|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|10|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|10|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|10|10|0|
|stdout JSON purity|10|10|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|H|daemon|7.196|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.825|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.809|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.589|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.891|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|1.007|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|1.003|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|0.875|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|0.719|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|0.630|ok|sample_global_cooling_temperature_memo.docx|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

## Run 7: Explain comparison (identity incomplete)

- source/run_id: performance-results-20260726-daemon-explain-ac.jsonl
- git_commit: unknown
- git_dirty: unknown
- worktree_fingerprint: unknown
- daemon_code_fingerprint_expected: unknown
- OS: unknown
- Python: unknown
- db_hash/db_snapshot_hash: ac-rag=unknown/unknown
- explain_enabled: True
- diagnostics_level: basic
- identifier_diagnostics_enabled: True
- identifier_diagnostics_requested: unknown
- pure_profile: False
- sequence_plan: explain-compare
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: unknown
- daemon_fallback_policy: unknown
- case_spec_fingerprint: unknown
- mixed_total/seed/time_buckets: unknown/unknown/unknown
- warmup_runs: unknown

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|unknown|unknown|H|daemon|unverified|5|5|0|0|6.654|7.418|8.000|7.418|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|L|daemon|unverified|5|5|0|0|0.867|1.074|2.000|1.074|INSUFFICIENT_N|PASS|YES|

### separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact positive|NOT_RUN|0|0|0|0|expectation対象行のみ|
|Exact negative|NOT_RUN|0|0|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|10|10|0|0|完了行のみを分母にする|
|全検索の正常完了率|PASS|10|10|0|0|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|10|10|0|0|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|10|10|0|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|10|10|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|10|0|10|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|FAIL|10|0|10|0|応答実値と要求値を照合|
|daemon timeout rate|PASS|10|10|0|0|limit=0.000%|
|latency_p95_slo|INSUFFICIENT_N|2|0|0|0|profile別target、low-N cells=2|
|hard_latency_limit|PASS|10|10|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|10|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|10|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts/evidence/results空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|10|10|0|
|stdout JSON purity|10|10|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|
|Matched identifier + raw occurrence|0|0|0|
|No-hit contract|0|0|0|

### slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|H|daemon|7.418|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|7.210|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.654|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.156|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.645|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|1.074|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|0.945|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|0.867|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|0.718|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|L|daemon|0.698|ok|sample_global_cooling_temperature_memo.docx|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)

## Run 8: Current clean mixed

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

## Run 9: Current compatible run

- source/run_id: performance-results-20260726-exact-v3.jsonl
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: 78eeda2d491a72abe2ee580b188bffc1dacba45bbbfa51e7e523bed794491fcc
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
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: 15.0
- daemon_fallback_policy: off
- case_spec_fingerprint: 5c450417213045388717c4b24a584a9d409fbb9d67620335f757a3585e5ed017
- mixed_total/seed/time_buckets: 500/20260726/10
- warmup_runs: 1

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|daemon|10|10|0|0|0.647|0.678|2.000|0.678|INSUFFICIENT_N|PASS|YES|
|incident-rag|7e3858deb3|f825401fbe|L|daemon|daemon|10|10|0|0|0.639|0.695|2.000|0.695|INSUFFICIENT_N|PASS|YES|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|L|daemon|daemon|10|10|0|0|0.675|1.080|2.000|1.080|INSUFFICIENT_N|PASS|YES|

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
|latency_p95_slo|INSUFFICIENT_N|3|0|0|0|profile別target、low-N cells=3|
|hard_latency_limit|PASS|30|30|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|30|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|30|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|PASS|15|15|0|0|通常contexts/evidence/results空・related_context隔離|
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
|RFC-EXACT-P-001|rfc-full-20k-rag|L|daemon|1.080|ok|rfc10002.txt|
|RFC-EXACT-N-001|rfc-full-20k-rag|L|daemon|0.957|partial|rfc10002.txt|
|RFC-EXACT-P-003|rfc-full-20k-rag|L|daemon|0.718|ok|rfc10004.txt|
|INC-EXACT-P-004|incident-rag|L|daemon|0.695|ok|ntsb_aviation_report_67441.pdf|
|RFC-EXACT-N-003|rfc-full-20k-rag|L|daemon|0.692|partial|rfc10004.txt|
|INC-EXACT-N-001|incident-rag|L|daemon|0.689|partial|ntsb_aviation_report_67438.pdf|
|RFC-EXACT-P-005|rfc-full-20k-rag|L|daemon|0.687|ok|rfc10026.txt|
|INC-EXACT-P-001|incident-rag|L|daemon|0.686|ok|ntsb_aviation_report_67438.pdf|
|AC-EXACT-N-004|ac-rag|L|daemon|0.678|partial|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|RFC-EXACT-N-002|rfc-full-20k-rag|L|daemon|0.675|partial|rfc10003.txt|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)


---

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-rejudged.md`

# RAG性能評価レポート 20260726-rejudged

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: H, L, V
- sequence_plan: default
- explain_mode: on
- diagnostics_level: basic
- pure_profile: False
- daemon repeats: 3
- no-daemon repeats: 1
- warmup_runs: 1
- timeout_seconds: 180
- daemon_slo_p95: 15.0
- min_samples_for_p95: 20
- budget_tokens: 1200
- max_chars: 1200

## list_dbs

- repeats: 10
- dbs: ac-rag, incident-rag, rfc-full-20k-rag
- JSON errors: 0
- p50: 0.029 sec
- p95: 0.031 sec

## latency by DB / profile / execution

|DB|profile|execution|N|success|timeout|errors|p50 sec|p95 sec|max sec|p95判定|
|--|--|--|--:|--:|--:|--:|--:|--:|--:|--|
|ac-rag|H|daemon|12|12|0|0|6.590|23.906|23.906|low-N(<20)|
|ac-rag|H|no-daemon|4|4|0|0|5.045|5.792|5.792|low-N(<20)|
|ac-rag|L|daemon|12|12|0|0|0.961|49.186|49.186|low-N(<20)|
|ac-rag|L|no-daemon|4|4|0|0|0.901|1.150|1.150|low-N(<20)|
|ac-rag|V|daemon|12|11|1|1|7.223|12.577|12.577|low-N(<20)|
|ac-rag|V|no-daemon|4|4|0|0|4.843|5.230|5.230|low-N(<20)|
|incident-rag|H|daemon|9|8|1|1|5.821|10.052|10.052|low-N(<20)|
|incident-rag|H|no-daemon|3|3|0|0|4.854|4.854|4.854|low-N(<20)|
|incident-rag|L|daemon|9|9|0|0|0.852|64.800|64.800|low-N(<20)|
|incident-rag|L|no-daemon|3|3|0|0|0.901|0.947|0.947|low-N(<20)|
|incident-rag|V|daemon|9|9|0|0|6.161|9.213|9.213|low-N(<20)|
|incident-rag|V|no-daemon|3|3|0|0|4.732|4.908|4.908|low-N(<20)|
|rfc-full-20k-rag|H|daemon|9|9|0|0|1.454|9.624|9.624|low-N(<20)|
|rfc-full-20k-rag|H|no-daemon|3|3|0|0|4.313|4.634|4.634|low-N(<20)|
|rfc-full-20k-rag|L|daemon|9|9|0|0|0.873|1.359|1.359|low-N(<20)|
|rfc-full-20k-rag|L|no-daemon|3|3|0|0|0.852|0.970|0.970|low-N(<20)|
|rfc-full-20k-rag|V|daemon|9|9|0|0|6.736|12.858|12.858|low-N(<20)|
|rfc-full-20k-rag|V|no-daemon|3|3|0|0|4.297|4.481|4.481|low-N(<20)|

## separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact修正のpilot確認|PASS|20|20|0|0|A2W negativeとA2L positiveを分離|
|完了した処理のJSON純度|PASS|118|118|0|0|timeoutはJSON破損ではなく未完了として別計上|
|全検索の正常完了率|FAIL|120|118|2|2|exit 0かつJSON parse成功のみ成功|
|daemon正常完了率|FAIL|90|88|2|2|daemon経路だけを分離|
|daemon latency SLO|FAIL|88|84|4|0|p95=12.858 sec / gate=15.000 sec。1件でもgate超過ならFAIL|
|no-daemon機能smoke|PASS|30|30|0|0|daemon問題との切り分け用|
|Vector有効性|未評価|0|0|0|0|gold span付きsemantic set未実行|
|no-hit応答品質|未確定|0|0|0|0|related_context隔離など最終契約が未固定|
|最終リリース|FAIL|0|0|0|0|Windows、soak、Vector、no-hit契約まで未充足|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|120|118|2|
|stdout JSON purity|120|118|2|
|Exact negative collision|12|12|0|
|Expected unmatched identifier|12|12|0|
|Exact positive candidate|24|23|1|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|V|daemon|193.393|timeout||
|INC_EXACT_001|incident-rag|H|daemon|186.949|timeout||
|INC_EXACT_001|incident-rag|L|daemon|64.800|ok|ntsb_aviation_report_67438.pdf|
|AC_BROAD_001|ac-rag|L|daemon|49.186|ok|sample_global_cooling_temperature_memo.docx|
|AC_SEM_001|ac-rag|H|daemon|23.906|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|22.436|ok|sample_global_cooling_temperature_memo.docx|
|RFC_BROAD_001|rfc-full-20k-rag|V|daemon|12.858|ok|rfc9743.txt|
|AC_EXACT_LOWDF_001|ac-rag|V|daemon|12.577|ok|lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf|
|AC_SEM_001|ac-rag|V|daemon|10.947|ok|sample_global_cooling_temperature_memo.docx|
|AC_EXACT_NEG_COLLISION_001|ac-rag|H|daemon|10.576|partial|sample_global_cooling_temperature_memo.docx|


---

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-daemon-pure-ac.md`

# RAG性能評価レポート 20260726-daemon-pure-ac

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: L, V, H
- sequence_plan: default
- explain_mode: off
- diagnostics_level: off
- pure_profile: True
- daemon repeats: 10
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
|ac-rag|H|daemon|10|10|0|0|4.445|6.101|6.101|OK|
|ac-rag|L|daemon|10|10|0|0|0.669|0.848|0.848|OK|
|ac-rag|V|daemon|10|10|0|0|4.514|6.183|6.183|OK|

## separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact修正のpilot確認|FAIL|0|0|0|0|A2W negativeとA2L positiveを分離|
|完了した処理のJSON純度|PASS|30|30|0|0|timeoutはJSON破損ではなく未完了として別計上|
|全検索の正常完了率|PASS|30|30|0|0|exit 0かつJSON parse成功のみ成功|
|daemon正常完了率|PASS|30|30|0|0|daemon経路だけを分離|
|daemon latency SLO|PASS|30|30|0|0|p95=6.101 sec / gate=15.000 sec。1件でもgate超過ならFAIL|
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
|AC_BROAD_001|ac-rag|V|daemon|6.183|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|H|daemon|6.101|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|V|daemon|5.600|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|5.334|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|H|daemon|5.206|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.067|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.044|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|V|daemon|4.813|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|4.610|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|AC_BROAD_001|ac-rag|V|daemon|4.514|ok|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|


---

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-daemon-transition-ac.md`

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


---

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-daemon-dbswitch-l.md`

# RAG性能評価レポート 20260726-daemon-dbswitch-l

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: L
- sequence_plan: db-switch
- explain_mode: off
- diagnostics_level: off
- pure_profile: True
- daemon repeats: 5
- no-daemon repeats: 0
- warmup_runs: 1
- timeout_seconds: 30
- daemon_slo_p95: 15.0
- min_samples_for_p95: 5
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
|ac-rag|L|daemon|10|10|0|0|0.742|0.804|0.804|OK|
|incident-rag|L|daemon|5|5|0|0|0.927|1.168|1.168|OK|
|rfc-full-20k-rag|L|daemon|5|5|0|0|1.290|1.569|1.569|OK|

## separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact修正のpilot確認|FAIL|10|10|0|0|A2W negativeとA2L positiveを分離|
|完了した処理のJSON純度|PASS|20|20|0|0|timeoutはJSON破損ではなく未完了として別計上|
|全検索の正常完了率|PASS|20|20|0|0|exit 0かつJSON parse成功のみ成功|
|daemon正常完了率|PASS|20|20|0|0|daemon経路だけを分離|
|daemon latency SLO|PASS|20|20|0|0|p95=1.446 sec / gate=15.000 sec。1件でもgate超過ならFAIL|
|no-daemon機能smoke|PASS|0|0|0|0|daemon問題との切り分け用|
|Vector有効性|未評価|0|0|0|0|gold span付きsemantic set未実行|
|no-hit応答品質|未確定|0|0|0|0|related_context隔離など最終契約が未固定|
|最終リリース|FAIL|0|0|0|0|Windows、soak、Vector、no-hit契約まで未充足|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|20|20|0|
|stdout JSON purity|20|20|0|
|Exact negative collision|10|10|0|
|Expected unmatched identifier|10|0|10|
|Exact positive candidate|10|10|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.569|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.446|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.290|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.277|ok|rfc10026.txt|
|INC_EXACT_001|incident-rag|L|daemon|1.168|ok|ntsb_aviation_report_67438.pdf|
|RFC_EXACT_001|rfc-full-20k-rag|L|daemon|1.166|ok|rfc10026.txt|
|INC_EXACT_001|incident-rag|L|daemon|1.049|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.927|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.872|ok|ntsb_aviation_report_67438.pdf|
|INC_EXACT_001|incident-rag|L|daemon|0.867|ok|ntsb_aviation_report_67438.pdf|


---

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-daemon-explain-ac.md`

# RAG性能評価レポート 20260726-daemon-explain-ac

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: L, H
- sequence_plan: explain-compare
- explain_mode: on
- diagnostics_level: basic
- pure_profile: False
- daemon repeats: 5
- no-daemon repeats: 0
- warmup_runs: 1
- timeout_seconds: 30
- daemon_slo_p95: 15.0
- min_samples_for_p95: 5
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
|ac-rag|H|daemon|10|10|0|0|6.654|7.418|7.418|OK|
|ac-rag|L|daemon|10|10|0|0|0.867|1.074|1.074|OK|

## separated gates

|Gate|判定|N|Pass|Fail|Timeout|理由|
|--|--|--:|--:|--:|--:|--|
|Exact修正のpilot確認|FAIL|0|0|0|0|A2W negativeとA2L positiveを分離|
|完了した処理のJSON純度|PASS|20|20|0|0|timeoutはJSON破損ではなく未完了として別計上|
|全検索の正常完了率|PASS|20|20|0|0|exit 0かつJSON parse成功のみ成功|
|daemon正常完了率|PASS|20|20|0|0|daemon経路だけを分離|
|daemon latency SLO|PASS|20|20|0|0|p95=7.210 sec / gate=15.000 sec。1件でもgate超過ならFAIL|
|no-daemon機能smoke|PASS|0|0|0|0|daemon問題との切り分け用|
|Vector有効性|未評価|0|0|0|0|gold span付きsemantic set未実行|
|no-hit応答品質|未確定|0|0|0|0|related_context隔離など最終契約が未固定|
|最終リリース|FAIL|0|0|0|0|Windows、soak、Vector、no-hit契約まで未充足|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|20|20|0|
|stdout JSON purity|20|20|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|AC_BROAD_001|ac-rag|H|daemon|7.418|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|7.210|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|7.196|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.825|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.809|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.654|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.589|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|6.156|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.891|ok|sample_global_cooling_temperature_memo.docx|
|AC_BROAD_001|ac-rag|H|daemon|5.645|ok|sample_global_cooling_temperature_memo.docx|


---

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-clean-mixed-v2.md`

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


---

## Included file: `.copilot/rag/docs/tests/performance-report-20260726-exact-v3.md`

# RAG性能評価ダッシュボード 20260726-exact-v3

異なるrun条件は統合せず、互換条件ごとのcellとして比較する。

## 判定基準

- H p95 target: 8.000 sec
- L p95 target: 2.000 sec
- V p95 target: 8.000 sec
- hard latency limit: 15.000 sec
- timeout rate limit: 0.000%
- p95 minimum N: 5

## list_dbs

list_dbs latency is shown per source/run and is never pooled.

|source/run|repeats|dbs|JSON errors|p50 sec|p95 sec|
|--|--:|--|--:|--:|--:|
|NOT_RUN|0||0|||

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Current compatible run|9ec0213cc4|78eeda2d49|seq=default, explain=False, diag=basic, pure=False|30|30|0|1.080|PASS|PASS|

## Run 1: Current compatible run

- source/run_id: 20260726-exact-v3
- git_commit: 9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa
- git_dirty: True
- worktree_fingerprint: 78eeda2d491a72abe2ee580b188bffc1dacba45bbbfa51e7e523bed794491fcc
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
- timeout_seconds: 30
- daemon_attempt_timeout_seconds: 15.0
- daemon_fallback_policy: off
- case_spec_fingerprint: 5c450417213045388717c4b24a584a9d409fbb9d67620335f757a3585e5ed017
- mixed_total/seed/time_buckets: 500/20260726/10
- warmup_runs: 1

### latency by compatible cell

|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|
|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|
|ac-rag|6b7bb428e1|f26efbb61f|L|daemon|daemon|10|10|0|0|0.647|0.678|2.000|0.678|PASS|PASS|NO|
|incident-rag|7e3858deb3|f825401fbe|L|daemon|daemon|10|10|0|0|0.639|0.695|2.000|0.695|PASS|PASS|NO|
|rfc-full-20k-rag|c7f4fba2fc|7c2a9d6f17|L|daemon|daemon|10|10|0|0|0.675|1.080|2.000|1.080|PASS|PASS|NO|

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
|time degradation|NOT_APPLICABLE|30|0|0|0|daemon first-attempt後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|30|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|PASS|15|15|0|0|通常contexts/evidence/results空・related_context隔離|
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
|RFC-EXACT-P-001|rfc-full-20k-rag|L|daemon|1.080|ok|rfc10002.txt|
|RFC-EXACT-N-001|rfc-full-20k-rag|L|daemon|0.957|partial|rfc10002.txt|
|RFC-EXACT-P-003|rfc-full-20k-rag|L|daemon|0.718|ok|rfc10004.txt|
|INC-EXACT-P-004|incident-rag|L|daemon|0.695|ok|ntsb_aviation_report_67441.pdf|
|RFC-EXACT-N-003|rfc-full-20k-rag|L|daemon|0.692|partial|rfc10004.txt|
|INC-EXACT-N-001|incident-rag|L|daemon|0.689|partial|ntsb_aviation_report_67438.pdf|
|RFC-EXACT-P-005|rfc-full-20k-rag|L|daemon|0.687|ok|rfc10026.txt|
|INC-EXACT-P-001|incident-rag|L|daemon|0.686|ok|ntsb_aviation_report_67438.pdf|
|AC-EXACT-N-004|ac-rag|L|daemon|0.678|partial|clasp_2026_doubling_energy_efficiency_room_air_conditioners.pdf|
|RFC-EXACT-N-002|rfc-full-20k-rag|L|daemon|0.675|partial|rfc10003.txt|

### Semantic gold

|profile|N|Hit@5|Context Recall@budget|
|--|--:|--:|--:|
|H|0|NOT_RUN|NOT_RUN|
|L|0|NOT_RUN|NOT_RUN|
|V|0|NOT_RUN|NOT_RUN|

- comparable H/L cases: 0
- Vector Rescue Rate: NOT_APPLICABLE (0/0 L misses)
- Vector Harm Rate: NOT_APPLICABLE (0/0 L hits)


---

## Included file: `.copilot/rag/docs/tests/release-candidate-test-plan.md`

# RAG v1 Release-Candidate Test Plan

## 2026-07-26 execution status

Completed on the current macOS build:

- Harness/contract regression tests: 30/30 PASS.
- Clean mixed: 500/500 first-attempt daemon success, timeout 0, fallback 0,
  one daemon generation, build identity 500/500.
- Clean mixed p95: ac H/L/V 0.697/0.839/0.852 seconds; incident
  0.862/0.835/0.696; rfc 1.035/1.095/0.715.
- Clean mixed maximum successful latency: 1.219 seconds.
- Clean mixed p95, hard maximum, and time degradation gates: PASS.
- Exact: positive 15/15, negative 15/15, expected unmatched 15/15,
  complete identifier plus raw occurrence 15/15.
- Strong-identifier no-hit contract: 15/15.
- Forced daemon timeout: first attempt timed out, one no-daemon fallback
  succeeded, the old generation retired, and the next required-daemon request
  started a new generation.

Artifacts:

- `performance-report-20260726-final-diagnostic.md`
- `performance-report-20260726-clean-mixed-v2.md`
- `performance-report-20260726-exact-v3.md`

Still not run:

- Human-authored Semantic gold 30 questions.
- Windows 48–54 request smoke.

The final v1 release state therefore remains `NOT_RUN`, not `FAIL`: completed
macOS daemon, Exact, no-hit, and fallback gates pass, while Semantic and
Windows gates remain outstanding.

## Test order

Run the suites in this order. A failed or unverified P0 gate stops the release
run; it does not invalidate historical evidence.

1. Unit and contract tests.
2. Exact 30 logical cases.
3. No-hit 15 cases.
4. Semantic gold 30 questions after human-authored spans are fixed.
5. Current-build clean mixed 500 requests.
6. Windows 48–54 request smoke.
7. One forced daemon-timeout fallback test.

Historical JSONL files are incident/baseline inputs. They never contribute to
the current-build release cohort.

## Current-build clean mixed

Product conditions:

- explain off
- diagnostics off
- identifier diagnostics off
- actual daemon required; cold fast path and synchronous fallback forbidden
- warmup 5, saved as excluded warmup rows
- timeout 30 seconds
- fixed seed 20260726
- 10 time buckets

The 500-request matrix slightly increases V so every DB/profile cell has at
least 20 observations:

| DB | H | L | V | Total |
|---|---:|---:|---:|---:|
| ac | 120 | 35 | 20 | 175 |
| incident | 100 | 30 | 20 | 150 |
| rfc | 120 | 35 | 20 | 175 |
| Total | 340 | 100 | 60 | 500 |

```bash
.copilot/rag/query/.venv/bin/python \
  .copilot/rag/docs/tests/run_performance_eval.py \
  --run-id current-clean-mixed-v1 \
  --sequence-plan clean-mixed \
  --profiles H L V \
  --executions daemon \
  --mixed-total 500 \
  --sequence-seed 20260726 \
  --time-buckets 10 \
  --warmup-runs 5 \
  --timeout 30 \
  --daemon-attempt-timeout 15 \
  --explain-mode off \
  --diagnostics-level off \
  --pure-profile \
  --min-samples-for-p95 20 \
  --p95-target-h 8 \
  --p95-target-l 2 \
  --p95-target-v 8 \
  --hard-latency-limit 15 \
  --timeout-rate-gate 0 \
  --restart-daemon
```

Acceptance:

- actual daemon first attempt: 500/500
- fallback/fast path: 0
- daemon generation changes: 0
- daemon code fingerprint equals the measured runtime fingerprint
- timeout: 0
- H/V cell p95 ≤ 8 seconds
- L cell p95 ≤ 2 seconds
- every successful response ≤ 15 seconds
- daemon first-attempt second-half/first-half profile p95 ratio ≤ 1.20 and
  second-half p95 remains under its absolute target

## Exact

Input: `data/exact-cases-v1.jsonl`

The file contains 5 positive and 5 verified-absent near-collision identifiers
per DB. Run L once for 30 logical observations:

```bash
.copilot/rag/query/.venv/bin/python \
  .copilot/rag/docs/tests/run_performance_eval.py \
  --run-id exact-v1 \
  --cases-file .copilot/rag/docs/tests/data/exact-cases-v1.jsonl \
  --profiles L \
  --executions daemon \
  --daemon-repeats 1 \
  --no-daemon-repeats 0 \
  --list-repeats 0 \
  --warmup-runs 1 \
  --timeout 30 \
  --explain-mode off \
  --diagnostics-level basic
```

Acceptance:

- positive Exact candidate: 15/15
- negative Exact candidate/signal: 0/15
- expected unmatched identifiers: 15/15
- matched identifier is the complete query identifier
- every inspected Exact candidate has a verified raw occurrence
- no A2W/A2L production branch

## No-hit

Input: `data/nohit-cases-v1.jsonl`

```bash
.copilot/rag/query/.venv/bin/python \
  .copilot/rag/docs/tests/run_performance_eval.py \
  --run-id nohit-v1 \
  --cases-file .copilot/rag/docs/tests/data/nohit-cases-v1.jsonl \
  --profiles L \
  --executions daemon \
  --daemon-repeats 1 \
  --no-daemon-repeats 0 \
  --list-repeats 0 \
  --warmup-runs 1 \
  --timeout 30 \
  --explain-mode off \
  --diagnostics-level basic
```

Acceptance:

- false Exact: 0/15
- unmatched identifier mismatch: 0/15
- `contexts`: empty
- legacy `evidence`: empty
- related candidates, when present, appear only in `related_context`
- prompt explicitly says the exact identifier was not found and related
  candidates are not proof

## Semantic gold

Create `data/semantic-gold-v1.jsonl` only after 10 human-checked questions per
DB have exact `span_text`, its hash, document revision, and DB snapshot hash.
Run H/L/V with the same 1200-token budget.

Metrics:

- Hit@5
- Context Recall@1200
- Vector Rescue: L misses and H hits / L misses
- Vector Harm: L hits and H misses / L hits
- Dense → RRF → final gold survival when a retrieval trace is available

Decision:

| H minus L | Decision |
|---|---|
| ≥10 points and Harm ≤5% | Keep Dense by default |
| 3–10 points | L-first, Dense on failure |
| <3 points | Remove Dense from default |
| Harm ≥10% | Tune fusion once, using a fixed 20/10 tune/holdout split |

## Fallback contract

Product mode allows one read-only synchronous fallback after a 12–15 second
daemon transport timeout. Pure daemon performance runs use `--require-daemon`
and forbid fallback.

The JSON response must distinguish:

- `first_attempt_success`
- `final_user_visible_success`
- `fallback_used`
- `actual_execution`
- per-attempt route, latency, and failure kind
- daemon PID, generation, transport, and code fingerprint

Force one timeout and verify: first attempt fails, the single fallback
succeeds, stdout remains pure JSON, the old daemon generation is retired, and
the next request starts a new generation.

## Windows smoke

Run 48–54 representative requests:

- 3 DB × H daemon/no-daemon × positive Exact/negative Exact/semantic/no-hit
- 3 DB × L daemon × the same four classes
- 3 DB × V daemon × semantic/broad
- 3 DB × file transport H daemon × Exact/no-hit
- optionally add stdin/prompt checks per DB

Windows smoke is a functional gate, not a p95 gate. Require no timeout, pure
JSON where requested, valid Japanese stdin, Windows-path handling, and the same
Exact/no-hit contracts as macOS.
