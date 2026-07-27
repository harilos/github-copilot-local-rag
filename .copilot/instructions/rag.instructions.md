# Local RAG Routing

Treat ordinary local RAG lookup as a simple, read-only task suitable for a
lightweight and fast model selected by Auto.

Do not require or assume any particular model. Do not create a multi-step
execution plan, inspect implementation code, edit files, or delegate ordinary
lookup to Codex, a coding agent, or a subagent. The single bounded structured
request defined by the `local-rag` skill is allowed and is not a separate
lookup or delegated planning step.

For an executed lookup, pass the entire latest human-authored visible prompt
as the final `search.py` argument, including wrapper text before a colon.
Never replace it with only an embedded question or extracted keywords.

When the user explicitly asks to answer from RAG, local documents, internal
or company information, or information installed in or provided to Copilot:

- Use the `local-rag` skill for ordinary lookup.
- When the user asks to show or print an exact ordinary Local RAG lookup
  command without executing it, load the `local-rag` skill exactly once and
  follow its static command-only contract.
- Use the `local-rag-admin` skill only for setup, database creation, build,
  add, resume, rebuild, status, or maintenance requests.
- Do not load administrative instructions during ordinary lookup.

A request to create, edit, inspect, or explain Source-Link settings is not an
ordinary lookup. Treat it as a human-only Local RAG Manager boundary: do not
list databases, search, inspect configuration, run an admin command, or open
the Manager. State only that a human can manage those settings through the
Local RAG Manager and stop.

Treat equivalent source-based wording in any language as an explicit lookup
request, even when the user does not say "RAG". Do not activate lookup merely
because a question mentions a company or an internal-sounding term.

Do not create or use a custom agent for local RAG. Do not set or require a
model name. GitHub Copilot model selection remains Auto.
