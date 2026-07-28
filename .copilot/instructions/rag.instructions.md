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

Do not inspect implementation files, delegate lookup to another agent, create
a plan, or run a management command. If the human asks to create or change a
database, Source, retrieval setting, repair, distribution package, or
management-PC transfer, say:

`Please use Local RAG Manager for that operation.`

Do not open the Manager automatically.

If the latest prompt names a database ending in `-rag`, search that database
once and do not list databases. Otherwise list databases once. Choose a
database only when one candidate clearly matches the title, query hint,
content summary, or Source display names/types. If multiple candidates are
reasonable, ask the human to choose and do not search.

Pass the latest human-authored semantic question as the final positional
argument. Remove only system-facing lookup routing such as “search Local RAG”
and an instruction to use the already selected database. Preserve all
remaining characters, identifiers, punctuation, and constraints. Never
rewrite the question as keywords and never search a second database or retry
automatically.

When a contextual reference such as “it”, “that design”, or “the previous
issue” appears, earlier human-authored messages from the same conversation
may be represented only as the minimum necessary structured retrieval hints:

- `--literal-identifier`
- `--entity`
- `--facet`
- `--semantic-hypothesis`
- `--answer-goal`

Never append earlier messages to the positional question. Previous assistant
answers and previous RAG results are not verified facts. If multiple
antecedents would materially change database selection or search meaning, ask
the human to clarify before listing or searching.

When `database_freshness.chat_notice.code` is
`local_rag_snapshot_older_than_30_days`, show its Japanese message at most
once in the current chat. Check only prior assistant messages in this chat.
Do not persist notification state. A new chat may show it once again.

Answer with body citations such as `[E1]` that are never links. End every RAG
answer with exactly one `## References` section containing only cited IDs.
When an item has `uri`, format the entry as `[E1] [filename.ext](URI)` and
attach the link only to the filename. Never show a raw URI. With no `uri`,
show the filename as plain text and optionally the stored relative path.

Do not require a particular Copilot model. Ordinary lookup must not use a
subagent. Shell selection changes command syntax only; the final answer is
always Markdown.
