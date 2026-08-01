# Local RAG Routing

Use the `local-rag` Skill only when the human explicitly asks to answer from
Local RAG, local documents, internal/company information, or information
installed for Copilot.

Equivalent explicit requests include “internal or company information” and
“information installed in or provided to Copilot”. Do not activate lookup
merely because the topic sounds internal, technical, or organization-specific.

Ordinary Local RAG access is read-only and has exactly two public operations:

1. list available databases and the kinds of material they contain;
2. search one selected database, or read cached detail through the same search
   entry point.

Do not inspect or invoke Local RAG's private implementation files or lower-level
commands merely to bypass those public entry points. This restriction does not
prohibit reading the human's workspace files, repository files, folder
structure, or configuration when they are relevant to the requested analysis.
Do not delegate ordinary lookup to another agent, create a plan, or run a
management command.

Initial runtime setup is the only management exception. When Local RAG is not
installed completely, its virtual-environment interpreter is missing, or a
public lookup returns `setup_required`, activate the `local-rag-setup` Skill and
run the public `~/.copilot/rag/setup.py` entry point directly. Do not redirect
the human to Local RAG Manager for initial runtime setup. On Windows, invoke
`%USERPROFILE%\.copilot\rag\query\.venv\Scripts\python.exe` directly with the
public setup script. Do not use PATH-based Python or `py -3`; the packaged
runtime is verified offline and does not run pip or prepare a model. On
macOS/Linux, preserve the existing setup behavior. Report meaningful progress
and sanitized diagnostics.

If the human asks to create or change a database, Source, retrieval setting,
repair, distribution package, or management-PC transfer, say:

`Please use Local RAG Manager for that operation.`

Do not open the Manager automatically.

If the latest prompt names a database ending in `-rag`, use that database and
do not list databases. Otherwise list databases exactly once. Choose a database
only when one candidate clearly matches the title, query hint, content summary,
or Source display names/types. If multiple candidates are reasonable, ask the
human to choose and do not search.

Use exactly one retrieval search for a simple, direct question. For a broad,
exploratory, multi-part, comparative, or evidence-synthesis request, up to four
retrieval searches may be used in total against the same selected database.
Each additional search must pursue a distinct subquestion, related concept,
mechanism, document family, comparison side, or evidence gap that materially
helps answer the original request. Stop as soon as the available material is
sufficient. `list_dbs.py` is not a retrieval search, and cached-detail reads do
not rerun retrieval.

For the first search, pass the latest human-authored semantic question as the
final positional argument. Remove only system-facing lookup routing such as
“search Local RAG” and an instruction to use the already selected database.
Preserve all remaining characters, identifiers, punctuation, and constraints.
A later search may use a focused semantic subquestion derived from the original
request and concepts surfaced by an earlier result. Do not reduce any search to
keywords, repeat a near-identical query, automatically retry a failed call, or
switch to another database without the human choosing it.

When a contextual reference such as “it”, “that design”, or “the previous
issue” appears, earlier human-authored messages from the same conversation may
be represented only as the minimum necessary structured retrieval hints:

- `--literal-identifier`
- `--entity`
- `--facet`
- `--semantic-hypothesis`
- `--answer-goal`

Earlier RAG results may suggest a concept for a distinct follow-up search, but
they are not verified facts. Previous assistant answers are not verified facts
either. If multiple antecedents would materially change database selection or
search meaning, ask the human to clarify before listing or searching.

When `database_freshness.chat_notice.code` is
`local_rag_content_snapshot_older_than_30_days`, show its Japanese message at
most once in the current chat. Deduplicate by
`database_freshness.chat_notice.dedupe_key`. Check only prior assistant
messages in this chat. Do not persist notification state. A new chat may show
it once again.

Cite material-supported claims in the body with plain IDs such as `[E1]`,
`[D1]`, or `[W1]`; never link those IDs. When more than one retrieval result is
used and item IDs collide, qualify them by retrieval order, for example
`[R1-E1]` and `[R2-E1]`. Do not put a URL or Markdown link in the answer body.

End every RAG answer with exactly one `## References` section containing every
body citation once. Nothing may follow it. For each RAG item, use
`reference.markdown` when present. Otherwise use `source_permalink` first,
then `source_url`, and display at most one URL for that source. Attach the link
only to the filename; with no URL, show the filename and optional stored
relative path as plain text.

Do not require a particular Copilot model. Ordinary lookup must not use a
subagent. Shell selection changes command syntax only; the final answer is
always Markdown.
