# Persistent Daemon Full Test Specification

## 1. Purpose

This specification verifies the persistent Local RAG manager/worker
architecture on Windows.

The primary test target is the code at commit `398dd6d`. A formal run must use
one clean, immutable commit for:

- the repository under test;
- the installed `~/.copilot/rag` runtime;
- every client process;
- the manager process;
- the worker process.

The test does not rebuild a database or index. The DB release tests use
recoverable temporary renames and must restore every path in a `finally`
operation.

The full Windows gate covers:

- unit and import-isolation contracts;
- manager and worker lifecycle;
- 100 short-lived direct clients;
- cold and warm concurrency 4;
- DB release for every installed test DB;
- client, worker, and manager crash recovery;
- a 200-request concurrency-4 soak;
- concurrency-8 overload behavior;
- handle, thread, and RSS trends;
- a shortened macOS smoke after the shared protocol passes.

This is an infrastructure and resource-ownership gate. It does not replace a
semantic relevance holdout.

## 2. Test philosophy

Do not copy a latency or resource number from another computer and call it a
hardware-independent gate. Classify every judgment as one of the following.

|Class|Meaning|Examples|
|---|---|---|
|Absolute safety contract|Must hold on every supported machine.|No response mix-up, one manager, one worker, no orphan, pure JSON, no fallback, no DB sharing violation, every client returns or fails explicitly within the 15-second outer deadline.|
|Declared product SLO|A release promise fixed before the run.|Supported concurrency is four; user-visible hard deadline is 15 seconds; queue capacity and error semantics are fixed by the product contract.|
|Baseline-relative capacity|Compared with a warm serial baseline from the same commit and machine.|Worker execution time, queue wait, cold-start cost, warm concurrency latency.|
|Trend gate|Evaluated across ordered post-warmup buckets.|Handle, thread, and RSS growth; latency degradation over time.|
|Diagnostic observation|Recorded but not independently release-blocking.|Peak RSS, allocator-retained memory, exact scheduling order between different client IDs.|

An absolute safety failure must never be waived because the machine is slow.
A machine that cannot satisfy the declared concurrency-4 service claim is a
NO-GO for that claim; it is not made green by increasing the deadline after
results are known.

## 3. Result states

Every case and aggregate gate has exactly one state:

- `PASS`: the case ran to completion and all applicable assertions passed.
- `FAIL`: the case ran, or started far enough to evaluate the assertion, and
  an assertion failed.
- `NOT_RUN`: the case did not run or could not produce the required
  measurement.

`NOT_RUN` requires a machine-readable reason:

```text
missing_prerequisite
runner_unavailable
measurement_unavailable
operator_stop
prior_safety_stop
not_applicable_platform
artifact_invalid
```

Rules:

1. `N=0` is always `NOT_RUN`, never PASS or FAIL.
2. A required Windows release phase with any `NOT_RUN` result makes the final
   Windows release gate `NOT_RUN`, not PASS.
3. A failure in a prerequisite may mark dependent cases `NOT_RUN` with
   `prior_safety_stop`; it must not manufacture additional failures.
4. macOS-only and Windows-only cases use `not_applicable_platform` on the other
   OS.
5. Missing resource measurements make the resource gate `NOT_RUN`; successful
   search results do not substitute for leak measurements.

## 4. Required environment

### Windows

- Windows 10 or later, x64.
- PowerShell 5.1 or later.
- The installed venv Python:

  ```text
  %USERPROFILE%\.copilot\rag\query\.venv\Scripts\python.exe
  ```

- Installed Local RAG code synchronized to the tested commit.
- At least three healthy databases where available:

  ```text
  ac-rag
  incident-rag
  rfc-full-20k-rag
  ```

- Enough free disk space to create logs and temporarily rename DB paths.
- Permission to query process information and terminate test-owned manager and
  worker processes.
- No unrelated Local RAG client or administrative operation during the formal
  run.

If fewer than three databases are installed, unit and lifecycle phases may
run, but the mixed-DB, all-DB release, and release gate are `NOT_RUN`.

### macOS

- The installed venv Python:

  ```text
  ~/.copilot/rag/query/.venv/bin/python
  ```

- The same tested commit and at least two healthy databases.

### Formal-run cleanliness

Before executing a formal run:

```powershell
$Repo = (Resolve-Path ".").Path
$Rag = Join-Path $HOME ".copilot\rag"
$Py = Join-Path $Rag "query\.venv\Scripts\python.exe"
$Search = Join-Path $Rag "query\search.py"
$RunId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$Out = Join-Path $env:TEMP "local-rag-persistent-daemon-$RunId"
New-Item -ItemType Directory -Force $Out | Out-Null

git -C $Repo rev-parse HEAD
git -C $Repo status --short --untracked-files=all
& $Py (Join-Path $Rag "query\setup.py") --verify-only --format json
& $Py (Join-Path $Rag "query\list_dbs.py") --format json
```

