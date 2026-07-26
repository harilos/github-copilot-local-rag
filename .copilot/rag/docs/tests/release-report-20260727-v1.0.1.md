# Local RAG 1.0.1 implementation and release validation report

Date: 2026-07-27

Tested code commit: `f12ff3fd7b5f7103f404678f6ab2683db1b0dbcb`

Target: `/Users/haruki/Documents/rag/.copilot/rag`

## Outcome

| Area | Result |
|---|---|
| Version metadata and README | PASS (`1.0.1`) |
| One-shot Copilot lookup contract | PASS |
| Runtime, daemon, deadline, and fallback | PASS |
| Windows clean mixed performance | PASS |
| Exact and no-hit contract | PASS |
| Windows management smoke | PASS |
| macOS short smoke | PASS |
| Semantic retrieval absolute accuracy | **FAIL** |
| Stable `v1.0.1` tag / GitHub Release | **NO-GO** |

The implementation and operational performance are stable on the tested
Windows and macOS environments. The stable release is still blocked because
the frozen Semantic gold v2 set did not meet its predeclared absolute accuracy
thresholds. No tag or GitHub Release was created.

`H-L = +10` percentage points and zero Vector Harm support keeping Dense in the
current default route. They do not replace the separate absolute accuracy
gate.

## 1. Implemented changes

### Copilot routing and skills

- Reduced `.copilot/instructions/rag.instructions.md` to a short lookup/admin
  router.
- Added an English-only `local-rag` read-only lookup skill.
- Added an English-only `local-rag-admin` management skill.
- Ordinary lookup does not create a plan, inspect implementation code, edit
  data, or delegate to Codex, a coding agent, or a subagent.
- When the database is omitted, Copilot runs `list_dbs.py --format json` once,
  selects one clearly matching database, and then runs `search.py` once with
  the complete original question.
- When the database is explicit, `list_dbs.py` is skipped.
- Query rewriting, automatic retry, automatic second-database search,
  `--auto`, model-name hardcoding, custom agents, and `.agent.md` were not
  introduced.

### Lightweight CLI contract

- Added pure JSON output to `list_dbs.py` while retaining its human-readable
  interface.
- The JSON database list contains only name, title, and a short query hint.
- Normal lookup instructions use the existing venv interpreter directly on
  macOS/Linux and Windows.
- A missing venv is treated as `setup_required`; normal lookup does not probe
  `python`, `python3`, and `py` in sequence.
- Search JSON separates authoritative `evidence`, `background_context`, and
  non-authoritative `related_context`, with explicit status, answerability,
  and warnings.
- Compact JSON removes ordinary execution/debug detail and keeps detailed
  candidate data behind diagnostic modes.

### Retrieval and daemon runtime

- Added a conservative low-document-frequency lexical anchor path inside one
  Hybrid request.
- Exact, BM25, Dense, metadata, RRF, and neighboring-context behavior remain
  parts of the same retrieval request.
- A raw low-DF token is not promoted to authoritative evidence unless its
  coverage and same-document support checks pass.
- Generic hyphenated words and terms such as `IPv6` are not automatically
  treated as strong Exact identifiers.
- No language-, region-, product-, filename-, A2W-, or query-specific branch
  was added.
- The daemon now honors adaptive Hybrid routing and tracks process readiness
  separately from Dense runtime readiness.
- Cold Dense initialization receives the remaining outer deadline instead of
  being misclassified as a warm daemon timeout.
- Warm daemon failure still uses one bounded read-only no-daemon fallback,
  retires the failed generation, and records first-attempt and user-visible
  outcomes separately.

### Evaluation and reporting

- Run populations are separated by compatible commit, DB snapshot, mode,
  diagnostics, profile, execution, timeout, and sequence conditions.
- `N=0` is reported as `NOT_RUN` or `NOT_APPLICABLE`.
- p95, hard maximum, timeout rate, low-N, generation stability, and time
  degradation are separate gates.
