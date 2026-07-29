---
name: local-rag
description: Performs bounded read-only research through the two public Local RAG entry points when the human explicitly asks to use local or installed documents.
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

Do not inspect or invoke Local RAG's private implementation files, lower-level
commands, or Source acquisition modules merely to bypass these public entry
points. This does not prohibit reading the human's workspace files, repository
files, folders, or configuration when they are relevant to the requested
analysis.

For database creation or editing, Source addition/update/resume, repair,
distribution, transfer, or link configuration, do not list or search. Say:

`Please use Local RAG Manager for that operation.`

Do not open the Manager automatically.

If setup is missing, say:

`Initial setup normally takes about 10 minutes. Please run setup from Local RAG Manager.`

Do not run setup from Copilot.

## Bounded retrieval routing

If the human names a database ending in `-rag`, use it directly:

- database-list calls: 0
- retrieval search calls: exactly 1 for a simple direct question
- retrieval search calls: at most 4 for a broad or multi-part research request

If no database is named:

- database-list calls: exactly 1
- retrieval search calls: 1 only when one database clearly matches
- retrieval search calls: at most 4 when distinct follow-up research is needed

Use only the returned database name, title, query hint, content summary, and
Source display names/types to choose. These fields are routing metadata, not
answer evidence. If multiple databases are plausible, ask the human to choose
and do not search.

All retrieval searches for one human request must use the same selected
database. Never use `--auto`, never search another database automatically, and
never delegate ordinary lookup to another agent.

A simple definition, fact lookup, identifier lookup, or other direct question
uses one retrieval search. Do not call RAG repeatedly merely to collect more
results.

A broad, exploratory, multi-part, comparative, or evidence-synthesis request
may use up to four retrieval searches in total. After reading a result, an
additional search is allowed only when it has a distinct purpose that
materially helps the original request, such as:

- deepening a relevant concept or mechanism surfaced by the result;
- covering a separate part of a multi-part question;
- retrieving the other side of a requested comparison;
- following a relevant document family, component, event, or dependency;
- resolving a clearly identified evidence gap.

Stop early when the material is sufficient. Do not repeat a query, run a
near-identical reformulation, retry an error or timeout, or use additional
searches only because the first result was short. A distinct follow-up search
is research expansion, not an automatic retry.

`list_dbs.py` does not count as a retrieval search. A cached-detail read using a
`result_set_id` does not rerun retrieval and does not count toward the four
retrieval searches.

## Search questions and hints

For the first retrieval search, the final positional argument is the latest
human-authored semantic question. Remove only system-facing routing such as
“search Local RAG”, an equivalent lookup instruction, and an instruction to use
the already selected database. Preserve all remaining characters, identifiers,
punctuation, and constraints. Do not turn the question into keywords.

A later retrieval search may use a focused semantic subquestion. It must remain
traceable to the original request and pursue the distinct purpose identified
from the preceding material. Preserve all original constraints that still
apply. Do not disguise a retry as a new search by merely shortening, expanding,
or reordering the same words.

If the latest prompt contains a contextual reference, use only the minimum
relevant earlier human-authored context through:

- `--literal-identifier` (at most three)
- `--entity` (at most five)
- `--facet` (at most four)
- `--semantic-hypothesis` (at most three)
- `--answer-goal` (one of `definition`, `evidence`, `comparison`,
  `procedure`, `history`, `survey`)

Do not treat an assistant answer, an inferred acronym expansion, or an earlier
RAG result as a verified fact. A concept surfaced by an earlier RAG result may
be used as an `--entity`, `--facet`, or semantic subquestion for a distinct
follow-up search, but the follow-up result must provide its own support. Put
speculation only in `--semantic-hypothesis`. If more than one antecedent is
reasonably possible and the difference matters, ask for clarification without
listing or searching.

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
  "<semantic-question-or-distinct-subquestion>"
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
  "<semantic-question-or-distinct-subquestion>"
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
  "<semantic-question-or-distinct-subquestion>"
