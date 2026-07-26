# Local RAG Routing

Treat ordinary local RAG lookup as a simple, read-only task suitable for a
lightweight and fast model selected by Auto.

Do not require or assume any particular model. Do not create a plan, inspect
implementation code, edit files, or delegate ordinary lookup to Codex, a
coding agent, or a subagent.

When the user explicitly requests RAG or local-document lookup:

- Use the `local-rag` skill for ordinary lookup.
- Use the `local-rag-admin` skill only for setup, database creation, build,
  add, resume, rebuild, status, or maintenance requests.
- Do not load administrative instructions during ordinary lookup.

Do not create or use a custom agent for local RAG. Do not set or require a
model name. GitHub Copilot model selection remains Auto.