- Exact/no-hit denominators are restricted to cases carrying the corresponding
  expectations.
- Added OR alternatives and required AND groups to Semantic gold evaluation.
- Related context is never scored as authoritative semantic evidence.
- JSONL is marked `-text` so frozen dataset bytes remain stable across Windows
  and macOS checkouts.

## 2. Principal changed files

- `.copilot/instructions/rag.instructions.md`
- `.copilot/skills/local-rag/SKILL.md`
- `.copilot/skills/local-rag-admin/SKILL.md`
- `.copilot/rag/query/list_dbs.py`
- `.copilot/rag/query/search.py`
- `.copilot/rag/query/ragd.py`
- `.copilot/rag/gen_db/software_rag_tool/scripts/query.py`
- `.copilot/rag/gen_db/software_rag_tool/software_rag_tool/catalog.py`
- `.copilot/rag/gen_db/software_rag_tool/software_rag_tool/db_runtime.py`
- `.copilot/rag/gen_db/software_rag_tool/software_rag_tool/retrieval.py`
- `.copilot/rag/gen_db/software_rag_tool/software_rag_tool/search_api.py`
- `.copilot/rag/gen_db/software_rag_tool/software_rag_tool/tokenize.py`
- `.copilot/rag/docs/tests/run_performance_eval.py`
- `.copilot/rag/docs/tests/run_forced_fallback_smoke.py`
- `.copilot/rag/docs/tests/data/semantic-gold-v2.jsonl`
- related query, retrieval, daemon, routing, and harness contract tests
- `.copilot/rag/VERSION`
- `README.md`
- `.gitattributes`

## 3. Static and contract tests

The same clean code commit was installed and tested on Windows.

| Suite | Result |
|---|---:|
| Query/runtime contracts | 95/95 PASS |
| Performance harness contracts | 33/33 PASS |
| Python syntax compilation | PASS |
| Diff whitespace validation | PASS |
| Windows checkout before formal tests | clean |
| macOS checkout before/after short smoke | clean |

The Windows management smoke also passed temporary DB creation, status, build,
resume, add, lexical rebuild, final status, and representative search. The
final temporary DB contained three documents and three chunks.

## 4. Windows clean mixed 500

Formal environment:

- commit: `f12ff3fd7b5f7103f404678f6ab2683db1b0dbcb`
- `git_dirty=false`
- transport: Windows TCP daemon
- production conditions: explain off, diagnostics off
- outer timeout: 15 seconds
- daemon warm-attempt timeout: 5 seconds
- warmup: 5
- measured requests: 500

Summary:

| Gate | Result |
|---|---:|
| Search success | 500/500 PASS |
| First-attempt daemon success | 500/500 PASS |
| Actual daemon execution | 500/500 PASS |
| JSON stdout purity | 500/500 PASS |
| Timeout | 0 PASS |
| Fallback | 0 PASS |
| Outer deadline exceeded | 0 PASS |
| Maximum latency | 1.467904 sec PASS |
| Time degradation | PASS |
| Daemon generation stability | PASS |

Per-cell latency:

| Database | Profile | N | p95 sec | Target sec | Max sec | Result |
|---|---:|---:|---:|---:|---:|---:|
| `ac-rag` | H | 120 | 1.242618 | 8 | 1.251432 | PASS |
| `ac-rag` | L | 35 | 1.232669 | 2 | 1.233151 | PASS |
| `ac-rag` | V | 20 | 1.222643 | 8 | 1.229356 | PASS |
| `incident-rag` | H | 100 | 1.271095 | 8 | 1.285806 | PASS |
| `incident-rag` | L | 30 | 1.251061 | 2 | 1.251846 | PASS |
| `incident-rag` | V | 20 | 1.248428 | 8 | 1.251165 | PASS |
| `rfc-full-20k-rag` | H | 120 | 1.449521 | 8 | 1.467904 | PASS |
| `rfc-full-20k-rag` | L | 35 | 1.330348 | 2 | 1.333351 | PASS |
| `rfc-full-20k-rag` | V | 20 | 1.234419 | 8 | 1.236192 | PASS |

