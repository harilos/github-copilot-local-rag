# RAG性能評価レポート 20260726-051622-list

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: H, L, V
- daemon repeats: 0
- no-daemon repeats: 0
- budget_tokens: 1200
- max_chars: 1200

## list_dbs

- repeats: 10
- dbs: ac-rag, incident-rag, rfc-full-20k-rag
- JSON errors: 0
- p50: 0.029 sec
- p95: 0.031 sec

## latency by DB / profile / execution

|DB|profile|execution|N|errors|p50 sec|p95 sec|max sec|
|--|--|--|--:|--:|--:|--:|--:|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|0|0|0|
|stdout JSON purity|0|0|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|0|0|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|