Required:

- `git rev-parse HEAD` equals the intended release commit;
- the pre-run worktree is clean;
- setup verification has `setup_complete=true`;
- each test DB is healthy;
- installed runtime hashes match the tested source.

Write these facts before any test to:

```text
persistent-daemon-environment-<run-id>.json
```

Do not write formal-run artifacts into the checkout while determining the
initial `git_dirty` value.

## 5. Required driver

The full lifecycle, crash, and concurrency tests require one orchestration
driver. The current repository has focused unit tests and a sequential
performance harness, but those are not a substitute for a multi-process
Windows driver.

The conforming driver path is:

```text
.copilot/rag/docs/tests/run_persistent_daemon_windows.py
```

If this driver is absent, phases that require it are `NOT_RUN` with
`runner_unavailable`. Do not mark them PASS by substituting
`run_performance_eval.py`, because that harness does not create the required
concurrent independent clients or crash conditions.

The driver must:

- use the installed venv Python as the client executable;
- start clients with `subprocess.Popen([...], shell=False)`;
- use `stdin=DEVNULL`, separate stdout/stderr pipes, and `close_fds=True`;
- never use `cmd.exe`, `.bat`, `.cmd`, `Start-Process`, or a nested PowerShell;
- use a common start barrier for concurrent cohorts;
- parse exactly one JSON object from every client stdout;
- sample manager and worker health through authenticated local transport;
- write append-only JSONL after each case or resource sample;
- restore renamed DB paths in `finally`;
- kill only PIDs whose manager generation and executable identity were
  captured by the run;
- preserve raw stdout, stderr, and daemon logs for every failure.

Required driver interface:

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase <phase> `
  --installed-rag "$Rag" `
  --output-dir "$Out" `
  --run-id "$RunId" `
  --db ac-rag `
  --db incident-rag `
  --db rfc-full-20k-rag
```

Supported phase names:

```text
baseline
structured-contract
broad-18
exact-30
semantic-frozen
lifecycle-20
clients-100
cold-c4
warm-c4
db-release
client-crash
worker-crash
manager-crash
soak-200-c4
overload-c8
mac-smoke
report
```

The current Python driver groups several specification phases. Use this
mapping when invoking
`run_persistent_daemon_windows.py`:

| Specification phase | Driver phase |
|---|---|
| `structured-contract` | `structured-contract` |
| `broad-18` | `broad-18` |
| `exact-30` | `exact-30` |
| `lifecycle-20` | `lifecycle-20` |
| `clients-100` | `clients-100` |
| `cold-c4` and `warm-c4` | `concurrency` |
| `db-release` | `db-release` |
| `client-crash`, `worker-crash`, and `manager-crash` | `crash` |
| `soak-200-c4` | `soak-200-c4` |
| `overload-c8` | `overload-c8` |
| `mac-smoke` | `mac-smoke` |

Specification-only orchestration and reporting labels that are not accepted
by the Python driver's `--phase` option must not be reported as executed
driver phases.

All count values are defaults defined by this test version. A diagnostic run
may reduce them, but a reduced run is not the corresponding full release
phase.

## 6. Record schema

All JSONL files use:

```json
{
  "schema": "local-rag.persistent-daemon-test.v1",
  "record_type": "case|event|resource_sample|gate",
  "run_id": "20260727-120000",
  "phase": "warm-c4",
  "case_id": "warm-c4-003",
  "iteration": 3,
  "result": "PASS|FAIL|NOT_RUN",
  "reason": null,
  "platform": "windows",
  "git_commit": "398dd6d...",
  "git_dirty_at_start": false,
  "started_at": "ISO-8601 UTC",
  "finished_at": "ISO-8601 UTC",
  "elapsed_seconds": 3.42,
  "deadline_seconds": 15.0,
  "concurrency": 4,
  "client_id": "UUID",
  "request_id": "UUID",
  "db": "ac-rag",
  "profile": "H|L|V",
  "query_kind": "exact|lexical|dense|hybrid_broad",
  "exit_code": 0,
  "stdout_json_valid": true,
  "response_status": "ok",
  "error_kind": null,
  "fallback_used": false,
  "response_identity_match": true,
  "response_db_match": true,
  "duplicate_execution": false,
  "manager_pid": 1000,
  "worker_pid": 1001,
  "manager_generation": "UUID",
  "worker_generation": "UUID",
  "model_load_count": 1,
  "open_database_count": 3,
  "handled_request_count": 40,
  "queue_depth": 0,
  "manager_rss_bytes": 0,
  "worker_rss_bytes": 0,
  "manager_handle_count": 0,
  "worker_handle_count": 0,
  "manager_thread_count": 0,
  "worker_thread_count": 0,
  "established_local_tcp": 0,
  "notes": []
}
```

Resource records additionally contain:

```json
{
  "sample_index": 5,
  "bucket_index": 2,
  "requests_completed": 40,
  "manager_alive": true,
  "worker_alive": true,
  "manager_process_count": 1,
  "worker_process_count": 1,
  "client_process_count": 0
}
```

The report must not infer a missing value as zero. An unavailable value is
`null` and makes its required measurement gate `NOT_RUN`.

## 7. Query matrix

Use reviewed, non-destructive questions that cover:

|Profile|CLI behavior|Purpose|
|---|---|---|
|L|`--retrieval-mode lexical`|Exact and lexical requests without Dense execution.|
|V|`--retrieval-mode dense`|Dense-only diagnostic ownership and latency.|
|H|default Hybrid with `--compact-json`|Normal product and broad discovery behavior.|

The test matrix must include:

- a verified identifier;
- an absent near-collision identifier;
- a filename or metadata identifier;
- a general semantic question;
- a broad related-document question.

The final positional argument is always the complete original question.
Normal client commands use the daemon; `--no-daemon` is prohibited except in a
separate diagnostic not counted by this gate.

Example direct client:

```powershell
& $Py $Search `
  --db ac-rag `
  --compact-json `
  --timeout 15 `
  "A2Lについて資料で確認できる範囲を教えて"
