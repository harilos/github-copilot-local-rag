---
name: local-rag
description: Performs one read-only lookup against installed local RAG databases when the user explicitly requests RAG or local-document search.
---

# Local RAG Lookup

This workflow is intentionally simple and must be usable by a lightweight,
fast model selected by Auto. No particular model is required or guaranteed.

## Rules

- Perform only read-only RAG lookup.
- Do not create a plan.
- Do not inspect RAG source code or implementation files.
- Do not edit files, databases, indexes, or configuration.
- Do not invoke Codex, another coding agent, or a subagent.
- Do not rewrite, shorten, split, or expand the user's question.
- Do not select a retrieval mode.
- Do not issue a second search automatically.
- Do not search another database after receiving a result.
- Do not suggest that the user rewrite the question into search keywords.

## Runtime

Use the RAG virtual-environment Python directly. Do not try `python`,
`python3`, or `py` for ordinary lookup.

On macOS or Linux:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/query/list_dbs.py \
  --format json
```

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/query/search.py \
  --db <selected-db> \
  --include-db-hint \
  --format json \
  "<complete-user-question>"
```

On Windows PowerShell:

```powershell
& "$HOME\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$HOME\.copilot\rag\query\list_dbs.py" `
  --format json
```

```powershell
& "$HOME\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$HOME\.copilot\rag\query\search.py" `
  --db <selected-db> `
  --include-db-hint `
  --format json `
  "<complete-user-question>"
```

If the platform-specific virtual-environment Python does not exist, do not
try another interpreter. State that RAG setup is required and stop.

## Database selection

If the user explicitly names a database, use it directly and do not call
`list_dbs.py`.

If no database is explicitly named:

1. Run `list_dbs.py --format json` exactly once.
2. Read only the database name, title, and query hint.
3. Select one database only when it clearly matches the user's request.
4. If multiple databases are plausible, show their names and ask the user to
   choose. Do not run `search.py` yet.
5. If no database is relevant, state that no suitable local RAG database was
   found and stop.

Do not use `--auto`. Database selection is the assistant's only retrieval
decision.

## Search

After selecting a database:

1. Pass the user's complete original question to `search.py` exactly once.
2. Use the default hybrid retrieval behavior.
3. Do not specify `--retrieval-mode`.
4. Do not retry after `ok`, `partial`, `no_hit`, or `error`.
5. A new search is allowed only when the user explicitly requests additional
   investigation or provides a meaningful clarification.

Dense, lexical, Exact, metadata, and fusion candidate retrieval performed
inside that one Hybrid call are not additional searches.

## Result handling

Follow the returned status:

- `ok`: Answer using only `evidence`.
- `partial`: Answer only the supported portion and clearly state the limits.
- `no_hit`: State that supporting evidence was not found and stop.
- `setup_required`: Tell the user that RAG setup is required.
- `error`: Briefly report the error and stop. Do not retry automatically.

Use only `evidence` for factual claims.
Treat `background_context` as background information only.
Never use `related_context` as proof.
Obey every item in `warnings`.

Do not infer missing table headers, column meanings, comparisons, rankings,
maximums, minimums, or qualitative labels such as "large", "small", or
"medium-sized" unless the evidence directly supports them.
