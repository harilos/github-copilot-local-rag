# Local RAG query runtime

This directory contains the lower retrieval runtime. It is not the public
Copilot command surface.

Public ordinary lookup uses:

```text
~/.copilot/rag/list_dbs.py
~/.copilot/rag/search.py
```

The root wrappers validate the public arguments before starting a child,
invoke the appropriate lower operation exactly once, enrich presentation
read-only, and preserve child stderr and exit status. Help or invalid public
arguments start no lower process.

## Runtime environment

Use the installed virtual-environment interpreter:

```text
macOS/Linux: ~/.copilot/rag/query/.venv/bin/python
Windows:     %USERPROFILE%\.copilot\rag\query\.venv\Scripts\python.exe
```

Ordinary lookup never probes PATH-based Python launchers. A missing or invalid
completion marker returns `setup_required`. It does not trigger an implicit
download.

## Lower search responsibilities

The lower runtime owns:

- database resolution compatibility;
- structured request validation;
- daemon transport and deadline;
- Exact, lexical, Dense, metadata, and facet candidate generation;
- fusion, document diversity, evidence/discovery separation;
- primary-first packing and structural context;
- deterministic initial-answer data and cached detail items.

It does not own public Source Link projection or database freshness notices.
Those belong to the root wrapper after retrieval and packing. The public Link
contract uses `source_provider`, `source_url`, and optional
`source_permalink`; it does not introduce a replacement single `uri` field.

When `LOCAL_RAG_WRAPPER_INTERNAL=1`, the lower JSON result may retain private
detail material required to publish one final enriched bundle. Private
Source IDs are never a public result field.

## Persistent daemon

On Windows, short-lived direct Python clients use one lightweight manager and
one spawned search worker. The manager does not initialize native retrieval
libraries. The worker owns ONNX, tokenizer, Chroma, read-only SQLite
connections, and a bounded per-DB cache.

Normal busy/starting behavior stays on the daemon route. Requests are
correlated by request and client IDs. CPU-heavy work is serial, while the
manager remains responsive to status, cancel, shutdown, and DB release.

Local RAG does not create a persistent DB maintenance state or writer lease.
Search and DB mutation are not rejected by a previous `active`, `failed`, or
`requires_repair` record. Concurrent-access failures reported by SQLite,
Chroma, or the OS remain ordinary errors for the current invocation.

## Result delivery

The lower runtime can produce one in-memory result plus detail items. The root
wrapper resolves optional Source Link fields first, then publishes the final
bundle once.
It never edits an already-published summary.

Each result manifest records the byte size and SHA-256 digest of every
immutable payload file (`summary.json` and cached detail items). `meta.json`
is the mutable ready/expiry marker, so it is intentionally outside that
immutable file map.

Temporary result location:

```text
<OS temp>/GitHubCopilotLocalRAG/results/<uuid>/
```

The pointer is ASCII-safe JSON; bundle files are UTF-8 JSON. Follow-up detail
reads only the cached result set and does not query a database.

## Development and diagnostics

Direct lower-runtime execution is reserved for tests and explicit diagnostics.
Do not document it in Copilot instructions or Skills. Manual users should
prefer the root public wrappers, and human changes should use Local RAG
Manager.
