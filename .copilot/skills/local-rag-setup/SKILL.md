---
name: local-rag-setup
description: Performs the Local RAG runtime initial setup directly when lookup reports setup_required or the installed virtual environment is missing.
---

# Local RAG Initial Setup

Use this Skill only for initial runtime setup or when a public Local RAG command
returns `setup_required`. Initial setup is performed by Copilot, not by Local RAG
Manager.

Use only the public setup entry point:

- `~/.copilot/rag/setup.py`

Do not invoke `query/setup.py` directly. Do not open or redirect the human to
Local RAG Manager for this operation.

Initial setup may create the Local RAG virtual environment, install pinned Python
dependencies, download or prepare the embedding model, verify databases, and
write the machine-verifiable completion marker. It normally takes several
minutes. Show meaningful progress from the command output. Do not claim success
until the command exits successfully and reports `setup_complete=true`.

## macOS/Linux

Run:

```bash
python3 ~/.copilot/rag/setup.py --format json
```

If `python3` is unavailable, try `python` once. Do not probe unrelated Python
installations.

## Windows PowerShell

Run one direct process:

```powershell
python "$env:USERPROFILE\.copilot\rag\setup.py" --format json
```

If `python` is unavailable, try this once:

```powershell
py -3 "$env:USERPROFILE\.copilot\rag\setup.py" --format json
```

Do not use `cmd.exe /c`, `Start-Process`, a batch wrapper, nested PowerShell, or
an stdin pipeline.

## Failure handling

If setup fails, report the failed phase and sanitized error details. For network,
proxy, CA, package-index, or model-download failures, ask only for the missing
machine-local network information needed to rerun setup. Do not send the human
to Manager as a generic fallback.

Do not create databases or add Sources as part of initial setup. Those remain
human Manager operations.
