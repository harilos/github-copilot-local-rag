# RAG v1 release-candidate test plan

## Decision

The macOS 500-request/one-hour rerun is intentionally omitted. The shared
runtime changed, so macOS receives the 24-request short smoke below. Windows
remains the formal release environment.

The Windows clean mixed 500 cohort is a daemon-health test:

- actual daemon required;
- synchronous fallback and the cold fast path are forbidden;
- daemon first-attempt success must be 500/500;
- fallback is tested in a separate, forced three-request contract suite.

A fallback success never converts a daemon failure into a clean mixed pass.

## Release provenance

Formal evidence is valid only when all of these conditions hold:

- macOS and Windows use the same detached release commit;
- `git status --porcelain --untracked-files=all` is empty before and after;
- recorded `git_dirty` is `false`;
- runtime, daemon, harness, case-set, and DB-snapshot fingerprints are present;
- generated evidence is written outside the clean checkout;
- no code, configuration, or case file changes occur during a suite.

The current workspace is a development worktree. Its results can validate the
implementation but cannot be the final release sign-off until the intended
changes are committed and rerun from clean checkouts.

## Runtime deadline contract

- outer user-visible deadline: 15 seconds;
- warm daemon soft timeout: 5 seconds;
- a daemon started by the current cold request may use the remaining outer
  deadline for model initialization;
- after a warm daemon timeout, retire the old generation and run synchronous
  fallback once with only the remaining time;
- when no time remains, do not spawn fallback;
- timeout and malformed-child-output responses remain one pure JSON object;
- `first_attempt_success`, `fallback_used`, `actual_execution`,
  `final_user_visible_success`, per-attempt latency/failure kind, and daemon
  generation are reported independently.

## macOS short smoke

Run only after a shared runtime change.

| Channel | Requests |
|---|---:|
| 3 DB Hybrid: daemon cold 1 + warm 2 | 9 |
| 3 DB: Exact positive + negative + strong no-hit | 9 |
| 3 DB: Hybrid no-daemon | 3 |
| 3 DB: forced warm-daemon timeout and fallback | 3 |
| Total | 24 |

Acceptance:

- all 24 primary requests succeed;
- JSON stdout purity is 100%;
- no cross-DB evidence;
- Exact positive/negative and no-hit contract errors are zero;
- warm daemon attempts complete within the 5-second soft timeout;
- cold requests and all final user-visible results complete within 15 seconds;
- every forced case records daemon timeout, exactly one successful no-daemon
  fallback, old-generation retirement, and a successful next-generation
  required-daemon request.

The deterministic fallback runner is:

```bash
.copilot/rag/query/.venv/bin/python \
  .copilot/rag/docs/tests/run_forced_fallback_smoke.py \
  --run-id mac-fallback-release \
  --outer-timeout 15 \
  --daemon-attempt-timeout 5
```

## Windows execution order

1. Record provenance, runtime fingerprint, and the three DB identities.
2. Run Python unit and contract tests.
3. Test setup and `setup_required`.
4. Test fixture build, add, interruption, and resume.
5. Run the P0 system/combination cases.
6. Run Exact/no-hit 30.
7. Run no-daemon, Japanese stdin, and Windows-path cases.
8. Run clean mixed 500.
9. Run forced fallback for all three DBs.
10. Confirm the checkout is still clean and produce sign-off.

A failed P0 phase stops later, longer phases.

## Windows clean mixed 500

| DB | H | L | V | Total |
|---|---:|---:|---:|---:|
| `ac-rag` | 120 | 35 | 20 | 175 |
| `incident-rag` | 100 | 30 | 20 | 150 |
| `rfc-full-20k-rag` | 120 | 35 | 20 | 175 |
| Total | 340 | 100 | 60 | 500 |

The harness itself adds `--require-daemon` to every daemon row.

