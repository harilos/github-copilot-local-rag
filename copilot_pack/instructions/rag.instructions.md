# RAG Instructions

Use this only when the user explicitly names a RAG database such as `xxx-rag`, or when the user naturally asks to search local RAG knowledge, documents, past decisions, runbooks, incidents, or design history.

Do not run RAG for ordinary questions unless the user asks for RAG-like lookup.

When a database name is explicit, call:

```bash
python ~/.copilot/rag/query/search.py --db xxx-rag --include-db-hint "<question>"
```

When the user asks naturally for RAG but no DB name is provided, call:

```bash
python ~/.copilot/rag/query/search.py --auto --include-db-hint "<question>"
```

If multiple DBs are available and the tool asks for a DB choice, show the candidate DB names and ask the user which one to use.

DB-specific instructions live under `~/.copilot/rag/dbs/<db-name>/DB_PROFILE.md`. Do not load those files into every prompt. The query tool reads only a short hint when needed.

When the user asks to create or rebuild a DB from documents, use:

```bash
python ~/.copilot/rag/gen_db/status.py --db xxx-rag --json
python ~/.copilot/rag/gen_db/build_db.py --db xxx-rag --root <input-folder> --source-id <source-name> --resume
```

When the user asks to add or update documents in an existing DB, use:

```bash
python ~/.copilot/rag/gen_db/status.py --db xxx-rag --json
python ~/.copilot/rag/gen_db/add_data.py --db xxx-rag --root <input-folder> --source-id <source-name>
```

Before starting a long build or add operation, inspect `status.py --json`.

- If `appears_active` is true, do not start a duplicate build. Show the current phase, file counts, and current file.
- If `can_resume` is true and the saved `root` / `source_id` match the user's intended input, run the `resume_command`.
- If the user explicitly asks to discard prior work and rebuild from scratch, run the `force_rebuild_command`.
- If the saved `root` / `source_id` do not match the user's intended input, ask for confirmation before reusing the previous state.
- During long operations, check `status.py` periodically rather than waiting only on stdout.

Treat Markdown files as normal source documents. If converted Markdown files and original Office/PDF files both exist in the specified input folder, process both unless the user explicitly asks to exclude one.

If Python or the virtual environment is missing, guide the user to run:

```bash
python ~/.copilot/rag/query/setup.py
```

On Windows:

```powershell
py -3 $HOME\.copilot\rag\query\setup.py
```
