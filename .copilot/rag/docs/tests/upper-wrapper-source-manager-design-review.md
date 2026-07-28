# Upper Wrapper and Source Manager Design Review

## Review scope

This is an independent, read-only design review against baseline
`45741be900347f5e781fdf6a0a3b4db933911607`.

The reviewed design adds two public lookup wrappers under the Local RAG root,
keeps the existing query programs as lower-level engines, moves Source-Link
URI resolution above the lower search engine, and replaces the existing human
manager and migration/export surface with a Source-oriented manager.

No real source names, organization names, URLs, user names, or workstation
paths are used in this document.

## Decision

**APPROVED FOR IMPLEMENTATION WITH THE P1 ITEMS BELOW AS REQUIRED GATES.**

No unresolved P0 design finding remains after adopting these constraints:

- the lower search returns relative stored paths and never reads the
  Source-Link sidecar;
- upper URI resolution is fail-open and never guesses a Source;
- an ambiguous or unstable catalog path produces no URI;
- the ready result bundle is published only after URI enrichment;
- distribution and administrative transfer use separate allowlists.

Process-to-process concurrent Source editing is explicitly outside the first
release. The absence of a Source-Link lock is therefore a documented
limitation, not a P0 finding. Revision and content hashes remain useful
conflict diagnostics but must not be described as a strict multi-process
compare-and-swap guarantee.

The previously discussed 12 KiB value is observational only. It must not be a
truncation threshold, control condition, release gate, or new production
constant.

## Baseline observations

### Lower Source-Link integration

At the baseline, Source-Link presentation is attached in
`software_rag_tool/search_api.py` by `_finalize_search_payload()`, which calls
`source_links.enrich_search_payload()`.

Retrieval initially carries a private `_source_id` on:

- evidence contexts;
- discovery document cards; and
- cached detail items.

The same finalization path then removes `_source_id` from all result lanes.
The compact projection also omits Source identity.

The new boundary must remove only this presentation-time sidecar/URI
integration. Candidate generation, Exact handling, Dense and lexical
retrieval, RRF, diversity, packing, evidence authority, answerability, and
status normalization remain frozen.

### Result bundle boundary

The final user instruction moves all URI resolution to the upper wrapper and
standardizes the public field as `uri`; it therefore supersedes the earlier
assumption that the lower publisher's legacy `source_url` /
`source_permalink` projection would remain canonical. The bounded presentation
changes allowed here are: carry `uri` through summary and cached detail,
prefer it in prompt rendering, and keep body citation IDs unlinked. They must
not alter retrieval, ranking, packing, result membership, or scores.

The upper search wrapper:

1. obtains one complete lower result;
2. enriches that in-memory payload;
3. calls the existing publisher exactly once; and
4. exposes the resulting pointer only after publication completes.

The upper wrapper must not edit an already-ready bundle in place.

The baseline lower `query/search.py` removes `_result_detail_items` before
ordinary stdout output. A narrowly scoped internal full-payload delivery mode
is required so the upper wrapper can preserve detailed cached context without
exposing that private array in the normal lower CLI output.

### Source inventory

The current read-only Source inventory already uses a visibility-aware
catalog query grouped by `source_id`. It does not need an embedding model or
Chroma to list Sources.

The new database-list wrapper may call the lower list command exactly once,
then use a read-only catalog projection for document, chunk, Source, and
freshness summaries. It must not change the lower `query/list_dbs.py`.

### Existing Source metadata

The current canonical sidecar schema already represents at most one
`source_type` and one `link` per Source. The current implementation also
contains:

- generic legacy schema adapters;
- a generic migration CLI;
- a DB-local `.source-links.lock`; and
- migration/export integration.

The new design keeps the current one-Source/one-Link schema, removes the
generic migration surface, and retains only the explicitly required
SharePoint legacy read compatibility.

### Installer and export topology