Formal artifact hashes:

- status:
  `6770a1eddf9557f7d79304c2c68ab431d2bc955c59dac35a9a0afc51826777f8`
- raw JSONL:
  `2bebe28c651f86b2d9542c604fd9bcc9931813e0c4e209c71fe6ce718f7bd637`
- generated report:
  `197eb732e3dde7f3e6bc9c19a5798cbb685e376ea260d0e8ea143ee894085678`

The remote artifacts remain under:

`C:\Users\harilos\Desktop\rag-release-results\f12ff3fd7b5f7103f404678f6ab2683db1b0dbcb`

## 5. Exact and no-hit

Windows:

| Gate | Result |
|---|---:|
| Requests | 30/30 PASS |
| Positive Exact | 15/15 PASS |
| Raw matched occurrence | 15/15 PASS |
| Negative near-collision | 15/15 PASS |
| False Exact | 0 PASS |
| Strong no-hit | 15/15 PASS |
| Timeout | 0 |
| Maximum latency | 1.305 sec |

macOS repeated the representative three-database contract:

- Exact positive: 3/3
- Exact negative and no-hit: 6/6
- false Exact: 0
- authoritative evidence on negative cases: 0

## 6. Forced fallback

Windows forced one warm-daemon timeout per database:

- 3/3 user-visible success
- exactly one no-daemon fallback per request
- old generation retired
- next required-daemon request used a new generation
- maximum wall time approximately 7.93 seconds

macOS repeated the same contract after final synchronization:

| Database | Wall sec | Result |
|---|---:|---:|
| `ac-rag` | 11.374896 | PASS |
| `incident-rag` | 10.565729 | PASS |
| `rfc-full-20k-rag` | 11.084644 | PASS |

All three remained within the 15-second user-visible deadline.

## 7. macOS final short smoke

The clean AC database and code were synchronized to
`/Users/haruki/.copilot/rag` before this run.

| Channel | Result | Maximum sec |
|---|---:|---:|
| 3 DB daemon cold/warm | 9/9 PASS | 6.204172 |
| 3 DB Exact/no-hit contract | 9/9 PASS | 1.103331 |
| 3 DB no-daemon Hybrid | 3/3 PASS | 5.828687 |
| 3 DB forced fallback | 3/3 PASS | 11.374896 |
| Total | 24/24 PASS | 11.374896 |

JSON purity and the 15-second deadline passed for all 24 requests.

## 8. Semantic gold v2

Freeze identity:

- dataset:
  `.copilot/rag/docs/tests/data/semantic-gold-v2.jsonl`
- dataset SHA-256:
  `fdcf70a137d091d6b453495d7ce1d20b44d28f691177d3b8aaf9a9e27c56eafd`
- cases: 30, ten per database
- languages: 24 Japanese, 6 English
- required claim groups: 48
- AC snapshot:
  `811c6d556abc020423b7e45c605637ea46690b02552869dc0327e1e1ac9acb56`
- incident snapshot:
  `f825401fbe70dac6ae5d41850a7e59d8db9e399a1a25b9f66fb9016c4aaa5ba6`
- RFC snapshot:
  `7c2a9d6f17f7e0397f61bf64c8897d760141de0e1c8b515e4ceb7adb13ba62ef`

Execution integrity:

| Gate | Result |
|---|---:|
| Requests | 90/90 PASS |
| JSON purity | 90/90 PASS |
| First-attempt success | 90/90 PASS |
| Timeout | 0 |
| Fallback | 0 |
| Maximum latency | 8.468470 sec |

Accuracy:

| Database | H recall | L recall | V recall | H per-DB gate |
|---|---:|---:|---:|---:|
| `ac-rag` | 60% | 40% | 50% | FAIL (`<70%`) |
| `incident-rag` | 50% | 50% | 70% | FAIL (`<70%`) |
| `rfc-full-20k-rag` | 10% | 0% | 0% | FAIL (`<70%`) |
| Overall | **40%** | **30%** | **40%** | **FAIL (`<80%`)** |

