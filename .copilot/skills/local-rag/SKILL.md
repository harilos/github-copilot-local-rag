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
- Do not create a multi-step execution plan. Create only the bounded
  structured retrieval request described below.
- Do not inspect RAG source code or implementation files.
- Do not edit files, databases, indexes, or configuration.
- Do not invoke Codex, another coding agent, or a subagent.
- Do not rewrite, shorten, split, or expand the user's question.
- Do not select a retrieval mode.
- Do not issue a second search automatically.
- Do not search another database after receiving a result.
- Do not suggest that the user rewrite the question into search keywords.

## Broad one-shot retrieval planning

Local RAG acts as both an evidence retriever and a broad local-document search
engine.

Before an ordinary lookup, create exactly one bounded structured retrieval
request using the current agent's own reasoning. Do not call another model,
coding agent, Codex subagent, or planner. Planning happens in the current turn
and does not add another lookup.

Preserve the complete user question verbatim as the final positional
argument. Extract identifiers and names exactly as written, including case,
digits, punctuation, hyphens, underscores, dots, slashes, and version
notation.

Create at most four retrieval facets:

1. One literal facet for important identifiers or names.
2. Up to three semantic facets covering distinct, relevant perspectives.

For a short definition question, semantic facets may cover uses,
specifications, decisions, history, or other reasonably connected
perspectives. Do not add unrelated generic topics to increase the result
count.

A plausible acronym expansion may be supplied only through
`--semantic-hypothesis`. It is a semantic-only search hypothesis, never an
Exact match or verified fact.

Wide coverage is the default: target eight distinct documents, accept six
when fewer useful candidates exist, allow labelled weak research leads, and
return at most one document card per distinct path. Only use narrow coverage
when the user explicitly asks for one source, only the best source, or no
related material.

Call `search.py` exactly once. Do not retry with rewritten keywords, inspect
the result with another command, search a second database, invoke another
agent, or discard `document_results` merely because Exact evidence was not
found.

## Runtime

Use the RAG virtual-environment Python directly. Do not try `python`,
`python3`, or `py` for ordinary lookup.

The required interpreter paths are:

```text
Windows:      %USERPROFILE%\.copilot\rag\query\.venv\Scripts\python.exe
macOS/Linux:  ~/.copilot/rag/query/.venv/bin/python
```

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
  --result-delivery file \
  --format json \
  "<complete-user-question>"
```

On Windows PowerShell, use the call operator only to start the installed
executable directly:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\query\list_dbs.py" `
  --format json
```

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\query\search.py" `
  --db <selected-db> `
  --include-db-hint `
  --compact-json `
  --result-delivery file `
  --answer-goal evidence `
  --facet "<literal or semantic facet>" `
  "<complete-user-question>"