The baseline installers overlay the payload and do not remove files that have
been retired from a later release. Merely deleting the old admin Skill,
migration CLI, or export scripts from the repository therefore leaves stale
installed entrypoints behind.

The baseline migration exporter is a broad RAG-tree snapshot with exclusions.
It is not an acceptable implementation of either a public distribution or a
purpose-specific administrative transfer.

## Required upper lookup flow

### Database listing

For one valid list request:

1. validate the upper arguments;
2. invoke lower `query/list_dbs.py` exactly once;
3. parse its JSON once;
4. add only read-only catalog summaries;
5. emit `local-rag.database-list.v2`; and
6. preserve every DB returned by the one lower invocation while limiting
   each DB's deterministic Source cards to eight.

Help and invalid arguments invoke the lower command zero times.

Catalog enrichment is fail-open. A malformed or unavailable catalog may
reduce optional summary fields but must not invalidate a valid lower list.
The eight-entry bound applies to Source cards inside one DB summary, not to
the number of databases.

### Search

For one valid search request:

1. validate the database name and wrapper arguments;
2. invoke lower `query/search.py` exactly once;
3. obtain one full JSON payload through the internal parent-wrapper mode;
4. confirm that every candidate path is a canonical relative stored path;
5. resolve a Source only through the selected DB's current visible catalog;
6. remove any unexpected pre-existing URI fields;
7. resolve the current Source Link at most once;
8. attach optional URI fields in memory;
9. publish the result bundle once when file delivery is requested; and
10. emit one JSON result or pointer.

No retry, second database, secondary lower search, or HTTP request is allowed.
The upper agent may remove only a system-facing lookup phrase such as
“search from Local RAG” before passing the latest human-authored semantic
question. Every semantic character, identifier, punctuation mark, and
constraint after that routing-only removal remains verbatim; these two
requirements are complementary rather than contradictory.

### Follow-up details

A cached-detail request invokes lower `query/result_detail.py` exactly once
and invokes lower search zero times. The bundle already contains the URI
observed during the original search, so the detail path must not resolve the
sidecar again.

If the intended public surface truly consists of only two root commands, the
detail operation must be a subcommand or mode of the root search wrapper.
Adding a third public lookup script would contradict that boundary.

## Safe path-to-Source resolution

The lower engine intentionally does not expose public or private Source IDs in
the final result. The upper wrapper may derive Source identity from the
catalog only under the following conservative contract:

- consider only currently visible documents;
- ignore null and empty `source_id` values;
- compare canonical `/`-separated relative paths without case folding;
- reject absolute POSIX paths, drive-qualified paths, UNC paths, traversal,
  and truncated paths;
- accept a path only when all current matching rows identify exactly one
  distinct Source;
- treat duplicate current rows for the same Source as the same identity;
- use path plus content hash for evidence when the evidence revision provides
  that hash;
- return no URI when the catalog is missing, unreadable, inconsistent, or
  ambiguous; and
- never search another Source or database as a fallback.

To prevent a result from being associated with a catalog state newer than the
retrieval, the search client must remain an active reader through upper
enrichment and result publication, or the wrapper must verify an unchanged DB
snapshot before publishing URIs. If stability cannot be confirmed, discard
all URI enrichment and publish the valid path-only result.

This is a security boundary: ambiguity is not an error, and path-only output
is preferable to a plausible wrong-Source URI.

## Fail-open requirements

The following conditions keep the lower status, answerability, ranking, and
path-only result unchanged:

- no Source-Link sidecar;
- malformed or unsupported sidecar;
- missing Source setting;
- catalog path with no Source;
- ambiguous path-to-Source mapping;
- disabled or incomplete Link;
- unsafe stored path;
- provider validation failure;
- resolver exception;
- sidecar deletion or replacement during the request; and
- freshness or Source Manager state unavailable.

Normal output must not expose sidecar contents, settings, internal paths,
tracebacks, or provider roots. Explain output may contain a bounded
non-sensitive status only.

