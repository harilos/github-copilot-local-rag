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

`build_db.py` defaults to resumable behavior. It creates or continues a build without discarding previous progress. Use `--force-rebuild` only when you intentionally want to delete clean records and recreate the Chroma collection and SQLite catalog.

Evaluation-only chunk variants can be created with optional chunker settings:

```bash
python ~/.copilot/rag/gen_db/build_db.py --db project-c1000-rag --root /path/to/source --source-id project --force-rebuild --chunk-max-chars 1000 --chunk-overlap 120
```

If omitted, the default chunker remains `1400` characters with `160` character overlap.

`add_data.py` keeps existing DB contents and only processes changed or new files. Progress is saved after each batch in `logs/index_state.json`, and current status is written to `logs/progress.json`, so rerunning the same command resumes instead of starting over. Each batch updates both Chroma and `catalog.sqlite`.

New builds use compact catalog schema v2. Document metadata and file lookup terms are stored once per document, identifier exact search uses a term dictionary plus term-chunk postings, and `embedding_text` is omitted from SQLite. Older catalog schemas are not migrated in place; rebuild from clean JSONL or run a force rebuild.

Check status at any time:

```bash
python ~/.copilot/rag/gen_db/status.py --db project-rag --json
```

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