```

Do not use `cmd.exe /c`, `cmd /c`, `Start-Process`, a `.bat` or `.cmd`
wrapper, a nested PowerShell process, PATH-based Python discovery, or a JSON
stdin pipeline on Windows. Start the venv `python.exe` process directly, wait
for that process, and read its stdout and stderr directly.

Normal lookup must use the persistent local daemon managed by `search.py`.
Do not add `--no-daemon` to an ordinary lookup. A STARTING or BUSY daemon is
not a reason to launch a synchronous search; wait for the bounded daemon queue
through the same direct `search.py` client process. `--no-daemon` is reserved
for explicit diagnostics requested by the user.

Use repeated planning arguments on Windows:

- `--answer-goal`
- `--literal-identifier` (maximum three)
- `--entity` (maximum five)
- `--facet` (maximum four)
- `--semantic-hypothesis` (maximum three, semantic-only)

These arguments also work on macOS and Linux. The `--request-json --stdin`
interface remains available for manual POSIX integration, but it is not the
normal Windows path.

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

1. Pass the user's complete original question as the final argument to
   `search.py` exactly once.
2. Use the default hybrid retrieval behavior.
3. Do not specify `--retrieval-mode`.
4. Do not retry after `ok`, `partial`, `no_hit`, or `error`.
5. A new search is allowed only when the user explicitly requests additional
   investigation or provides a meaningful clarification.
6. Use `--result-delivery file`. Read the returned `summary_file` exactly
   once. Do not read `manifest.json` or any detail item for the initial answer.
7. Treat `summary.json` as the complete initial-answer result. Do not run
   `jq`, `grep`, `head`, `tail`, or another command to post-process it.

Dense, lexical, Exact, metadata, and fusion candidate retrieval performed
inside that one Hybrid call are not additional searches.

## Initial answer

The initial RAG result contains a self-contained `initial_response`.

Answer the user's first question using only:

- `initial_response.answer_draft_markdown`;
- `initial_response.key_points`;
- `initial_response.limitations`;
- the source IDs and short evidence entries in `summary.json`.

Do not read `manifest.json` or detailed item files for the initial answer.
Do not add claims that are absent from the initial response.

A lightweight model should normally be able to use
`answer_draft_markdown` with only minor stylistic editing. Preserve every
limitation and warning.

## Follow-up detail retrieval

A successful lookup may return a `result_set_id` and item IDs such as `E1`,
`E2`, `D1`, and `D2`.

When the user asks to tell them more, explain in more detail, show more
context from a source, or makes an equivalent follow-up request, do not run
`list_dbs.py` or `search.py` again. Run `result_detail.py` exactly once.

Use:

- the evidence IDs used in the previous answer when the user refers to that
  evidence;
- `follow_up.default_item_ids` when the user only asks for more detail;
- explicitly named item IDs when the user identifies a source.

Normally expand one to three items. Invoke the installed venv interpreter
directly:

```text
<venv-python> <rag-root>/query/result_detail.py
  --result-set-id <result-set-uuid>
  --item-id <item-id>
  --detail-level expanded
  --result-delivery file
```

Read the returned `detail_file` exactly once and use its
`answer_draft_markdown` as the basis of the expanded answer. This reads the
previous result bundle and is not a new search.

Run a new search only when the user explicitly requests another search, a
different database, refreshed information, a new subject, or a viewpoint not
represented in the previous result set.

If the temporary result has expired, report that cached detail is no longer
available. Do not automatically repeat the search.

## Result handling

Follow the returned status:

- `ok`: Answer using only `evidence`.
- `partial`: Answer only the supported portion and clearly state the limits.
- `no_hit`: State that direct supporting evidence was not found. If
  `document_results` is nonempty, present them as related research leads.
- `setup_required`: Tell the user that RAG setup is required.
- `error`: Briefly report the error and stop. Do not retry automatically.

Use only `evidence` for factual claims.
Identify the supporting evidence ID, source path, and available location in
the answer.
When a result contains `source_permalink`, prefer it as the document link.
Otherwise, when a result contains `source_url`, use that link. When neither
field exists, cite the stored document path. Do not run another command to
resolve a missing source URL. A missing source URL does not reduce the
authority or relevance of the evidence.
Treat `background_context` as background information only.
Never use `related_context` as proof.
Treat `document_results` as broad discovery results. Weak or
non-authoritative cards are not proof.
Obey every item in `warnings`.
Preserve source severity and uncertainty when translating. Do not strengthen
labels: for example, render `Substantial` damage as "substantial damage"
with the source label preserved, not "destroyed", unless the evidence
explicitly uses the stronger term.

Do not infer missing table headers, column meanings, comparisons, rankings,
maximums, minimums, or qualitative labels such as "large", "small", or
"medium-sized" unless the evidence directly supports them.

When answering:

1. Give the direct answer supported by `evidence`, if available.
2. Summarize broader viewpoints across distinct `document_results`.
3. Clearly label weak or indirect sources.
4. Explain when an identifier was not found literally.
5. Never present an inferred acronym expansion as verified.

If six or more document results are available, use at least three distinct
sources in the answer and normally mention five or more when the user asks
for related material. If direct evidence is empty but document results exist,
say that direct evidence was not found, then present the broader results. Do
not report that the entire RAG search found nothing.
