# Copilot Local RAG Pack

This pack is intended to be copied under `$HOME/.copilot` without overwriting your existing `copilot-instructions.md`.

## Install On macOS / Linux

From this repository:

```bash
./install.sh
```

This creates `~/.copilot` if it does not exist.

## Install On Windows

PowerShell:

```powershell
.\install.ps1
```

This creates `$HOME\.copilot` if it does not exist.
Use the installers rather than manually copying `.copilot`; they preserve the
machine-local `rag/config/network.json`. The supported install location for
Copilot-facing skills and commands is `$HOME/.copilot`.

Then add one short line to your existing `~/.copilot/copilot-instructions.md` if you want Copilot to know the RAG entrypoint:

```text
For requests to use RAG, local documents, internal or company information, or information installed in or provided to Copilot, read ~/.copilot/instructions/rag.instructions.md.
```

This pack does not include `copilot-instructions.md` and will not overwrite your existing top-level Copilot instructions.

## Export For Migration

Create a private migration archive from an installed Local RAG environment:

```bash
~/.copilot/rag/export_migration.sh
```

Specify an output path when needed:

```bash
~/.copilot/rag/export_migration.sh \
  --output "$HOME/local-rag-migration.tar.gz"
```

The archive includes the RAG databases, indexes, local ONNX model, RAG code,
the RAG instruction, and the two RAG skills. New files under `rag` are
included automatically unless they match the transient/private blacklist.
Outside `rag`, only these exact integration files are included:

```text
instructions/rag.instructions.md
skills/local-rag/SKILL.md
skills/local-rag-admin/SKILL.md
```

The platform-specific `query/.venv`, daemon state, caches, and SQLite
transient files are excluded. Machine-local `config/network.json` is included
by default when present. Export fails without displaying the URL if that file
contains a proxy username, password, token, secret, or other persisted
credential. Use `--exclude-network-config` when the destination should receive
new proxy/CA settings instead. The script refuses to export while the daemon
or a DB maintenance operation is active, or while an uncheckpointed SQLite
WAL or journal exists.

Verify an archive after copying it:

```bash
~/.copilot/rag/export_migration.sh \
  --verify "$HOME/local-rag-migration.tar.gz"
```

The archive is created with mode `0600`, includes a manifest and per-file
SHA-256 checksums, and may contain company documents. It is never uploaded
automatically. On the destination, extract it privately, copy the bundled
`.copilot` contents without deleting unrelated Copilot files, and run Local
RAG setup to recreate the virtual environment.

## Layout

```text
~/.copilot/
  instructions/
    rag.instructions.md
  rag/
    query/
      requirements.txt
      setup.py
      prepare_onnx_model.py
      search.py
      ragd.py
      list_dbs.py
      proxy_client.py
    models/
      ruri-v3-30m-onnx-int8/
    gen_db/
      create_db.py
      build_db.py
      add_data.py
      software_rag_tool/
    dbs/
      <name>-rag/
        VERSION.json
        db.json
        DB_PROFILE.md
        source-links.json        # optional, human-managed
        catalog.sqlite
        data/
        index/
        logs/
```

## Human Manager

Run the interactive manager when a person wants to inspect or maintain Local
RAG without assembling command-line arguments:

- [Local RAG Manager 日本語操作ガイド](docs/local-rag-manager-guide-ja.md)
- [Local RAG Manager 設定項目レビュー](docs/local-rag-manager-settings-review-ja.md)

macOS/Linux:

```bash
~/.copilot/rag/query/.venv/bin/python ~/.copilot/rag/manage.py
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\manage.py"
```

Git Bash:

```bash
"$HOME/.copilot/rag/query/.venv/Scripts/python.exe" \
  "$HOME/.copilot/rag/manage.py"
```

The top-level menu contains setup/verification, database selection, database
creation, the Japanese help guide, and exit. After a database is selected, the manager exposes search,
the read-only Source inventory, build/resume, add/update, detailed status,
search-index repair, and guarded database deletion. The manager delegates to
the existing scripts with argument arrays; it does not implement a second
retriever or update detector.

A Source appears only after indexed catalog documents contain its
`source_id`. The Source screens cannot create, delete, or rename Sources.
They may attach one optional Source Link to an existing Source.

Source-Link configuration is stored with its database:

```text
<db-root>/source-links.json
<db-root>/source-links.json.bak
```

Each existing `source_id` has at most one enabled or disabled Provider
configuration. The manager derives one observed top-level stored root from the
Source's current visible catalog documents. URL generation removes that root
component exactly once, then passes the remaining Source-relative path to the
Provider. There is no user-entered Source-Link path prefix, longest-prefix
selection, or mixed Provider configuration inside one Source.
Search resolves links only after retrieval, ranking, packing, and evidence
classification. It adds `source_url` or `source_permalink` without changing
document IDs, chunk IDs, scores, ordering, authority, answerability, or search
status. Missing or invalid settings fail open to the stored path. No database
or index rebuild is required.

