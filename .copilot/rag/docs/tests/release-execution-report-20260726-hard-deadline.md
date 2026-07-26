# Hard-deadline implementation and release-test report

Date: 2026-07-26  
Workspace: `/Users/haruki/Documents/rag`  
Development commit: `9ec0213cc48eb5ca63dfbce46623f8b63dfd7dfa`

## Outcome

- macOS short smoke: **PASS (21/21 primary + 3/3 forced fallback)**
- Unit/contract regression: **PASS (41/41)**
- Windows formal suite: **NOT_RUN**
- Formal release provenance: **INVALID for sign-off (`git_dirty=true`)**
- Overall release state: **NOT_RUN / NOT_READY**

The macOS 500-request rerun was intentionally omitted. Windows clean mixed 500
remains the formal daemon-performance gate.

## Agent-authored test decision

The test-design review separated two populations:

1. Windows clean mixed 500 uses required daemon execution and forbids fallback.
2. Forced fallback is a separate three-request contract suite, one request per
   DB.

This prevents a successful no-daemon retry from hiding a daemon failure in the
500-request cohort.

The implementation review found four P0 issues in the previous code:

- no absolute outer deadline;
- startup-failed fallback did not receive remaining time;
- expired deadlines could still start a `0.1` second fallback;
- the formal harness still accepted the old 30-second outer / 12–15-second
  daemon contract.

## Implemented runtime contract

### Search CLI

`query/search.py` now:

- defaults to a 15-second outer deadline;
- defaults to a 5-second warm-daemon soft timeout;
- creates one absolute deadline and shares its remaining time across health,
  daemon startup, TCP-to-file retry, daemon query, retirement, and fallback;
- reserves one second for process termination and structured output;
- does not start fallback after the deadline is exhausted;
- permits a daemon started by the current cold request to initialize within
  the remaining outer deadline;
- runs synchronous search in a dedicated process group;
- terminates the process tree on timeout;
- converts malformed synchronous JSON into one structured error JSON;
- records first attempt, fallback, final user-visible result, cold/warm route,
  latency, failure kind, and generation separately;
- retires the failed generation before fallback.

### Daemon lifecycle

`query/ragd.py` now:

- checks that the published state file still belongs to its generation and
  PID;
- exits when another generation replaces it or its state is unpublished;
- applies this ownership check to file, TCP, and Unix transports.

This closes the orphan-generation condition observed during the test run.

### Performance harness

`docs/tests/run_performance_eval.py` now:

- accepts a clean mixed release run only with outer timeout 15 and daemon soft
  timeout 5;
- continues to add `--require-daemon` to clean daemon rows;
- records outer-deadline exhaustion and overrun separately;
- reports an all-row outer-deadline gate in addition to the successful-response
  hard maximum;
- limits its wrapper grace period to one second.

### New test assets

- `docs/tests/run_forced_fallback_smoke.py`
- `docs/tests/data/mac-smoke-contract-cases-v1.jsonl`
- `query/test_ragd_contracts.py`
- expanded `query/test_search_contracts.py`
- expanded `docs/tests/test_run_performance_eval.py`
- updated `docs/tests/release-candidate-test-plan.md`

## Regression tests

| Suite | Result |
|---|---:|
| `query/test_search_contracts.py` | 11/11 PASS |
| `query/test_ragd_contracts.py` | 3/3 PASS |
| `docs/tests/test_run_performance_eval.py` | 27/27 PASS |
| Total | 41/41 PASS |
| `py_compile` | PASS |
| `git diff --check` | PASS |

The added tests cover:

- absolute remaining-time calculation;
- cold-versus-warm daemon timeout policy;
- no fallback spawn after deadline exhaustion;
- malformed child JSON wrapping;
- process-group termination without inherited-pipe hang;
- daemon state ownership/supersession;
- old 30/15 clean contract rejection;
- new 15/5 clean contract acceptance;
- failed-row inclusion in outer-deadline reporting.

## macOS short smoke

### Summary

