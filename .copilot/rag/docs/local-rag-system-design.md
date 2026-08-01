# Local RAG Current System Design

## 1. Boundaries

The product has three boundaries:

```text
Copilot ordinary lookup
  -> root list_dbs.py
  -> root search.py

Human management
  -> manage.py

Internal implementation
  -> query runtime
  -> ingestion/index package
  -> Source Manager package
```

Copilot-facing instructions expose only the two root read-only entry points.
Management requests stop at the Local RAG Manager boundary. The Manager is an
orchestrator and does not create a second retriever or update detector.

## 2. Public database list

The root database list calls the lower runtime list exactly once, then opens
each catalog read-only. It returns:

- database name, title, query hint;
- a short public content summary;
- Source type/display cards;
- distinct Source and unattributed-document counts.

Raw Source IDs, sidecar-only Sources, absolute paths, and acquisition settings
are not public routing metadata. At most eight Source cards are returned.
Anonymous entries are aggregated without deriving a public label from an
internal Source ID.

## 3. Public search

The root search wrapper calls the lower search exactly once. It forces one
full JSON result internally, performs read-only presentation enrichment, and
then renders the requested compact JSON, prompt, or file pointer.

```text
question + structured hints
  -> lower search
  -> retrieval/fusion/diversity/packing
  -> evidence/context/document classification
  -> exact private Source identity subprocess handoff
  -> optional Source URL resolution
  -> compact/prompt/file projection
  -> public Source Link fields
```

The public wrapper never changes retrieval candidates, scores, order,
authority, answerability, or status. The lower process keeps its exact
`_source_id` only on the trusted wrapper subprocess handoff; direct low-level
CLI output remains path-only. The wrapper resolves the full canonical stored
path before compact projection and never guesses Source identity from a path
or basename. Catalog changes during resolution invalidate every link for that
response.

Public output removes the transient ID and keeps `source_provider`,
`source_url`, and the optional `source_permalink`. Consumers prefer the
permalink without discarding the ordinary browser URL.

## 4. Conversational hints

The latest visible human prompt remains the final positional question. A
contextual reference may use the minimum necessary earlier human-authored
context only through structured fields:

- literal identifier;
- entity;
- facet;
- semantic hypothesis;
- answer goal.

Earlier messages are never appended to the question. Assistant answers and
prior RAG results are not verified facts. Material ambiguity is resolved by
asking the human before database selection or search.

## 5. Retrieval

The lower runtime owns:

- verified Exact and identifier collision protection;
- BM25/lexical retrieval;
- local ONNX Dense retrieval;
- metadata and filename retrieval;
- bounded facet fan-out;
- RRF and candidate floors;
- evidence and discovery lanes;
- document diversity;
- primary-first packing;
- structural context expansion.

Evidence remains strict. Discovery remains broad and document-level. Exact
no-hit can produce empty evidence while retaining related document cards.
Surrounding chunks never inherit Exact, Dense, lexical, anchor, or metadata
signals from a primary match.

## 6. Result bundles

File delivery publishes an immutable temporary result set:

```text
<OS temp>/GitHubCopilotLocalRAG/results/<uuid>/
  meta.json
  summary.json
  manifest.json
  items/
```

Every file is UTF-8 JSON published by temporary file plus atomic replace.
The initial pointer is ASCII-safe. The summary is self-contained and is never
silently shortened to meet a byte target. The manifest records each bundle
file's relative path, size, and SHA-256. Follow-up detail reads cached items
through the same public search entry point and never reruns retrieval.

Source Link values are copied into summary/detail at original-search time. Later
Source Metadata edits do not mutate an existing result set.

## 7. Freshness

Database freshness is presentation metadata, not a search gate.

1. Read only a validated `rag-wrapper.json` using
   `local-rag.wrapper.v1` and `content_snapshot_at`.
2. Missing, invalid, or future timestamps are `unknown`; filesystem time,
   `VERSION.json`, and the legacy `db-snapshot.json` are not freshness
   substitutes.
3. Age greater than or equal to 30 days is `stale`.
4. Distribution, copy, and repack preserve the content snapshot timestamp.
   Only a successful first searchable reflection or qualifying content update
   advances it.

The Japanese chat notice is nested under `database_freshness.chat_notice`.
Its stable contract is:

```text
code = local_rag_content_snapshot_older_than_30_days
scope = conversation
dedupe_key = local_rag_content_snapshot_stale
```

Copilot shows that deduplicated notice at most once per conversation.

## 8. Persistent daemon

Short-lived search clients use a local manager/worker daemon:

```text
public client
  -> local framed transport
  -> lightweight manager
  -> bounded per-client queue
  -> one persistent search worker
```

The manager owns transport, queueing, deadlines, cancellation, state, and
worker supervision. The worker owns ONNX, tokenizer, Chroma, SQLite, and a
small per-DB runtime cache. CPU-heavy retrieval is serial by default.

Normal lookup never falls back merely because the daemon is busy or starting.
The outer deadline includes startup, queue wait, retrieval, serialization, and
client output.

Local RAG does not persist a DB maintenance state and does not block search,
build, add, or repair through an independent writer lease. A mutation failure
belongs only to that invocation; after correcting the cause, the operator can
retry immediately. SQLite, Chroma, and normal OS file operations may still
report their own concurrent-access errors.