```powershell
py -3 .copilot\rag\docs\tests\run_performance_eval.py `
  --run-id "windows-clean-mixed-$env:RELEASE_SHA" `
  --output-dir "$env:TEMP\rag-release-$env:RELEASE_SHA" `
  --sequence-plan clean-mixed `
  --profiles H L V `
  --executions daemon `
  --mixed-total 500 `
  --sequence-seed 20260726 `
  --time-buckets 10 `
  --warmup-runs 5 `
  --timeout 15 `
  --daemon-attempt-timeout 5 `
  --explain-mode off `
  --diagnostics-level off `
  --pure-profile `
  --min-samples-for-p95 20 `
  --p95-target-h 8 `
  --p95-target-l 2 `
  --p95-target-v 8 `
  --hard-latency-limit 15 `
  --timeout-rate-gate 0 `
  --restart-daemon
```

Strict acceptance:

- first-attempt daemon success 500/500;
- final user-visible success 500/500;
- actual execution daemon 500/500;
- timeout 0 and fallback 0;
- JSON stdout purity 500/500;
- every DB/profile H/V p95 <= 8 seconds;
- every DB/profile L p95 <= 2 seconds;
- every successful response and every failed/timeout row stays within 15 seconds;
- low-N cells 0;
- second-half/first-half profile p95 ratio <= 1.20 while the absolute target
  remains satisfied;
- one daemon generation and fingerprint match 500/500.

The harness rejects the former `--timeout 30 --daemon-attempt-timeout 15`
cohort as a release run.

## Windows Exact/no-hit 30

Use `data/exact-cases-v1.jsonl`: five positive and five verified-absent
near-collisions for each DB. The 15 negative rows also carry the strong no-hit
contract.

Acceptance:

- positive Exact 15/15;
- negative Exact 15/15 and false Exact 0;
- expected unmatched identifier 15/15;
- complete matched identifier/raw occurrence 15/15;
- negative rows have empty normal contexts/evidence/results;
- related candidates appear only under `related_context`;
- timeout 0 and JSON purity 100%.

## Windows additional paths

- no-daemon: 3 DB x H/L/V = 9;
- Japanese stdin: every DB in daemon and no-daemon mode = 6;
- backslash, spaces, and Japanese Windows paths = at least 6;
- daemon cold/warm pair for every DB = 6;
- representative TCP and file transport for every DB;
- prompt, JSON, explain-JSON, argv, and stdin from `IF-CART-WIN`;
- all Windows rows from the existing pairwise table;
- setup first run and idempotent second run;
- offline query after setup;
- fixture clean build, no-op rebuild, add, update, and interruption/resume at
  SQLite/FTS/Exact/Chroma boundaries;
- resumed and clean-build UID/count/logical-hash equality;
- daemon-open DB update without old/new-generation mixing;
- one-writer/BUSY contract and status transitions.

## Forced fallback acceptance

Run separately from clean mixed, once for every DB:

- warm daemon attempt times out at 5 seconds;
- `first_attempt_success=false`;
- failure kind is `timeout`;
- fallback route is `no-daemon`, used exactly once;
- `final_user_visible_success=true`;
- total latency is at most 15 seconds;
- stdout is pure JSON;
- old generation is retired;
- the next `--require-daemon` request succeeds with a new generation.

## Stop and rerun rules

- dirty checkout, commit mismatch, or fingerprint mismatch: evidence invalid;
- JSON contamination, cross-DB evidence, or wall time over 15 seconds:
  immediate release block;
- any timeout/fallback in clean mixed: `DAEMON_GATE_FAIL`;
- forced fallback failure, duplicate fallback, or wall time over 15 seconds:
  fallback contract fail;
- Exact/no-hit fix: rerun the entire 30-case suite;
- lifecycle inconsistency: do not start the 500-request run;
- shared runtime fix: rerun the complete macOS 24 and all affected Windows
  suites;
- retrieval/fusion/embedding change: rerun Semantic gold as well.

Final release requires the same clean commit, Windows formal PASS, macOS short
smoke PASS, and clean status before and after both runs.
