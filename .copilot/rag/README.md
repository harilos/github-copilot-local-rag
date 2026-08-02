# Copilot Local RAG Pack

This directory is installed under `~/.copilot/rag`. From a repository clone,
use the repository installers; an overlay install preserves machine-local
runtime state and removes only explicitly retired product files. A
Manager-generated package includes `install.sh` and `install.ps1` at its root.
Extract the package and run the installer for the receiving operating system;
it copies the contained `.copilot` directory into the user's home directory.

On a clean installation, prepare the runtime before lookup or Manager use:

```bash
python3 -B ~/.copilot/rag/setup.py --format human
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  -B `
  "$env:USERPROFILE\.copilot\rag\setup.py" --format human
```

The official Windows x64 package embeds its fixed Python, locked dependencies,
and ONNX model. Packaged setup is offline and never falls back to system
Python. In VS Code Copilot Chat, use Agent mode and enable `runInTerminal` in
Configure Tools; also enable `readFile` for file result delivery.

The receiving user's `~/.copilot/copilot-instructions.md` must contain:

<!-- markdownlint-disable MD013 -->

```text
For requests to use RAG, local documents, internal or company information, or information installed in or provided to Copilot, read ~/.copilot/instructions/rag.instructions.md.
```

<!-- markdownlint-enable MD013 -->

## Public lookup boundary

Ordinary read-only lookup has two public entry points:

```text
~/.copilot/rag/list_dbs.py
~/.copilot/rag/search.py
```

Run them with the installed RAG virtual-environment interpreter.

macOS/Linux:

```bash
~/.copilot/rag/query/.venv/bin/python \
  -B \
  ~/.copilot/rag/list_dbs.py --format json

~/.copilot/rag/query/.venv/bin/python \
  -B \
  ~/.copilot/rag/search.py \
  --db <db-name>-rag \
  --include-db-hint \
  --compact-json \
  --result-delivery file \
  --format json \
  "<question>"
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  -B `
  "$env:USERPROFILE\.copilot\rag\list_dbs.py" `
  --format json

& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  -B `
  "$env:USERPROFILE\.copilot\rag\search.py" `
  --db <db-name>-rag `
  --include-db-hint `
  --compact-json `
  --result-delivery file `
  --format json `
  "<question>"
```

The database list returns a bounded public content summary and Source display
metadata. It does not disclose raw Source IDs. Each public search invocation
delegates to the installed retrieval runtime once, then adds optional public
`source_url` and `source_permalink` values plus database freshness metadata
derived only from `rag-wrapper.json.content_snapshot_at`. A stale database may
include one Japanese conversation notice under
`database_freshness.chat_notice`. Packaging time is recorded separately and
never advances content freshness.

File delivery returns a small JSON pointer. The referenced summary is
self-contained. Cached follow-up detail is read through the same public
`search.py` entry point by passing `--result-set-id`; it does not rerun
retrieval.

## Human Manager

Use the interactive Manager for every change:

```bash
~/.copilot/rag/query/.venv/bin/python -B ~/.copilot/rag/manage.py
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  -B `
  "$env:USERPROFILE\.copilot\rag\manage.py"
