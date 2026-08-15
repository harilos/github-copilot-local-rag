# LRR-PERF-011-R2 catalog書込増幅削減 製品化結果

## 判定

`GO（2026-08-15補正gate）`

元指示のgateでは、catalog区間がpaired medianで21.68%短縮して20% gateを通過した一方、400 records core full ADDは1.63%短縮で10% gate未達、10%更新は2.21%短縮で15% gate未達だったため`NO_GO`だった。この元判定と正式測定値は変更せず保存する。

ユーザー補正により、catalog 20%以上短縮を効果の主gateとし、core full ADD／Office E2E／10%更新は短縮必須ではなく非退行gateとする。既存値はそれぞれ1.63%／0.36%／2.21%短縮で、すべて非退行である。正しさ、独立review P0/P1、p95、RSS、DB容量のgateも全PASSしているため、追加測定なしで補正gateの`GO`と再判定する。

## Gitと実施範囲

- repository: `harilos/github-copilot-local-rag`
- branch: `perf/perf-011-r2-catalog-write-productization`
- start／baseline main: `80515b71ba4e7a166883f5641c20144e24b509c7`
- product commit: `31f7447feebb4720c99f640fcb5287f2fbafc01f`
- tested SHA: `184a643a325499d3a701187874ebeb7076cd2117`
- reviewed SHA: `184a643a325499d3a701187874ebeb7076cd2117`
- merged SHA: `PENDING (Drive結果票の新規保存確認後にmain統合)`
- main push: `PENDING (Drive結果票の新規保存確認後)`
- Release／tag／配布物公開: `NOT_RUN`
- 実DB／旧20k DB／秘密文書: `NOT_USED`

変更した製品fileは`catalog.py`と`incremental.py`だけ。`catalog.py`は+190/-8、`incremental.py`は+1/-2で、製品差分はgross 201 LOC、net +181 LOC。指示の160〜270 LOCおよびhard上限300 LOC内である。

`_insert_record(conn, record, now)`の3引数signatureはcompatibility wrapperとして維持し、batch用の`_insert_record_with_document_pks`を分離した。`upsert_records(records, *, delete_ids=None)`でTEMP tableと集合SQLを使い、document／chunk／identifier更新を1 transactionにまとめた。`incremental._flush_batch()`はcatalog呼出し配線だけを変更した。

直接testは+319/-3 LOC、非配布benchmarkは723 LOC。指示書の想定test 130〜270、benchmark 150〜270を超えたが、製品差分hard stopには該当しない。増分はactual Office生成、OpenXML byte正規化、fresh-process actual ONNX／Chroma、raw paired値、実manifest／provider／RSS／非計測SQL probeを自己完結させた証跡コードであり、製品APIや設定は増やしていない。

## Fixtureとruntime

- seed: `1102`
- DOCX: 40 documents × 5 records = 200
- PPTX: 20 decks × 10 non-empty slides = 200
- total: 400 records
- update: DOCX 4 + PPTX 2 = 40 records
- initial Office file manifest SHA-256: `c434d07788fb5e5f9af735348ed86fbb0e782959a62ff96849fcae4cd908d846`
- updated Office file manifest SHA-256: `b7be21753a33507ea3942b93a297b834333ceb6d700969aa6dc093134cf71654`
- initial ordered record manifest SHA-256: `80662004c03795d6992ffb3cbd4ac6cdc6994d9a06ddb31645b3592417968687`
- updated ordered record manifest SHA-256: `e29a76fd6edc4cc87ca75bb537ce04a3e746cf5adc8d671ead720b9e433a4258`
- second-generation byte reproducibility: `PASS`
- CPython: `3.13.1` 64-bit Windows
- Chroma: `1.5.9`
- ONNX Runtime: `1.22.1`, provider `CPUExecutionProvider`
- NumPy: `2.3.2`
- model: `cl-nagoya/ruri-v3-30m`, dynamic-int8, dimension 256
- `model.onnx` SHA-256: `BD07545FA4EB8E7765939536944DA1138A7E61441D81633ADFA1FAF218DF6BB1`

各sampleはfresh process／fresh temp DB root／一意Chroma directoryで実行した。`OnnxRuntimeEmbedder`、CPU provider、実製品manifest、Chroma collection metadata、`chroma.sqlite3`、count 400を各workerがhard assertした。SQL traceと`total_changes`は4本の別process probeへ分離し、性能測定pathでは`catalog.connect`を差し替えていない。

## 正式性能結果

warm-up pairはcore 1回、Office E2E 1回。採用sampleはcore 5 pair、Office E2E 3 pair。全体423.1秒で完走し、45分上限内だった。

| cohort／指標 | baseline p50 | candidate p50 | paired median短縮 | 元指示gate |
|---|---:|---:|---:|---|
| core catalog | 0.4681 s | 0.3684 s | 21.68% | PASS (>=20%) |
| core full ADD | 8.0277 s | 7.9601 s | 1.63% | FAIL (<10%) |
| core 10% update | 0.8424 s | 0.8267 s | 2.21% | FAIL (<15%) |
| core update catalog | 0.0898 s | 0.0624 s | 30.74% | reference |
| Office E2E full ADD | 12.4805 s | 12.4407 s | 0.36% | PASS (non-regression) |
| Office E2E update | 2.7841 s | 2.7265 s | 2.09% | reference |