An unreleased `rag-source-links-v1` file with exactly one mapping is read only
when its legacy path prefix matches the one observed stored root. A Source
with no mapping remains unconfigured. Multiple mappings, a root mismatch, no
observed root, or multiple observed roots fail open to path-only results. The
manager performs an explicit save to publish the single-configuration
`rag-source-links-v2` schema while retaining the prior primary as a backup.

Per-file Source Links require exactly one observed stored root. If one Source
contains documents from several top-level roots, add those provider roots
again under separate stable Source IDs; the manager does not split or reindex
the database automatically.

GitHub repository URLs and refs are entered manually; the manager does not
inspect `.git` or run Git. SharePoint has one normal input: the document-library
or folder root used for direct file links. Its strategy is fixed to
`append-relative-path`; Microsoft Graph and a separate home URL are not used.
Older home-only settings remain readable for compatibility but are not offered
or newly saved by the manager.

Copying the complete database directory preserves the active sidecar and its
local rollback backup. The migration exporter validates and includes only the
active v2 sidecar; it excludes `source-links.json.bak` because that rollback
file may contain an older internal URL. Treat either form of transfer as
sensitive. Real sidecars are not part of public source archives or tracked
fixtures.

## Search

Search uses hybrid retrieval internally: Chroma dense search, SQLite FTS5 BM25, identifier exact match, metadata/path search, weighted RRF, duplicate suppression, and context packing. Copilot only passes the full question once. Distinct primary excerpts are reserved first; optional same-section, table-header, code, or configuration context uses only the remaining budget. Context does not inherit retrieval signals. A table row without available headers adds `table_headers_incomplete`.

`search.py` auto-starts `ragd` when dense search is needed. The daemon keeps the ONNX Runtime session, Sudachi, and Chroma client warm, then exits after 3 idle hours. When the daemon is cold and SQLite has a strong exact/path/BM25 hit, `search.py` can return a lexical fast-path result without loading the embedding model.

Explicit DB:

```bash
python ~/.copilot/rag/query/search.py --db project-rag "このAPIの設計意図は？"
```

Natural-language trigger:

If the user does not name a DB, list DBs first and choose the most relevant one from the hints:

```bash
python ~/.copilot/rag/query/list_dbs.py
python ~/.copilot/rag/query/search.py --db project-rag "過去の設計書からこの仕様を調べて"
```

If there are multiple DBs and no DB name is explicit, the tool returns candidates instead of guessing.

The tool also runs when the question itself contains a DB name:

```bash
python ~/.copilot/rag/query/search.py "project-ragでこの障害の復旧手順を調べて"
```

For questions containing quotes, shell metacharacters, or multiline code, use stdin:

```bash
printf '%s\n' "質問文" | python ~/.copilot/rag/query/search.py --db project-rag --stdin
```

Developer diagnostics:

```bash
python ~/.copilot/rag/query/search.py --db project-rag --explain --format json "A2Wに関する情報を教えて"
```

Evaluation-only retrieval modes:

```bash
python ~/.copilot/rag/query/search.py --db project-rag --retrieval-mode lexical "質問"
python ~/.copilot/rag/query/search.py --db project-rag --retrieval-mode dense "質問"
python ~/.copilot/rag/query/search.py --db project-rag --retrieval-mode hybrid "質問"
```

Omitting `--retrieval-mode` keeps the normal hybrid behavior.

### Two-stage result delivery

Copilot-facing lookup uses a small temporary result pointer so the initial
answer does not have to pass a large multilingual payload through a shell:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/query/search.py \
  --db "<project-rag>" \
  --compact-json \
  --result-delivery file \
  "<complete-user-question>"
```

The command writes a self-contained `summary.json` and cached detail items
under the operating system temporary directory. The pointer printed to stdout
contains only a result-set UUID, the summary path, expiry, and byte count. The
summary contains a deterministic extractive answer draft, concise evidence,
limitations, broad document cards, and default follow-up item IDs. Initial
answers read only this summary.

A follow-up that asks for more detail reads the same cached result without
running retrieval again:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/query/result_detail.py \
  --result-set-id "<result-set-uuid>" \
  --item-id "<item-id>" \
  --detail-level expanded \
  --result-delivery file
```

Temporary result sets expire after 60 minutes by default, extend on access up
to a four-hour hard lifetime, and are isolated by UUID. They are not stored in
the repository, `.copilot`, a database, an export, or a release archive.
`--result-delivery stdout` preserves the existing direct JSON interface.

Before distributing changes, tracked text can be checked for user-profile
paths and optional local sensitive terms:

```bash
RAG_SENSITIVE_TERMS_FILE="<path-to-untracked-denylist>" \
  python ~/.copilot/rag/query/source_hygiene.py
```

The denylist remains untracked and is excluded by both installers. Findings
report only the file and line; the configured literal is never printed. The
migration exporter also excludes the local denylist.

## Create Or Update A DB

Choose a stable Source ID that describes the ingestion origin. Examples are
`sharepoint-docs`, `redmine-issues`, `github-repository`, and
`filesystem-docs`. Reuse the same Source ID when updating the same source.

