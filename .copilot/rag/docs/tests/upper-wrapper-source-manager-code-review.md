# Upper Wrapper and Source Manager Code Review

## Review decision

**APPROVED — unresolved P0: 0, unresolved P1: 0.**

This is an independent final-diff review against baseline
`45741be900347f5e781fdf6a0a3b4db933911607`. The review covers the root lookup
wrappers, Source Manager, the human Manager UI, Source Metadata/URI
presentation, result bundles, packages/installers, tests, and documentation.
It does not treat external Windows execution as a substitute for code review;
the current Windows result is recorded separately below.

## Scope and frozen-code gate

The following retrieval-critical files have no source diff from the baseline:

- `query/list_dbs.py`
- `query/result_detail.py`
- `software_rag_tool/retrieval.py`
- `software_rag_tool/catalog.py`
- `software_rag_tool/db_runtime.py`
- `software_rag_tool/tokenize.py`
- `software_rag_tool/store.py`

The only accepted lower-search changes are:

1. `search_api.py` removes lower-layer Source-Link enrichment, normalizes
   public stored paths, and strips private Source identity; and
2. `query/search.py` retains private detail items only when invoked by the
   trusted upper wrapper.

Review found no change to candidate generation, Exact/identifier handling,
Dense or lexical retrieval, RRF, result ranking, diversification, packing,
evidence authority, answerability, or search status.

## Upper-wrapper and URI boundary

- Root `list_dbs.py` calls lower `query/list_dbs.py` exactly once after upper
  argument validation. Help and invalid input call it zero times.
- Root `search.py` calls lower `query/search.py` exactly once. Cached detail
  calls lower `result_detail.py` once and lower search zero times.
- URI enrichment occurs only after lower retrieval, classification, and
  packing.
- Source identity is conservatively joined from the selected DB's currently
  visible catalog rows by canonical relative path, with content-hash checking
  when available.
- Ambiguous, missing, unsafe, or changed catalog identity produces path-only
  output.
- A catalog fingerprint is checked before and after enrichment and again
  before file-bundle publication. A change removes every URI.
- Lower or stale legacy URI fields are removed before enrichment.
- Public output contains `uri`, not Source-Link rules or private Source IDs.
- File delivery publishes one already-enriched bundle; it never patches a
  ready result directory.
- Cached detail retains the URI observed at original-search time and does not
  reread the sidecar.
- Normal lookup performs no external HTTP validation.

These properties keep Source-Link failure fail-open and prevent URI metadata
from changing ranking, status, answerability, or evidence authority.

## Source Manager, Providers, and resume behavior

- A provisional random local key is allocated before trusted ADD success.
  Indexed `source_id` is accepted only from validated ADD JSON for the exact
  requested key.
- Persisted Source configuration uses fixed DB-relative work paths. Absolute
  runtime roots and credentials are rejected or redacted.
- Source/event/work paths reject traversal, symlinks, junctions, reparse
  points, special files, and VCS metadata at the ADD boundary.
- Git uses an external control directory, fetches, resets to the remote
  default branch, and cleans the managed worktree before ADD. Human-edited
  canonical Link metadata is preserved on refresh.
- SVN control metadata remains outside the ADD root. Recursive and direct-file
  refreshes preserve their documented behavior.
- Redmine first freezes a unique Issue-ID inventory, rejects pagination
  mutation before detail fetching, fetches details serially, reflects stable
  batches of five, persists the frozen ID list for resume, and never interprets
  a shorter later time window as deletion.
- SharePoint on Windows validates the synchronized external tree and passes
  that root directly to ADD without copying it into the DB. Runtime roots are
  not persisted in Source state. Once indexed, the SharePoint ingestion root
  is immutable; retargeting requires a new Source.
- Other imports are one-shot, copy only regular files into the fixed managed
  root, and redact the transient input path after success.
- ADD-only and metadata-only recovery do not repeat a completed fetch. A
  SharePoint interrupted reflection deliberately revalidates its external
  root instead of trusting a persisted absolute path.
- Canonical Source Metadata publication preserves an existing Link unless an
  explicit pending replacement exists.
- Source network routing reuses the canonical network module and resolves the
  effective route once per top-level external Source operation.

## Packages and installers

- Distribution and administration transfer use separate positive allowlists.
- Distribution excludes Source Manager state/work, virtual environments,
  machine-local network configuration, rollback backups, credentials, and
  temporary result data.
