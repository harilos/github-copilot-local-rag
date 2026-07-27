---
name: local-rag
description: Performs one read-only lookup against installed local RAG databases when the user explicitly asks to answer from RAG, local documents, internal or company information, or information installed in or provided to Copilot.
---

# Local RAG Lookup

This workflow is intentionally simple and must be usable by a lightweight,
fast model selected by Auto. No particular model is required or guaranteed.

## Activation

Use this lookup when the user explicitly asks for an answer based on RAG,
local documents, internal or company information, or information installed in
or provided to Copilot. Treat equivalent source-based wording in any language
as explicit. Do not activate lookup merely because the question mentions a
company or an internal-sounding term.

## One-command decision

Before running any command, inspect the original question for an explicit
database name ending in `-rag`.

| Original question | Required action |
| --- | --- |
| Contains an explicit `<name>-rag` database | Use that exact database. Do not run `list_dbs.py`. Run `search.py` exactly once. |
| Does not contain an explicit database | Run `list_dbs.py` exactly once. Then either choose one clear match and run `search.py` once, or ask the user to choose without searching. |

This decision is mandatory. Never run `list_dbs.py` merely to confirm a
database already named by the user.

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
  --compact-json \
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
  --compact-json `
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
6. Treat the `search.py` output as the complete retrieval result. Do not run
   `jq`, `grep`, `head`, `tail`, or another tool to inspect or re-extract it.
7. If the shell tool still reports that compact output was truncated or
   offloaded to a file, do not open that file. Briefly report the error and
   stop.

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
Identify the supporting evidence ID, source path, and available location in
the answer.
Treat `background_context` as background information only.
Never use `related_context` as proof.
Obey every item in `warnings`.
Preserve source severity and uncertainty when translating. Do not strengthen
labels: for example, render `Substantial` damage as "substantial damage"
with the source label preserved, not "destroyed", unless the evidence
explicitly uses the stronger term.

Do not infer missing table headers, column meanings, comparisons, rankings,
maximums, minimums, or qualitative labels such as "large", "small", or
"medium-sized" unless the evidence directly supports them.
