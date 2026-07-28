# Copilot Local RAG Pack

This directory is installed under `~/.copilot/rag`. Use the repository
installers; an overlay install preserves machine-local runtime state and
removes only explicitly retired product files.

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
  ~/.copilot/rag/list_dbs.py --format json

~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/search.py \
  --db <db-name>-rag \
  --compact-json \
  --result-delivery file \
  --format json \
  "<question>"
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\list_dbs.py" `
  --format json

& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\search.py" `
  --db <db-name>-rag `
  --compact-json `
  --result-delivery file `
  --format json `
  "<question>"
```

The database list returns a bounded public content summary and Source display
metadata. It does not disclose raw Source IDs. Search delegates to the
installed retrieval runtime once, then adds optional public `uri` values and
database freshness metadata. A stale database may include one Japanese
conversation notice under `database_freshness.chat_notice`.

File delivery returns a small JSON pointer. The referenced summary is
self-contained. Cached follow-up detail is read through the same public
`search.py` entry point by passing `--result-set-id`; it does not rerun
retrieval.

## Human Manager

Use the interactive Manager for every change:

```bash
~/.copilot/rag/query/.venv/bin/python ~/.copilot/rag/manage.py
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\manage.py"
```

Top-level menu:

1. Create a new database.
2. Select and manage a database.
3. Update or resume every Source in every database.
4. Create or import a distribution/management-PC package.
5. Verify this computer's setup.
0. Exit.

Selected-database menu:

1. View or update Sources.
2. Add a new Source.
3. Update or resume every Source in this database.
4. Edit the database title and query hint.
5. Diagnose and repair problems.
6. Delete this database with explicit confirmation.
0. Return.

A Source is the unit of acquisition and URL generation. The Source Manager
supports GitHub, SVN, Redmine, SharePoint synchronized folders, and a local
one-time import. It stores provider configuration and resumable checkpoints
outside indexed document identity. A Source becomes searchable only after
successful ingestion.

SharePoint Source acquisition and update are intentionally Windows-only. They
read a synchronized root selected by environment configuration directly and
do not copy the synchronized tree into the DB. A packaged database can be
copied to macOS and searched there; macOS does not update that SharePoint
Source.

See [Local RAG Manager 日本語操作ガイド](docs/local-rag-manager-guide-ja.md).

## Portable packages

The Manager provides two distinct package types:

- Distribution package: a ZIP for read-only search clients. It contains the
  public wrappers, runtime code, selected searchable databases, and model
  assets needed by the selected package contract.
- Management-PC transfer: a resumable folder for another management computer.
  It also carries administration code and Source acquisition state.

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
classification, and context expansion. Public output contains only `uri`:
the permalink when available, otherwise the ordinary source URL. Invalid,
missing, or ambiguous metadata fails open to the stored relative path and
never changes ranking, answerability, or search status.

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
