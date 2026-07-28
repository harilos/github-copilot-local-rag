# GitHub Copilot Local RAG compliance harness v1

## Purpose

This harness verifies that GitHub Copilot follows the current Local RAG
one-shot and cached-detail contracts. It runs 16 cases in three model
profiles:

- `auto`;
- a Mini-class model supplied at run time;
- a Standard-class model supplied at run time.

Phase A contains 48 fresh sessions: 16 cases once in each profile. Phase B is
an additional repeatability cohort over eight focused cases: four Auto
executions plus two Mini and two Standard executions per case, for exactly 64
fresh sessions. No concrete Mini or Standard model name is stored in this
repository. Auto is requested with the Copilot CLI's `auto` selector; the
model actually selected by Auto is detected from OpenTelemetry and recorded,
but is not a pass/fail choice.

This is a behavioral compliance harness, not a semantic relevance benchmark.
It does not replace Exact, broad-discovery, or unseen Semantic holdout tests.

This is also an **optional, explicitly metered product test**. It is excluded
from normal unit tests, full regression runs, release gates, and routine
post-change validation. A missing, failed, quota-blocked, or unexecuted
Copilot cohort does not fail the Local RAG software release gate. Report its
status separately as `PASS`, `FAIL`, `UNVERIFIED`, or `NOT_RUN`.

The runner refuses all product-model execution unless
`-AllowMeteredRun` is supplied. Phase B additionally requires
`-AllowRepeatCohort`. Static `-SelfTest` remains free of model calls and does
not require either switch.

## Files

```text
docs/tests/copilot-compliance-v1.md
docs/tests/data/copilot-compliance-cases-v1.jsonl
docs/tests/run_copilot_compliance.ps1
docs/tests/collect_copilot_compliance.py
```

Raw output and reports must be written outside the fixture workspace and are
not committed.

## Case coverage

| ID | Contract |
| --- | --- |
| CPL-001 | Explicit DB: zero list calls and one search |
| CPL-002 | Implicit clear DB: one list and one search |
| CPL-003 | Ambiguous DB choice stops after one list call |
| CPL-004 | General Python explanation does not activate RAG |
| CPL-005 | Absent `ORBIT-8` near `ORBIT-7` stays no-hit and does not retry |
| CPL-006 | Broad multi-document discovery remains one search |
| CPL-007 | File result pointer; initial answer uses summary first and may read cached detail once when needed |
| CPL-008 | Follow-up detail uses the cached result, not retrieval |
| CPL-009 | Source reference priority is permalink, URL, then stored path |
| CPL-010 | Source-Link management explanation performs no mutation or manager run |
| CPL-011 | An explicit UTF-8 `queries.txt` is read once and not modified |
| CPL-012 | Only the specified UTF-8 `report.md` output is written |
| CPL-013 | A missing input does not trigger similar-file selection, search, or creation |
| CPL-014 | Windows PowerShell command uses direct venv Python and no wrapper |
| CPL-015 | Windows Git Bash uses the direct venv `Scripts/python.exe` path |
| CPL-016 | Valid partial stdout JSON is honored without an automatic retry |

Phase B focuses on `CPL-001`, `CPL-002`, `CPL-005`, `CPL-007`, `CPL-008`,
`CPL-009`, `CPL-010`, and `CPL-016`.

Every case declares exact counts for:

- `list_dbs.py`;
- `search.py`;
- `result_detail.py`;
- `summary.json` reads;
- expanded detail-file reads;
- forbidden `manifest.json` and raw `items/*.json` reads;
- initial result pointers and detail pointers;
- subagent calls;
- automatic retries.

## Synthetic fixtures

The committed JSONL contains placeholders only. Build a disposable fixture
workspace and provide a JSON object that resolves every placeholder. Use only
synthetic documents and synthetic database identities in that workspace.

Example variable file:

```json
{
  "EXPLICIT_DB": "synthetic-topic-rag",
  "DIRECT_QUESTION": "What does the fixture say about the primary component?",
  "CLEAR_TOPIC_QUESTION": "What does the synthetic cooling fixture say?",
  "AMBIGUOUS_TOPIC": "the shared synthetic topic",
  "BROAD_TOPIC": "the synthetic component and related designs",
  "TABLE_QUESTION": "What does the synthetic table establish?",
  "FOLLOW_UP_ITEM_ID": "E1",
  "SOURCE_PERMALINK": "https://permalink.invalid/synthetic-item",
  "SOURCE_URL": "https://browse.invalid/synthetic-item",
  "SOURCE_PATH": "Synthetic Root/docs/source.md",
  "QUERIES_FILE_SENTINEL": "日本語の合図"
}
```

Fixture requirements:

1. `EXPLICIT_DB` names an installed synthetic DB ending in `-rag`.
2. `CLEAR_TOPIC_QUESTION` clearly matches exactly one DB from its name,
   title, and query hint.
3. `AMBIGUOUS_TOPIC` plausibly matches at least two installed synthetic DBs.
4. `ORBIT-7` has a literal occurrence and `ORBIT-8` is absent.
5. The absent identifier still produces non-authoritative `related_context`
   or `document_results`, plus a partial stdout result for `CPL-016`.
