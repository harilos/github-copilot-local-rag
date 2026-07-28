# DB Generation

Use this folder only when creating, rebuilding, or adding data to a RAG database.

Create an empty DB layout:

```bash
python ~/.copilot/rag/gen_db/create_db.py --db project-rag --title "Project Knowledge"
```

This creates `VERSION.json` in the DB root. The file records `created_at`, `db_hash`, collection name, and the tool hash used at layout creation time.
Use `--query-hint` when the DB has a known domain and you want Copilot to see a short DB-specific hint during retrieval.

The default vector profile is `cl-nagoya/ruri-v3-30m` with ONNX Runtime INT8. Prepare the query runtime before dense indexing:

```bash
python ~/.copilot/rag/query/setup.py
```

Build or rebuild from an input folder:

```bash
python ~/.copilot/rag/gen_db/status.py --db project-rag
python ~/.copilot/rag/gen_db/build_db.py --db project-rag --root /path/to/source --source-id project --resume
```

Add or update another input folder:

```bash
python ~/.copilot/rag/gen_db/add_data.py --db project-rag --root /path/to/source --source-id project-extra
```

`source_type` and Source Link are optional DB-local presentation metadata.
Build and add do not ask for, infer, or save either value, and there is no
`--source-type` ingestion option. A newly indexed Source starts unspecified.
Use the human Manager later if its type or browser link should be configured.

The physical `source-links.json` sidecar may contain older Source-Link schemas.
Preview or apply the explicit Source Metadata migration with:

```bash
python ~/.copilot/rag/gen_db/migrate_source_metadata.py
python ~/.copilot/rag/gen_db/migrate_source_metadata.py --apply
python ~/.copilot/rag/gen_db/migrate_source_metadata.py --db project-rag
python ~/.copilot/rag/gen_db/migrate_source_metadata.py \
  --db project-rag --apply --format json
```

The no-DB form scans all DBs independently. The Manager always passes its
selected DB through `--db`. Migration changes only the DB-local sidecar; it
does not call add, rebuild indexes, or alter catalog, Chroma, clean records,
document IDs, or indexed content.

To ingest only one part of a larger, stable source tree, keep the larger
directory as `--root` and select a relative directory with `--scan-subdir`:

```bash
python ~/.copilot/rag/gen_db/build_db.py \
  --db project-rag \
  --root "/data/Project Knowledge" \
  --source-id project \
  --scan-subdir "plans/FY26" \
  --resume
```

Only files below `/data/Project Knowledge/plans/FY26` are scanned, while the
stored path remains `Project Knowledge/plans/FY26/...`. Stored paths always
include the root directory name and use `/` on every platform.
`--include-root-name-in-path` is accepted for compatibility and has no
disabling counterpart.

`build_db.py` defaults to resumable behavior. It creates or continues a build without discarding previous progress. Use `--force-rebuild` only when you intentionally want to delete clean records and recreate the Chroma collection and SQLite catalog.

Evaluation-only chunk variants can be created with optional chunker settings:

```bash
python ~/.copilot/rag/gen_db/build_db.py --db project-c1000-rag --root /path/to/source --source-id project --force-rebuild --chunk-max-chars 1000 --chunk-overlap 120
```

If omitted, the default chunker remains `1400` characters with `160` character overlap.

`add_data.py` keeps existing DB contents and only processes changed or new files. With `--scan-subdir`, discovery and deletion reconciliation are limited to that scope. Disjoint scopes remain unchanged, and overlapping parent/child scans use the same stored path and document identity. By default, every five indexed documents are committed to Chroma and `catalog.sqlite`, then progress is saved in `logs/index_state.json`; the final batch is committed even when it contains fewer than five documents. Use `--batch-size-files <count>` to change this checkpoint size. Resume preserves the saved batch size and rejects a conflicting explicit value, so at most four successfully prepared documents normally need to be repeated with the default. Current status is written to `logs/progress.json`.

Changing to root-prefixed paths changes path-derived document IDs. Existing
databases must be rebuilt once to adopt this behavior. No in-place old-ID
migration is performed.

New builds use compact catalog schema v2. Document metadata and file lookup terms are stored once per document, identifier exact search uses a term dictionary plus term-chunk postings, and `embedding_text` is omitted from SQLite. Older catalog schemas are not migrated in place; rebuild from clean JSONL or run a force rebuild.

Check status at any time:

```bash
python ~/.copilot/rag/gen_db/status.py --db project-rag --json
```

Status reports the logical root, root display name, normalized scan
subdirectory, resolved scan root, stored path prefix, and the mandatory
root-name policy. Generated resume and force-rebuild commands preserve those
values.

Search keeps indexed chunks unchanged. After retrieval and document
diversification, it reserves the output budget for every selected primary
excerpt before adding useful surrounding context. Available same-section
paragraphs, table headers or footnotes, enclosing code/configuration blocks,
and page or slide context may be returned as `context_before` and
`context_after`. These ranges do not inherit Exact, Dense, BM25, anchor, or
metadata signals. If a table row has no verified header context, the result
includes `table_headers_incomplete`. Broad `document_results` remain short
document cards.

Rebuild one derived component from existing clean records:

```bash
python ~/.copilot/rag/gen_db/rebuild_component.py --db project-rag --component lexical
python ~/.copilot/rag/gen_db/rebuild_component.py --db project-rag --component vector
```

`lexical` rebuilds SQLite FTS5, identifier, and metadata indexes without recomputing embeddings. `vector` recreates Chroma from clean JSONL.

Markdown files are first-class inputs. A converted Markdown file and its original Office/PDF file can coexist under the same input root; both are processed unless you remove one from the input.

DB directories live under:

```text
~/.copilot/rag/dbs/<db-name>/
```

These directories are ignored by git.