| cohort／variant | full ADD p95 | update p95 | peak RSS p95 | DB bytes p95 |
|---|---:|---:|---:|---:|
| core baseline | 8.2697 s | 0.8893 s | 472.29 MiB | 4,226,143 |
| core candidate | 8.1001 s | 0.8522 s | 474.64 MiB | 4,217,952 |
| Office E2E baseline | 12.6517 s | 2.8050 s | 475.37 MiB | 4,209,977 |
| Office E2E candidate | 12.5735 s | 2.7465 s | 471.78 MiB | 4,214,074 |

Core paired raw wall値:

| pair | baseline full | candidate full | baseline catalog | candidate catalog | baseline update | candidate update |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 8.2697 | 7.9630 | 0.4698 | 0.3679 | 0.8893 | 0.8267 |
| 2 | 8.0088 | 7.9601 | 0.4681 | 0.3698 | 0.8424 | 0.8062 |
| 3 | 8.0277 | 7.8970 | 0.4660 | 0.3684 | 0.8569 | 0.8522 |
| 4 | 8.0170 | 8.1001 | 0.4665 | 0.3652 | 0.8384 | 0.8272 |
| 5 | 8.0839 | 6.9083 | 0.4776 | 0.3723 | 0.8261 | 0.8079 |

Office E2E paired raw wall値:

| pair | baseline full | candidate full | baseline update | candidate update |
|---:|---:|---:|---:|---:|
| 1 | 12.4805 | 12.4360 | 2.8050 | 2.7465 |
| 2 | 12.6517 | 12.5735 | 2.7841 | 2.7036 |
| 3 | 11.4133 | 12.4407 | 2.7722 | 2.7265 |

非計測SQL probeではcore baseline 30,540 statements／14,147 total changes、candidate 22,033／7,061だった。計測値へtrace callback overheadは混入していない。

元指示gate一覧（測定時の判定を保存）:

- catalog paired median >=20%: `PASS`
- core full ADD paired median >=10%: `FAIL`
- Office E2E non-regression: `PASS`
- 10% update paired median >=15%: `FAIL`
- full／update p95 <= baseline +10%: `PASS`
- peak RSS <= baseline +15%: `PASS`
- DB bytes <= baseline +5%: `PASS`

2026-08-15補正gate一覧（追加測定なし）:

- 効果主gate — catalog paired median >=20%: `PASS (21.68%)`
- 防波堤 — core full ADD非退行: `PASS (1.63%短縮)`
- 防波堤 — Office E2E非退行: `PASS (0.36%短縮)`
- 防波堤 — 10% update非退行: `PASS (2.21%短縮)`
- 正しさ／logical gold／検索契約: `PASS`
- 独立review P0/P1 0: `PASS`
- full／update p95 <= baseline +10%: `PASS`
- peak RSS <= baseline +15%: `PASS`
- DB bytes <= baseline +5%: `PASS`
- 補正総合判定: `GO`

## 正しさと回帰試験

- 新規catalog製品化test 10/10 PASS。
- 必須3 suiteを含む第一群 121 PASS: Unicode filename exact、ingestion layer invariants、retrieval contracts、新規catalog test。
- 関連第二群 102 PASS／4 SKIP（106 cases）: DB write integrity、SharePoint read error、index integrity、broad search、Source delete、ingestion observability、ingestion scope、token-safe chunking。
- 合計: `223 PASS / 4 SKIP / 227 cases / new failure 0`。
- `PRAGMA integrity_check=ok`、foreign key error 0、baseline／candidateのlogical/search gold一致。
- 最初のDB integrity実行1件はWindows CP932でmanifest JSONを読む環境エラーだった。製品差分と無関係なため、合格済みtestを反復せず、その失敗点以降だけ`PYTHONUTF8=1`で実行しPASSした。
- `py_compile`: PASS。
- `git diff --check`: PASS。

## 独立review

独立Reviewerはtested SHA `184a643a325499d3a701187874ebeb7076cd2117`を監査し、製品差分と正式測定器に残存P0／P1なしとして正式実行GOを出した。確認事項は3引数helper互換、製品scope、OpenXML byte再現、fresh subprocess、測定外SQL probe、actual ONNX provider、実manifest／Chroma metadataのhard assertである。

## 証跡と結論

- formal raw／aggregate: `perf-011-r2-evidence/formal.json`
- formal JSON SHA-256: `A8DFE0D2CA5A6F762BA4BC554515A0B150AD38B5D4F5DF329A53FEAC64AEC610`
- preflight: `perf-011-r2-evidence/preflight-fixed.json`
- smokeは`formal:false`であり、正式判定へ流用していない。

本candidateはcatalog書込量を21.68%削減し、実ONNX／Chromaを含むcore full ADD、Office E2E、10%更新も悪化させていない。元指示gateでの`NO_GO`を履歴として残しつつ、ユーザー補正gateでは`GO`とする。結果票のDrive新規保存を確認後、branchと証拠を保持したままmainへ通常統合し、後続LRR-PERF-012-R2はその更新後mainから開始する。