All external URI existence checks remain outside normal lookup.

## Source Manager state

The required managed layout is:

```text
<db-root>/sources/<source-slug-and-hash>/
  source.json
  state.json
  events.jsonl
  work/
```

Requirements:

- allocate a sanitized slug plus stable random/hash key at registration,
  before a trustworthy ADD result supplies `source_id`;
- verify the `source_id` inside `source.json` after opening the directory;
- store only DB-relative paths in managed JSON;
- reject symlinks, traversal, absolute paths, drive paths, and UNC paths;
- publish JSON atomically as UTF-8;
- keep temporary and work files below the validated Source directory;
- treat catalog Source inventory as authoritative for indexed content;
- never create an indexed Source merely by creating manager state; and
- never place search results or source-document contents in manager state.

Avoid two independent canonical Link configurations. The search-facing
`source-links.json` should remain the canonical portable Link projection.
`source.json` may hold human workflow and Source description data, but it must
not silently diverge into a second active provider configuration.

Because strict multi-process editing is deferred:

- advertise single-editor operation;
- do not claim strict CAS semantics;
- retain revision/etag conflict diagnostics;
- use unique temporary names and atomic replace;
- never auto-merge conflicting edits; and
- make interrupted-write recovery explicit.

## Legacy behavior

The generic migration command and generic legacy migration module should be
removed from the supported surface.

The remaining legacy adapter must:

- be read-only until a human explicitly saves;
- accept only the documented SharePoint legacy shape;
- never auto-select among several old mappings;
- never migrate another provider implicitly;
- fail open to path-only output;
- preserve the previous valid primary as a rollback artifact when an explicit
  human save replaces it; and
- never mutate catalog, Chroma, clean records, document IDs, or chunk IDs.

Old unsupported provider settings should be reported to the human manager as
manual handling required, not silently converted or discarded.

## Human Manager checklist

The final shallow menu must expose only:

1. New database
2. Database management
3. Update all databases
4. Distribution migration
5. Terminal confirmation
0. Exit

Database and Source submenus may expose the required human operations, but
Source-Link configuration remains human-only and must not be described in the
Copilot lookup Skill.

Provider configuration must be bounded and explicit for:

- GitHub;
- SVN;
- the supported Redmine cases;
- SharePoint on Windows; and
- Other Web.

Source examples shown during build/add may mention generic provider categories
such as SharePoint, Redmine, GitHub, SVN, or a folder. They must not contain
real names or URLs.

## Distribution, transfer, and installer checklist

Distribution and administrative transfer are distinct allowlisted commands.

Distribution must include the selected searchable DB snapshot, its validated
Source Metadata, the required model, and the allowlisted search runtime.
Distribution must exclude:

- Source Manager fetch settings, work trees, and resume history;
- virtual environments;
- result spools and run state;
- real network configuration;
- Source-Link rollback backups;
- Source Manager state and work directories; and
- credentials or local denylist files.

Administrative transfer may include explicitly selected portable data, but
must use a separate allowlist, clear sensitivity warnings, safe archive paths,
and credential validation.

The repository installers must:

- install the two root lookup wrappers and human manager;
- preserve machine-local runtime and network configuration;
- avoid copying generated DB/Source state as part of a normal repository
  installation (the dedicated distribution package is a separate operation);
- remove only an exact retirement allowlist of obsolete tool-owned files;
- retire the old admin Skill, old export command/helper, and generic migration
  command/module from an existing installation; and
- never perform broad deletion or remove unknown user files.

Deleting files from the repository without this installer retirement step is
insufficient.

## P1 findings and mandatory closure

### P1-01 — Full detail payload transport

The lower stdout path currently removes cached detail items. Add one
non-public parent-wrapper transport mode or equivalent trusted IPC so the
upper wrapper receives the complete result without exposing private detail
data through the ordinary lower CLI.

Gate: file-delivered summary and expanded details must retain the same
structural context as the baseline bundle behavior.