6. The broad case has multiple useful synthetic documents.
7. The table case includes an explicit limitation or missing-header fixture.
8. The direct result exposes the three distinct synthetic source references
   from the variable file so URL priority is observable.
9. `fixtures/queries.txt` is UTF-8 without a BOM, contains
    `QUERIES_FILE_SENTINEL`, and
    `fixtures/missing-input.txt` does not exist.
10. `compliance-output/` is writable. `EXECUTION_TAG` is generated by the
    runner and must not be placed in the variable file.
11. The raw-output root is outside the fixture workspace.

Do not place user names, company names, company URLs, credentials, production
paths, or real internal text in the committed cases or documentation.
Runtime-resolved prompts remain only in the external raw-output directory.

## Model parameters

Obtain currently available model identifiers from the Copilot environment at
run time. Pass them to:

```text
-MiniModel <runtime-mini-model-id>
-StandardModel <runtime-standard-model-id>
```

Do not edit the case file to add a model name. `-AutoModel` must remain
exactly `auto`. Mini and Standard must be explicit, non-Auto, and distinct.
For explicit profiles, the collector requires the specific
`gen_ai.request.model` value to equal the runtime argument and requires one
unambiguous selected response model. For Auto, it requires one selected model
to be observable and records every selected model. Auto may select a different
model for a resumed follow-up turn, so multiple selected models are valid only
for the Auto profile.

## Execution

Use PowerShell and an explicit Python executable for the collector:

```powershell
& "<POWERSHELL_EXE>" -NoProfile -File `
  "<REPO_ROOT>/.copilot/rag/docs/tests/run_copilot_compliance.ps1" `
  -CollectorPython "<PYTHON_EXE>" `
  -CopilotPath "<COPILOT_EXE>" `
  -MiniModel "<MINI_MODEL_FROM_CURRENT_COPILOT_LIST>" `
  -StandardModel "<STANDARD_MODEL_FROM_CURRENT_COPILOT_LIST>" `
  -FixtureWorkspace "<SYNTHETIC_FIXTURE_WORKSPACE>" `
  -VariablesJson "<SYNTHETIC_VARIABLE_FILE>" `
  -OutputRoot "<OUTPUT_DIRECTORY_OUTSIDE_WORKSPACE>" `
  -AllowMeteredRun `
  -Phase A
```

Run Phase B only after a separate, explicit decision to spend the additional
model quota. Use the same command with `-Phase B -AllowRepeatCohort`. Phase B
writes its raw cohort under `phase-b/` and does not overwrite Phase A.

The runner deliberately does not use:

- `--silent`;
- autopilot;
- session continuation between cases;
- prompt/response OTel content capture;
- a hard-coded Mini or Standard model.

Each case/profile starts with a new UUID session. A two-turn case resumes only
its own session for the second turn. OTel is written to a distinct JSONL file
per case/profile. The temporary per-turn OTel files are concatenated into
that one case file.

The runner sets the Copilot CLI's minimum accepted 30-credit session ceiling
per turn. This is a safety ceiling, not an expected charge; actual reported
AI Credits are recorded separately. Override it only after reviewing the
initial measurements.

Do not place this command in a standard CI job, installer validation, normal
`full test` alias, or release script. Prefer the static self-test for routine
changes. Run a paid product cohort only when a human explicitly requests it
and has reviewed the current quota and billing state.

## Machine-verifiable gates

The collector fails closed. Missing CLI JSON events, missing OTel model data,
invalid JSONL, absent run metadata, or an unobservable required tool call
cannot become PASS.

It verifies:

- all 16 cases and all three profiles are present;
- all Copilot turn exit codes are zero;
- the selected model is visible in OTel;
- exact Local RAG script call counts;
- exact result-pointer and cached-detail-pointer counts;
- exact `summary.json` and expanded-detail reads;
- no `manifest.json` or raw detail-item reads;
- no unrelated file-read tool calls;
- no file-write/edit/delete tool calls except the single exact output allowed
  by `CPL-012`;
- no tool invocation other than the exact RAG commands, approved summary or
  cached-response reads, loading the ordinary `local-rag` skill, and loading
  `local-rag-admin` only for `CPL-010`;
- no fixture-workspace changes except the exact `CPL-012` report path;
- direct installed venv Python for every RAG command;
- `shell=False` cannot be observed directly from Copilot, so the external
  command contract is instead checked for the direct venv executable and the
  absence of shell wrappers;
- no PATH-based `python`, `python3`, or `py` fallback;
- no `cmd.exe /c`, `Start-Process`, batch wrapper, `--auto`,
  `--no-daemon`, `--retrieval-mode`, or JSON stdin request;
- no `jq`, `grep`, `head`, or `tail` post-processing;
- required list/search/detail options;
- the complete human-authored visible prompt is observable as one directly
  quoted final argument in each search call;
- Copilot-generated runtime, session, status, and system metadata is absent
  from that argument;