```

## 8. Broad-search and accuracy gate separation

The broad-by-default feature has three independent responsibilities:

1. preserve the strict Evidence lane and Exact safety;
2. return useful, diversified `document_results`;
3. preserve the structured one-shot request contract.

These are not daemon lifecycle measurements. Report them separately:

```text
broad_search_contract
broad_search_quality
exact_safety
frozen_semantic_accuracy
daemon_runtime
```

Rules:

- A daemon/runtime PASS does not waive a broad-search or semantic failure.
- A broad-search PASS does not reclassify the failed frozen Semantic v2 result.
- `document_results` count as discovery results, not authoritative evidence.
- The frozen Semantic evaluator counts only authoritative final `evidence`,
  never `document_results` or `related_context`.
- Infrastructure errors are reported in the runtime gate. Relevance and
  calibration errors are reported in the corresponding quality gate.

## 9. Phase SC: structured JSON and argv equivalence

Run:

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase structured-contract --installed-rag "$Rag" `
  --output-dir "$Out" --run-id "$RunId" --db ac-rag
```

The driver must construct the same `rag-search-request-v1` request through:

1. `--request-json --stdin`, with bytes supplied directly to
   `subprocess.Popen.communicate()` and no shell pipeline; and
2. repeated argv:

   ```text
   --answer-goal
   --literal-identifier
   --entity
   --facet
   --semantic-hypothesis
   ```

The request must contain Japanese, spaces, quotes, a backslash, a hyphen, an
underscore, a slash, a dot, and mixed-case identifier text.

Compare two levels.

### Normalized request equivalence

The internal normalized representation must be equal for:

- schema version;
- original question;
- answer goal;
- literal identifiers;
- entities;
- facet kind and query;
- inferred concept term and `semantic_only=true`;
- default coverage.

If an input feature cannot be represented by repeated argv, it must be
rejected as unsupported by the equivalence case rather than silently dropped.

### Retrieval behavior equivalence

Run both representations in the same warm manager/worker generation and
compare, after removing request IDs, timing, process metrics, and other
documented volatile fields:

- status and answerability;
- unmatched identifiers;
- verified Exact identifiers;
- evidence chunk IDs and source paths;
- ordered `document_results.path`;
- support levels;
- coverage policy and distinct-document count;
- Dense-used and Dense-skipped state.

No second search, alternate DB, shell JSON post-processing, or no-daemon
fallback is allowed.

Gate:

```text
normalized representation mismatch  0
retrieval behavior mismatch          0
original-question mutation           0
semantic hypothesis used as Exact    0
stdout JSON failure                  0
compact JSON over 16,384 bytes       0
```

## 10. Phase B18: 18-case broad-search evaluation

### Dataset

Use a frozen, human-reviewed dataset:

```text
.copilot/rag/docs/tests/data/broad-search-cases-v1.jsonl
```

If it does not exist, has no recorded SHA-256, or was graded after inspecting
the evaluated run, the phase is `NOT_RUN/missing_prerequisite`.

Before the first evaluated implementation, inspect the full corpus and record
acceptable documents and aspects. For each of three DBs, define six cases:

1. existing identifier definition;
2. the same identifier asking for related documents;
3. absent near-collision identifier definition;
4. the absent identifier asking for related documents;
5. a general topic with at least six reviewed useful documents;
6. a true one-document control.

Required human document grades:

```text
3 = direct evidence
2 = strongly related
1 = weak but useful research lead
0 = noise
```

Grade 1 is useful for this feature. Every case also records:

- reviewed aspects/facets;
- whether direct evidence exists;
- whether at least six useful distinct documents exist;
- whether it is the one-document control;
- acceptable document paths and stable document IDs;
- expected literal identifier behavior.

Run:

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase broad-18 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Execute each case once through the normal warm daemon route, using structured
planning declared in the dataset. Call `search.py` exactly once per case.

### Metrics

For each case record:

```text
DistinctDoc@8
UsefulDoc@8
GoldDocumentRecall@8
AspectCoverage@8
duplicate path count
maximum cards per document
support-level confusion matrix
compact JSON bytes
evidence document count
document_results count
```

Definitions:

- `DistinctDoc@8`: unique document paths in the first eight cards.
- `UsefulDoc@8`: grade 1, 2, or 3 documents in the first eight cards.
- `GoldDocumentRecall@8`: reviewed useful documents in the first eight divided
  by all reviewed useful documents, capped only by the reviewed set, not by
  output count.
- `AspectCoverage@8`: reviewed aspects represented by a useful top-eight
  document divided by all applicable reviewed aspects.
- Duplicate paths are normalized for Windows case-insensitivity and path
  separators before comparison.

### Quality gates

For cases with at least six reviewed useful documents:

```text
returned distinct documents       >= 6
useful documents in top 8         >= 5
direct/strong document in top 3   >= 1 when one exists
grade-0 documents in top 8        <= 2
duplicate paths                   0
cards per document                <= 1
excerpts represented per document <= 2
aspect/facet coverage             >= 60%
```

For the true one-document control:

- returning one reviewed useful document is PASS;
- arbitrary grade-0 padding is forbidden;
- `insufficient_distinct_related_documents` is required when fewer than the
  requested minimum are returned.

Across all 18 cases:

```text
search completion                 18/18
pure stdout JSON                  18/18
timeout                           0
compact JSON hard limit           <= 16,384 bytes each
duplicate paths                   0
false authoritative discovery     0
```

The expected compact size of 12 KiB or less is reported as an optimization
target. The hard gate is 16,384 bytes.

### Support-level calibration

Report the full human-grade versus support-level confusion matrix.

Absolute calibration rules:

- `direct` must have human grade 3 and must be tied to authoritative evidence;
- `authoritative=true` is allowed only for a direct evidence document;
- grade 0 must never be labelled `direct` or `strong`;
- an absent identifier or inferred acronym expansion must never make a card
  authoritative;
- `weak` may contain grade 1 research leads and must state that it is not
  proof;
- a direct evidence document may also have one document card, but its full
  excerpt must not be duplicated.

Moderate-versus-weak boundary differences are reported diagnostically unless
they violate the useful/noise gates above.

## 11. Phase E30: Exact positive/negative 30

Use the existing expectation-scoped dataset:

```text
.copilot/rag/docs/tests/data/exact-cases-v1.jsonl
```

It contains 30 cases: five positive and five negative cases for each DB.

Run:

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase exact-30 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Use only rows whose explicit expectation applies to the asserted gate.
Do not put positive cases into an unmatched-identifier denominator.

Positive gate:

- complete literal identifier is preserved;
- at least one verified Exact candidate exists;
- the raw occurrence is verified;
- direct evidence is retained;
- broad document results remain available.

Negative gate:

- Exact candidate count is zero;
- Exact signals are zero;
- authoritative evidence for the absent identifier is empty;
- the complete identifier appears in `unmatched_identifiers`;
- a near-collision is never evidence;
- related `document_results` may remain, but are non-authoritative.

Aggregate gate:

```text
expectation-scoped cases complete  30/30
false Exact                        0
lossy alias Exact                  0
neighbor Exact inheritance        0
raw occurrence failures           0
JSON purity failures              0
compact JSON over 16,384 bytes     0
```

## 12. Phase SF: frozen Semantic accuracy

The frozen dataset is:

```text
.copilot/rag/docs/tests/data/semantic-gold-v2.jsonl
SHA-256: fdcf70a137d091d6b453495d7ce1d20b44d28f691177d3b8aaf9a9e27c56eafd
```

Validate the dataset hash and the DB snapshot hashes recorded in
`semantic-gold-v2.md` before execution. Snapshot mismatch makes this phase
`NOT_RUN/artifact_invalid`.

The frozen gates remain unchanged:

- dataset validation: 30/30;
- H/L/V requests and pure JSON: 90/90;
- timeout: 0;
- Hybrid H Hit@5 and Context Recall@1200: at least 80% overall;
- Hybrid H Hit@5 and Context Recall@1200: at least 70% per DB;
- H recall is not lower than L recall;
- Vector Harm Rate: at most 5%.

Run only for regression recording:

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_performance_eval.py" `
  --run-id "$RunId-semantic-frozen" `
  --cases-file "$Repo\.copilot\rag\docs\tests\data\semantic-gold-v2.jsonl" `
  --profiles H L V `
  --executions daemon `
  --daemon-repeats 1 `
  --no-daemon-repeats 0 `
  --timeout 15 `
  --budget-tokens 1200 `
  --max-chars 1200 `
  --diagnostics-level off `
  --explain-mode off `
  --output-dir "$Out"
```

