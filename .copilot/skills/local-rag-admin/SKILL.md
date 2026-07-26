---
name: local-rag-admin
description: Sets up and maintains local RAG databases, including create, build, add, status, resume, rebuild, proxy, and certificate operations.
---

# Local RAG Administration

Use this workflow only for setup, database creation, build, add, resume,
rebuild, status, troubleshooting, or maintenance requests. These operations
may write files and indexes or run for a long time.

Do not require or set a GitHub Copilot model name. Do not create a custom
agent.

## Python selection

Setup is the only workflow that may use a system Python because the RAG
virtual environment does not exist yet.

For setup on macOS or Linux:

```bash
python3 ~/.copilot/rag/query/setup.py
```

For setup on Windows PowerShell:

```powershell
py -3 "$HOME\.copilot\rag\query\setup.py"
```

If that platform command is unavailable, report the missing Python
prerequisite. Do not try a sequence of unrelated interpreters.

After setup, use only the platform-specific RAG virtual-environment Python:

- macOS/Linux: `~/.copilot/rag/query/.venv/bin/python`
- Windows: `$HOME\.copilot\rag\query\.venv\Scripts\python.exe`

If the virtual-environment Python or its `.rag-deps-installed` marker is
missing, report `setup_required` and guide the user through setup.

## Setup, proxy, and CA certificates

Run setup only when the user requests it. Use `--proxy` when the user provides
a corporate proxy:

```bash
python3 ~/.copilot/rag/query/setup.py --proxy http://proxy.example:8080
```

For TLS or certificate failures, explain that the company CA certificate may
need to be assigned to `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, or `PIP_CERT`.
Do not recommend disabling certificate verification as the normal solution.

## Help and database listing

For help, describe the supported operations before showing raw commands:
setup, list databases, search, create, build, add, status, resume, and lexical
component rebuild.

After setup, list databases without loading the embedding model:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/query/list_dbs.py \
  --format json
```

## Status

Inspect status before starting or resuming a long operation:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/gen_db/status.py \
  --db <name-rag> \
  --json
```

Report the phase, completed and total files, current file, `appears_active`,
`can_resume`, recent errors, and the safe next action. Do not start or resume
work when the user requested status only.

Do not start a duplicate operation while `appears_active` is true. During a
long operation, check status periodically instead of relying only on stdout.

## Create and build

Before creating a database, require:

- a database name ending in `-rag`;
- a readable input root;
- a stable source ID;
- a title.

Ask only for missing values. List existing databases once and do not recreate
an existing database.

Create a missing database:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/gen_db/create_db.py \
  --db <name-rag> \
  --title "<title>"
```

Add `--query-hint "<short-hint>"` only when the user supplied a useful hint or
it can be stated without inventing domain facts.

After checking status, build:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/gen_db/build_db.py \
  --db <name-rag> \
  --root "<input-folder>" \
  --source-id <source-name> \
  --resume
```

After completion, report document, chunk, and collection counts plus
extraction errors.

## Add or update data

Check status first. Preserve existing contents and run:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/gen_db/add_data.py \
  --db <name-rag> \
  --root "<input-folder>" \
  --source-id <source-name>
```

Use `--retry-errors` only when the user explicitly requests retrying prior
extraction failures or approves the explained retry.

Treat Markdown as a normal source format. When converted Markdown and original
Office or PDF files both exist under the requested root, process both unless
the user explicitly excludes one.

## Resume and force rebuild

Use the saved `resume_command` only when `can_resume` is true and the saved
root and source ID match the user's intended input.

Never run `force_rebuild_command` or `--force-rebuild` unless the user clearly
asks to discard prior work. Ask for confirmation when the saved input identity
does not match the requested input.

## Component rebuild

For a SQLite, lexical, FTS, identifier, or metadata-only rebuild:

1. Check status and verify that no operation is active.
2. Run:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/gen_db/rebuild_component.py \
  --db <name-rag> \
  --component lexical
```

3. Explain that embeddings are not recomputed.

Do not substitute `vector`, `extract`, or `all` unless the user explicitly
requests that larger rebuild scope.

On Windows, use the same script paths with
`$HOME\.copilot\rag\query\.venv\Scripts\python.exe` and PowerShell quoting.
