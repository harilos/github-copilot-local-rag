# Query Runtime

This is the folder Copilot should call for searches.

Install dependencies and prepare the default ONNX INT8 embedding model:

```bash
python setup.py
```

Copilot should run this when the user asks `RAGの初期設定をして` or `RAGをセットアップして`.
If `.venv/.rag-deps-installed` is missing, `search.py` and `build_db.py` return `setup_required` instead of running setup automatically. Copilot-facing wording is handled by the instruction file, not by these CLI scripts.

To skip model preparation and only install Python dependencies:

```bash
python setup.py --no-prepare-model
```

Proxy/certificate environments:

```bash
python setup.py --proxy http://proxy.example:8080
```

If pip or model download fails with SSL/certificate errors, set the company CA certificate path before retrying:

```bash
export REQUESTS_CA_BUNDLE=/path/to/company-ca.pem
export SSL_CERT_FILE=/path/to/company-ca.pem
export PIP_CERT=/path/to/company-ca.pem
```

On Windows PowerShell:

```powershell
$env:REQUESTS_CA_BUNDLE="C:\path\company-ca.pem"
$env:SSL_CERT_FILE="C:\path\company-ca.pem"
$env:PIP_CERT="C:\path\company-ca.pem"
py -3 setup.py --proxy http://proxy.example:8080
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

When the user explicitly asks for RAG but does not name a DB, list DBs and choose one from the hints:

```bash
python list_dbs.py
python search.py --db project-rag "過去の運用手順から調べて"
```

`--auto` remains available for compatibility, but Copilot-facing instructions should prefer explicit DB selection after `list_dbs.py`.

Do not run RAG for ordinary general questions. A topic merely matching a DB hint is not enough.
If a non-general proper noun or local-looking identifier appears, Copilot should run `list_dbs.py` to check whether a clearly relevant DB exists. Use RAG only when the DB name or hint clearly matches; otherwise answer normally.

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

Evaluation-only retrieval modes:

```bash
python search.py --db project-rag --retrieval-mode lexical "質問"
python search.py --db project-rag --retrieval-mode dense "質問"
python search.py --db project-rag --retrieval-mode hybrid "質問"
```

The default remains `hybrid`.

List DBs:

```bash
python list_dbs.py
```

If local Python is unavailable, use `proxy_client.py` to call a RAG service running on another machine.