The frozen v2 dataset has already been observed and its historical formal gate
failed. A better current result is a regression result, not permission to
rewrite the historical judgment or tune against v2 and call it an unseen
release validation.

Use `semantic-gold-v2-dev.jsonl` only for development. A later stable semantic
release requires a new unseen v3 holdout with thresholds declared before
inspection.

Final reporting must therefore preserve:

```text
frozen_semantic_v2_historical = FAIL
frozen_semantic_v2_current_regression = PASS|FAIL|NOT_RUN
new_unseen_semantic_release_gate = NOT_RUN until v3 exists
```

## 13. Phase U: unit and static contracts

Execute from the repository test directory using the installed venv:

```powershell
Push-Location "$Repo\.copilot\rag\query"
& $Py -m unittest -v `
  test_persistent_daemon_contracts `
  test_ragd_contracts `
  test_search_contracts
$UnitExit = $LASTEXITCODE
Pop-Location
if ($UnitExit -ne 0) { exit $UnitExit }
```

Minimum assertions:

- importing `search.py`, `ragd.py`, and `rag_manager.py` does not load
  Chroma, ONNX Runtime, transformers, sentence-transformers, or Sudachi;
- `spawn` is used for the worker;
- one manager serializes worker execution;
- queue and per-client bounds are enforced;
- response IDs and DB identity are validated;
- STARTING and BUSY do not start a synchronous fallback;
- worker shutdown follows bounded graceful, terminate, kill, join, close;
- release leases block the target DB until resume;
- manager and worker files are part of the code fingerprint;
- admin mutation entrypoints use the DB mutation guard.

Any failure is an immediate full-run `FAIL`.

## 14. Phase B: same-machine baseline

Run before concurrency and after one clean daemon shutdown:

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase baseline --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Collect:

- manager startup time;
- worker spawn time;
- Dense warmup time;
- first request after manager start;
- 10 serial warm requests for each H/L/V profile and DB;
- median, MAD, maximum, and p95 only where `N >= 20`;
- queue wait and worker execution separately;
- resource baseline after warmup and after a two-minute idle period.

Do not compare p95 from a cell with fewer than 20 samples. Mark it
`NOT_RUN` with `measurement_unavailable`; median and maximum remain valid.

The baseline does not waive the 15-second outer deadline. It defines
same-machine regression and trend references.

## 15. Phase L: 20 repeated lifecycle cycles

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase lifecycle-20 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag
```