| Gate | Result |
|---|---:|
| Primary requests | 21/21 PASS |
| Forced fallback requests | 3/3 PASS |
| All user-visible requests | 24/24 PASS |
| JSON stdout purity | 24/24 PASS |
| Final wall time <= 15 sec | 24/24 PASS |
| Hybrid daemon cold/warm | 9/9 PASS |
| Exact positive | 3/3 PASS |
| Exact negative channel | 3/3 PASS |
| Strong no-hit | 3/3 PASS |
| Hybrid no-daemon | 3/3 PASS |
| Forced timeout/fallback | 3/3 PASS |
| Daemon fingerprint match | 18/18 PASS |
| Orphan daemon after final suite | 0 |

Primary non-fallback maximum wall time was **6.096 seconds**. Cold-daemon
maximum was **6.096 seconds**. Warm daemon first-attempt maximum was **0.519
seconds**. Hybrid no-daemon maximum was **5.355 seconds**.

Forced fallback wall times (provenance-enriched v2 artifact):

| DB | Wall sec | Daemon attempt | Fallback | Final |
|---|---:|---|---|---|
| `ac-rag` | 10.554 | timeout at 5 sec | one no-daemon success | PASS |
| `incident-rag` | 9.409 | timeout at 5 sec | one no-daemon success | PASS |
| `rfc-full-20k-rag` | 9.922 | timeout at 5 sec | one no-daemon success | PASS |

Every forced case also confirmed pure JSON, old-generation retirement, and a
successful next required-daemon request with a new generation.

### Final evidence

- `performance-report-20260726-mac-short-final-coldwarm-ac.md`
- `performance-report-20260726-mac-short-final-coldwarm-incident.md`
- `performance-report-20260726-mac-short-final-coldwarm-rfc.md`
- `performance-report-20260726-mac-short-final-contract9.md`
- `performance-report-20260726-mac-short-final-nodaemon3.md`
- `forced-fallback-report-20260726-mac-short-final-fallback3-v2.md`

Raw data:

- `data/performance-results-20260726-mac-short-final-coldwarm-ac.jsonl`
- `data/performance-results-20260726-mac-short-final-coldwarm-incident.jsonl`
- `data/performance-results-20260726-mac-short-final-coldwarm-rfc.jsonl`
- `data/performance-results-20260726-mac-short-final-contract9.jsonl`
- `data/performance-results-20260726-mac-short-final-nodaemon3.jsonl`
- `data/forced-fallback-results-20260726-mac-short-final-fallback3-v2.jsonl`

## Diagnostic incident found during smoke

The first no-daemon attempt degraded from the historical 4–6 second range to
over 16 seconds. Process inspection found 104 orphan `ragd.py` processes from
older runs, all restricted to this workspace's daemon path and parent PID 1.

Actions:

- stopped the 104 identified orphan processes with `SIGTERM`;
- verified the orphan count returned to zero;
- reran the affected smoke from a clean process state;
- added generation/PID state ownership so superseded daemons self-terminate;
- verified the final suite leaves zero orphan daemons.

The contaminated attempts are retained as diagnostic evidence but are excluded
from the final macOS smoke cohort.

## Windows formal suite

Windows was not available in this execution environment. The formal run must
still execute, on the same clean release commit:

- setup and idempotent setup;
- fixture build/add/update/interruption/resume;
- prior P0 system/combination cases;
- Exact/no-hit 30;
- no-daemon H/L/V;
- Japanese stdin;
- backslash, spaces, and Japanese paths;
- TCP/file transport;
- clean mixed 500 under strict 15/5;
- forced fallback for all three DBs;
- pre/post clean-worktree verification.

The exact matrix, command, acceptance criteria, and stop rules are in
`release-candidate-test-plan.md`.

## Provenance and release judgment

All final macOS daemon rows use runtime fingerprint:

`ba9b8b13f50d9aa1b136eb57b4eade6dcee610cd2f30fc024914bedf2cc0950c`

The expected and actual daemon fingerprint matched 18/18. However, all rows
correctly record `git_dirty=true`, and Windows has not run. Therefore:

- implementation evidence: **PASS**
- macOS short smoke: **PASS**
- Windows formal gate: **NOT_RUN**
- clean release provenance: **NOT_RUN**
- final release: **NOT_READY**

Formal sign-off requires committing the intended source and case files,
checking out that same commit cleanly on macOS and Windows, writing generated
evidence outside the checkout, and rerunning the macOS 24 plus the Windows
formal suite.

## Final agent review

Pending independent review of the final raw JSONL, reports, source diff, and
provenance state.
