# Local Copilot RAG System Design

## 目的

任意のVS CodeプロジェクトやGitHub Copilot Chatから、ローカルに置いた多数の企画、開発、運用、障害、設計、議事録、仕様、コード関連資料を検索できるRAG基盤を作る。

`ac-rag` は検証用DB名の一つにすぎない。特定業界や特定語彙には寄せない。

外部I/Fは単純に保つ。

```bash
python ~/.copilot/rag/query/search.py --db xxx-rag "<question>"
```

Copilotは検索方式を選ばない。Dense検索、BM25、完全一致、ファイル名検索、RRF、rerank、重複排除、context組み立てはすべてPython側で隠蔽する。

## 基本方針

- `$HOME/.copilot` に丸ごとコピーして使える構成にする。
- 既存の `~/.copilot/copilot-instructions.md` は上書きしない。
- DB本体、DBごとの指示、抽出済み本文、索引、ログは `rag/dbs/` 配下に置き、gitignore対象にする。
- 生成系と検索系を分離する。
- DBは多数作れる前提にする。
- 明示DB名 `xxx-rag` を最優先にする。
- 自然言語で「過去資料」「ローカルRAG」「設計書から調べて」などと言われた場合もRAG起動できる。
- 最初からhybrid retrievalを前提にする。
- 長時間生成は小さいwork item単位でcommitし、中断後にresumeできるようにする。
- 実装はMITで新規作成する。外部I/Fと運用要件はこの設計書を正とする。

## 配置

配布元。

```text
copilot_pack/
  instructions/
    rag.instructions.md
  rag/
    README.md
    query/
      .venv/                  # ローカル作成。配布しない
      requirements.txt
      setup.py
      setup.ps1
      search.py               # Copilotが呼ぶ単一入口
      list_dbs.py
      proxy_client.py
    gen_db/
      requirements.txt
      create_db.py
      build_db.py
      add_data.py
      status.py
      rebuild_component.py    # lexical/vector/extractなどの再構築
      software_rag_tool/
    dbs/
      README.md
      .gitkeep
      <db-name>-rag/          # gitignore対象
```

コピー後。

```text
~/.copilot/
  instructions/
    rag.instructions.md
  rag/
    query/
    gen_db/
    dbs/
      ac-rag/
      project-a-rag/
      ops-runbook-rag/
```

`~/.copilot/copilot-instructions.md` は本パックでは作らない。ユーザーが既存のCopilot設定やユーザー指示に、必要なら次の短い参照だけ追加する。

```text
RAGが必要な場合は ~/.copilot/instructions/rag.instructions.md を参照してください。
```

`~/.copilot/instructions/` がVS CodeやCopilotから自動再帰探索されることは前提にしない。自動読込は環境依存になりやすいため、トップ指示から明示参照する運用にする。

## DBレイアウト

各DBは `~/.copilot/rag/dbs/<db-name>-rag/` に閉じる。

```text
~/.copilot/rag/dbs/xxx-rag/
  manifest.json              # DB名、schema version、active_generation
  DB_PROFILE.md              # DB固有の短い説明。通常promptには常時入れない
  catalog.sqlite             # 正本。本文、メタデータ、状態、FTS、識別子
  chroma/                    # Dense vector index。再構築可能な派生物
  objects/                   # 抽出済みテキスト等のCAS。任意
  logs/
    progress.json            # UI/AI表示用snapshot
    events-YYYYMMDD.jsonl    # 診断用event log
  locks/
    writer.lock
```

`progress.json` と `events-*.jsonl` は正本にしない。resume判定、成功状態、失敗状態、generation、work itemは最終的に `catalog.sqlite` を正本にする。

## 採用コンポーネント

| 用途 | 採用 |
| --- | --- |
| Dense vector search | Chroma |
| 本文・メタデータ正本 | SQLite |
| 日本語BM25 | SQLite FTS5 + SudachiPy |
| 識別子・完全一致 | SQLite通常テーブル + index |
| ファイル名・見出し検索 | SQLite FTS5 / metadata trigram |
| 検索統合 | Python weighted RRF |
| rerank | 任意。初期OFF、評価後ON |
| embedding | `cl-nagoya/ruri-v3-130m` |
| query/runtime venv | `~/.copilot/rag/query/.venv` |