For each cycle:

1. start a client with no live manager;
2. observe one manager and one worker;
3. run one search;
4. request graceful manager shutdown;
5. wait for manager and worker exit;
6. confirm the TCP port is closed;
7. confirm the lifecycle lock is acquirable;
8. confirm state is absent or explicitly dead;
9. confirm no test-owned child remains.

Absolute gate:

```text
cycles completed             20/20
search success               20/20
manager count                exactly 1 while live
worker count                 exactly 1 while live
model loads/generation       exactly 1 when Dense became ready
orphan manager/worker        0 after every cycle
port reuse failures          0
state/lock ownership errors  0
```

## 16. Phase C100: 100 direct clients

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase clients-100 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Use one warm manager generation. Launch 100 independent direct
`python.exe search.py` clients in bounded cohorts. Mix DBs and H/L/V profiles.

Absolute gate:

```text
clients completed                  100/100
stdout JSON parse errors           0
response/request mismatch          0
response/client mismatch           0
response DB mismatch               0
duplicate execution                0
ordinary no-daemon fallback        0
client process count after cohort  baseline
established client sockets idle    baseline
manager generations                1
worker generations                 1
live managers                      1
live workers                       1
```

Every client must return a successful expected status or an explicit product
error within 15 seconds. For the advertised concurrency-four cohort, an
overload or queue-deadline error is not a successful request.

