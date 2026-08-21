# LRR-AGENT-003 read-only MCP PoC

## Purpose

Agent-002 R5 showed that a model-facing PowerShell procedure is not a reliable
transport boundary: four runs used no tool and three runs fell back to generic
terminal repository searches. This PoC replaces that surface with one semantic
read-only MCP tool and gives the custom agent no terminal, file, web, edit, or
subagent tool.

## Frozen scope

- Branch: `poc/agent-003-readonly-mcp`, based on the recorded `origin/main`.
- Server: `tools/experiments/agent_003_readonly_mcp/server.py`,
  standard-library JSON-RPC over stdio.
- Workspace configuration: `.vscode/mcp.json`, server name
  `localRagAgent003`.
- Agent: `.github/agents/agent003-readonly-local-rag.agent.md`.
- Public Local RAG calls: installed `list_dbs.py` and `search.py` only.
- No installer, package, existing product agent, database, fixture, or Agent-002
  artifact changes in this PoC.

## Security boundary

The model can provide only a semantic question and an optional validated
database name. It cannot provide a command, executable, path, environment
variable, URL, search mode, or output destination. The server uses no shell,
checks the public result pointer and summary schema, bounds time and output,
and exposes MCP read-only/non-destructive annotations.

## Direct verification

Run:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" -B `
  "tools\experiments\agent_003_readonly_mcp\test_server.py"
```

Only after that passes, open this worktree in the dedicated VS Code profile,
review/start `localRagAgent003`, select `AGENT003-READONLY-RAG`, and use a
natural internal question. The first MCP configuration start may require the
normal VS Code server-trust confirmation. That trust step is not automated.

Suggested non-secret synthetic smoke question:

```text
関数 ledger_sync のメンテナンス窓はいつですか？
```

The expected behavior is candidate routing followed by one search of
`agent002-decoy-rag`; the answer must cite returned evidence. Do not use the
Orion approval-code prompt as the primary smoke because model safety refusal is
independent of retrieval transport correctness.
