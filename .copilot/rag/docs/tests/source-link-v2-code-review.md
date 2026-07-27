# Source-Link v2 Independent Final Candidate Code Review

## Review basis

- Review date: 2026-07-28
- Branch: `main`
- Baseline and current `HEAD`:
  `a935497306d692bd4383bb266814902feb449ec8`
- Reviewed state: the uncommitted Source-Link v2 candidate diff from that
  baseline.
- Formal Copilot Phase A run:
  `1f60819b11184ed48f02e0172cf15cdc`
- Formal report SHA-256:
  `e29ae04bc14bffc16b240eb29d689843e9360ad23b0843f683efac5cd84384fe`
- Scope: Source-Link schema and identity, observed-root derivation, Manager
  flows, persistence and locking, search and result-bundle integration,
  migration export, live URL validation, Windows reliability, Copilot
  routing and compliance, source hygiene, and the current sanitized evidence.
- Excluded: unrelated pre-existing performance JSONL and
  release/performance report changes.

Priority follows the review scheme:

- **P0**: security, credential disclosure, wrong-Source URL disclosure, data
  loss, corruption, deadlock, or arbitrary path operation.
- **P1**: major product-contract, Windows, migration, or required validation
  failure.
- **P2**: important non-blocking hardening or evidence-quality issue.
- **P3**: minor maintainability or polish issue.

## Decision

### Candidate implementation review

**ACCEPTED — candidate-origin P0: 0; candidate-origin P1: 0.**

The fixed candidate has no open implementation finding for Source-Link
identity, wrong-Source isolation, database identity, locking, atomic
publication, credential rejection, fail-open search behavior, or source
hygiene.

### Overall requested validation

**BLOCKED / UNVERIFIED — the required Copilot formal validation is not
complete.**

The current formal evidence is:

- Auto: **16/16 PASS**;
- Mini: **15/16 PASS**, with one P1 model-compliance failure in `CPL-012`;
- Standard: **UNVERIFIED** because every attempted case encountered the same
  external HTTP 402 quota error;
- Phase B: **NOT_RUN**.

The Mini failure is not a candidate instruction, harness, or Source-Link code
defect. The prompt unambiguously permitted exactly one structured write and
explicitly prohibited shell, PowerShell, placeholder, and no-op calls. The
model nevertheless issued one `Write-Output 'noop'` call before the approved
structured write. The collector correctly rejected it. No extra file or
database mutation occurred, so this is P1 model noncompliance, not P0.

The Standard rows are not product behavior failures. Their raw transcripts
contain `model.call_failure` and `session.error` with HTTP 402
`quota_exceeded`, `premium_interactions` at 1500/1500, and a reset date of
2026-08-01. Standard behavior is therefore unverified.

Phase B was correctly not started after the incomplete Phase A.

### Push authorization

**The candidate must not be represented or pushed as `PASS_AND_PUSHED`, a
completed release gate, or a stable release.**

The implementation diff is technically clean from the independent code-review
perspective, but the user's strict end-to-end completion condition is not met:
Standard Phase A is unverified and Phase B is not run. A successful-completion
push is not authorized by this review. Any checkpoint outside that contract
must be explicitly labelled unreleased and validation-incomplete; it must not
be reported as a passing final result.

## Open findings and blockers

### P0

None.

### Candidate-origin P1

None.

### P1-MODEL-001: Mini issued a forbidden no-op shell call

Formal run `1f60819b11184ed48f02e0172cf15cdc`, Mini `CPL-012`,
performed:

1. one forbidden PowerShell `Write-Output 'noop'` call; then
2. the one approved structured file-write call.

The output file was correct and no additional file changed, but the tool-call
contract was violated. This is a Copilot model/product compliance finding,
not a repository candidate defect.

Do not:

- whitelist the no-op;
- remove or weaken the case;
- select a lucky rerun;
- treat the correct final file as erasing the unauthorized tool call.

The numeric Mini Phase A threshold of 15/16 is met, but the finding must remain
visible in the final report.

### External blocker: Standard quota exhaustion

All 16 Standard cases are **UNVERIFIED**, not behaviorally failed. The
inference service rejected the requests because the account had exhausted its
premium-interaction quota. No candidate defect can be inferred from tool calls
or answers that were never produced or could not complete.

The safe next action is to rerun the entire Standard Phase A on the same frozen
candidate after quota reset or with an explicitly authorized entitlement.
Do not reuse partial Standard rows as a passing cohort.

### P2-001: URL credential rejection is intentionally conservative

The recursively decoded URL classifier rejects some benign-looking
credential-adjacent assignment names when their normalized components match
the deny grammar. This can reject a provider URL, but it fails closed: it
cannot disclose a credential, generate a wrong-Source URL, mutate retrieval,
or weaken export.

If a real provider requires such a query parameter, narrow the grammar only
with paired negative credential tests and the recursive-encoding matrix
intact.

### P3

