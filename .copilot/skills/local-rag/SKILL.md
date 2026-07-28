---
name: local-rag
description: Performs one read-only lookup through the two public Local RAG entry points when the human explicitly asks to use local or installed documents.
---

# Local RAG Lookup

Activate this Skill only for an explicit request to use RAG, local documents,
internal or company information, or information installed in or provided to
Copilot. Do not activate lookup merely because the topic sounds internal,
technical, or organization-specific.

## Boundary

This Skill exposes only:

- `~/.copilot/rag/list_dbs.py`
- `~/.copilot/rag/search.py`

Do not inspect or invoke any lower-level command or Source acquisition module.
For database creation or editing, Source addition/update/resume, repair,
distribution, transfer, or link configuration, do not list or search. Say:

`Please use Local RAG Manager for that operation.`

Do not open the Manager automatically.

## One-call routing

If the human names a database ending in `-rag`, use it directly:

- database-list calls: 0
- search calls: 1

If no database is named:

- database-list calls: exactly 1
- search calls: 1 only when one database clearly matches

Use only the returned database name, title, query hint, content summary, and
Source display names/types to choose. These fields are routing metadata, not
answer evidence. If multiple databases are plausible, ask the human to choose
and do not search.

Never use `--auto`. Never retry, rewrite the query, search another database,
or delegate ordinary lookup to another agent.

## Semantic question

The final positional argument is the latest human-authored semantic question.
Remove only system-facing routing such as “search Local RAG”, an equivalent
lookup instruction, and an instruction to use the already selected database.
Preserve all remaining characters, identifiers, punctuation, and constraints.
Do not turn the question into keywords.

If the latest prompt contains a contextual reference, use only the minimum
relevant earlier human-authored context through:

- `--literal-identifier` (at most three)
- `--entity` (at most five)
- `--facet` (at most four)
- `--semantic-hypothesis` (at most three)
- `--answer-goal` (one of `definition`, `evidence`, `comparison`,
  `procedure`, `history`, `survey`)

Never append earlier messages to the positional question. Do not treat an
assistant answer, an inferred acronym expansion, or an earlier RAG result as a
verified fact. Put speculation only in `--semantic-hypothesis`. If more than
one antecedent is reasonably possible and the difference matters, ask for
clarification without listing or searching.

## Runtime

Use the installed virtual-environment interpreter directly. Do not probe
`python`, `python3`, or `py`.

### macOS/Linux

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/list_dbs.py \
  --format json
```

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/search.py \
  --db <selected-db> \
  --include-db-hint \
  --compact-json \
  --result-delivery file \
  --format json \
  "<semantic-question>"
```

### Windows PowerShell

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\list_dbs.py" `
  --format json
```

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\search.py" `
  --db <selected-db> `
  --include-db-hint `
  --compact-json `
  --result-delivery file `
  --format json `
  "<semantic-question>"
```

### Windows Git Bash

```bash
"$HOME/.copilot/rag/query/.venv/Scripts/python.exe" \
  "$HOME/.copilot/rag/search.py" \
  --db <selected-db> \
  --include-db-hint \
  --compact-json \
  --result-delivery file \
  --format json \
  "<semantic-question>"
```

On Windows, do not use `cmd.exe /c`, `cmd /c`, `Start-Process`, a batch
wrapper, nested PowerShell, PATH-based Python discovery, or an stdin pipeline.
Execute one direct process and read stdout/stderr directly. Do not combine the
search invocation with a pipeline or JSON-processing command.

Do not add `--no-daemon` to ordinary lookup. Do not select a retrieval mode.
If the platform-specific interpreter is missing, report that setup is
required and stop.

## Initial result

Normal lookup uses file delivery:

1. execute the public search entry point once;
2. parse its pointer JSON;
3. read `summary_file` once;
4. do not scan the result directory or read its manifest/items directly;
5. do not use `jq`, `grep`, `head`, or `tail`.

Use `evidence` for supported factual claims. `background_context` is
background only. `related_context` is not proof. `document_results` may be
used for a clearly labelled provisional answer.

For `partial`, preserve every limitation. For `no_hit`, state that direct
evidence was not found. If related/document results exist, still construct
the best clearly labelled provisional answer so the human can judge whether
it is useful; it may be off-target. Never promote related material to verified
evidence.

If the initial summary is insufficient and cached text would materially help,
read cached detail at most once for one to three returned item IDs through the
same public search entry point:

```text
<venv-python> <rag-root>/search.py
  --result-set-id <uuid>
  --item-id <E1|D1|...>
  --detail-level expanded
  --result-delivery file
```

Repeat `--item-id` for up to three IDs. This is cached detail, not a new
search. It must not include `--db` or a question. Read the returned detail
file once. If it has expired, report that fact and do not repeat the search.

## Freshness notice

If the search response contains:

```text
database_freshness.status = stale
database_freshness.chat_notice.code =
  local_rag_snapshot_older_than_30_days
```

show `database_freshness.chat_notice.message_ja` exactly once in the current
chat.

Do not show it again if a prior assistant message in this chat already did,
even after changing databases. Do not persist notification state. Do not show
the notice for `current` or `unknown`.

## Mandatory citation format

The final answer is always Markdown.

In the body:

- cite only with plain IDs such as `[E1]`, `[E2]`, `[D1]`;
- never link a citation ID;
- never show a raw URI;
- never link a filename in the body.

End with exactly one `## References` section. Include only IDs cited in the
body, in first-citation order, once each.

For each cited item:

1. derive the filename from the final component of its stored relative path;
2. when `uri` exists, write `[E1] [filename.ext](URI)`;
3. when `uri` is absent, write `[E1] filename.ext`, optionally followed by
   the stored relative path as plain text;
4. attach a Markdown link only to the filename.

If the body cites nothing, still emit:

```markdown
## References

No sources were cited in the answer.
```

Before sending, verify that body IDs are unlinked, exactly one References
section is last, all and only cited IDs are listed, only filename text is
linked, and no raw URI is visible.