Ruri v3のprefixは固定する。

```text
文書embedding: 検索文書: <chunk text>
質問embedding: 検索クエリ: <question>
```

prefix、model id、model revision、embedding dimension、tokenizer設定はfingerprintとして保存する。変更時はvector再構築対象にする。

## 入力ポリシー

指定されたinput root配下の対応ファイルは、原則すべて処理する。

- `.md` / `.txt` は変換済み資料としてそのまま取り込む。
- 同名の `.md` と `.docx` / `.doc` / `.pptx` / `.ppt` / `.pdf` があっても片方を暗黙にスキップしない。
- 「指定されたものをできるだけ処理する」を優先する。
- 重複は入力時に消さず、検索時にduplicate collapseする。
- 旧Office形式の変換失敗はDB全体の失敗にしない。ファイル単位でerrorにして継続する。

対応形式。

```text
.md .txt .log
.pdf
.docx .doc
.pptx .ppt
.xlsx
.py .js .ts .tsx .java .cs .go .rs .sql .yaml .yml .json .toml .ini .xml
```

旧形式 `.doc` / `.ppt` はLibreOffice CLIを第一候補にする。macOSの `.doc` は `textutil` fallbackを許可する。

## Chunk設計

検索用chunkとCopilotへ返すcontextは分ける。

| 種類 | 境界 |
| --- | --- |
| Markdown / Word / PDF | 見出し、段落、箇条書き、表 |
| PowerPoint | slide、title、speaker notes |
| Excel | sheet、table、row group |
| ソースコード | function、class、config block |
| 議事録 | agenda、decision、action item、発言block |

開始値。

- chunk size: 300-700 model tokens
- overlap: 小さめ。構造境界を優先
- Ruriは長文対応でも、1chunkに複数話題を詰めない

chunkには次を持たせる。

```text
document_id, chunk_uid, section_id, parent_id
prev_chunk_id, next_chunk_id
source path, source type, title, heading path
page / slide / sheet / line / symbol
char offset, raw text, normalized lexical text
chunk_hash, content_hash
visible_from, visible_until
```

最終contextでは、RRFまたはrerank後の上位chunkだけ親・隣接chunkを展開する。通常は前後±1、同一section内で最大1200-1600 tokens程度。コードは可能なら関数全体、表はheaderと該当行を返す。

先に隣接展開してからrerankしない。同一ファイルのchunkで候補が埋まりやすくなるため。

## catalog.sqlite

SQLiteを正本にする。Chromaは再構築可能な派生索引として扱う。

主要テーブル。

```text
database_meta
source_file
revision
chunk
identifier_occurrence
work_item
generation
```

FTS。

```text
fts_word(path_tokens, title_tokens, heading_tokens, body_tokens)
file_fts(basename_tokens, stem_tokens, path_tokens, title_tokens)
metadata_trigram(value, kind, chunk_uid)
```

`fts_word` はSudachiPy A-modeの `normalized_form` を空白区切りで格納する。原文は `chunk.text` に必ず残す。NFKC済みテキストだけを保存して、API名、大小文字、全半角、記号情報を失ってはいけない。

本文全文のtrigramは初期OFFにする。ファイル名、見出し、symbolなどmetadataにはtrigramを使えるようにする。本文trigramはDB単位の評価で必要な場合だけ有効化する。

## 段階別hash

再構築範囲を最小化するため、hashを段階ごとに分ける。

```text
raw_key       = raw bytes + source identity
extract_key   = raw_key + extractor fingerprint
chunk_set_key = extract_key + chunker fingerprint
lexical_key   = chunk text + tokenizer fingerprint
embedding_key = 実際のprefix付き入力 + model fingerprint
```

tokenizer変更ならFTSだけ、embedding model変更ならvectorだけ、extractor変更ならextract以降だけを再構築できる。

## 生成・追加pipeline

処理は冪等なwork itemで進める。

```text
discover
  -> hash
  -> extract
  -> normalize
  -> chunk
  -> write catalog chunks
  -> write FTS / identifier indexes
  -> embed
  -> Chroma upsert
  -> validate
  -> publish generation
```