## 17. Phase C4: cold and warm concurrency four

### Cold

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase cold-c4 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Start four clients at one common barrier with no live manager.

### Warm

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase warm-c4 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Warm the manager and Dense runtime first, then run H/L/V and mixed-DB
concurrency-four cohorts.

Absolute contract:

```text
one manager
one worker
one model load in the generation
four matching request/client/DB responses
no JSON corruption
no duplicate execution
no fallback
no manager or worker generation change
no response after the 15-second outer deadline
```

Advertised concurrency-four support additionally requires every reviewed
request to succeed. `queue_deadline_expired` is a valid bounded failure
response but fails the support claim.

Baseline-relative reporting:

- compare worker execution time with the matching warm serial cell;
- report queue wait separately;
- compare concurrent throughput with serialized throughput;
- flag a regression when the same-profile median worker time exceeds the
  serial median by more than `max(4 × serial MAD, 25%)`;
- do not fail solely because queued end-to-end latency is approximately the
  sum of serialized worker times, provided all declared deadlines and support
  gates pass.

## 18. Phase R: DB release and management coexistence

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase db-release --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

For every DB:

1. search it so the worker opens its runtime;
2. issue `release_db` and record the lease;
3. wait for `db_released`;
4. verify the old worker is fully reaped;
5. temporarily rename `catalog.sqlite`;
6. temporarily rename the Chroma directory;
7. restore both paths in `finally`;
8. resume the lease;
9. confirm the retired manager exits on Windows;
10. run one new search and verify the new snapshot and generation.

The driver must refuse to rename:

- a path outside the selected DB root;
- a missing path;
- a path already carrying the test suffix;
- a path without a recorded restoration plan.

Management coexistence:

- queue requests for DB A and DB B;
- release DB A;
- DB A requests are cancelled or blocked with the documented release error;
- DB B must never receive DB A data;
- after resume, DB A reloads correctly.

Absolute gate:

```text
PermissionError/sharing violation  0
stale SQLite transaction           0
stale Chroma handle                0
wrong-DB response                  0
restore failures                   0
old worker alive after ACK         0
next search success                1 per DB
```

A restore failure is an immediate safety stop.

## 19. Phase X: crash and termination recovery

### Client termination

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase client-crash --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag
```

Terminate a test client:

- while queued;
- while its request is active;
- immediately after sending;
- while reading the response.

Require the manager to remain alive, other clients to succeed, and the request
to be cancelled or its response discarded without a generation restart.

### Worker termination

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase worker-crash --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag
```

Inject, in separate cases:

- normal worker exit;
- worker hang;
- a worker-held test file handle.

Require:

- manager control endpoint remains responsive;
- old worker is terminated and reaped;
- held file becomes renameable;
- exactly one replacement worker starts;
- queued response IDs are not mixed;
- the next healthy request succeeds.

