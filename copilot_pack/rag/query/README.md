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

Or include the DB name in the question:

```bash
python search.py "project-ragでこのAPIの設計意図を調べて"
```

Allow natural-language RAG trigger:

```bash
python search.py --auto "過去の運用手順から調べて"
```

List DBs:

```bash
python list_dbs.py
```

If local Python is unavailable, use `proxy_client.py` to call a RAG service running on another machine.
