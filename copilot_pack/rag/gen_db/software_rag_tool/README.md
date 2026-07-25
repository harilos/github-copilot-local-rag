# Software RAG Tool

MIT-licensed local RAG tool for Japanese-first software knowledge.

This implementation is newly written and keeps the public command-line interface small and stable.

## Usage

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python scripts/prepare.py --root /path/to/input
python scripts/index_build.py
python scripts/query.py --db project-rag "ブラジルの空調の特徴を教えて" --top-k 8 --max-chars 1200 --format prompt
```

In the Copilot pack, prefer the wrapper commands:

```bash
python ~/.copilot/rag/gen_db/build_db.py --db project-rag --root /path/to/input --source-id project
python ~/.copilot/rag/gen_db/add_data.py --db project-rag --root /path/to/more-input --source-id project-extra
python ~/.copilot/rag/gen_db/status.py --db project-rag
```

These wrappers process files in small resumable batches, persist resume state in `logs/index_state.json`, and publish current progress in `logs/progress.json`.

For legacy `.doc` and `.ppt`, install LibreOffice and make `soffice` or `libreoffice` available on `PATH`.

## Interface

The generated clean data is written as JSONL:

```text
$RAG_OUTPUT_ROOT/data/clean/
```

The vector index is written to:

```text
$CHROMA_DIR_V2
```

The lexical/source catalog is written to:

```text
$RAG_OUTPUT_ROOT/catalog.sqlite
```

The default model is `cl-nagoya/ruri-v3-130m`. For Ruri v3, the tool applies separate prefixes:

```text
EMBED_DOCUMENT_PREFIX=検索文書: 
EMBED_QUERY_PREFIX=検索クエリ: 
```

Changing the embedding model or prefixes requires rebuilding the vector index. Changing the tokenizer can be handled by rebuilding the lexical catalog from clean JSONL.

`index_build.py` deletes and recreates the target collection by default, then verifies that the Chroma collection count equals the clean JSONL record count. Use `--no-reset` only for deliberate incremental upserts.

Markdown files are treated as normal source documents. If converted Markdown and original Office/PDF files both exist in the input root, both are processed. Query output suppresses duplicate chunk hashes where possible.

`query.py` uses hybrid retrieval by default: Chroma dense search, SQLite FTS5 BM25, identifier exact match, metadata/path search, weighted RRF, duplicate suppression, and context packing. Use `--explain` to inspect retriever ranks and RRF details.