Vector observations:

- H minus L recall: `+10` percentage points
- Vector Rescue: `3/21` L misses
- Vector Harm: `0/9` L hits
- Dense policy decision: retain Dense in the current default route
- Absolute release accuracy: FAIL

The final run was reproduced once with identical code, cases, snapshots,
thresholds, and settings solely to close a missing-raw provenance gap. It
reproduced the same failing verdict and did not involve retrieval tuning or
pass-seeking.

Final Semantic artifact hashes:

- raw JSONL:
  `7039e7fef3f2e82d2bb860357b10970b9a7acbc887eeb86688861f3b5a9d8e8d`
- generated report:
  `66eecdaae138048b190da303dec0dd007d7a1ce34581df2460fc57c15f7268f1`

## 9. Database and installation state

Six unlabelled AC ingestion-test documents were removed from the production
test corpus by a recoverable move. `ac-rag` was rebuilt on Windows:

- documents: 15
- chunks: 1195
- snapshot:
  `811c6d556abc020423b7e45c605637ea46690b02552869dc0327e1e1ac9acb56`

The clean database and implementation were synchronized to both:

- `/Users/haruki/Documents/rag/.copilot/rag`
- `/Users/haruki/.copilot/rag`

The repository and installed AC snapshots match. The installed runtime files
and `VERSION=1.0.1` also match.

Recoverable pre-clean macOS AC backups:

- `/tmp/ac-rag-mac-repo-backup-20260727-025332-preclean`
- `/Users/haruki/.copilot/rag/dbs/ac-rag.backup-20260727-025332-preclean`

The retrieval/runtime code changes do not otherwise require users to rebuild an
uncontaminated existing database or index. The AC rebuild was required for this
specific corpus-hygiene correction.

## 10. Independent high-capability agent review

The independent reviewer inspected the Windows raw/report/status artifacts,
macOS artifacts, frozen dataset, fingerprints, commit, dirty state, and DB
snapshots.

Review conclusion:

- evidence validity: PASS
- provenance gap after evidence reproduction: closed
- P0 issues: 0
- runtime, performance, Exact, no-hit, deadline, and fallback: PASS
- Semantic absolute accuracy: FAIL
- stable `v1.0.1` tag / Release: NO-GO

The reviewer explicitly rejected using `H-L +10` and zero Harm as a waiver for
the frozen absolute accuracy thresholds.

## 11. Deliberately not implemented or executed

These items were excluded by the requested scope or stop rules:

- Python-side database auto-selection
- `--auto` during ordinary lookup
- model-name hardcoding or requiring a particular Auto-selected model
- custom agent or `.agent.md`
- new LLM, reranker, embedding model, or RRF grid search
- query- or proper-noun-specific rescue branches
- large table parser or schema migration
- DB-wide rebuild other than the contaminated AC corpus
- macOS 500-request run or long soak
- further tuning or repeated pass-seeking on Semantic gold v2

Semantic gold v2 is now a fixed development/regression set. Any later retrieval
improvement must be made in a later commit/version and evaluated once against a
new, unseen, pre-frozen holdout set.

## 12. Remaining risks and release decision

Remaining P1 risk:

- semantic retrieval recall is below the frozen release threshold, especially
  for the RFC database.

Minor integration limitation:

- Auto is intentionally allowed to select any model. Model selection itself is
  not a gate, and no particular lightweight model can be guaranteed by this
  repository.

Final decision:

```text
implementation:               PASS
Windows functional/performance: PASS
macOS short smoke:            PASS
Semantic absolute accuracy:   FAIL
stable v1.0.1 tag/release:    NO-GO
```

The tested implementation may remain on `main`, but the stable tag and GitHub
Release must remain absent until a later version meets a new valid semantic
release gate.