運用ルール。

- 1DBにつきwriterは1つ。
- SQLiteはWALを有効化する。
- ファイル単位または少数ファイル単位でcommitする。
- work itemにleaseとheartbeatを持たせる。
- Chroma IDとcache keyは決定的にする。
- ファイル処理前後でstatを確認し、処理中に変わったファイルはretry対象にする。
- full scan完了前に未発見ファイルを削除扱いしない。
- parse失敗時は前回公開済みの正常版を残す。
- events logは診断用で、resume正本にはしない。

小バッチ化は検索品質を落とさない。同じchunkに同じembeddingを作り、同じIDでupsertする限り、一括構築と検索結果は同等になる。

## 原子的な公開

SQLiteとChromaをまたぐ単一transactionは作れないため、generationで可視性を管理する。

```text
visible_from <= active_generation < visible_until
```

公開手順。

1. target generationを作る。
2. 新chunkをtarget generationでSQLite、FTS、identifier、Chromaへ追加する。
3. 置換対象の旧chunkに `visible_until = target_generation` を設定する。
4. 件数、参照、embedding dimension、Chroma record IDを検証する。
5. 短いSQLite transactionで `active_generation` を切り替える。
6. queryは開始時のgenerationを最後まで固定する。

Chroma metadataには `chunk_uid`、`document_id`、`visible_from`、`visible_until`、`embedding_key` 程度だけ持たせる。本文、詳細metadata、source locationはSQLiteから引く。

## クエリ解析

LLMによるquery rewriteは初期版では使わない。Python側で決定的に処理する。

| 名前 | 内容 |
| --- | --- |
| raw | 入力質問そのもの |
| canonical | NFKC、casefold、空白整理 |
| lexical | SudachiPy A-mode normalized tokens |
| anchors | 引用符、backtick、ファイル名、パス、英数字混在、記号を含む識別子 |

anchorはドメイン固有語ではなく形で抽出する。

```text
ABC-1234
A2W
HTTPServerError
http_server_error
foo.bar.Baz
config.yaml
/api/v2/items
UUID
hex string
URL
error code
```

Copilotは質問を分解しない。質問全文を1回だけ `search.py` に渡す。

## 検索pipeline

初期完成形。

```text
question
  -> query analysis
  -> Dense search        Chroma + Ruri
  -> BM25 body search    SQLite FTS5 + Sudachi
  -> metadata search     path/title/heading/symbol
  -> exact search        identifier table
  -> family-level ranking
  -> weighted RRF
  -> optional rerank
  -> anchor rescue
  -> duplicate collapse
  -> document diversity
  -> parent/neighbor expansion
  -> token budget packing
  -> prompt/json output
```

開始値。

| 段階 | top |
| --- | ---: |
| Dense | 60 |
| BM25本文 | 60 |
| title/path/heading/symbol | 30 |
| exact identifier | 30 |
| RRF後 | 40-60 |
| rerank対象 | 30-50 |

BM25 score、cosine distance、reranker raw scoreは直接足さない。尺度と向きが違うため、rank統合を基本にする。

## BM25

質問全文をそのままFTS構文として渡さない。内部で次を作る。

- 稀な語を中心にしたAND
- 意味語全体のOR
- 引用部や連続語のphrase
- path/title/headingへの列重み

固定stopword辞書だけに頼らず、FTS内の文書頻度を利用する。識別力の低い語は弱め、稀な語は強める。

BM25のAND、OR、phraseを別々のRRF投票にしない。同じ検索方式のquery variantは、まず1つのBM25 family rankingへ集約する。

## RRF

weighted RRFの開始値。

```text
score(d) =
  1.0 / (60 + rank_dense(d))
+ 1.0 / (60 + rank_bm25(d))
+ 0.6 / (60 + rank_metadata(d))
+ 1.2 / (60 + rank_exact(d))
```

exactは強いanchorがある場合だけ参加させる。

注意点。

- exactを必須条件にしない。
- 稀なIDの完全一致がある場合は、最良候補を最終contextに残す anchor rescue を入れる。
- k=60と重みは初期値であり、評価結果で調整する。
- RRF順位を最終の「確信度」として表示しない。