### Manager termination

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase manager-crash --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag
```

Forcefully terminate the manager after confirming
`worker_job_object_active=true`.

Require:

- the Job Object terminates the worker;
- no model process remains;
- the port becomes reusable;
- the lifecycle lock is recoverable;
- the next search starts exactly one new manager and worker generation.

If Job Object assignment is unavailable, this phase is `FAIL` for the Windows
release gate, not a performance waiver.

## 20. Phase S: 200-request concurrency-four soak

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase soak-200-c4 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Run 200 total requests with four independent client sessions. Mix:

- all available DBs;
- H, L, and V;
- broad document searches;
- Exact positive and negative cases.

Before taking the resource baseline, establish the measured concurrency-four
steady state in this exact order:

1. start one clean manager/worker generation and wait for Dense warmup;
2. run one Hybrid request against each test DB so all three DB runtimes are
   present in the worker cache;
3. run 20 additional requests as five concurrency-four waves, using the same
   mixed DB and H/L/V pattern as the measured soak;
4. require all 20 requests to succeed on the same manager and worker
   generations with `model_load_count=1`;
5. require `active_requests=0` and `queue_depth=0` for three consecutive
   health polls 100 ms apart;
6. idle for two seconds, then repeat the quiescence check;
7. take three baseline samples 250 ms apart.

The 20 concurrency warmup requests are recorded as one
`excluded_resource_warmup` event. They are not case rows, do not count toward
the 200-request cohort, and do not contribute resource-trend samples.

This sequence is the normative meaning of **after warmup** for the soak
resource gate. Serial DB-cache warmup alone is insufficient because it does
not initialize the manager's concurrent request-handler and queue paths. A
warmup failure makes `soak_200_c4` fail and leaves the dependent resource
gates `NOT_RUN`; it must not be hidden by moving the baseline.

Use 10 ordered time buckets of 20 completed requests. Sample resources:

- at the three-point steady-state baseline defined above;
- at every bucket boundary;
- after the final request;
- after a two-minute idle period.

Absolute gate:

```text
successful expected responses  200/200
timeouts                       0
fallback                       0
response mismatch              0
DB mismatch                    0
JSON corruption                0
unexpected worker recycle      0
manager generations            1
worker generations             1
orphan processes               0
```

Do not mix a forced recycle into this clean cohort. Test forced recycling in a
separate crash phase.

## 21. Resource and degradation gates

Let the warm baseline be the median of the three samples taken after the
normative steady-state sequence in Phase S: Dense ready, all three DB caches
open, 20 excluded requests in five concurrency-four waves, daemon quiescent,
and a two-second idle. All baseline samples must report
`active_requests=0`, `queue_depth=0`, and the same manager and worker
generations used by the measured cohort.

Changing the warmup definition does not change any handle, thread, RSS, or
latency threshold below. It only prevents one-time allocation of the
advertised concurrency-four path from being classified as post-warmup growth.

### Handles

Calculate:

```text
manager allowance = max(16, ceil(10% of manager warm baseline))
worker allowance  = max(32, ceil(10% of worker warm baseline))
```

PASS requires:

- final post-idle handle count is within baseline plus allowance;
- no positive handle slope persists across all 10 buckets;
- no abrupt unexplained increase of 100 or more handles in one bucket.

### Threads

Calculate:

```text
manager allowance = max(4, ceil(10% of manager warm baseline))
worker allowance  = max(4, ceil(10% of worker warm baseline))
```

PASS requires final post-idle thread count within the allowance and no
monotonic increase across every bucket.

### RSS

RSS is a trend gate because native allocators retain memory.

FAIL when either is true:

1. post-warmup RSS increases in every time bucket and linear slope remains
   positive after excluding the first warm bucket; or
2. final post-idle RSS exceeds warm baseline by both:

   ```text
   max(20% of baseline, measurement noise band)
   and
   200 MiB
   ```

The noise band is `max(4 × baseline MAD, 32 MiB)` and is calculated before the
soak.

### Latency degradation

For matching profiles, compare the first and last three buckets.

FAIL when:

- median worker execution time degrades by more than
  `max(4 × baseline MAD, 25%)`; and
- the change is not explained by a recorded cold load, recycle, or DB
  transition.

The 15-second outer deadline remains an independent absolute gate.

## 22. Phase O: concurrency-eight overload

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase overload-c8 --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId" --db ac-rag --db incident-rag `
  --db rfc-full-20k-rag
```

Concurrency eight is not an advertised success level. Accept explicit:

```text
daemon_overloaded
queue_deadline_expired
```

Require:

- no manager crash;
- no second worker;
- no response mix-up;
- no fallback;
- no competing model load;
- every client receives success or an explicit bounded overload/deadline
  response within 15 seconds;
- a healthy request succeeds immediately after the overload cohort.

Do not require all eight slow requests to succeed.

## 23. macOS shortened smoke

Run only after all shared unit/protocol tests pass:

```bash
REPO="$(pwd)"
RAG="$HOME/.copilot/rag"
PY="$RAG/query/.venv/bin/python"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
OUT="${TMPDIR:-/tmp}/local-rag-persistent-daemon-$RUN_ID"
mkdir -p "$OUT"

"$PY" "$REPO/.copilot/rag/docs/tests/run_persistent_daemon_windows.py" \
  --phase mac-smoke \
  --installed-rag "$RAG" \
  --output-dir "$OUT" \
  --run-id "$RUN_ID" \
  --db ac-rag \
  --db incident-rag