None.

## Source-Link implementation recheck

### One Source, one configuration

- v2 accepts at most one Provider configuration per existing catalog Source.
- v2 rejects normal-use `database`, `mappings`, `mapping_id`, and
  `path_prefix` fields.
- A sidecar entry cannot create a catalog Source.
- Display-only metadata does not change `source_id`.
- Legacy v1 read compatibility does not rewrite the sidecar.
- Ambiguous or unsafe legacy input fails open to path-only output.

### Observed root and wrong-Source isolation

- The observed root is derived from current visible catalog documents.
- Zero or multiple observed roots do not produce a per-file URL.
- Root removal is component-based and performed exactly once.
- Absolute, drive-qualified, UNC, traversal, and noncanonical stored paths
  fail closed.
- Resolver selection is keyed by the document's existing catalog
  `source_id`.
- Unknown, blank, disabled, unmatched, or cross-Source configurations remain
  path-only.
- The Source-Link contract suite verifies distinct configurations for two
  Sources and rejects unmatched Source use.

No wrong-Source URL path was found.

### Search and database identity

Source-Link enrichment remains after retrieval, fusion, diversification,
packing, evidence classification, and context expansion. The recheck confirms
that enrichment does not alter:

- `doc_id` or `chunk_uid`;
- clean records, catalog rows, Chroma, or embeddings;
- candidates, scores, RRF, ranking, packing, or document diversity;
- evidence authority, answerability, or search status.

`VERSION.json` database identity is derived without the sidecar. The
performance snapshot digest covers `VERSION.json`, `db.json`,
`index/manifest.json`, `logs/index_state.json`, and visible catalog chunk
identity; it does not include `source-links.json` or its backup. Source-Link
cache invalidation uses its own content/revision identity.

Changing the sidecar therefore does not change the logical DB hash, DB
snapshot hash, catalog/vector consistency identity, or daemon DB generation.

### Atomic publication and race behavior

- A persistent per-DB regular lock file is used.
- POSIX uses kernel `flock`; Windows uses one-byte kernel locking.
- The lock file is not unlinked or rewritten during normal release.
- Save validates both revision and raw-content etag.
- Revision and etag are rechecked before primary publication.
- The previous valid primary is retained as the rollback backup only during a
  successful publication sequence.
- Temporary files are flushed, fsynced where practical, and atomically
  published.
- Contention and sharing violations are bounded; there is no unbounded retry.
- Symlink, directory, malformed-lock, concurrent writer, replacement-race,
  and compare-and-swap regressions pass.

No sidecar corruption, lost update, deadlock, or stale wrong-Source URL issue
was found.

### Security and fail-open behavior

- Only HTTP and HTTPS Provider URLs are accepted.
- Embedded credentials, sensitive assignment keys, recursively encoded
  secrets, credential-like paths/fragments, bearer/basic forms, and unsafe
  templates are rejected.
- Provider resolution performs no HTTP request during normal search.
- Missing, deleted, malformed, oversized, ambiguous, or invalid sidecars
  return the original path-only result without changing search status.
- Normal output does not expose mapping settings or Manager-only home URLs.
- Migration export validates the live sidecar, staged snapshot, and extracted
  archive before publication.
- The local backup and persistent lock are excluded from migration payloads.

The reviewed `source_links.py` digest remains:

```text
278f463e3893694f5f12079f2a8de598837170aa3d8ce5f3c47d3103e047330b
```

This is the same implementation digest used by the completed live and
real-Windows Source-Link evidence.

## Current automated validation

The independent final review reran the current candidate's local suites:

| Validation | Result |
| --- | --- |
| Full query contract discovery | **350/350 PASS** |
| Documentation runner unit tests | **78/78 PASS** |
| Focused Source-Link contracts | **41/41 PASS** |
| Migration/export contracts | **10/10 PASS** |
| Source inventory contracts | **11/11 PASS** |
| Manager contracts | **31/31 PASS** |
| Collector static self-test and negative gates | **PASS** |
| Source hygiene scan | **PASS** |
| `git diff --check` and staged diff check | **PASS** |

Previously collected evidence remains applicable because the relevant
Source-Link implementation, Manager, live-E2E runner, and Windows-reliability
runner digests did not change:

| Validation | Result |
| --- | --- |
| Independent credential mutation matrix | **378/378 PASS** |
| Retrieval contracts with fixed hash seeds | **PASS** |
| Live GitHub-compatible and disposable Redmine E2E | **11/11 PASS** |
| Live search-status invariant | **11/11 true** |
| Real Windows reliability, clean run 1 | **23/23 PASS** |
| Real Windows reliability, clean run 2 | **23/23 PASS** |
| Windows sequential saves / concurrent writers / stress | **100 / 8 / 1,000 PASS** |
| Windows Manager start/exit and credential rejection | **100 / 53 PASS per run** |

