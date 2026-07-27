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

## Setup verification

Run setup only when the user requests it. Normal setup verifies the runtime,
model inference, JSON database listing, and installed database health before
reporting success. Prefer JSON output so completion is machine-verifiable:

```bash
python3 ~/.copilot/rag/query/setup.py --format json
```

Verify without installing, downloading, or modifying the runtime or databases:

```bash
python3 ~/.copilot/rag/query/setup.py --verify-only --format json
```

On Windows PowerShell:

```powershell
py -3 "$HOME\.copilot\rag\query\setup.py" --verify-only --format json
```

Treat `setup_complete: true` as runtime completion. Treat `lookup_ready`
separately because a complete runtime may have no installed database.

The installers automatically migrate the legacy `ok` completion marker after
an offline deep verification. If an installation was updated without an
installer, run this explicit offline migration:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/query/setup.py \
  --migrate-legacy-marker \
  --format json
```

## Temporary proxy and CA configuration

For temporary network configuration, use CLI options:

```bash
python3 ~/.copilot/rag/query/setup.py \
  --proxy http://proxy.example:8080 \
  --ca-bundle /path/to/company-ca.pem \
  --no-proxy .internal.example \
  --format json
```

On Windows PowerShell:

```powershell
py -3 "$HOME\.copilot\rag\query\setup.py" `
  --proxy http://proxy.example:8080 `
  --ca-bundle "C:\certs\company-ca.pem" `
  --no-proxy .internal.example `
  --format json
```

Never place proxy credentials in a response, log, command explanation, or
configuration preview. Do not disable TLS certificate verification.

## Persistent proxy and CA configuration

The optional persistent file is:

```text
~/.copilot/rag/config/network.json
```

Do not require repeated `--proxy` options when this file is valid. When the
user asks to save persistent configuration:

1. Inspect an existing file before changing it.
2. Ask for a missing proxy URL.
3. Ask for a company CA path only when it is required.
4. Default to `"mode": "auto"`.
5. Use `"mode": "required"` only when the user explicitly prohibits direct
   external access.
6. Preserve existing fields that the user did not ask to change.
7. Never display stored proxy credentials.
8. Do not persist credentials embedded in a proxy URL. Store a
   credential-free endpoint and use deliberate environment configuration for
   authentication.
9. On POSIX, make a newly written `network.json` owner-readable and
   owner-writable only (`0600`). Do not broaden the existing Windows profile
   ACL.

Use CLI options instead for temporary proxy use. `auto` probes only the
configured proxy endpoint before an external operation, selects proxy or
direct once, and never retries the real operation through the other route.
`required` fails when the configured proxy is unavailable. `off` ignores only
tool-local proxy and CA values while preserving explicit CLI and environment
settings.

Use `--network-config <path>` for an explicit alternative file and
`--ignore-network-config` to ignore persistent configuration for one command.
Normal lookup and other offline operations must not load or probe this
configuration.

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

## Migration export

When the user asks to move, back up, or export the installed Local RAG
environment, use the installed POSIX exporter:

```bash
~/.copilot/rag/export_migration.sh \
  --output "$HOME/local-rag-migration.tar.gz"
```

The exporter uses a blacklist under `~/.copilot/rag`, so new RAG files are
included by default. Outside `rag`, it includes only the RAG instruction and
the `local-rag` and `local-rag-admin` skills. It intentionally excludes the
platform-specific virtual environment, daemon state, caches, SQLite
transients, and private credential files.

Do not start or terminate the daemon or a database maintenance operation
implicitly for export. If the exporter reports an active daemon, active DB
maintenance, or an uncheckpointed SQLite WAL, explain the condition and stop
or perform a separately authorized graceful maintenance action.

Persistent `network.json` is included by default when present. The export must
fail without displaying the URL when `proxy_url` contains a username or
password, or when the configuration contains a persisted password, token,
secret, or credential field. Never export embedded proxy credentials.

Exclude the persistent network configuration when the user wants to configure
the destination separately:

```bash
~/.copilot/rag/export_migration.sh \
  --exclude-network-config \
  --output "$HOME/local-rag-migration.tar.gz"
```

Never print the configuration contents or proxy credentials. Treat every
migration archive as sensitive because it can contain local or company
documents. Do not upload, commit, email, or otherwise transmit it unless the
user explicitly requests that separate action.

Verify a transferred archive before restoring it:

```bash
~/.copilot/rag/export_migration.sh \
  --verify "$HOME/local-rag-migration.tar.gz"
```

On the destination, preserve unrelated Copilot files and recreate the RAG
virtual environment with the normal setup workflow.

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

When only part of a larger source tree should be ingested, preserve the larger
directory as `--root` and pass the selected relative folder through
`--scan-subdir`.

Root-name inclusion in stored document paths is always enabled.

Example:

- root: `Project Knowledge`
- scan subdirectory: `plans/FY26`
- stored path: `Project Knowledge/plans/FY26/...`

Do not replace `--root` with the selected scan subdirectory.

Before resuming, compare the saved root, source ID, and scan subdirectory.
Do not resume when they differ.

Changing from old relative paths to root-prefixed paths changes path-derived
document IDs. Rebuild an existing database once if it must adopt the new path
format.

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
root, source ID, and scan subdirectory match the user's intended input.

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
