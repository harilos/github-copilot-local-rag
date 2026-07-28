# Local RAG ingestion and index implementation

This directory is the internal implementation behind Local RAG Manager.
Ordinary Copilot lookup must not invoke files here. Human users should use:

```text
~/.copilot/rag/manage.py
```

The CLI files remain stable implementation interfaces for Manager,
automation, tests, and recovery:

- `create_db.py`
- `build_db.py`
- `add_data.py`
- `status.py`
- `rebuild_component.py`

## Database and Source identity

A database name must end in `-rag`. A Source ID identifies one acquisition
source and is stable across updates. Provider/display/link configuration is
not accepted by build/add; it is managed separately by the human Manager.

Stored paths always include the logical root directory name:

```text
<root-name>/<path-relative-to-logical-root>
```

`--include-root-name-in-path` remains accepted for compatibility but cannot
disable this rule. Persistent separators are `/` on every platform.

## Scoped ingestion

`--scan-subdir <relative-subdirectory>` limits discovery and reconciliation
without changing path identity.

```bash
python build_db.py \
  --db <db-name>-rag \
  --root <source-root> \
  --source-id <source-id> \
  --scan-subdir <relative-subdirectory> \
  --resume
```

The scan subdirectory must be relative and resolve under the logical root.
Absolute, drive-qualified, UNC, and traversal paths are rejected. A missing
scan directory is an error rather than a whole-root fallback.

Incremental add reconciles only the selected scan scope. Two overlapping
scopes resolve the same file to the same stored path and document ID. Scanning
another sibling scope does not mark earlier documents as deleted.

## Resume and status

Progress records persist:

- logical root and root display name;
- normalized scan subdirectory and resolved scan root;
- mandatory root-name policy;
- Source ID;
- phase, current file/batch, counts, recent events, and last error;
- safe resume and explicit force-rebuild commands.

Resume rejects mismatched root, Source ID, or scan subdirectory. Status is
read-only and does not load the embedding model merely to show progress.

## Search-index repair

Manager maps human choices to:

- lexical: recreate catalog/full-text/identifier indexes from clean records;
- vector: recreate Chroma from clean records;
- all: recreate both.

Extraction is a full rebuild, not a search-index repair option. Source
Metadata remains a DB-root sidecar and survives lexical, vector, and all
index repair.

## Mutation coordination

Build, add, resume, and repair use the database mutation guard and daemon
release protocol. Per-DB maintenance blocks search only for the target DB.
Unrelated DBs remain searchable after the bounded worker-handle recycle.
Partially updated target content is never published to search.

## Development environment

The query virtual environment also supplies the ingestion package
dependencies. The default local model is an ONNX INT8 export with 256
dimensions. Normal lookup never downloads a missing model; setup must prepare
it first.

Use synthetic names and temporary directories in tests:

```text
<source-root>
<relative-subdirectory>
<db-name>-rag
<source-id>
```

Do not put organization names, usernames, internal paths, or provider
credentials in tracked fixtures or documentation.