The optional untracked `RAG_SENSITIVE_TERMS_FILE` denylist was not configured
for this review. The built-in tracked-source hygiene scan passed.

## Formal Copilot evidence

### Current Phase A only

| Requested profile | Actual model | Runs | Pass | P0 | P1 | Classification |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Auto | `claude-sonnet-4.6` | 9 | 9 | 0 | 0 | PASS |
| Auto | `gpt-5.3-codex` | 5 | 5 | 0 | 0 | PASS |
| Auto | `gpt-5.4-mini` | 2 | 2 | 0 | 0 | PASS |
| Mini | `gpt-5.4-mini` | 16 | 15 | 0 | 1 | MODEL NONCOMPLIANCE |
| Standard | `gpt-5.4` | 16 | 0 verified | 0 | 0 attributable | HTTP 402 / UNVERIFIED |

Aggregate:

```text
Phase A executions: 48
Recorded PASS:       31
Auto:                16/16 PASS
Mini:                15/16 PASS
Standard:            UNVERIFIED (HTTP 402 quota_exceeded)
Phase B:             NOT_RUN
```

Observed token totals in the raw collector report are telemetry records, not
hard gates:

| Requested profile | Input tokens | Output tokens |
| --- | ---: | ---: |
| Auto | 3,636,114 | 28,026 |
| Mini | 3,365,466 | 39,578 |
| Standard partial attempts | 115,754 | 1,654 |

No reliable nonzero AI Credits telemetry was available in this report; do not
infer credits from token totals.

Earlier diagnostic and failed cohorts are superseded and excluded from this
final evidence table. They are not release evidence and their older digests
must not be presented as the current result.

## Evidence binding

| Artifact | SHA-256 |
| --- | --- |
| `source_links.py` | `278f463e3893694f5f12079f2a8de598837170aa3d8ce5f3c47d3103e047330b` |
| `source_inventory.py` | `5f66af24c37a49da9e88266d4a3259b69aab0ef70611907cf365ff4ba63f181b` |
| Manager | `a180bc23b0a8f778ce195a2c902586f2fb35ec04085c6970dd8b62d5de6fea46` |
| Migration archive implementation | `24ceaa25f0e85d1ef9374c1e06f50d7290dbe177759f59509612805fedc930b5` |
| Search integration | `b1ec4288b5644bd8977c1e69bd6a89901e089175ceaeac14f49b4d30a1a49553` |
| Copilot collector | `f5d14e3962d09172be630c9d2a8277b2837d3a6175dba583125a73da4cb07aca` |
| Copilot case matrix | `8bea9fe5c3ee91ce212dfa7a136e5d424c43b8301f48bcc3d2969252f239fb42` |
| Copilot PowerShell runner | `bde92a3416c752918ab211a741041b05e90a3b4288b1ea5c31ba047f47839b88` |
| `local-rag` skill | `39a9873305a879c8aff3db74fd062ae8a8f9db23a754f12d625b5a907b941dd1` |
| Always-loaded RAG router | `ffcf6aa885f42b663f2e2f8853170bf578abc00624b3aa394ec1bbd70dbe0f3a` |
| Copilot compliance specification | `3eb94d6031fd771983267dfadb9e39cc3419ec87784738afdb71dd9ce62d729a` |
| Lightweight-routing contracts | `6c85da2e3bfef30f2801cb15005d167d0266cedd8ea2efc0be52f4af15ce6feb` |
| Retrieval implementation | `e47b8336bf3afb04d699792734ae2bd404966b5fea2f9cfb351cb60e6fda53b2` |
| Retrieval contracts | `b6ee1df2a98b43ffaddb50297cdee49dfdb1411afcd6c033192eabc07e108071` |
| Formal Phase A report | `e29ae04bc14bffc16b240eb29d689843e9360ad23b0843f683efac5cd84384fe` |

## Privacy and review integrity

The tracked live and Windows evidence contains only synthetic identifiers,
reserved invalid domains, URL hashes, placeholders, and aggregate results.
The current built-in source-hygiene scan passed.

This independent review changed only this review artifact. It did not modify
implementation source, runners, product Skills, cases, fixtures, or tests.
Unrelated dirty performance artifacts were preserved and excluded.

## Required next steps

1. Keep the exact candidate hashes frozen.
2. After quota reset or authorized entitlement, rerun all 16 Standard Phase A
   cases from fresh sessions.
3. Do not relabel quota failures as model failures or passing tests.
4. If the complete Phase A meets the declared gate, run the full Phase B
   matrix from fresh sessions.
5. Preserve the Mini `CPL-012` model noncompliance in the final report; do not
   weaken its oracle.
6. Re-run source hygiene and diff checks after adding final sanitized formal
   artifacts.
7. Obtain an independent final review of the completed Phase A and Phase B
   reports before any `PASS_AND_PUSHED` conclusion.

Until those steps complete, the only accurate overall state is
**BLOCKED / UNVERIFIED**, not PASS.
