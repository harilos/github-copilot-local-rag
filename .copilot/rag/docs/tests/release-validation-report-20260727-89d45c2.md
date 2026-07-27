# Local RAG 統合リリース検証レポート

- 検証日: 2026-07-27
- runtime検証対象commit: `89d45c2b2d7cb9617f23c7b4942d574a56f73cd5`
- branch: `main`
- Windows正式run: `89d45c2-full`
- macOS短縮run: `89d45c2-mac-smoke`
- 判定規格: エージェント設計・エージェント最終レビュー済み

## 1. 結論

| 判定対象 | 結果 |
|---|---|
| Setup completion / installer contract | **PASS** |
| Python unit / contract tests | **PASS** |
| Windows persistent daemon runtime | **PASS** |
| Windows concurrency / lifecycle / recovery | **PASS** |
| Windows resource leak gates | **PASS** |
| macOS短縮runtime smoke | **PASS** |
| Exact positive / negative | **PASS** |
| Broad search runtime contract | **PASS** |
| Broad search relevance quality | **FAIL** |
| Frozen Semantic gold v2 regression | **FAIL** |
| 新規未見Semantic v3 holdout | **NOT_RUN** |
| runtime・運用品質 | **GO** |
| stable product release | **NO-GO** |

常駐manager・単一worker・Windows multi-session実行、終了処理、回復処理、
資源上限、15秒deadline、JSON純度、Exact安全性はリリース可能な水準に達した。

一方、Broad検索は18件すべて正常に8文書を返したものの、弱い候補を過剰に
含めるケースと、1文書controlでも8文書へ水増しするケースが残った。
Semantic accuracyも凍結済みv2の事前Gateを満たしていない。したがってstable
tagおよびGitHub stable releaseは作成しない。

## 2. 試験の独立性と来歴

### Windows runtime / Broad / Exact

- 対象commit: `89d45c2`
- Windows 11
- installed Python:
  `C:\Users\harilos\.copilot\rag\query\.venv\Scripts\python.exe`
- installed RAG:
  `C:\Users\harilos\.copilot\rag`
- 3 DB:
  `ac-rag`, `incident-rag`, `rfc-full-20k-rag`
- 合計: 525 rows

### macOS短縮確認

- 対象commit: `89d45c2`
- installed RAG: `/Users/haruki/.copilot/rag`
- 合計: 20 rows

### Semantic regression

- frozen dataset:
  `semantic-gold-v2.jsonl`
- dataset SHA-256:
  `fdcf70a137d091d6b453495d7ce1d20b44d28f691177d3b8aaf9a9e27c56eafd`
- regression実行対象: `249f803`
- 実行後から`89d45c2`まで、retrieval scoring、search result packing、
  embedding modelおよびindexを変更していない。差分はdaemon telemetry、
  setup completion、installerおよび試験契約である。
- v2は既に結果を見た固定development/regression setであり、正式Gateの
  合否を後から変更するためには使用しない。

### Broad search set

- dataset:
  `broad-search-cases-v1.jsonl`
- dataset SHA-256:
  `80afa1ef87297771d50a53c5eb65b836f0fe4f9679c10f3199da878da78e33c3`
- 3 DB × 6 cases = 18 cases

## 3. フル試験中に発見・修正した問題

1. installerがsource側の`.venv`、`query/run`、実`network.json`および
   transient fileを配布対象へ含み得る問題を修正した。
2. Windows daemonが起動元のconsole/pipeを保持し、呼び出し側がEOF待ちに
   なる可能性を`CREATE_NO_WINDOW`、handle分離、専用logで修正した。
3. workerのDense準備完了が通常request pipe上の応答待ちになる問題を、
   専用一方向status pipeとmonotonic state revisionへ分離した。
4. install更新後も旧setup completion markerが残り得る問題を修正した。
   installerはcopy前にmarkerを原子的に退避し、失敗時には復元しない。
5. setup completionがrequirements fileの存在だけで成立し得る問題を修正した。
   recursive `-r`、PEP 508 specifier、installed distribution versionを
   offline検証し、`requirements=pass`をmarkerの必須条件にした。
6. `--refresh-completion-marker`を追加し、pip、download、model build、
   DB変更、network probeなしで完全検証後にmarkerを原子的に更新するようにした。

最終エージェントレビューではP0/P1指摘は残っていない。

## 4. Static / unit / setup verification

| 項目 | 結果 |
|---|---:|
| query unit / contract | **196/196 PASS** |
| docs test driver | **73/73 PASS** |
| Python compile | **PASS** |
| POSIX shell syntax | **PASS** |
| generated-file diff check | **PASS** |
| setup contracts | **29/29 PASS** |
| installer contracts | **4/4 PASS** |
| network contracts | **26/26 PASS** |
| requirements verified in actual venv | **15/15 PASS** |

