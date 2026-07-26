# RAG性能評価ダッシュボード 20260726-condition-separated-dashboard

異なるrun条件は統合せず、互換条件ごとのcellとして比較する。

## 判定基準

- H p95 target: 8.000 sec
- L p95 target: 2.000 sec
- V p95 target: 8.000 sec
- hard latency limit: 15.000 sec
- timeout rate limit: 0.000%
- p95 minimum N: 10

## list_dbs

- repeats: 10
- dbs: ac-rag, incident-rag, rfc-full-20k-rag
- JSON errors: 0
- p50: 0.045 sec
- p95: 0.093 sec

## Run比較

|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|
|--:|--|--|--|--|--:|--:|--:|--:|--|--|
|1|Historical baseline|unknown|unknown|seq=legacy, explain=unknown, diag=unknown, pure=unknown|111|109|2|64.800|FAIL|FAIL|
|2|Historical baseline|unknown|unknown|seq=legacy, explain=unknown, diag=unknown, pure=unknown|9|6|3|8.515|INSUFFICIENT_N|PASS|
|3|Current pure-profile (identity incomplete)|unknown|unknown|seq=default, explain=False, diag=off, pure=True|30|30|0|6.183|PASS|PASS|
|4|Profile transition (identity incomplete)|unknown|unknown|seq=profile-transition, explain=False, diag=off, pure=True|30|30|0|7.360|INSUFFICIENT_N|PASS|
|5|DB switch (identity incomplete)|unknown|unknown|seq=db-switch, explain=False, diag=off, pure=True|20|20|0|1.569|INSUFFICIENT_N|PASS|
|6|Explain comparison (identity incomplete)|unknown|unknown|seq=explain-compare, explain=False, diag=basic, pure=False|10|10|0|7.196|INSUFFICIENT_N|PASS|
|7|Explain comparison (identity incomplete)|unknown|unknown|seq=explain-compare, explain=True, diag=basic, pure=False|10|10|0|7.418|INSUFFICIENT_N|PASS|

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
|ac-rag|unknown|unknown|H|daemon|unverified|12|12|0|0|6.590|23.906|8.000|23.906|FAIL|FAIL|NO|
|ac-rag|unknown|unknown|H|no-daemon|unverified|4|4|0|0|5.045|5.792|8.000|5.792|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|L|daemon|unverified|12|12|0|0|0.961|49.186|2.000|49.186|FAIL|FAIL|NO|
|ac-rag|unknown|unknown|L|no-daemon|unverified|4|4|0|0|0.901|1.150|2.000|1.150|INSUFFICIENT_N|PASS|YES|
|ac-rag|unknown|unknown|V|daemon|unverified|12|11|1|1|7.223|12.577|8.000|12.577|FAIL|PASS|NO|
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
|Exact positive|FAIL|22|21|1|1|expectation対象行のみ|
|Exact negative|PASS|8|8|0|0|expectation対象行のみ|
|JSON stdout purity|PASS|109|109|0|0|完了行のみを分母にする|
|全検索の正常完了率|FAIL|111|109|2|2|exit 0・JSON parse・payload errorなし|
|daemon first-attempt成功率|UNVERIFIED|90|88|2|2|fallback後の成功と分離|
|final user-visible成功率|UNVERIFIED|90|88|2|0|fallback後の最終結果|
|fallback rate|UNVERIFIED|90|90|0|0|clean daemon runでは0|
|daemon build identity|UNVERIFIED|90|0|90|0|daemon応答code fingerprintと測定側を比較|
|diagnostics mode contract|UNVERIFIED|109|0|109|0|応答実値と要求値を照合|
|daemon timeout rate|FAIL|90|88|2|2|limit=0.000%|
|latency_p95_slo|FAIL|9|0|3|0|profile別target、low-N cells=6|
|hard_latency_limit|FAIL|88|84|4|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|90|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|90|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|PASS|21|21|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|111|109|2|
|stdout JSON purity|111|109|2|
|Exact negative collision|8|8|0|
|Expected unmatched identifier|8|8|0|
|Exact positive candidate|22|21|1|
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
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

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
|Exact positive|PASS|2|2|0|0|expectation対象行のみ|
|Exact negative|PASS|2|2|0|0|expectation対象行のみ|
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
|time degradation|NOT_APPLICABLE|9|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|9|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|
|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|

### expectation-scoped observations

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|9|6|3|
|stdout JSON purity|9|6|3|
|Exact negative collision|2|2|0|
|Expected unmatched identifier|2|2|0|
|Exact positive candidate|2|2|0|
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
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

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
|ac-rag|unknown|unknown|H|daemon|unverified|10|10|0|0|4.445|6.101|8.000|6.101|PASS|PASS|NO|
|ac-rag|unknown|unknown|L|daemon|unverified|10|10|0|0|0.669|0.848|2.000|0.848|PASS|PASS|NO|
|ac-rag|unknown|unknown|V|daemon|unverified|10|10|0|0|4.514|6.183|8.000|6.183|PASS|PASS|NO|

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
|latency_p95_slo|PASS|3|3|0|0|profile別target、low-N cells=0|
|hard_latency_limit|PASS|30|30|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|30|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|30|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
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
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

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
|ac-rag|unknown|unknown|L|daemon|unverified|15|15|0|0|0.746|0.943|2.000|0.943|PASS|PASS|NO|
|ac-rag|unknown|unknown|V|daemon|unverified|10|10|0|0|5.306|7.360|8.000|7.360|PASS|PASS|NO|

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
|latency_p95_slo|INSUFFICIENT_N|3|2|0|0|profile別target、low-N cells=1|
|hard_latency_limit|PASS|30|30|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|30|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|30|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
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
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

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
|ac-rag|unknown|unknown|L|daemon|unverified|10|10|0|0|0.742|0.804|2.000|0.804|PASS|PASS|NO|
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
|latency_p95_slo|INSUFFICIENT_N|3|1|0|0|profile別target、low-N cells=2|
|hard_latency_limit|PASS|20|20|0|0|max limit=15.000 sec|
|time degradation|NOT_APPLICABLE|20|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|20|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
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
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

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
|time degradation|NOT_APPLICABLE|10|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|10|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
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
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

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
|time degradation|NOT_APPLICABLE|10|0|0|0|後半p95/前半p95 ≤ 1.20かつ絶対target内|
|daemon generation stability|NOT_APPLICABLE|10|0|0|0|clean run中のgeneration変更0|
|no-daemon smoke|NOT_RUN|0|0|0|0|N=0はNOT_RUN|
|no-hit contract|NOT_RUN|0|0|0|0|通常contexts空・related_context隔離|
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
- Vector Rescue Rate: NOT_RUN
- Vector Harm Rate: NOT_RUN

