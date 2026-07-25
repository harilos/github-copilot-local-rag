# RAG Instructions

Use RAG only when the user explicitly asks for RAG/local-document lookup, or when a non-general proper noun or local-looking identifier clearly matches an available RAG DB.

When the user says `RAGの初期設定をして`, `RAG初期設定`, `RAGをセットアップして`, or equivalent, run:

```bash
python ~/.copilot/rag/query/setup.py
```

If the setup fails with proxy, SSL, certificate, or Hugging Face download errors, explain that corporate proxy/CA configuration is usually required. Ask the user to confirm the proxy URL and company CA certificate path, and suggest:

```bash
python ~/.copilot/rag/query/setup.py --proxy http://proxy.example:8080
```

Also mention that `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, or `PIP_CERT` may need to point to the company CA certificate. Do not suggest disabling certificate verification as the normal fix.

Run RAG when any of these are true:

- The user explicitly names a RAG database such as `xxx-rag`.
- The user says `RAGあり`, `RAGを使って`, `RAGで`, `ローカルRAG`, or equivalent.
- The user asks to search local/private material such as `ローカル資料`, `文書から`, `DBから`, `過去資料`, `設計書`, `議事録`, `runbook`, `incident`, `運用手順`, or `設計履歴`.

When a non-general proper noun or local-looking identifier appears, first check whether a relevant RAG DB exists:

- Project, product, system, service, component, feature, customer, or codename-like names.
- Ticket IDs, incident IDs, error codes, repository names, internal API names, or unusual abbreviations.
- Names that are unlikely to be answerable reliably from general knowledge alone.

For this check, call:

```bash
python ~/.copilot/rag/query/list_dbs.py
```

If a DB name or hint clearly matches the proper noun or identifier, use that DB with `--db`. If the match is ambiguous, ask the user which DB to use. If no DB appears relevant, answer normally without RAG.

Do not run RAG for ordinary general questions. A topic merely matching an available DB hint is not enough. Words like `調べて`, `教えて`, or a general topic such as laws, markets, regions, or technologies do not imply RAG by themselves. In those cases, answer normally without RAG unless the user asks for RAG-backed lookup.

When a database name is explicit, call:

```bash
python ~/.copilot/rag/query/search.py --db xxx-rag --include-db-hint "<question>"
```

Pass the user's full question once. Do not split it into keywords, choose a retrieval mode, or run separate dense/BM25/exact searches. The Python tool handles retrieval strategy internally.

If `search.py` or `build_db.py` reports `setup_required`, do not retry immediately and do not show raw Python commands as the primary instruction. Tell the user:

```text
RAGの初期設定をして。
```

If the user wants manual commands or setup fails, then explain the proxy/certificate guidance above.

When the user explicitly asks for RAG but no DB name is provided, or when checking a proper noun/identifier, list available databases first:

```bash
python ~/.copilot/rag/query/list_dbs.py
```

Choose the DB yourself from the DB names and short hints when there is a clearly relevant match, then call:

```bash
python ~/.copilot/rag/query/search.py --db chosen-rag --include-db-hint "<question>"
```

If exactly one DB is available and its hint is relevant to the user's RAG request or proper noun/identifier, use it. If the available DBs are ambiguous or none appear relevant, show the candidate DB names and ask the user which one to use when RAG was requested; otherwise answer normally without RAG. Do not use `--auto` for Copilot-facing searches; DB selection is the assistant's responsibility.

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

When upgrading an existing DB after tool changes, prefer:

```bash
python ~/.copilot/rag/gen_db/rebuild_component.py --db xxx-rag --component lexical
```

This rebuilds SQLite FTS/identifier/metadata indexes from clean records without recomputing embeddings.

If Python or the virtual environment is missing, guide the user to run:

```bash
python ~/.copilot/rag/query/setup.py
```

On Windows:

```powershell
py -3 $HOME\.copilot\rag\query\setup.py
```