### macOS actual installer

- `./install.sh`: PASS
- `setup.py --verify-only --format json`: `status=ready`
- `setup_complete=true`
- `lookup_ready=true`
- `requirements=pass`
- healthy DBs: 3
- model load: PASS
- embedding dimension: 256
- stale `.pre-update` marker: 0

### Windows actual installer

- `install.ps1`: PASS
- `setup.py --verify-only --format json`: `status=ready`
- `setup_complete=true`
- `lookup_ready=true`
- `requirements=pass`
- healthy DBs: 3
- model load: PASS
- embedding dimension: 256
- stale `.pre-update` marker: 0

DBまたはindexの再構築は不要であり、実施していない。

## 5. Windows persistent daemon正式試験

### Runtime gates

| Gate | 結果 | 実測 |
|---|---|---|
| structured request equivalence | **PASS** | JSON/argv同一表現、質問全文維持 |
| start/search/stop lifecycle | **PASS** | 20/20 |
| direct search clients | **PASS** | 100/100、全client回収 |
| cold concurrency 4 | **PASS** | 4/4、同一generation |
| warm concurrency 2/4 | **PASS** | 120/120、同一generation |
| DB release / rename / reload | **PASS** | 3/3 DB |
| client termination recovery | **PASS** | daemon継続 |
| worker exit recovery | **PASS** | worker `6844` → `1800`、probe成功 |
| worker hang recovery | **PASS** | timeout後reap、次request成功 |
| manager forced termination | **PASS** | Job Objectがworker回収、次世代成功 |
| concurrency-4 soak | **PASS** | 200/200、generation安定 |
| overload 8 safety | **PASS** | 4成功、4 bounded、後続健康 |
| Exact positive / negative | **PASS** | 30/30 |
| fallback | **PASS** | 0 |
| JSON parse error | **PASS** | 0 |
| hard deadline | **PASS** | 最大14.776190秒 |
| compact JSON hard size | **PASS** | 最大12,027 bytes |

overload 8はadvertised concurrencyではなく安全試験である。4件のbounded errorは
`daemon_overloaded`またはdeadline制御として許容され、後続requestは成功した。

raw summaryの`failure_count=5`は、意図的なworker hangの1行とoverload時の
bounded error 4行である。run全体の`overall=FAIL`はこれらの安全試験ではなく、
独立したBroad quality Gateの未達による。`identity_mismatches=1`も意図的に
停止したworker requestのtimeout行であり、通常requestのresponse混線ではない。
recovery Gateと後続healthy probeはいずれもPASSした。

### Resource gates

| Resource | warm baseline | final | limit / 判定 |
|---|---:|---:|---|
| manager handles | 166 | 167 | ≤182, **PASS** |
| worker handles | 340 | 339 | ≤372, **PASS** |
| manager threads | 6 | 8 | ≤10, **PASS** |
| worker threads | 129 | 126 | ≤133, **PASS** |
| manager RSS | 31,838,208 | 36,466,688 | monotonic=false, material=false |
| worker RSS | 743,780,352 | 744,579,072 | monotonic=false, material=false |

unexpected model load、worker重複、orphan process、fallback競合は発生していない。

## 6. macOS短縮runtime smoke

| 項目 | 結果 |
|---|---:|
| cold requests | 2/2 PASS |
| warm requests | 16/16 PASS |
| worker recovery | 2/2 PASS |
| 合計 | **20/20 PASS** |
| fallback | 0 |
| JSON parse error | 0 |
| identity mismatch | 0 |
| Dense ready telemetry | ready / 0.001011秒 |
| 最大latency | 4.850324秒 |
| 最大stdout | 11,588 bytes |
| shutdown / process reap | PASS |

共通runtime変更の最終確認として十分であり、MacでWindowsと同じ長時間試験は
重複実施していない。

## 7. Broad search 18-case評価

### Runtime contract

| 項目 | 結果 |
|---|---:|
| completed | 18/18 |
| runtime success | 18/18 |
| distinct documents | 全case 8 |
| duplicate paths | 0 |
| aspect coverage ≥60% | 18/18 |
| JSON hard limit | 18/18 PASS |

### Relevance quality

| 指標 | 実測 | 判定 |
|---|---:|---|
| full quality pass | 2/18 | **FAIL** |
| median DistinctDoc@8 | 8 | PASS |
| median UsefulDoc@8 | 3 | **FAIL** |
| mean UsefulDoc@8 | 3.389 | **FAIL** |
| useful documents total | 61/144 | **FAIL** |
| grade-0 noise total | 83/144 | **FAIL** |
| support calibration | 5/18 | **FAIL** |
| identifier contract | 13/18 | **FAIL** |

