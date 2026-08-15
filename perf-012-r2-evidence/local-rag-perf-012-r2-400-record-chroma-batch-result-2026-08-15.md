# PERF-012-R2 400-record Chroma batch 製品化結果

## 判定

**POC_GO / main 統合可**

8/8 baseline と 8/128 candidate だけを比較し、actual ONNX／actual Chroma、400 records の正式マトリクスを最終候補に対して1回実行した。core full ADD は paired median で **21.19%短縮**し、採用基準15%を通過した。Office E2Eも **15.77%短縮**し、非退行条件を満たした。正しさ、P0/P1、p95、fresh-process RSS、DB容量、manifest、False Anchorの各gateもPASSした。

旧PERF-012の1000-record feasibilityは120秒時点で未完了だったため、今回の採否根拠には用いていない。R2正本どおり400 recordsで完走・判定した。

## 固定条件

- base/main: `9514bfbe8adb93fd9a88145559f57fa8940abe51`
- tested/reviewed candidate: `3350897ba15b307ab411416e3b6022e8f6ad1dc1`
- branch: `perf/perf-012-r2-400-record-chroma-batch`
- Python: CPython 3.13.1
- model: `cl-nagoya/ruri-v3-30m` ONNX dynamic-int8, dimension 256
- model SHA-256: `BD07545FA4EB8E7765939536944DA1138A7E61441D81633ADFA1FAF218DF6BB1`
- inference batch: baseline/candidateとも8
- Chroma write batch: baseline 8、candidate 128
- fixture: DOCX 40×5 records + PPTX 20×10 records = 400 records
- fixture seed: 1102
- initial fixture manifest SHA-256: `c434d07788fb5e5f9af735348ed86fbb0e782959a62ff96849fcae4cd908d846`
- ordered record manifest SHA-256: `80662004c03795d6992ffb3cbd4ac6cdc6994d9a06ddb31645b3592417968687`
- pair order: AB / BA / AB、core 3ペア、Office E2E 3ペア
- 全sampleはfresh Python process、固有output root／Chroma root、実model warm-up後に測定
- formal elapsed: 171.877秒（45分上限内）

## 製品差分

- `.copilot/rag/gen_db/software_rag_tool/software_rag_tool/store.py`
  - 推論バッチ8を維持し、Chroma書込をprivate内部既定128へ集約
  - 新しいpublic CLI／API／config／環境変数は追加しない
  - count/dimensionをChroma書込前に検証
  - progressはChroma書込成功後だけ進める
  - final partial batchを確実にflush
- `.copilot/rag/gen_db/software_rag_tool/software_rag_tool/embeddings.py`
  - ONNX document入力を1回だけtokenize
  - attention mask、またはpad tokenを除いたinput IDsからhard token limitを検証
  - queryのbounded truncationは不変
- 製品LOC: additions 107 / deletions 14 / churn 121 / net +93
- hard stop 180 LOC未満、想定90〜170 net LOC内

## 正式性能結果

| cohort | baseline p50 | candidate p50 | paired median短縮 | baseline p95 | candidate p95 |
|---|---:|---:|---:|---:|---:|
| core full ADD | 6.4474 s | 5.0809 s | 21.19% | 6.5274 s | 5.1023 s |
| Office E2E | 11.5058 s | 9.6525 s | 15.77% | 11.5632 s | 9.7487 s |

- core vector phase paired median短縮: 22.95%
- Office E2E vector phase paired median短縮: 26.79%
- core Chroma write calls: p50 50 → 4
- core candidate write batches: 128 / 128 / 128 / 16
- Office E2E Chroma write calls: 60 → 12
- p95 gate: candidateはcore／Office E2Eともbaseline比+10%以内（実際はいずれも短縮）

## RSS・DB容量

| cohort | baseline RSS p95 | candidate RSS p95 | baseline DB p95 | candidate DB p95 |
|---|---:|---:|---:|---:|
| core | 496,353,280 B | 500,170,752 B | 3,992,671 B | 4,004,960 B |
| Office E2E | 502,136,832 B | 500,301,824 B | 3,992,889 B | 4,009,274 B |

- RSSは測定phase中のcurrent WorkingSetを5ms間隔で採取したfresh-process peak。両cohortとも+15% gate内。
- DB容量は両cohortとも+5% gate内。

## 正しさ・品質

- 12/12 formal raw samplesでactual `OnnxRuntimeEmbedder` + `CPUExecutionProvider`
- actual product `index/manifest.json` とactual Chroma collection metadataを全sampleで検証
- record count、collection、model、backend、dimension、quantization一致
- ID／document／metadataのcanonical hashが全pair一致
- 400×256 vectorの最小cosine: 0.9999999999999998（gate 0.999999以上）
- 最大絶対差: 0.0（gate 1e-6以下）
- 非有限vector: 0
- frozen retrieval結果: 全pair一致
- actual catalog exact + actual hybridによるFalse Anchor: 2 case×12 samples、候補数0・exact signal 0、false anchor合計0
- failure／KeyboardInterruptでは成功済みChroma書込分だけprogressへ反映し、retryで全recordへ収束
- manifestはvector/catalog成功後だけpublishし、失敗時はpublishしないdirect contractを確認
- empty、Unicode、token limit直上、極端長、final partialをdirect testで確認

## テスト・レビュー

- direct acceptance: 12/12 PASS
- 関連test総計: 43 PASS / 2 SKIP / 45 cases
- SKIP 2件はworktree内model path不存在による既存real-tokenizer fixture。正式測定は別の固定runtime modelをactual使用してPASS。
- independent reviewer: P0=0 / P1=0、formal開始前PASS
- formal evidence独立再計算: PASS / GO、P0=0 / P1=0
- `git diff --check`: PASS
- 全受入マトリクスは最終候補へ1回だけ実行。追加測定なし。

## 証拠

- preflight: `perf-012-r2-evidence/preflight.json`
- formal raw/aggregate: `perf-012-r2-evidence/formal.json`
- formal SHA-256: `3687DDEE50320B7F1BD40DB105F664D81A1248DA254DCD0C9520721B5842C279`
- formal driver SHA-256: `5D5379526F04420EC6DD17A4D8A60757C5C389D7D5107F51F9A26A36B871145E`
- raw samples: 12（core baseline/candidate各3、Office E2E baseline/candidate各3）

実DB、秘密文書、認証済み外部sourceは使用していない。実DB不存在はBLOCK理由にしていない。

## 統合状態

- Drive result: `https://drive.google.com/file/d/1vAt3xNkH4Q13ppI-aCXQDEcxVKKzlDuM/view?usp=drivesdk`
- remote product/evidence integration commit: `8d1f360ed10568b2693121afa54292844852dcb6`
- remote tree: `8808e86d4441765d2032314b5967d5363e34847e`（local tested result treeと完全一致）
- branch readback: `perf/perf-012-r2-400-record-chroma-batch` → `8d1f360ed10568b2693121afa54292844852dcb6`
- main readback: `main` → `8d1f360ed10568b2693121afa54292844852dcb6`
- update mode: non-force fast-forward

main統合ゲートは完了。Release／tagは別gateであり、このタスクでは実施していない。PERF-011のbranch／formal証拠／Drive結果票は変更せず保持した。