## rerank

rerankerは候補生成後にだけ使う。初期リリースではOFFでもよいが、設計とCLIは最初から対応する。

| 段階 | 構成 |
| --- | --- |
| 初期 | RRFのみ |
| 軽量 | 日本語reranker small、上位20-30件 |
| 品質重視 | `cl-nagoya/ruri-v3-reranker-310m`、上位40-50件 |

rerankerに渡す文字列。

```text
<query>
[path]
[title]
[heading]
<chunk text>
```

embedding用の `検索クエリ:` / `検索文書:` prefixはrerankerへ流用しない。

reranker有効時はreranker順位を主順位にし、RRF順位はguardまたはtie-breakに使う。identifier完全一致はanchor rescueで保護する。

## 重複排除と文書多様化

- exact duplicate: normalized text hashでcluster化
- near duplicate: 5-gram shingleのJaccardまたはcontainment
- adjacent overlap: offsetとprev/nextで結合
- provenance: 削除せず全sourceとrevisionを保持
- 1文書あたり原則2ブロック、最大3ブロック
- anchor完全一致は上限を迂回可能

文書代表scoreは最高chunkを中心にし、2件目以降はsoft penaltyをかける。長い文書がchunk数だけで有利にならないようにする。

## 出力形式

既定はCopilot向けprompt形式。

```text
## Retrieved evidence
Database: xxx-rag
Generation: 42

[R1] docs/design/api.md - 再送制御 - lines 120-148
根拠原文...

回答では根拠IDとsource locationを引用すること。
根拠が不足する場合は断定しないこと。
```

JSON形式は `local-rag.search.v1` とし、`db`、`query`、`generation`、`status`、`evidence`、`warnings`、`truncated` を返す。各evidenceには `id`、`source.path`、`location`、`text`、`signals` を含める。

`status` は `ok`、`partial`、`no_evidence`、`error` に絞る。

既定出力にraw cosine distance、BM25値、RRF値は出さない。これらは回答の確信度ではない。開発者向けには `--explain` で出す。

## CLI

Copilot向け検索。

```bash
python ~/.copilot/rag/query/search.py --db xxx-rag "<question>"
python ~/.copilot/rag/query/search.py --db xxx-rag --stdin
python ~/.copilot/rag/query/search.py --auto "<question>"
```

検索CLIオプション。

```text
--format prompt|json
--budget-tokens N
--timeout SEC
--stdin
--explain
--include-db-hint
```

運用者向け。

```bash
python ~/.copilot/rag/gen_db/create_db.py --db xxx-rag --title "Project Knowledge"
python ~/.copilot/rag/gen_db/build_db.py --db xxx-rag --root PATH --source-id SOURCE --resume
python ~/.copilot/rag/gen_db/add_data.py --db xxx-rag --root PATH --source-id SOURCE
python ~/.copilot/rag/gen_db/status.py --db xxx-rag --json
python ~/.copilot/rag/gen_db/rebuild_component.py --db xxx-rag --component lexical
python ~/.copilot/rag/gen_db/rebuild_component.py --db xxx-rag --component vector
python ~/.copilot/rag/gen_db/rebuild_component.py --db xxx-rag --component extract
```

将来のPython API。

```python
search(db_name, question, context_budget=None) -> SearchResponse
```

内部Retrieverは公開APIにしない。Copilotやユーザーに検索戦略を選ばせないため。

## Copilot向け指示

`~/.copilot/instructions/rag.instructions.md` には短く書く。

```text
ユーザーが xxx-rag というDB名を明示した場合、またはローカル資料、過去資料、設計書、議事録、障害、運用手順、RAG検索を求めた場合だけRAGを使う。

質問全体を変更せず、次のコマンドへ1回渡す。
python ~/.copilot/rag/query/search.py --db xxx-rag "<question>"

キーワード分解、検索モード選択、複数回検索は行わない。
返された [R1] などの根拠ID、path、locationを引用して回答する。
根拠が不足する場合は、文書内に記載があると断定しない。
```

自然言語でDB名がない場合。

```bash
python ~/.copilot/rag/query/search.py --auto "<question>"
```