```

Minimum macOS coverage:

- one cold concurrency-two cohort;
- one excluded one-shot Dense readiness probe after the cold cohort and before
  warm concurrency; this probe is test orchestration only, is not retried, and
  is not counted among the 20 formal mixed-request rows;
- readiness must be observed within 60 seconds of starting the worker
  generation as `dense_warmup_state=ready` and `model_load_count=1`, followed
  by three consecutive idle health samples;
- one warm concurrency-two cohort;
- 20 mixed requests;
- one graceful shutdown;
- one worker termination and recovery;
- request/client/DB identity checks;
- no fallback, orphan, JSON corruption, or generation duplication.
- the excluded Dense probe and every Dense-required H/V request in the warm
  cohort must report `dense_used=true`;
- the Mac runtime smoke uses a non-identifier incident H question so the
  runtime Dense gate cannot be bypassed by Exact/path fast paths. The original
  filename question remains in the separate Broad-quality regression gate,
  where skipping Discovery Dense after Exact is still a failure.

Windows Job Object and Windows DB sharing tests are
`NOT_RUN/not_applicable_platform` on macOS.

## 24. Stop conditions

Stop the full run immediately and preserve artifacts when any of the following
occurs:

- a renamed DB path cannot be restored;
- a test target resolves outside the selected DB root;
- more than one live manager or more than one live search worker exists;
- a response request ID, client ID, or DB does not match;
- a test-owned manager or worker cannot be reaped;
- the manager crash leaves a worker alive;
- JSON output is mixed with logs;
- an ordinary request uses no-daemon fallback;
- a DB reports corruption or a count/hash inconsistency after a release test;
- free system memory falls below the larger of 1 GiB and 10% of physical RAM;
- manager or worker handle count increases by 1,000 from warm baseline;
- the test cannot identify PIDs safely enough to avoid terminating an
  unrelated process.

When stopped:

1. stop launching new clients;
2. do not delete state or logs;
3. restore DB paths;
4. terminate only identity-verified test generations;
5. write the triggering case as `FAIL`;
6. write dependent cases as `NOT_RUN/prior_safety_stop`;
7. capture process, port, state, and log snapshots.

## 25. Artifacts

Formal artifacts are written outside the checkout during the run:

```text
persistent-daemon-environment-<run-id>.json
persistent-daemon-results-<run-id>.jsonl
persistent-daemon-events-<run-id>.jsonl
persistent-daemon-resources-<run-id>.jsonl
persistent-daemon-processes-<run-id>.jsonl
persistent-daemon-broad-search-<run-id>.jsonl
persistent-daemon-exact-<run-id>.jsonl
persistent-daemon-semantic-frozen-<run-id>.jsonl
persistent-daemon-full-report-<run-id>.md
persistent-daemon-failures-<run-id>/
```

The failure directory contains per-case:

```text
<case-id>.stdout.txt
<case-id>.stderr.txt
<case-id>.response.json
<case-id>.state.json
<case-id>.health.json
<case-id>.processes.json
<case-id>.ragd.log
```

Never include the daemon token or proxy credentials in a committed report.
Redact them before copying selected artifacts into the repository.

Build the report with:

```powershell
& $Py "$Repo\.copilot\rag\docs\tests\run_persistent_daemon_windows.py" `
  --phase report --installed-rag "$Rag" --output-dir "$Out" `
  --run-id "$RunId"
```

## 26. Final judgment

Report these gates separately:

```text
unit_contract
structured_request_equivalence
broad_search_contract
broad_search_quality
exact_safety
frozen_semantic_v2_historical
frozen_semantic_v2_current_regression
new_unseen_semantic_release_gate
lifecycle
client_release
cold_concurrency_4
warm_concurrency_4
db_release
client_crash_recovery
worker_crash_recovery
manager_crash_recovery
soak_200
overload_8_safety
resource_handles
resource_threads
resource_rss
latency_trend
mac_short_smoke
windows_runtime_release
broad_feature_release
overall_stable_release
```

`windows_runtime_release=PASS` requires every required Windows lifecycle,
concurrency, crash, soak, resource, and macOS shared-protocol gate to be PASS.

`broad_feature_release=PASS` requires structured request equivalence,
18-case broad quality, Exact safety, compact output, and runtime contract gates
to be PASS.

`overall_stable_release=PASS` additionally requires a PASS on a new unseen
Semantic release holdout. The already observed frozen v2 or v2-dev set cannot
fill that role.

Neither `NOT_RUN` nor a reduced diagnostic cohort is a release PASS. It is
valid and useful for `windows_runtime_release` to PASS while
`overall_stable_release` remains `NOT_RUN` or FAIL because semantic accuracy is
a separate gate.

The report must show both:

- protocol/runtime safety; and
- whether this machine supports the declared four concurrent clients within
  the existing outer deadline.

A slow but correctly bounded overload response can pass overload safety while
failing the concurrency-four support gate. Do not collapse those two judgments
into one result.
