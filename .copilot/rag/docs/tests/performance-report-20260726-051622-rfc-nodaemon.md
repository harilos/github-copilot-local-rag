# RAG性能評価レポート 20260726-051622-rfc-nodaemon

## 実行条件

- OS: macOS-26.4.1-arm64-arm-64bit-Mach-O
- Python: 3.13.14
- profiles: H, L, V
- daemon repeats: 0
- no-daemon repeats: 1
- budget_tokens: 1200
- max_chars: 1200

## list_dbs

- repeats: 0
- dbs: 
- JSON errors: 0
- p50:  sec
- p95:  sec

## latency by DB / profile / execution

|DB|profile|execution|N|errors|p50 sec|p95 sec|max sec|
|--|--|--|--:|--:|--:|--:|--:|
|rfc-full-20k-rag|H|no-daemon|3|0|4.313|4.634|4.634|
|rfc-full-20k-rag|L|no-daemon|3|0|0.852|0.970|0.970|
|rfc-full-20k-rag|V|no-daemon|3|0|4.297|4.481|4.481|

## quality gates observed

|Gate|N|Pass|Fail|
|--|--:|--:|--:|
|JSON stdout parse|9|9|0|
|stdout JSON purity|9|9|0|
|Exact negative collision|0|0|0|
|Expected unmatched identifier|0|0|0|
|Exact positive candidate|3|3|0|

## slowest searches

|case|db|profile|execution|latency sec|status|top1|
|--|--|--|--|--:|--|--|
|RFC_BROAD_001|rfc-full-20k-rag|H|no-daemon|4.634|ok|rfc10003.txt|
|RFC_BROAD_001|rfc-full-20k-rag|V|no-daemon|4.481|ok|rfc9743.txt|
|RFC_EXACT_001|rfc-full-20k-rag|H|no-daemon|4.313|ok|rfc10026.txt|
|RFC_SEM_001|rfc-full-20k-rag|V|no-daemon|4.297|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|V|no-daemon|4.169|ok|rfc9926.txt|
|RFC_SEM_001|rfc-full-20k-rag|H|no-daemon|4.159|ok|rfc10026.txt|
|RFC_EXACT_001|rfc-full-20k-rag|L|no-daemon|0.970|ok|rfc10026.txt|
|RFC_BROAD_001|rfc-full-20k-rag|L|no-daemon|0.852|ok|rfc10003.txt|
|RFC_SEM_001|rfc-full-20k-rag|L|no-daemon|0.836|ok|rfc10026.txt|