- Administration transfer validates manifest coverage, checksums, file type,
  archive paths, private-key names/content, Source-Link credentials, and
  source fingerprints.
- DB publication is staged. A multi-DB bootstrap publication failure removes
  every DB published by that invocation rather than leaving a partial set.
- Existing destination DBs are rejected before writes.
- Portable ADD state uses DB-relative markers. SharePoint external-root state
  uses a Source-key marker and is rebound from destination `source.json` plus
  the destination environment; the old absolute root is not distributed.
- Package bootstrap validates every file before installation and uses atomic
  file publication.
- POSIX and Windows installers remove only the explicit retired-file
  allowlist. They do not prune unknown user content or databases.

## Lock and concurrency boundary

The final product intentionally does **not** create persistent DB
maintenance/writer locks or a `.source-links.lock`. Tests assert that a
Source-Link save creates no such lock. Daemon/runtime-internal synchronization
is outside this removed administration-lock surface.

Strict simultaneous Source edits from multiple Manager processes are not
advertised. Revision and content hashes detect ordinary stale edits, and
unique temporary files plus atomic replace prevent partial JSON publication,
but they are not claimed as a formal cross-process transaction.

## Closed P1 design gates

| Gate | Closure |
|---|---|
| Full detail payload transport | Trusted parent-wrapper environment retains detail items only for the upper process. |
| Stable path-to-Source association | Catalog/content fingerprint checks surround URI enrichment and bundle publication. |
| Installer tombstones | Exact retired-file allowlists exist in both installers. |
| Public detail boundary | Root `search.py` dispatches cached detail internally; no third public lookup command is added. |
| Canonical Source configuration | DB-local `source-links.json` remains search-facing truth; Source workflow metadata publishes into it and preserves the prior valid primary on failure. |
| Legacy compatibility | Only the bounded SharePoint legacy shape is read-compatible; other legacy providers remain path-only/manual. |

Additional review findings closed before this final decision include unsafe
Source/event symlinks, untrusted ADD identity, Git refresh state, canonical
Link overwrite, Redmine unstable inventory/resume, SharePoint direct-root
portability, private-key package leakage, partial multi-DB publish rollback,
proxy credentials, and indexed SharePoint root retargeting.

## Automated test evidence

- Final macOS regression reported by the coordinating agent:
  - query contracts: **430/430 PASS**
  - Source Manager contracts: **36/36 PASS**
  - documentation/contracts: **78/78 PASS**
  - total: **544/544 PASS**
- Independent focused review run: **249 PASS**; the only non-result was one
  local mock-socket case blocked by the review sandbox's socket permission.
  The same network suite is included in the coordinating agent's passing full
  regression, so this is not an implementation failure.
- Package focused suite: **22/22 PASS**.
- `py_compile`: PASS.
- `git diff --check`: PASS.
- Frozen-file diff gate: PASS.
- Tracked-source hygiene scan for supplied work identifiers and user-profile
  absolute paths: no match.

## Windows validation

The final synchronized Windows execution passed:

- public wrapper contracts: **21/21 PASS**;
- Manager contracts: **41/41 PASS**;
- package contracts: **22/22 PASS**;
- Source Manager contracts: **36/36 PASS**;
- total focused Windows gate: **120/120 PASS**;
- root database list schema v2, UTF-8 Japanese file-delivery search, and
  cached detail: PASS;
- redirected `NO_COLOR` output: valid JSON with no ANSI escape;
- Manager Japanese help: PASS;
- local absolute-path exposure: none;
- persistent maintenance artifacts: 0.

Windows execution found and closed two portability defects before this final
gate: a transient-path check that examined the machine `Temp` ancestor rather
than the allowlisted tree-relative path, and a read-only file handle that
Windows could not `fsync`. Both fixes have direct regression coverage.

## Known limitations

- Strict multi-process Source/Source-Link editing is unsupported.
- Unsupported non-SharePoint legacy Link configurations require human
  reconfiguration.
- An unavailable or changing catalog yields path-only results.
- SharePoint direct ADD requires a safe, locally available synchronized tree
  and destination environment configuration after administration transfer.
- Large JSON size is observational; the implementation does not discard
  evidence, distinct documents, or complete URIs to meet a 12 KiB target.
- External Source URLs are generated but not contacted during normal search.

## Final finding summary

- P0: **0**
- P1: **0**
- P2/P3 requiring release action: **0**
- Code-review decision: **APPROVED**
- Windows release decision: **deferred to the final Windows execution report**