```

Manager prompt examples are organization-customizable without editing
`manage.py`. Copy `config/manage-custom.example.json` to the untracked
`config/manage-custom.json` and override only the required keys. The schema is
`local-rag.manage-custom.v1`. An absolute file selected by
`LOCAL_RAG_MANAGE_CUSTOM_CONFIG` has highest priority, followed by
`manage-custom.json`, then the tracked example. Invalid individual fields
fall back to the next layer with a non-sensitive warning. Credentials and
other secret values are rejected.

Top-level menu:

```text
1. Create a new database.
2. Select and manage a database.
3. Update or resume every Source in every database.
4. Create or import a distribution/management-PC package.
5. Verify this computer's setup.
6. Stop the authenticated search daemon.
0. Exit.
```

Selected-database menu:

```text
1. View or update Sources.
2. Add a new Source.
3. Update or resume every Source in this database.
4. Edit the database title and query hint.
5. Copy this database, optionally excluding selected Sources.
6. Diagnose and repair problems.
7. Delete this database with explicit confirmation.
0. Return.
```

A Source is the unit of acquisition and URL generation. The Source Manager
supports GitHub repositories, GitHub Issues, GitHub Wiki, SVN, Redmine,
SharePoint synchronized folders, Teams shared folders synchronized through
OneDrive, GitLab Issues, and a local one-time import. GitHub Issues uses the
authenticated `gh` CLI and materializes Issue bodies and comments as Markdown.
GitHub Wiki synchronizes the repository's `.wiki.git` remote. File-based
Sources can ingest all supported files or documents only;
Markdown is included in the documents-only selection. It stores provider
configuration and resumable checkpoints outside indexed document identity. A
Source becomes searchable only after successful ingestion.

GitLab Issue Sources use a machine-local access token with `read_api`
permission. GitLab fetch settings stored in the DB contain only the
GitLab/project URLs, an instance-derived environment name, and the update
window. Open and closed Issues plus Discussions are materialized as Markdown
and reflected in batches of five.
If an Issue is deleted or becomes invisible to the acquisition account, its
existing RAG document is retained. Later updates only create or overwrite
Issues visible to that account. After the first successful ingestion, the
GitLab installation and project URLs are immutable; add another Source to
collect a different project.

SharePoint Source acquisition and update are intentionally Windows-only. They
read the synchronized root registered in this computer's Source connection
settings and do not copy the synchronized tree into the DB. Teams uses the
same root. A packaged database can be copied to macOS and searched there;
macOS does not update either Source type.

See [Local RAG Manager 日本語操作ガイド](docs/local-rag-manager-guide-ja.md).

## Portable packages

The Manager provides two distinct package types:

- Distribution package: a ZIP for read-only search clients. It contains the
  public wrappers, runtime code, all current databases, and model
  assets needed by the selected package contract. After extraction, run
  `sh ./install.sh` on macOS/Linux or `.\install.ps1` in Windows PowerShell.
- Management-PC transfer: a resumable folder for another management computer.
  It also carries all current databases, administration code, and Source
  acquisition state.

Package creation takes no global Local RAG lock. It reads files defensively
and aborts if a source file changes during the copy. The validated manifest
uses relative paths and SHA-256 checksums. Temporary files, runtime locks,
virtual environments, credentials, and `source-links.json.bak` are excluded.
The active Source metadata sidecar can contain internal URLs, so every package
must be handled as sensitive local data.

## Source Metadata and links

Optional DB-local Source metadata is stored at:

```text
<db-root>/source-links.json
```

The canonical schema is `rag-source-metadata-v1`. Each existing Source may
have a display name, a Source type, and at most one enabled or disabled Link.
The Manager derives the observed top-level stored root from current visible
catalog documents; there is no user-entered path prefix.

Source Metadata editing is single-editor in this initial release. Do not run
two Manager processes that edit the same DB at the same time. Writes use a
temporary file, revision/etag recheck, and atomic replacement, but deliberately
do not create a persistent lock file or claim strict multi-process CAS.

Resolved links are applied only after retrieval, ranking, packing, evidence
classification, and context expansion. `source_url` keeps the ordinary
browser link and `source_permalink` keeps the optional fixed link; consumers
prefer the permalink without discarding the ordinary URL. Invalid or missing
metadata fails open to the stored relative path and never changes ranking,
answerability, or search status.

## Local runtime and network

Lookup is local-only. It never downloads a model or checks a resolved document
URL. Setup and Source acquisition are the only network-capable workflows.
Machine-local proxy and CA configuration lives under
`rag/config/network.json`; installers preserve it and never distribute an
active file from the repository payload.

The persistent daemon owns the model and database handles. Short-lived public
search clients communicate with it and return one result. If setup is missing,
ordinary lookup reports `setup_required` instead of probing PATH-based Python
interpreters.

## Installed layout

```text
~/.copilot/
  instructions/
    rag.instructions.md
  skills/
    local-rag/
      SKILL.md
  rag/
    list_dbs.py
    search.py
    manage.py
    query/                  # internal runtime
    gen_db/                 # internal ingestion/index implementation
    source_manager/         # acquisition and package primitives
    config/
    dbs/
      <db-name>-rag/
    models/
    docs/
```

The lower runtime and ingestion scripts are implementation details. Copilot
ordinary lookup uses only the two root public entry points. Human changes go
through Local RAG Manager.
