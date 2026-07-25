# DB Generation

Use this folder only when creating, rebuilding, or adding data to a RAG database.

Create an empty DB layout:

```bash
python ~/.copilot/rag/gen_db/create_db.py --db project-rag --title "Project Knowledge"
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

`build_db.py` defaults to resumable behavior. It creates or continues a build without discarding previous progress. Use `--force-rebuild` only when you intentionally want to delete clean records and recreate the Chroma collection.

`add_data.py` keeps existing DB contents and only processes changed or new files. Progress is saved after each batch in `logs/index_state.json`, and current status is written to `logs/progress.json`, so rerunning the same command resumes instead of starting over.

Check status at any time:

```bash
python ~/.copilot/rag/gen_db/status.py --db project-rag --json
```

Markdown files are first-class inputs. A converted Markdown file and its original Office/PDF file can coexist under the same input root; both are processed unless you remove one from the input.

DB directories live under:

```text
~/.copilot/rag/dbs/<db-name>/
```

These directories are ignored by git.
