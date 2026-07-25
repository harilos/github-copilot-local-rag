# Copilot Local RAG Pack

This pack is intended to be copied under `$HOME/.copilot` without overwriting your existing `copilot-instructions.md`.

## Install On macOS / Linux

From this repository:

```bash
./install.sh
```

This creates `~/.copilot` if it does not exist.

Manual copy:

```bash
mkdir -p ~/.copilot
cp -R .copilot/. ~/.copilot/
```

## Install On Windows

PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.copilot" | Out-Null
Copy-Item -Recurse -Force ".\.copilot\*" "$HOME\.copilot\"
```

This creates `$HOME\.copilot` if it does not exist.

Then add one short line to your existing `~/.copilot/copilot-instructions.md` if you want Copilot to know the RAG entrypoint:

```text
RAGが必要な場合は ~/.copilot/instructions/rag.instructions.md を参照してください。
```

This pack does not include `copilot-instructions.md` and will not overwrite your existing top-level Copilot instructions.

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
        catalog.sqlite
        data/
        index/
        logs/
```

## Search

Search uses hybrid retrieval internally: Chroma dense search, SQLite FTS5 BM25, identifier exact match, metadata/path search, weighted RRF, duplicate suppression, and context packing. Copilot only passes the full question once.

`search.py` auto-starts `ragd` when dense search is needed. The daemon keeps the ONNX Runtime session, Sudachi, and Chroma client warm, then exits after 3 idle hours. When the daemon is cold and SQLite has a strong exact/path/BM25 hit, `search.py` can return a lexical fast-path result without loading the embedding model.

Explicit DB:

```bash
python ~/.copilot/rag/query/search.py --db project-rag "このAPIの設計意図は？"
```

Natural-language trigger:

```bash
python ~/.copilot/rag/query/search.py --auto "過去の設計書からこの仕様を調べて"
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

## Create Or Update A DB

```bash
python ~/.copilot/rag/gen_db/create_db.py --db project-rag --title "Project Knowledge"
python ~/.copilot/rag/gen_db/status.py --db project-rag
python ~/.copilot/rag/gen_db/build_db.py --db project-rag --root /path/to/docs --source-id project --resume
python ~/.copilot/rag/gen_db/add_data.py --db project-rag --root /path/to/more-docs --source-id project-extra
python ~/.copilot/rag/gen_db/rebuild_component.py --db project-rag --component lexical
```

Each DB gets `VERSION.json` at creation time. It contains `created_at`, `db_hash`, the Chroma collection name, and the tool hash used to create the DB layout.

`build_db.py` and `add_data.py` process files in small resumable batches. Use `status.py` before starting another long run. Use `build_db.py --force-rebuild` only when you intentionally want to discard prior clean records and recreate the Chroma collection.

`rebuild_component.py --component lexical` rebuilds `catalog.sqlite` from existing clean JSONL without recomputing embeddings. Use it when upgrading an older DB to hybrid search.

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
python ~/.copilot/rag/query/setup.py
```

This creates the query virtual environment and prepares the default Ruri-v3-30m ONNX INT8 model.

To install dependencies only:

```bash
python ~/.copilot/rag/query/setup.py --no-prepare-model
```

If Python cannot be installed locally, use `rag/query/proxy_client.py` to call a proxy RAG service on another machine.