### P1-02 — Stable path-to-Source association

Path uniqueness alone does not protect against a catalog mutation between
retrieval and URI publication.

Gate: retain a read boundary through publication or prove the DB snapshot did
not change. On uncertainty, publish no URI.

### P1-03 — Installer tombstones

The current overlay installers leave removed commands and Skills installed.

Gate: POSIX and Windows installer tests must prove exact obsolete files are
removed while unrelated files and user state remain untouched.

### P1-04 — Public detail boundary

“Only two public entrypoints” and “call `result_detail.py` once” are compatible
only if detail is an internal lower call reached through the root search
wrapper.

Gate: document and test the root search detail mode; do not expose a third
Copilot-facing lookup command.

### P1-05 — Canonical Source configuration

The new per-Source manager tree and portable aggregate sidecar can become
conflicting sources of truth.

Gate: define one canonical Link representation and make publication failure
leave the previous search-facing sidecar valid.

### P1-06 — Legacy compatibility boundary

The baseline generic adapters cover more providers than the new
SharePoint-only compatibility rule.

Gate: add a provider-by-provider legacy matrix proving that only the allowed
SharePoint legacy shape is converted and every other old shape remains
path-only without data loss.

## Frozen-code gate

Against baseline `45741be900347f5e781fdf6a0a3b4db933911607`, keep zero semantic
diff for retrieval and zero source diff for:

- `query/list_dbs.py`;
- `query/result_detail.py`;
- retrieval candidate generation;
- Exact and identifier handling;
- Dense and lexical retrieval;
- RRF and ranking;
- diversity and packing;
- database runtime; and
- catalog mutation logic.

The only lower-search exceptions are:

1. removal of Source-Link/URI presentation from
   `software_rag_tool/search_api.py`; and
2. the minimal internal full-payload handoff in `query/search.py`.

Review these exceptions separately and reject any change to scores, ordering,
selected chunks, budgets, status, or answerability.

## Safe implementation order

1. Freeze baseline hashes and add lower-call-count tests.
2. Remove lower sidecar/URI access without changing retrieval output.
3. Add the internal full-payload parent handoff.
4. Implement the root list wrapper and database-list v2 projection.
5. Implement the root search wrapper with path validation and path-only
   fail-open.
6. Add stable catalog path-to-Source resolution.
7. Resolve URIs in memory and publish bundles once.
8. Add the per-Source manager state engine.
9. Refactor the human Manager menus and provider workflows.
10. Replace generic migration with the bounded SharePoint legacy adapter.
11. Add separate allowlisted distribution and administrative transfer tools.
12. Update both installers, including exact retired-file cleanup.
13. Remove obsolete admin Skill/export/migration files and update
    documentation.
14. Run focused inverse tests, smoke tests, source-hygiene checks, and the
    frozen-code diff gate.

## Minimum release checks

- valid list: lower list calls exactly 1;
- valid search: lower search calls exactly 1;
- help/invalid input: lower calls 0;
- cached detail: lower detail calls 1 and lower search calls 0;
- lower normal search reads Source-Link sidecar 0 times;
- normal search performs external HTTP calls 0 times;
- ambiguous Source mapping emits URI 0 times;
- absolute path disclosure 0;
- URI double-resolution 0;
- bundle post-publication rewrite 0;
- ranking/status/answerability changes 0;
- installer stale admin/export/migration entrypoints 0;
- distribution user data or credential inclusion 0;
- source hygiene violations 0; and
- every required test reports a nonzero executed count.

## Remaining accepted limitations

- strict concurrent edits from multiple Source Manager processes are not
  supported in the first release;
- revision/etag conflicts are diagnostic rather than a formal cross-process
  CAS guarantee;
- unsupported legacy providers require manual human handling;
- unavailable or unstable catalog identity produces path-only search output;
  and
- output size is measured for observability only and does not remove evidence,
  URIs, or bundle content.