```bash
python ~/.copilot/rag/gen_db/create_db.py --db project-rag --title "Project Knowledge"
python ~/.copilot/rag/gen_db/status.py --db project-rag
python ~/.copilot/rag/gen_db/build_db.py --db project-rag --root /path/to/docs --source-id project --resume
python ~/.copilot/rag/gen_db/add_data.py --db project-rag --root /path/to/more-docs --source-id project-extra
python ~/.copilot/rag/gen_db/rebuild_component.py --db project-rag --component lexical
```

To scan only part of a stable logical root:

```bash
python ~/.copilot/rag/gen_db/build_db.py \
  --db project-rag \
  --root "/data/Project Knowledge" \
  --source-id project \
  --scan-subdir "plans/FY26" \
  --resume
```

Stored paths always include the logical root directory name, such as
`Project Knowledge/plans/FY26/report.pdf`, and always use `/`. The
`--include-root-name-in-path` option remains accepted for compatibility but
cannot disable this rule. `add_data.py` reconciles deletions only inside the
selected scan scope, so a later `plans/FY27` add does not remove FY26 data.

Changing to root-prefixed paths changes path-derived document IDs. Rebuild an
existing database once to adopt this behavior; there is no old-ID migration.

Each DB gets `VERSION.json` at creation time. It contains `created_at`, `db_hash`, the Chroma collection name, and the tool hash used to create the DB layout.

`build_db.py` and `add_data.py` process files in small resumable batches. Use `status.py` before starting another long run. Use `build_db.py --force-rebuild` only when you intentionally want to discard prior clean records and recreate the Chroma collection.

Build, add, resume, and index-repair operations place only their target
database in maintenance. Searches for that database return immediately with
`status: "busy"` and `error: "db_maintenance_in_progress"`; they are not
queued, retried, or routed through no-daemon fallback. Other databases remain
searchable after the short worker-handle release/restart interval. The
maintenance lease is persisted per database, prevents a second writer, and is
not an immutable-generation or blue-green database mechanism.

Evaluation-only chunk variants can be built without changing defaults:

```bash
python ~/.copilot/rag/gen_db/build_db.py --db project-c1000-rag --root /path/to/docs --source-id project --force-rebuild --chunk-max-chars 1000 --chunk-overlap 120
```

Omitting these options keeps the normal `1400` character chunk size and `160` character overlap.

`catalog.sqlite` uses compact schema v2: document metadata is stored once per document, file lookup is document-level, and identifiers are stored as a term dictionary plus term-chunk postings. `embedding_text` is not stored in SQLite; vector rebuilds read it from clean JSONL.

`rebuild_component.py --component lexical` rebuilds `catalog.sqlite` from existing clean JSONL without recomputing embeddings. There is no in-place migration for older catalog schemas; rebuild the catalog or rebuild the DB.

Markdown files are treated as normal input. If `manual.md` and `manual.docx` both exist under the input root, both are processed; search output suppresses duplicate chunks by content hash where possible.

The default embedding model is `cl-nagoya/ruri-v3-30m` exported locally to ONNX Runtime INT8 with:

```text
検索文書: <document chunk>
検索クエリ: <question>
```

## Python Setup

The query virtual environment belongs under:

```text
~/.copilot/rag/query/.venv
```

Install it with:

```bash
python ~/.copilot/rag/query/setup.py --format json
```

This creates the query virtual environment, prepares the default Ruri-v3-30m
ONNX INT8 model, runs a real 256-dimensional embedding, validates
`list_dbs.py` JSON, and performs read-only health checks for installed
databases.

Verify without installing, downloading, or modifying the runtime or databases:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/query/setup.py \
  --verify-only \
  --format json
```

When using Copilot, `RAGの初期設定をして` means running the same setup command. If the query runtime has not been initialized, search and DB build commands return `setup_required` instead of starting setup automatically. Copilot-facing wording is handled by the instruction file, not by these CLI scripts.

To skip model preparation when a valid local model is already installed:

```bash
python ~/.copilot/rag/query/setup.py --no-prepare-model
```

Post-install verification still runs. A missing or invalid model therefore
returns `setup_complete: false`; dependencies alone are not a completed
setup.

During an upgrade, the installers detect the legacy `ok` completion marker
and migrate it only after the same offline deep verification passes. This
upgrade path does not install packages, download a model, or modify a
database.

Temporary corporate proxy/certificate configuration:

```bash
python ~/.copilot/rag/query/setup.py \
  --proxy http://proxy.example:8080 \
  --ca-bundle /path/to/company-ca.pem \
  --format json
```

Persistent configuration may be stored in
`~/.copilot/rag/config/network.json`; use `config/network.example.json` as
the template. The real file is never installed from the payload or included
in Git/release ZIP files. `mode=auto` selects the route before the first real
network operation. Use `mode=required` only when direct external access is
prohibited. Do not disable certificate verification.

If Python cannot be installed locally, use `rag/query/proxy_client.py` to call a proxy RAG service on another machine.