複数DB候補が返った場合、Copilotは勝手に選ばず候補名をユーザーに聞く。

DB固有の指示は `~/.copilot/rag/dbs/<db-name>/DB_PROFILE.md` に置く。ただし常時promptへ入れない。`search.py` が必要最小限のhintだけを返す。

DB生成・追加を頼まれた場合。

```text
1. status.py --json を先に実行する。
2. appears_active が true なら重複起動しない。
3. can_resume が true でroot/source_idが一致するならresumeする。
4. root/source_idが違うならadd_data.pyを使うか、ユーザーに確認する。
5. 「最初から」「破棄して」「作り直し」が明示された時だけforce rebuildする。
6. 長時間処理中はstatus.pyで進捗を見る。
```

## 評価

DBごとに50-100件の評価質問を用意する。

```text
Recall@60
MRR
nDCG
token budget内の根拠回収率
identifier exact成功率
同一ファイル占有率
duplicate率
p50/p95 latency
index size
生成resume成功率
抽出失敗率
```

評価セットには、日本語自然質問、英語資料への日本語質問、ファイル名指定、API名、エラーコード、ticket ID、略語、過去決定の根拠確認、同名MarkdownとOffice原本、抽出失敗ファイル混在を含める。

## 実装順序

最初から目指す構成は、Chroma + SQLite FTS5/Sudachi + identifier exact + metadata検索 + weighted RRF + context組み立て。

1. `catalog.sqlite` schema、WAL、manifest、generationを作る。
2. 既存clean JSONLからcatalogを再構築できる `rebuild_component.py --component lexical` を作る。
3. build/add時にchunk、FTS、identifier、work itemをSQLiteへ書く。
4. SudachiPy A-mode tokenizerとfallback tokenizerを実装する。
5. identifier/anchor抽出を実装する。
6. query側でDense、BM25、metadata、exactを取得する。
7. family ranking集約とweighted RRFを実装する。
8. duplicate collapse、document diversity、context packingを実装する。
9. `--format json|prompt`、`--budget-tokens`、`--stdin`、`--explain` を実装する。
10. progress/stateの正本をSQLiteへ移す。
11. 評価で必要ならrerankerを追加する。
12. 評価で必要なDBだけ本文trigramを有効化する。
13. CLI cold startが問題になったらdaemon/proxyを追加する。

既存のChroma単体DBから移行する場合、最初にclean JSONLを使って `catalog.sqlite` とFTS/identifierだけを作る。embedding再計算は不要にする。

## リスクと対応

| リスク | 対応 |
| --- | --- |
| SQLiteとChromaの二重管理 | SQLiteを正本にし、Chromaはchunk_uid参照だけ持つ |
| FTS5標準tokenizerが日本語に弱い | SudachiPy A-mode normalized tokensを投入する |
| Sudachiが複合語を分割しすぎる | raw完全一致、identifier、metadata trigramで補完する |
| 本文trigramの容量増加 | 初期OFF、DB単位で有効化 |
| rerankerの遅延 | 初期OFF、上位候補だけ処理 |
| Ruriが日本語中心 | 英語subsetを評価し、lexical/exactでrecallを保護する |
| 小chunkで文脈不足 | 検索後に親・隣接chunkを展開する |
| 同一ファイルが結果を占有 | document diversityとsoft penalty |
| 重複排除で版違いを消す | provenance、revision、日付を保持する |
| Office/PDF抽出誤り | extractor version、warning、locationを保存する |
| 長時間生成が中断される | work item、lease、heartbeat、小バッチcommit |
| Copilotが余計な判断をする | instructionsに「1回search.pyへ渡すだけ」と明記する |

## ライセンス

本実装はMIT License。

主要依存のライセンスは採用時に `NOTICE` またはREADMEに明記する。

| 対象 | ライセンス方針 |
| --- | --- |
| 本実装 | MIT |
| Ruri v3 130m | Apache-2.0 |
| Ruri v3 reranker | Apache-2.0 |
| SudachiPy | Apache-2.0 |
| SQLite | public domain相当 |
| Chroma | 採用versionのライセンスを固定して記録 |

明示ライセンスを確認できない外部コード、README文面、ディレクトリ構造は採用しない。