The Manager exposes an explicit authenticated daemon shutdown for diagnosis and
maintenance. Queued searches are cancelled, and a long-running active search
may be stopped after the bounded drain period. It is not an exclusive writer
lease: operators must ensure no concurrent search starts while using that
manual control. The next ordinary search starts a fresh daemon generation
lazily.

## 9. Stored paths and ingestion scope

Newly indexed paths are canonical POSIX-style relative paths:

```text
<logical-root-name>/<path-relative-to-logical-root>
```

The root name is mandatory. A scan subdirectory changes discovery scope but
not stored-path identity. Incremental reconciliation is limited to the
selected scope, so scanning another subdirectory does not tombstone documents
outside that scope.

The same canonical stored path is used for metadata, catalog identity,
embedding path prefix, state, progress, and path-derived document identity.
Old databases using paths without the root name require one rebuild; no
in-place identity migration exists.

## 10. Source Manager

The Source Manager package is separate from search presentation. It provides:

- validated provider settings;
- deterministic acquisition plans;
- checkpointed execution;
- Source-local state;
- package creation and validation.

Supported acquisition sources are GitHub repositories, GitHub Issues, GitHub
Wiki, SVN, Redmine, GitLab Issues, SharePoint synchronized folders, Teams
shared folders synchronized through OneDrive, and one-time local input. GitHub
Issues is acquired through an authenticated `gh` CLI and GitHub Wiki through
the repository's `.wiki.git` remote. New Sources are introduced only through
a successful add/ingestion flow.

SharePoint acquisition/update is Windows-only and resolves a synchronized
root from machine-local environment configuration. The synchronized tree is
used directly and is not copied into the DB. The resulting database is
portable and searchable on macOS. macOS does not perform SharePoint updates.
Teams uses the same Windows-only machine root.

GitLab Issue acquisition covers open and closed Issues, resolves a project URL
through the REST API on every run, then serially materializes Issue details and
Discussions as Markdown.
The access token is encrypted in machine-local configuration, bound to an
environment name derived from the GitLab installation URL, and never stored
in the database. Search reflection occurs after each stable batch of five.
The numeric project ID is absent from Source configuration and generated
Markdown. While a run is resumable, its checkpoint records the resolved ID so
a recreated project cannot be mixed into the frozen Issue queue.
Issues deleted from GitLab or no longer visible to the acquisition account
do not remove existing RAG documents. Subsequent runs only create or overwrite
Issues that remain visible to that account.
After initial ingestion, the GitLab installation and project URLs are
immutable; a different project requires a new Source.

## 11. Source Metadata

The optional DB-local sidecar is:

```text
<db-root>/source-links.json
```

Its canonical schema is `rag-source-metadata-v1`. Each catalog Source may have:

- optional display name;
- optional Source type;
- at most one enabled/disabled Link.

Observed stored roots come from currently visible catalog documents. One root
permits per-file URL generation; zero roots permits home-only; multiple roots
require separate Source IDs. The sidecar never creates a catalog Source and
never changes IDs, embeddings, indexes, hashes, ranking, or status.

The first release has a single-editor contract for Source Metadata. Atomic
replacement plus revision/etag rechecks protect one Manager process, but no
persistent lock file is created and strict multi-process CAS is not claimed.

## 12. Portable packages

The Manager creates two different artifacts:

### Distribution ZIP

Contains read-only public entry points, runtime, selected databases, and
required search assets. It also contains package-root `install.sh` and
`install.ps1` helpers that overlay-copy only the packaged `.copilot` payload
into the receiving user's Copilot home. It excludes management acquisition
state.

### Management-PC transfer folder

Contains administration code, Source settings, checkpoints, databases, and
assets required to continue management on another computer. Creation is
resumable.

Package creation does not take a global product lock. It detects file changes
during copying and aborts rather than publishing an inconsistent artifact.
All manifest paths are relative and checksummed. Runtime locks, virtual
environments, temporary files, credentials, and backup sidecars are excluded.
Product runtime, public documentation, and the two Local RAG Skills are
collected by explicit exclusions, so a new non-test runtime module is packaged
without another file allowlist update. Machine-local configuration and DB
contents do not use that broad collector: configuration includes only tracked
examples, and each package kind keeps a separate restrictive DB contract.

## 13. Network

Ordinary lookup is completely local:

- no model download;
- no Source URL probe;
- no external provider API;
- localhost daemon traffic bypasses proxies.

Setup and provider acquisition resolve proxy/CA configuration once per
top-level operation. Machine-local configuration is optional and never
affects offline search. Embedded proxy credentials are rejected for persisted
configuration.
GitLab API requests carrying `PRIVATE-TOKEN` do not follow redirects.

## 14. Security invariants

- No absolute workstation path in indexed public metadata.
- No raw Source ID in the public database-list contract.
- No Source configuration in ordinary search output.
- No provider credential or token in Source Metadata, package manifests,
  logs, or tracked examples.
- No sidecar error may turn successful search into failure.
- No URL resolution may change evidence authority or relevance.
- All destructive Manager actions validate a direct child DB path and require
  exact human confirmation.