良い例として、AC generalはuseful 7/noise 1、RFC generalはuseful 8/noise 0
だった。一方、identifier/definition系ではExact evidenceを得た時点で
`dense_skipped_reason=verified_identifier_exact`または
`verified_rfc_exact`となるケースがあり、「Evidenceは早く確定してもDiscovery
laneはDenseを継続する」という製品契約を満たしていない。

3つのone-document controlはすべて次の結果だった。

| Case | returned | useful | noise |
|---|---:|---:|---:|
| AC control | 8 | 1 | 7 |
| Incident control | 8 | 1 | 7 |
| RFC control | 8 | 1 | 7 |

有用文書が1件しかない場合には1件を返し
`insufficient_distinct_related_documents`を出すべきであり、件数を満たすための
grade-0文書追加は許容できない。このためBroad runtimeはPASSだが品質はFAIL。

## 8. Frozen Semantic gold v2 regression

### Runtime

- searches: 90/90成功
- timeout: 0
- JSON purity: 90/90
- fallback: 0
- 最大latency: 0.831秒

### Current regression metrics

| DB | Profile | Hit@5 | Authoritative Evidence Recall |
|---|---|---:|---:|
| ac-rag | H | 50% | 46.67% |
| ac-rag | L | 20% | 8.33% |
| ac-rag | V | 40% | 40% |
| incident-rag | H | 10% | 10% |
| incident-rag | L | 10% | 10% |
| incident-rag | V | 0% | 0% |
| rfc-full-20k-rag | H | 30% | 30% |
| rfc-full-20k-rag | L | 10% | 10% |
| rfc-full-20k-rag | V | 0% | 0% |
| **Overall** | **H** | **30%** | **28.89%** |
| **Overall** | **L** | **13.33%** | **9.44%** |
| **Overall** | **V** | **13.33%** | **13.33%** |

- comparable H/L cases: 30
- Vector Rescue: 5/26 = 19.23%
- Vector Harm: 0/4 = 0%
- 事前Gate: overall 80%、各DB 70%
- 判定: **FAIL**

DenseはLexical正解を壊さず一部を救済しているため、直ちに削除する根拠はない。
ただし現在の絶対精度ではstable releaseを許可できない。

既存dashboardのseparated gateにある`Vector有効性=NOT_RUN`は汎用report判定行の
制約による表示である。本節はraw Semantic gold fieldsから独立集計した正式な
regression評価である。

## 9. Release decision

### GO

- installer / setup completion contract
- normal local runtime
- Windows persistent manager / single worker architecture
- four concurrent clients
- lifecycle / crash / hang recovery
- DB release
- deadline / JSON / fallback contract
- Exact safety
- macOS common-runtime smoke

### NO-GO

- Broad search relevance and support-level calibration
- one-document control
- Exact fast path後のDense Discovery継続
- frozen Semantic absolute accuracy
- unseen Semantic v3 holdout

### 次の最小修正範囲

1. Evidence laneのExact fast pathとDiscovery lane継続を完全に分離する。
2. document-level grade/calibrationを見直し、無信号・grade-0候補で件数を
   水増ししない。
3. 有用候補が少ない場合は件数目標を下げ、
   `insufficient_distinct_related_documents`を返す。
4. v2をdevelopment/regression setとして原因分析に使う。
5. 修正後、未見のSemantic v3をfreezeし、事前Gateを一度だけ正式評価する。

Embedding model変更、DB/index再構築、reranker導入はこの結果だけではまだ必要と
確定していない。

## 10. Artifacts

- Windows report:
  `persistent-daemon-full-report-20260727-89d45c2-windows.md`
- Windows results:
  `data/persistent-daemon-results-20260727-89d45c2-windows.jsonl`
- Windows events:
  `data/persistent-daemon-events-20260727-89d45c2-windows.jsonl`
- Windows resources:
  `data/persistent-daemon-resources-20260727-89d45c2-windows.jsonl`
- Windows summary:
  `data/persistent-daemon-summary-20260727-89d45c2-windows.json`
- macOS report:
  `persistent-daemon-full-report-20260727-89d45c2-macos.md`
- macOS results:
  `data/persistent-daemon-results-20260727-89d45c2-macos.jsonl`
- macOS events:
  `data/persistent-daemon-events-20260727-89d45c2-macos.jsonl`
- macOS summary:
  `data/persistent-daemon-summary-20260727-89d45c2-macos.json`
- Semantic report:
  `performance-report-20260727-semantic-v2-regression.md`
- Semantic raw:
  `data/performance-results-20260727-semantic-v2-regression.jsonl`
