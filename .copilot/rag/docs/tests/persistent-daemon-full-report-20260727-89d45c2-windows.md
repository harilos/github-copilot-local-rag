# Persistent daemon full test report

- run_id: `89d45c2-full`
- platform: `win32`
- started_at: `2026-07-27T08:27:20.815247+00:00`
- finished_at: `2026-07-27T08:35:05.102565+00:00`
- overall: **FAIL**

## Gates

| Gate | Result | Detail |
|---|---|---|
| `structured_request_equivalence` | **PASS** | contract_search_calls=2, warmup_search_calls=1, normalized_equal=True, normalized_complete=True, behavior_equal=True, same_generation=True, question_unchanged=True, db_equal=True, exact_safe=True, json_pure=True, runtime=True |
| `lifecycle_20` | **PASS** | completed=20/20, requested=20, failures=0 |
| `clients_100` | **PASS** | completed=100/100, requested=100, stable_identity=True, clients_gone=True |
| `cold_concurrency_4` | **PASS** | success=4/4, stable_identity=True |
| `warm_concurrency_2_4` | **PASS** | completed=120/120, requested=120, stable_identity=True |
| `db_release_all` | **PASS** |  |
| `client_crash_recovery` | **PASS** |  |
| `worker_exit_recovery` | **PASS** | old=6844, new=1800, probe=PASS |
| `worker_hang_recovery` | **PASS** | suspended=True, timeout_row=error/required daemon attempt failed: timeout, reaped=True, next=PASS |
| `manager_job_recovery` | **PASS** | job=True, manager_gone=True, worker_gone=True, next=PASS |
| `soak_200_c4` | **PASS** | completed=200/200, stable_generation=True |
| `resource_manager_handles` | **PASS** | baseline=166.0, final=167.0, limit=182.0 |
| `resource_worker_handles` | **PASS** | baseline=340.0, final=339.0, limit=372.0 |
| `resource_manager_threads` | **PASS** | baseline=6.0, final=8.0, limit=10.0 |
| `resource_worker_threads` | **PASS** | baseline=129.0, final=126.0, limit=133.0 |
| `resource_manager_rss` | **PASS** | baseline=31838208, final=36466688, monotonic=False, material=False |
| `resource_worker_rss` | **PASS** | baseline=743780352, final=744579072, monotonic=False, material=False |
| `overload_8_safety` | **PASS** | success=4/8, bounded=4, healthy=PASS |
| `exact_30` | **PASS** | completed=30/30, failures= |
| `broad_search_18` | **FAIL** | completed=18/18, median_distinct=8.0, median_useful=3.0, failures=AC-BROAD-EXISTING-DEFINITION-001:runtime=PASS,distinct=8,useful=2,noise=6,aspect=1.00,calibration=False,identifier=False,bytes=11766;AC-BROAD-EXISTING-RELATED-002:runtime=PASS,distinct=8,useful=6,noise=2,aspect=1.00,calibration=False,identifier=True,bytes=11072;AC-BROAD-ABSENT-DEFINITION-003:runtime=PASS,distinct=8,useful=2,noise=6,aspect=0.67,calibration=False,identifier=True,bytes=10899;AC-BROAD-ABSENT-RELATED-004:runtime=PASS,distinct=8,useful=5,noise=3,aspect=0.75,calibration=False,identifier=True,bytes=11965;AC-BROAD-GENERAL-005:runtime=PASS,distinct=8,useful=7,noise=1,aspect=1.00,calibration=False,identifier=True,bytes=10655;AC-BROAD-ONE-DOCUMENT-006:runtime=PASS,distinct=8,useful=1,noise=7,aspect=1.00,calibration=False,identifier=False,bytes=11311;INCIDENT-BROAD-EXISTING-DEFINITION-007:runtime=PASS,distinct=8,useful=4,noise=4,aspect=1.00,calibration=False,identifier=True,bytes=11754;INCIDENT-BROAD-EXISTING-RELATED-008:runtime=PASS,distinct=8,useful=4,noise=4,aspect=1.00,calibration=False,identifier=True,bytes=12016;INCIDENT-BROAD-ABSENT-RELATED-010:runtime=PASS,distinct=8,useful=3,noise=5,aspect=1.00,calibration=False,identifier=True,bytes=11231;INCIDENT-BROAD-GENERAL-011:runtime=PASS,distinct=8,useful=4,noise=4,aspect=1.00,calibration=False,identifier=True,bytes=11379;INCIDENT-BROAD-ONE-DOCUMENT-012:runtime=PASS,distinct=8,useful=1,noise=7,aspect=1.00,calibration=True,identifier=True,bytes=10875;RFC-BROAD-EXISTING-DEFINITION-013:runtime=PASS,distinct=8,useful=3,noise=5,aspect=1.00,calibration=False,identifier=False,bytes=11090;RFC-BROAD-EXISTING-RELATED-014:runtime=PASS,distinct=8,useful=5,noise=3,aspect=1.00,calibration=True,identifier=False,bytes=10242;RFC-BROAD-ABSENT-RELATED-016:runtime=PASS,distinct=8,useful=1,noise=7,aspect=1.00,calibration=False,identifier=True,bytes=10963;RFC-BROAD-GENERAL-017:runtime=PASS,distinct=8,useful=8,noise=0,aspect=1.00,calibration=False,identifier=True,bytes=12026;RFC-BROAD-ONE-DOCUMENT-018:runtime=PASS,distinct=8,useful=1, |
| `mac_short_smoke` | **NOT_RUN** | not_applicable_platform |

## Counts

- cases: 525
- failures: 5
- JSON parse errors: 0
- fallbacks: 0
- response mismatches: 1

The frozen Semantic accuracy gate is independent of this runtime report.