- PowerShell here-strings and other multiline question containers are absent;
- `--answer-goal` uses only the documented closed enum and every planning
  value remains one quoted argv token;
- no subagent/delegated-planner tool call;
- no automatic search retry, including after a nonzero process exit;
- an explicit output-file case uses only its one approved structured write
  tool, with no shell placeholder or no-op calls.

For `CPL-009`, `source_permalink` must be the selected link and the lower
priority `source_url` must not be presented as an alternative. The stored path
may still appear as non-link provenance because the lookup Skill separately
requires source-path identification.

The workspace snapshot excludes runtime-owned mutable areas:

```text
.git/
.copilot/rag/dbs/
.copilot/rag/models/
.copilot/rag/query/.venv/
.copilot/rag/query/run/
.copilot/rag/logs/
```

Except for the exact `CPL-012` output file, every other added, removed, or
modified fixture-workspace file fails the case. Raw CLI tool events are also
inspected so a source-code read can fail even when it does not change a file.

If a future Copilot CLI version changes its JSON event schema so tool
arguments are no longer observable, the harness fails with
`tool_call_telemetry_not_observed`; update and review the parser rather than
weakening the gate.

Pointers count only when they occur in a completion event correlated by tool
call ID to the corresponding `search.py` or `result_detail.py` invocation.
Merely mentioning a pointer schema or `summary.json` in assistant text cannot
satisfy a gate. File-read counts likewise require a recognized read
invocation, not a textual path mention.

CLI `1.0.75` may omit `data.phase` on a terminal assistant message. In that
schema, the collector accepts a nonempty message only when it contains an
explicit empty `toolRequests` list and is followed by the matching
`assistant.turn_end` without another assistant/tool action intervening.
For `CPL-014` and `CPL-015`, the case also requires exactly one direct
`local-rag` Skill call. Zero, duplicate, different, or nested/spoofed Skill
loads fail the case. The separately labelled lookup question is copied
exactly as the final argv token; surrounding static-command instructions are
not part of that question.

Every repeated structured planning value is one quoted argv token. A split
multiword facet is invalid even when the executable, database, and final
question are otherwise correct. A correlated nonzero or truncated search
process completion is an explicit case failure.

Search stdout is accepted only from the exact call-correlated final
completion. CLI `1.0.75` uses one direct `data.result.contents` item with
`type=shell_exit`, matching nonempty shell IDs, exact boolean success,
integer exit code zero, `outputTruncated=false`, and a complete
`outputPreview`. When that schema is present it is exclusive: partial,
nested, truncated, failed, ambiguous, or conflicting fields cannot satisfy
the stdout contract. A documented legacy root stdout field or exact
`data.result.content` is accepted only when the shell-exit schema is absent.

The missing-file case permits one narrowly defined PowerShell probe: a
single-quoted exact path assigned to `$path`, followed by one
`Test-Path -LiteralPath` condition, a same-variable
`Get-Content -LiteralPath ... -Raw` branch, and the fixed
`__MISSING__` marker branch. The collector rejects alternate paths, globbing,
interpolation, pipelines, added commands, changed markers, and wrong tools.

The CLI transcript does not expose a reliable stdout-versus-stderr stream
identity for a PowerShell tool result. The formal compliance case therefore
checks the correlated partial JSON, no retry, and answer fidelity. Stream
purity and warning separation remain mandatory in the subprocess contract
suite, where the two streams are independently observable.

## Output

The output tree is:

```text
<output>/
  auto/
    CPL-001/
      copilot.jsonl
      otel.jsonl
      stderr-turn-1.log
      run.json
  mini/
    ...
  standard/
    ...
  copilot-compliance-report-v1.json
  phase-b/
    auto/
      CPL-001/
        repeat-01/
        repeat-02/
        repeat-03/
        repeat-04/
    mini/
      ...
    standard/
      ...
  copilot-compliance-report-v1-phase-b.json
```

The Phase A report has exactly 48 result rows. The Phase B report has exactly
64 result rows. Each is PASS only when every row passes. Reports also record
selected models, token/credit telemetry when exported by the installed CLI,
call counts, file-I/O counts, and exact failure reasons.

## Static self-test

Run before spending model credits:

```powershell
& "<POWERSHELL_EXE>" -NoProfile -File `
  "<REPO_ROOT>/.copilot/rag/docs/tests/run_copilot_compliance.ps1" `
  -CollectorPython "<PYTHON_EXE>" `
  -SelfTest
```

Or directly:

```text
<PYTHON_EXE> collect_copilot_compliance.py --self-test
```

The self-test checks parser behavior with synthetic event records and proves
that an extra search, a subagent, an unapproved shell call, a file write, an
unlinked pointer, empty telemetry, invalid model-profile settings, and
metadata identity mismatches fail their gates. The PowerShell entry point
also verifies that the JSONL reader returns all 16 cases and that an empty
workspace diff remains an empty array. Its output explicitly states that it
is not a Copilot product compliance result. It never creates a product PASS,
deletes a test, or substitutes generated observations for real executions.
