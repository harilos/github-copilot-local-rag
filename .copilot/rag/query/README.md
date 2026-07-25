# Query Runtime

This is the folder Copilot should call for searches.

Install dependencies and prepare the default ONNX INT8 embedding model:

```bash
python setup.py
```

To skip model preparation and only install Python dependencies:

```bash
python setup.py --no-prepare-model
```

Search with an explicit DB:

```bash
python search.py --db project-rag "このAPIの設計意図は？"
```

The search command hides retrieval strategy. It runs dense search, BM25, exact identifier lookup, metadata/path lookup, RRF fusion, deduplication, and context packing internally.

Dense search uses `cl-nagoya/ruri-v3-30m` via ONNX Runtime INT8. `search.py` auto-starts `ragd` when needed and the daemon exits after 3 idle hours.

Or include the DB name in the question:

```bash
python search.py "project-ragでこのAPIの設計意図を調べて"
```

Allow natural-language RAG trigger:

```bash
python search.py --auto "過去の運用手順から調べて"
```

Use stdin for multiline questions or code:

```bash
python search.py --db project-rag --stdin
```

Useful options:

```bash
python search.py --db project-rag --budget-tokens 3000 --timeout 60 "質問"
python search.py --db project-rag --explain --format json "A2W"
python search.py --db project-rag --no-daemon "同期実行したい質問"
```

List DBs:

```bash
python list_dbs.py
```

If local Python is unavailable, use `proxy_client.py` to call a RAG service running on another machine.
