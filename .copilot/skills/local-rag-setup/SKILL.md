---
name: local-rag-setup
description: Performs the Local RAG runtime initial setup directly when lookup reports setup_required or the installed runtime is missing.
---

# Local RAG Initial Setup

Use this Skill only for initial runtime setup or when a public Local RAG command
returns `setup_required`. Use only the public `~/.copilot/rag/setup.py`
entry point. Do not invoke `query/setup.py` directly and do not redirect the
human to Local RAG Manager.

Do not create databases, update Sources, rebuild indexes, or change network
configuration as part of initial setup.

## Windows PowerShell

The official Windows x64 package contains a fixed, verified Python runtime,
dependencies, and ONNX model. Run exactly one direct command:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\setup.py" --format json
```

Do not use PATH-based `python`, `py -3`, `cmd.exe /c`,
`Start-Process`, a batch wrapper, nested PowerShell, or an stdin pipeline.
Windows packaged setup is offline: it must not create a venv, run pip, download
or convert a model, or fall back to system Python.

After setup, tell the human to select one of the three installed LOCAL-RAG
Agents in VS Code Copilot Chat. The installer registers the fixed user-level
`localragagent003` server. It exposes only the read-only search and evidence
tools and does not change VS Code approval settings.

## macOS/Linux

The existing setup contract remains unchanged:

```bash
python3 ~/.copilot/rag/setup.py --format json
```

If `python3` is unavailable, try `python` once. Do not probe unrelated
installations.

## Success and failure

Do not claim success until the command exits successfully and reports
`setup_complete=true`. Report the failed phase and sanitized diagnostics on
failure. A VS Code integration warning does not change the meaning of
`setup_complete` or `lookup_ready`.