```

On Windows, do not use `cmd.exe /c`, `cmd /c`, `Start-Process`, a batch wrapper,
nested PowerShell, PATH-based Python discovery, or an stdin pipeline. Execute
one direct process per retrieval search and read stdout/stderr directly. Do not
combine a search invocation with a pipeline or JSON-processing command.

Do not add `--no-daemon` to ordinary lookup. Do not select a retrieval mode. If
the platform-specific interpreter is missing, use the setup message above in
the human's language and stop.

## Per-search result handling

For each retrieval search:

1. execute the public search entry point once for that distinct question;
2. parse its pointer JSON;
3. read its `summary_file` once;
4. do not scan the result directory or read its manifest/items directly;
5. do not use `jq`, `grep`, `head`, or `tail`;
6. decide whether the original request is now sufficiently covered before any
   additional search.

Keep each returned summary as a separate result set. Use `evidence` for
supported factual claims. `background_context` is background only.
`related_context` is not proof. `document_results` may support a clearly
labelled provisional answer or identify a distinct follow-up research concept.

For `partial`, preserve every limitation. For `no_hit`, state that direct
evidence was not found. If related/document results exist, they may still be
used to construct the best clearly labelled provisional answer or to identify
a distinct follow-up subquestion. Never promote related material to verified
evidence.

If a summary is insufficient and its cached text would materially help, read
cached detail at most once for one to three returned item IDs through the same
public search entry point:

```text
<venv-python> <rag-root>/search.py
  --result-set-id <uuid>
  --item-id <E1|D1|...>
  --detail-level expanded
  --result-delivery file
```

Repeat `--item-id` for up to three IDs. This is cached detail, not a new search.
It must not include `--db` or a question. Read the returned detail file once. If
it has expired, report that fact and do not repeat the retrieval search.

## Combining RAG and workspace material

The final answer may freely combine:

- one to four Local RAG result sets;
- cached detail;
- relevant workspace files and folder structure;
- user-provided documents;
- the assistant's comparison, organization, and clearly identified inference.

Do not require the final answer to copy `answer_draft_markdown` or follow a
fixed structure. Tables, lists, code, design alternatives, and cross-source
analysis are allowed.

## Freshness notice

If any search response contains:

```text
database_freshness.status = stale
database_freshness.chat_notice.code =
  local_rag_content_snapshot_older_than_30_days
```

show `database_freshness.chat_notice.message_ja` exactly once in the current
chat. Deduplicate it using `database_freshness.chat_notice.dedupe_key`.

Do not show it again if a prior assistant message in this chat already did,
even after additional searches or changing databases in a later human request.
Do not persist notification state. Do not show the notice for `current` or
`unknown`.

## Mandatory citations and references

The final answer is always Markdown. Material-supported claims must carry a
plain body citation. Do not put a URL or Markdown link in the answer body.

For one RAG result set, use the returned IDs such as `[E1]`, `[B1]`, and `[D1]`.
When two or more result sets are used and their IDs could collide, qualify each
RAG citation by retrieval order, for example `[R1-E1]`, `[R2-E1]`, and
`[R3-D2]`. Keep the same qualified ID in References. For workspace material
that has no returned ID, assign stable answer-local IDs such as `[W1]`.

Cite material at the claim, sentence, bullet, paragraph, or table cell it
supports. Clearly distinguish direct evidence, background, related material,
and the assistant's own inference. Do not fabricate a citation ID.

End with exactly one `## References` section. Nothing may appear after it.
Include every body citation exactly once, normally in first-citation order.

For each RAG source:

1. use its `reference.markdown` when available;
2. prefix that reference with the exact body citation ID, including any
   retrieval-order qualifier;
3. if `reference.markdown` is unavailable, use `source_permalink` first and
   otherwise `source_url`;
4. display at most one URL for the source;
5. attach a Markdown link only to the filename;
6. when no URL exists, show the filename and stored relative path as plain
   text.

Do not print `source_url` and `source_permalink` as alternatives. Do not expose
a raw URL. A valid mixed-source footer may look like:

```markdown
## References

- [R1-E1] [design.md](https://example.invalid/fixed/design.md)
- [R2-D1] specification.pdf — `project/docs/specification.pdf`
- [W1] `src/service/config.py`
```

If the body cites nothing, still emit:

```markdown
## References

No sources were cited in the answer.
```

Before sending, verify only the citation contract: material-supported claims
have plain IDs, the body contains no URL or Markdown link, exactly one
References section is last, every body ID appears there once, and each source
shows at most one URL. Do not constrain the answer's structure beyond these
requirements.
