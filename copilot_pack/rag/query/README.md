# Query Runtime

This is the folder Copilot should call for searches.

Install dependencies into this folder:

```bash
python setup.py
```

Search with an explicit DB:

```bash
python search.py --db project-rag "このAPIの設計意図は？"
```

The search command hides retrieval strategy. It runs dense search, BM25, exact identifier lookup, metadata/path lookup, RRF fusion, deduplication, and context packing internally.

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
```

List DBs:

```bash
python list_dbs.py
```

If local Python is unavailable, use `proxy_client.py` to call a RAG service running on another machine.
