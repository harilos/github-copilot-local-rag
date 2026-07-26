# ローカルRAG 最終組み合わせ試験仕様書

対象は `~/.copilot/rag` に配置されるローカルRAG一式とする。目的は、リリース前にバグ、不整合、性能劣化、OS差異、永続化境界の破損を発見すること。

通常の利用者向け検索I/Fは増やさない。検索方式の切替、failpoint、短縮idle、固定candidateなどはQA harnessまたはテスト専用オプションに隔離し、デフォルト実行へ影響させない。

## 1. 試験方針

最終試験は7層で構成する。

| 層 | 主目的 | 規模目安 |
|---|---|---:|
| S0 Smoke | 配置、3DB、最小検索、削除済みDB非表示 | 12-20 |
| S1 外部I/F全直積 | argv/stdin、prompt/JSON/explain、daemon差 | 72 |
| S2 検索組み合わせ | OS、DB、質問種別、実行経路の相互作用 | 72以上 |
| S3 検索内部 | Dense、BM25、Exact、Metadata、RRF、Postprocess | 約80 |
| S4 ライフサイクル | setup、build、resume、add、rebuild、status | 約100 |
| S5 障害・並行・OS差 | kill、disk full、lock、DB切替、Unicode | 約80 |
| S6 品質・性能・Soak | ベクトル有効性、遅延、RSS、長時間安定性 | 114問 + 負荷 |

通常条件はPairwiseで削減する。ただし、以下はPairwiseへ落とさず、全組み合わせまたは必須3-wayとして扱う。

- DB x 質問種別 x daemon/no-daemon
- OS x 入力方式 x 文字コード・特殊文字
- Windows x daemonがDBを保持中 x add/rebuild/DB差替え
- DB切替順 x daemon cache x retriever
- 永続化境界 x 強制終了 x resume
- retriever重複 x 文書偏り x token budget境界

## 2. リリース対象DBと基準値

| DB | docs | chunks | collection | サイズ | schema | tokenizer | db_hash |
|---|---:|---:|---:|---:|---:|---|---|
| `ac-rag` | 21 | 1,205 | 1,205 | 27MB | 2 | `sudachi-a-v1` | `6b7bb428e1cec42bddaa35f510913c25f9a767bc22e9104444768322122fabf9` |
| `incident-rag` | 201 | 2,144 | 2,144 | 45MB | 2 | `sudachi-a-v1` | `7e3858deb3f4b69ee171a77ffeae8d4ddc69ff7a90b73162a37ef33ef1aada56` |
| `rfc-full-20k-rag` | 386 | 19,518 | 19,518 | 480MB | 2 | `sudachi-a-v1` | `c7f4fba2fc8266d4a0d67efdb443e01c811ff0c90bc8a81b35e979ee6f9051fc` |

`rfc-2000-rag` はmacOSとWindowsの両方で削除済みDBとして扱う。

合格条件:

- `list_dbs.py` に出ない。
- 検索すると規定の「DBなし」エラーになる。
- daemon cacheから古い結果を返さない。
- 自動再作成されない。

試験前に記録する環境:

- Git commit、`git status --porcelain`
- OS名、OS version、CPU architecture
- CPU型番、物理/論理core数、RAM
- shellとversion
- Python/pip version
- ONNX Runtime version、Execution Provider、thread設定
- model ID、revision/checksum、INT8、embedding dimension
- tokenizerと辞書version
- DBごとの実ファイルサイズ、件数、hash
- 電源モード、ウイルス対策の有無

対応OSの最小基準:

- macOS: 対応architectureを明記する。Intel Macを対象にするなら `x86_64`、Apple Siliconも配布対象なら `arm64` を別水準にする。
- Windows: Windows 11 x64 + PowerShell 7を最低基準にする。PowerShell 5.1、cmd、Git Bashを正式対応する場合は別水準を追加する。

## 3. 試験前に凍結する動作契約

期待値が決まっていない試験は合否判定できない。リリース前に以下を仕様として固定する。

| 項目 | 契約 |
|---|---|
| DB名 | pathではなく論理名。`..`、絶対path、separator、予約名を拒否する |
| DB名の大小文字 | 全OSで同じ規則。case差だけの2DBは許可しない |
| 不完全DBの検索 | fail closed。非0終了、contextを返さない |
| retriever単体障害 | 縮退を許す場合は `degraded` とcomponentを機械可読で返す。silent fallbackは禁止 |
| query上限 | 最大文字数/token数、拒否または切捨て規則を固定。切捨てはexplainに記録 |
| stdout/stderr | stdoutはpromptまたは単一JSONだけ。log、model load、warningはstderr |
| build中検索 | 初回build中はBUSY。旧世代がある更新中は旧世代を読むかBUSY |
| writer競合 | 1 DBにつきwriterは1つ |
| `add_data` identity | `(source_id, 正規化相対path)` |
| 同一文書の再追加 | no-op |
| 同一pathの内容更新 | 旧chunk/vector/FTS/postingを全削除し、新世代へ原子的に置換 |
| 入力から消えた文書 | `add_data`では暗黙削除しない |
| resume中の入力変更 | content hash差を検出して停止。旧新混在を作らない |
| lexical rebuild | 論理本文とDenseを変更せず、同条件ならdb_hash不変 |
| DB差替え | daemonはdb_hash/generation変化を検出して再open |
| path正規化 | 内部separatorは `/`。UnicodeはNFC推奨。表示用原文pathも保持 |

## 4. 共通オラクルと証跡

### 4.1 検索結果のcanonical比較

検索結果のcanonical比較では、出力から次だけを抽出する。

```text
status / degraded_components
db_name / db_hash
result rank
chunk_uid
doc_id
chunk_index
source/path/location
text_sha256
retrieval seedかneighborか
neighborの元seed UID
```

同値比較から除外するもの:

- request ID
- daemon PID
- timestamp
- 各処理時間
- 同順位に影響しない浮動小数点末尾

同じOSでは、以下を原則canonical result完全一致とする。

- argv と stdin
- prompt と JSON
- explainなし と explainあり
- daemon cold と daemon warm
- daemon と `--no-daemon`
- 同じqueryの反復

Mac/Windows間の期待値:

- Exact、Metadata、BM25の非tie結果: chunk UIDと順位が一致
- 一意identifier、一意filename: top1完全一致
- Denseを含む非tie query: gold top1一致
- Dense top10集合: overlap 0.9以上を目標
- Hybrid最終top5: overlap 0.8以上。差分はexplainで説明可能

1ケースごとに保存する証跡:

- test ID、開始/終了時刻
- 実行command、shell展開後query hash
- OS、shell、Python、commit
- exit code、stdout、stderr
- wall time、CPU time、peak RSS
- daemon PID/start時刻、cold/warm、model load回数
- 検索前後のDB hash・件数
- canonical result
- explain JSON
- pass/failと差分

### 4.2 DB不変条件

| ID | 不変条件 |
|---|---|
| INV-01 | 完成DBのSQLite chunk UID集合とChroma ID集合が一致 |
| INV-02 | `chunks == collection_count` |
| INV-03 | chunk UIDと `(doc_id, chunk_index)` に重複がない |
| INV-04 | document、FTS、identifier posting、file lookupに孤児参照がない |
| INV-05 | vector dimension、model、quantization、prefix/profileがDB metadataと一致 |
| INV-06 | `progress`はcommit済み範囲より先行しない |
| INV-07 | readyなcomponentは同じgenerationを指す |
| INV-08 | `VERSION.json`のschema/tokenizer/hashが実体と一致 |
| INV-09 | no-op再実行で件数、UID集合、論理hashが変わらない |
| INV-10 | 失敗時にSQLite/Chroma/lexicalの部分世代を公開しない |
| INV-11 | `events.jsonl`の成功runはterminal successが1つ |
| INV-12 | list/status/searchは論理DBを変更しない |
| INV-13 | stale lock、孤児process、増え続けるtmpを残さない |
| INV-14 | Git管理対象は試験前後ともclean |

日常試験ではSQLite `quick_check`、最終gateでは `integrity_check` と `foreign_key_check` を実施する。Chromaは件数だけでなくID集合も比較する。

## 5. Gold queryセット

本番3DBそれぞれで最低38問、合計114問を固定する。

| 種別 | DBあたり | 作成方法 |
|---|---:|---|
| Dense semantic | 5 | 根拠を意味保持で言い換え、固有語や長い共通句を避ける |
| BM25/phrase | 5 | 本文中の希少な語句。identifierではないものも含める |
| Exact | 10 | df=1、低df、高df、大小文字、数字・記号混在 |
| Metadata | 5 | basename、stem、相対path、title |
| Mixed/RRF | 5 | 2系統以上のretrieverが同じ根拠を返す |
| Neighbor/budget | 5 | 根拠がchunk境界、文書先頭・末尾にある |
| No-hit/低関連 | 3 | DBにないUUIDと無関係な自然言語 |

goldはchunk UIDだけでなく、原文位置で保存する。

```text
query_id
db
query_class
question
answerable
gold_doc_id
gold_source
gold_start_char / gold_end_char または gold_sentence_sha256
required_evidence_count
expected_retriever
stable_top1
```

No-hit queryでは「Denseが何も返さない」ことを期待しない。確認対象は、Exact/BM25/Metadataの偽一致、別DB混入、異常な高信頼表示、架空source、crashがないこと。

## 6. 組み合わせ設計

### 6.1 外部I/F全直積 `IF-CART-*`

各DBに、安定して複数retrieverが候補を返す基準queryを1つ固定する。

```text
2 OS x 3 DB x 2入力 x 3出力 x 2実行経路 = 72ケース
```

因子:

- OS: Mac / Windows
- DB: `ac-rag` / `incident-rag` / `rfc-full-20k-rag`
- 入力: argv / stdin
- 出力: prompt / JSON / JSON+explain
- 実行経路: daemon warm / `--no-daemon`

全72ケースの合格条件:

1. exit code 0。
2. stdoutが指定形式だけで構成される。
3. JSONはstdout全体をそのままparserへ渡して成功する。
4. 同一OSではcanonical resultが一致する。
5. 指定DB以外のchunkが0件。
6. DB件数・hashが不変。
7. query時に外部通信しない。

daemon coldはプロセス状態を変えるため、`DAE-COLD-*` として両OS x 3DBで別実施する。

Windows stdin正式例:

```powershell
'<質問>' | python "$HOME/.copilot/rag/query/search.py" --db ac-rag --stdin
```

### 6.2 6因子Pairwise `PAIR-001..024`

因子:

- OS: Mac / Windows
- DB: ac / incident / rfc20k
- 入力: argv / stdin
- 出力: prompt / json / explain-json
- 実行: daemon-cold / daemon-warm / no-daemon
- query: sem-ja / sem-en / mixed / exact / lexical / file / code-special / nohit

全直積864件に対し、以下の24件で全ペアを覆う。`explain` は `--explain --format json` を表す。daemon-cold行は毎回daemonを完全停止してから開始する。

| ID | OS | DB | 入力 | 出力 | 実行 | query |
|---|---|---|---|---|---|---|
| PAIR-001 | Mac | ac | argv | explain | no-daemon | sem-ja |
| PAIR-002 | Win | ac | stdin | prompt | daemon-cold | sem-en |
| PAIR-003 | Mac | ac | stdin | explain | daemon-cold | mixed |
| PAIR-004 | Mac | ac | argv | prompt | daemon-warm | exact |
| PAIR-005 | Win | ac | stdin | explain | daemon-warm | lexical |
| PAIR-006 | Win | ac | stdin | json | daemon-warm | file |
| PAIR-007 | Win | ac | argv | json | daemon-cold | code-special |
| PAIR-008 | Win | ac | stdin | prompt | no-daemon | nohit |
| PAIR-009 | Mac | incident | argv | prompt | daemon-cold | sem-ja |
| PAIR-010 | Mac | incident | argv | explain | no-daemon | sem-en |
| PAIR-011 | Mac | incident | stdin | json | no-daemon | mixed |
| PAIR-012 | Mac | incident | stdin | explain | no-daemon | exact |
| PAIR-013 | Win | incident | argv | json | no-daemon | lexical |
| PAIR-014 | Win | incident | stdin | prompt | no-daemon | file |
| PAIR-015 | Win | incident | stdin | prompt | daemon-warm | code-special |
| PAIR-016 | Mac | incident | argv | explain | daemon-cold | nohit |
| PAIR-017 | Win | rfc20k | stdin | json | daemon-warm | sem-ja |
| PAIR-018 | Win | rfc20k | stdin | json | daemon-warm | sem-en |
| PAIR-019 | Win | rfc20k | argv | prompt | daemon-warm | mixed |
| PAIR-020 | Win | rfc20k | stdin | json | daemon-cold | exact |
| PAIR-021 | Mac | rfc20k | stdin | prompt | daemon-cold | lexical |
| PAIR-022 | Mac | rfc20k | argv | explain | daemon-cold | file |
| PAIR-023 | Mac | rfc20k | stdin | explain | no-daemon | code-special |
| PAIR-024 | Win | rfc20k | argv | json | daemon-warm | nohit |

最終リリースでは、24個のDB x queryを daemon-cold / daemon-warm / no-daemon の全3経路で繰り返す。これを `TRI-DBQ-001..072` とし、DB x query x 実行経路の全72通りを保証する。OS、入力、出力は上表を基準に巡回割当する。

### 6.3 Pairwiseへ落とさない必須3-way

| ID群 | 組み合わせ | 主な故障 |
|---|---|---|
| T3-ENC-* | OS x argv/stdin x 日本語/CRLF/コード記号 | shell、文字コード、改行 |
| T3-CACHE-* | DB切替順 x daemon状態 x query種別 | collection、FTS、exact混線 |
| T3-OUT-* | 入力方式 x 出力形式 x quote/newline/backslash | JSON汚染、escape |
| T3-POST-* | retriever重複 x 同一文書偏り x budget境界 | dedup、diversity、packing |
| T3-WINUPD-* | Windows x daemon warm x add/rebuild/swap | open-file、rename制約 |
| T3-XOS-* | DB生成OS x 検索OS x Dense/Metadata | Chroma互換、path差 |
| T3-IDLE-* | idle期限 x 実行中request x 新規request | 終了競合 |
| T3-BROKEN-* | 1DB破損 x daemon多DB保持 x 他DB検索 | 障害分離 |

## 7. 検索内部試験

### 7.1 Dense / Vector

| ID | 試験 | 合格条件 |
|---|---|---|
| DEN-RESCUE-`<DB>`-01..05 | lexical/exact/metadataでは取れない意味言い換え | Dense top候補からRRF、最終contextまでgoldが残る |
| DEN-PROFILE-001 | model/revision/dimension/prefix照合 | DB profileとruntimeが一致 |
| DEN-FINITE-001 | query embedding検査 | dimension正、NaN/Infなし |
| DEN-TRUNC-001 | 最大token付近 | truncation有無と位置がexplainに出る |
| DEN-TAIL-ID-001 | 長文query末尾にidentifier | Denseが切れてもExact analyzerが全文を扱う |
| DEN-DBISO-001 | ac -> incident -> rfc -> ac | collection取り違え0 |
| DEN-FAIL-001 | model session故障注入 | 規定のfailまたは明示縮退。silent fallback禁止 |
| DEN-OFFLINE-001 | setup後、network遮断 | queryが完全offlineで成功 |

DEN-RESCUEでは、Denseなしのcandidate poolにgoldがなく、Denseありで最終contextへ入った因果を確認する。

### 7.2 BM25 / Sudachi

| ID | 試験 |
|---|---|
| LEX-JA-001 | 空白のない日本語 |
| LEX-EN-001 | 英語phrase、hyphen、複数形 |
| LEX-MIX-001 | 日本語 + 英語 + 数字 + 略語 |
| LEX-SYNTAX-001 | `"`, `*`, `-`, `:`, `NEAR`, `OR`を通常入力として扱う |
| LEX-LONG-001 | 高頻度語でもcandidate capを超えない |
| LEX-EMPTYTOK-001 | 記号だけでtoken 0でもcrashしない |
| LEX-TOKENIZER-001 | `sudachi-a-v1`不一致を検知 |
| LEX-POLARITY-001 | BM25 scoreの符号・昇降順を取り違えない |

### 7.3 Exact / Metadata / RRF / Postprocess

Exact:

- EXA-UNIQ-001: df=1 identifierはgold top1。
- EXA-LOWDF-001: 複数chunk occurrenceを漏れなく候補化。
- EXA-HIGHDF-001: 高df identifierで時間・メモリ・候補数が上限内。
- EXA-CASE-001: casefold対象とcase-sensitive対象が規則どおり。
- EXA-BOUND-001: `ABC-123` と `XABC-123Y` を混同しない。
- EXA-PUNCT-001: `/ . _ - :` を含むID/API/設定key。
- EXA-SQL-001: `' OR 1=1 --`、`%`、`_` がSQL/LIKE構文として実行されない。
- EXA-DUP-001: 同一termの複数occurrenceでも最終chunk重複0。
- EXA-ALIAS-001: canonical/aliasの一致種別がexplainに出る。
- EXA-FALSE-001: 通常語と似た略語で偽Exact候補を作らない。
- EXA-NEG-COLLISION-001: 存在しない `A2W` が共通prefix `A2` を介して `A2L` へ誤一致しない。

Exact検索ではlossyな自動生成aliasを使わない。許可するのは、完全なidentifier equality、full-length casefold equality、完全一致filename/pathに限定する。prefix、suffix、数字部分、substring、internal punctuation削除、segment一致をExact扱いしない。

Metadata:

- META-FILE-001: 完全なbasename。
- META-STEM-001: 拡張子なし。
- META-PATH-001: 相対path。
- META-SEP-001: `/` と `\`。
- META-CASE-001: OSのfilesystem case規則に引きずられない。
- META-DUPNAME-001: 同名fileが複数document。
- META-NONASCII-001: 日本語、空白、括弧、`#` を含むfilename。
- META-WINPATH-001: drive letter、UNC表記のescape。

RRFは `--explain` からretriever別candidate ID、rank、weight、RRF寄与を取得し、試験側で再計算する。

- RRF-MATH-001: `sum(weight / (k + rank))` が表示精度内で一致。
- RRF-RANK-001: rank起点、欠番、重複の規則が一定。
- RRF-ONCE-001: 同一retriever内の重複候補を二重加点しない。
- RRF-MULTI-001: 複数retriever一致chunkを正しく合算。
- RRF-TIE-001: 同点tie-breakが決定的。
- RRF-MISSING-001: 1 retrieverが空または故障した際の規則が一定。
- RRF-PROV-001: 最終seedはretriever候補和集合に属する。
- RRF-ORDER-001: raw scoreの大小でなくrankを使う。

Postprocess:

- POST-DEDUP-001: 同一chunkが複数retriever由来でも1回だけ出力。
- POST-TEXTDUP-001: 同一・準同一本文を規定どおり重複抑制。
- POST-DIV-001: 上位が1文書へ集中した場合に文書多様化が働く。
- POST-DIV-GOLD-001: 多様化のためにgoldを落とさない。
- POST-NEIGH-001: 中間chunkは前後を同一docから展開。
- POST-FIRST-001: 文書先頭で前docへ越境しない。
- POST-LAST-001: 文書末尾で次docへ越境しない。
- POST-MERGE-001: neighborが別seedでもある場合に二重出力しない。
- POST-BUDGET-N1/N/NPLUS1: budget境界で採用/skip/truncateが規定どおり。
- POST-HEADER-001: source見出しとdelimiter overheadもbudgetへ算入。
- POST-OVERSIZE-001: 単一chunkがbudget超過しても壊れたJSONを出さない。
- POST-SOURCE-001: source locationがcatalogと一致。

`anchor_rescue` は `raw_exact`、一意なfull-length `casefold_exact`、完全一致filename/pathだけで許可する。generated alias、prefix、substring、segment一致、lexical candidateでは発火させない。

## 8. 入力・CLI・出力

入力異常系:

- IN-NOQUERY-001: positionalなしはusage error。
- IN-EMPTY-001: stdin EOFはusage error。
- IN-SPACE-001: 空白だけはusage error。
- IN-BOTH-001: positionalと`--stdin`併用は曖昧に選ばずusage error。
- IN-MULTILINE-001: 複数行queryを保持し、末尾改行だけ規定どおり処理。
- IN-CRLF-001: CRLF stdinはLF版と同一。
- IN-QUOTE-001: 一重・二重引用符を文字列として検索。
- IN-SHELL-001: backtick、`$()`、`%VAR%`、`!^&|` をshell構文として扱わない。
- IN-DASH-001: `-foo`で始まるqueryはstdinまたは`--`で扱える。
- IN-BOM-001: UTF-8 BOMは規定どおり除去または明示拒否。
- IN-NUL-001: NULは明示拒否。
- IN-BADUTF8-001: 不正UTF-8はtracebackでなく入力エラー。
- IN-VERY-LONG-001: 極端に長いqueryでもhang/OOMなし、暗黙切捨てなし。
- IN-BROKENPIPE-001: downstreamがpipeを閉じてもtracebackを撒かない。

DB指定:

- DB-NOTFOUND-001: 存在しないDB。
- DB-REMOVED-001: `rfc-2000-rag`。
- DB-TRAVERSAL-001: `../`、絶対path。
- DB-CASE-001: DB名の大小文字規則がOS間で同じ。
- DB-INCOMPLETE-001: build途中。
- DB-SCHEMA-001: 未対応schema。
- DB-TOKENIZER-001: tokenizer不一致。
- DB-MODEL-001: embedding profile不一致。
- DB-COUNT-001: catalog/collection件数不一致。
- DB-CORRUPT-001: SQLite/Chroma破損。

JSON出力:

- OUT-JSON-PARSE-001: stdout全体が単一JSON。
- OUT-JSON-LOG-001: stdoutへlog、progress、model loadが混入しない。
- OUT-JSON-SCHEMA-001: schema versionと必須field/type。
- OUT-JSON-FINITE-001: NaN/Infinityなし。
- OUT-JSON-ESCAPE-001: newline、quote、backslash、control文字。
- OUT-JSON-UNICODE-001: 日本語文字化けなし。
- OUT-JSON-EXPLAIN-001: explain追加で最終結果不変。
- OUT-JSON-ERROR-001: error時に成功JSONの途中まで出さない。
- OUT-JSON-WINPATH-001: Windows pathが正しくescape。

prompt出力:

- OUT-PROMPT-EVIDENCE-001: JSONと同じ証拠集合・順序。
- OUT-PROMPT-SOURCE-001: 各contextにsource/location。
- OUT-PROMPT-DELIM-001: 本文中のMarkdown fenceやdelimiterで構造が壊れない。
- OUT-PROMPT-INJECTION-001: 文書本文を命令ではなく引用contextとして区切る。
- OUT-PROMPT-BUDGET-001: header込みでbudget内。
- OUT-PROMPT-NOHIT-001: 架空sourceや架空根拠を生成しない。

### 8.5 メタモルフィック試験

| ID | 変換 | 不変性 |
|---|---|---|
| MOR-ARGV-STDIN | argv -> stdin | canonical result一致 |
| MOR-DAEMON-DIRECT | daemon -> no-daemon | 同一OSで一致 |
| MOR-EXPLAIN | explain追加 | 最終context不変 |
| MOR-FORMAT | JSON -> prompt | evidence集合・順序不変 |
| MOR-REPEAT | 同じqueryを10回 | 結果不変 |
| MOR-DBSWITCH | A -> B -> C -> A | 最後のAが最初と一致 |
| MOR-LF-CRLF | LF -> CRLF | 一致 |
| MOR-SPACE | 前後空白・連続空白 | 正規化後一致 |
| MOR-NFC-NFD | Unicode正規化形 | 自然言語は一致 |
| MOR-ID-WRAP | ID -> `"ID"` -> `(ID)` | Exact候補維持 |
| MOR-ID-SENTENCE | ID単体 -> 文中へ埋込み | gold Exact維持 |
| MOR-PATHSEP | `/` -> `\` | Metadata候補一致 |
| MOR-POLITE | 一般的な前置きを追加 | goldが最終budget内に残る |
| MOR-READONLY | 検索前後 | DBの論理内容と件数不変 |

NFKCやcasefoldはcode identifierを壊す可能性があるため、自然言語とcase-sensitive identifierの期待値を分ける。

## 9. DB一覧と初期設定

`list_dbs.py`:

- LST-001: 3DBだけを表示。
- LST-002: name/title/hint/schema/tokenizer/hashが正しい。
- LST-003: `rfc-2000-rag`が出ない。
- LST-004: unrelated directory、tmp、lockをDB扱いしない。
- LST-005: 1DB metadata破損でも他DB一覧を失わない。
- LST-006: modelをloadせず、warm p95 1秒以下。
- LST-007: cwdを変えても同じ。
- LST-008: 実行前後でDBを変更しない。

`setup.py`:

- SET-001: 両OSの完全未設定状態でvenv、固定依存、30m ONNX INT8、smoke推論成功。
- SET-002: 正常状態で2回目no-op相当、再downloadなし。
- SET-003: package欠落・誤versionを修復または明示失敗。
- SET-004: model 0 byte・partial・hash不一致を使用しない。
- SET-005: download中network切断で完成名へpartialを公開しない。
- SET-006: warm cache offlineは成功、cold cache offlineは不足物を明示。
- SET-007: disk full / permission deniedで既存正常環境を壊さない。
- SET-008: setup二重起動でも1 writer、最終環境正常。
- SET-009: HOMEに空白・日本語・長pathでも成功。
- SET-010: setup後profileがmodel、INT8、dimension、prefix一致。
- SET-011: query offlineで外部通信0。
- SET-012: Git管理対象clean。

## 10. daemonと並行実行

| ID | 優先 | 試験・合格条件 |
|---|---:|---|
| DAE-001 | P0 | daemon不在から自動起動して成功 |
| DAE-002 | P0 | 2回目は同一daemon、model再loadなし |
| DAE-003 | P0 | ac -> incident -> rfc -> acでDB混線0 |
| DAE-004 | P0 | 8 process同時cold start後、resident daemonは1つ |
| DAE-005 | P0 | 3DB同時cold start、全応答のDB/hashが正しい |
| DAE-006 | P0 | stale PID/socket/pipe/lockから自動回復 |
| DAE-007 | P0 | model load中にkill、次回回復 |
| DAE-008 | P0 | query中にkill、partial JSONを成功扱いしない |
| DAE-009 | P0 | daemon存在中の`--no-daemon`がdaemonを使わない |
| DAE-010 | P0 | `--no-daemon`終了後に子processを残さない |
| DAE-011 | P0 | idle期限直前・一致・直後 |
| DAE-012 | P0 | active/queued request中にidle終了しない |
| DAE-013 | P0 | idle終了後にendpoint/lock/processを残さない |
| DAE-014 | P0 | 終了境界の新規requestを失わない |
| DAE-015 | P1 | sleep/resume後に応答または安全再起動 |
| DAE-016 | P0 | client/daemon protocol・code version不一致を検出 |
| DAE-017 | P0 | 1DB破損でもdaemonと他2DBは正常 |
| DAE-018 | P1 | 全DBを起動時にloadせずlazy open |
| DAE-019 | P1 | model sessionをDB数分複製しない |
| DAE-020 | P0 | 同名DBのdb_hash変更を検出してcache invalidate |
| DAE-021 | P0 | cache済みDB directory削除後に古い結果を返さない |
| DAE-022 | P0 | client Ctrl-C後も他requestとdaemonは正常 |

## 11. DBライフサイクルと障害注入

状態遷移:

```text
未作成
  -> create済み空DB
  -> building
  -> ready

building
  -> interrupted/failed
  -> resume
  -> ready

ready
  -> adding/rebuilding
  -> 旧readyを維持 または BUSY
  -> 新readyをatomic publish
```

QA専用failpoint:

| FI | 注入点 |
|---|---|
| FI-01 | operation start event直後 |
| FI-02 | source manifest / clean staging作成後 |
| FI-03 | SQLite chunk transaction commit直後 |
| FI-04 | FTS / Exact commit直後 |
| FI-05 | Chroma upsert/persist直後 |
| FI-06 | `index_state` checkpoint直後 |
| FI-07 | `progress` checkpoint直後 |
| FI-08 | VERSION/db_hash更新直前・直後 |
| FI-09 | staging generationのrename/swap直前・直後 |
| FI-10 | terminal success event直前 |

各点で通常例外とhard killを実施する。disk fullは実ディスクを埋めず、容量制限した隔離volumeで行う。

create/build/resume:

- CRT-001: 有効名・日本語titleでschema 2の正しい初期状態。
- CRT-002: 同名createは上書きせず明示失敗。
- CRT-003: case差だけの名前は両OSで同じ衝突判定。
- CRT-004: traversal、絶対path、Windows予約名でDB root外へ書かない。
- BLD-001: 正常clean build後に全INV通過。
- BLD-002: 同じ入力・設定で再buildし、UID集合と論理hash一致。
- BLD-004: 正常文書+破損文書は対象と理由を記録し、完全成功扱いしない。
- RES-001: early/mid/last batchでCtrl-Cし、resume後clean buildと論理一致。
- RES-002: FI-01..10でhard killし、1回のresumeでINV回復。
- RES-005: SQLite先行/Chroma先行をidempotentに補正。
- RES-007: 中断中に入力内容変更で `INPUT_CHANGED`、旧新混在なし。
- RES-012: resume二重起動でもwriterは1つ。

add/update:

- ADD-001: 新規source追加で全componentの増分一致。
- ADD-002: 同じsource/path/content再追加は完全no-op。
- ADD-003: 同一identityの内容短縮/拡大で旧chunk/vector/index残存0。
- ADD-006: slash、NFC/NFD、case、CRLF/LF差でOS間件数が変わらない。
- ADD-008: 各cross-store境界でkillし、rerun後clean addと一致。
- ADD-010: add二重/add+rebuildはmutual exclusion。
- ADD-011: daemonがDB open中でも特にWindowsで旧/新一貫。
- ADD-014: db_hashは公開時に一度だけ変化。

status/rebuild/integrity:

- statusは未作成、create直後、building、interrupted、ready、failed、recoverableを区別。
- statusはSQLite/ChromaのUID差分、VERSION/state不一致、events末尾partialを検出。
- `rebuild_component.py --component lexical` はChroma UID、embedding、本文を変更しない。
- rebuildは連続2回で件数/hash/tmpが変化しない。
- 破損注入はDBコピーに対して行い、本番3DBを直接変更しない。

## 12. 入力文書・filesystem fixture

build系は本番DBだけでなく、一時corpusで以下を確認する。

- PDF: 通常、2段組、表、header/footer、画像のみ、暗号化、破損
- Word: 見出し、表、header/footer、破損zip
- PowerPoint: title、本文、notesの対応範囲、破損
- Markdown: heading、front matter、code fence、長いtable
- txt: UTF-8、BOM、LF/CRLF、空、巨大1行
- source/config: snake_case、camelCase、qualified name、JSON/YAML/TOML/XML
- 同一内容の別path、同名別directory
- 日本語・空白・括弧・`#`・`&` を含むfilename
- NFC/NFD
- case差だけのpath
- 深い階層とWindows長path境界
- symlink/junction loop、root外link
- Office一時file、hidden file、binary file

抽出非対応形式や画像-only PDFをどう扱うかは、skip・warning・失敗のいずれかを先に固定する。無言で成功し、対象文書が消える動作は禁止する。

## 13. Mac/Windows固有・DB移送

| ID | 試験 |
|---|---|
| XOS-001 | 同一source snapshotを両OSでbuildし、docs/chunks/UID/hash比較 |
| XOS-002 | CRLF/LF、BOM差 |
| XOS-003 | 日本語filenameのNFC/NFD |
| XOS-004 | case差fileのvalidationが同じ |
| XOS-005 | HOME/rootに空白・日本語・長path |
| XOS-006 | symlink/junction loop |
| XOS-007 | Windowsでdaemonが開いたDBのadd/rebuild/swap |
| XOS-008 | timezone/locale/mtime変更でUID/hash不変 |
| XOS-009 | Mac生成DBをWindowsでoffline検索 |
| XOS-010 | Windows生成DBをMacでoffline検索 |
| XOS-011 | `rfc-full-20k-rag`のクロスOS移送 |
| XOS-012 | 移送後もDense候補が生成され、silent lexical-onlyにならない |

クロスOSのChroma永続形式を保証しない場合は、manifestで非互換を検知し、destination側でDense再構築を要求する。成功表示のままDenseだけ無効になる状態はP0不具合。

## 14. ベクトル有効性・検索品質gate

品質gateは同じ最終context token budgetで比較する。

| 指標 | 合格基準 |
|---|---|
| 一意Exact | Hit@1 = 100% |
| 一意filename/path | Hit@1 = 100% |
| 希少phrase | Hit@5 = 100% |
| Dense rescue | 用意した全ケースでgoldが最終contextに残る |
| Semantic subset | HのContext RecallがLを明確に上回る |
| Vector harm | Lで成功しHで失敗する割合5%以下を目標 |
| 回帰 | 承認済みbaselineからgold脱落なし。変更は個別承認 |
| RRF | explainからの外部再計算と一致 |
| Evidence density | 前版から悪化しない |

## 15. 性能・リソース

性能測定条件:

- AC電源、固定power mode
- warm-up 5回
- query class x DBをwarmで最低50回
- process-cold 10回
- system-cold 3回
- query順をrandomize
- stdoutはfile/pipeへ出しterminal描画を除外
- client wall、queue、model load、embedding、Dense、Lexical、fusion、postprocessを分離

仮SLO:

| 指標 | 仮gate |
|---|---|
| `list_dbs` warm | p95 1秒以下、model loadなし |
| daemon warm ac/incident | p95 2.5秒以下 |
| daemon warm rfc-full-20k | p95 5秒以下 |
| cold first query | p95 20秒以下、最大30秒以下 |
| explain overhead | 通常JSON比20%以内 |
| daemon idle CPU | 1 core換算平均1%未満 |
| 全DB巡回後RSS | 1.5GiB以下を仮上限 |
| memory leak | 50回後から1000回後の増加が100MiBまたは10%以内 |
| handle/thread leak | 増加20個または10%以内 |
| concurrency 4 | error 0、p95はsingleの4.5倍以下 |
| queryによるDB成長 | 論理内容・永続サイズの増加なし |

## 16. 長時間・負荷

Soak:

- SOAK-001: 8時間または10,000 query、unexpected error 0。
- SOAK-002: ac 50%、incident 30%、rfc 20%をrandom、DB混線0。
- SOAK-003: concurrency 1 + 定期4/8 burst、invalid JSON/timeout 0。
- SOAK-004: 3時間以上idle -> 終了 -> 再検索、正常再起動。
- SOAK-005: daemon kill/restart 50回、stale endpoint 0。
- SOAK-006: sleep/resume 3回、zombie/永久停止0。
- SOAK-007: 3DB切替反復でRSS/handleが収束。

## 17. `--explain`に必要な観測情報

最終試験の診断性を確保するため、少なくとも以下を返せるようにする。

- output schema version
- request ID
- DB name/hash/schema/tokenizer/generation
- client/daemon version、cold/warm、cache hit
- model ID/checksum、ONNX provider、INT8、thread設定
- query文字数/token数/hash、truncation
- retrieverごとのstatus、candidate数、ID、rank、raw score
- Exact matched term/alias/field
- Metadata matched path/title
- RRF weight、rank、寄与、合計
- dedup/diversityで除外した理由
- neighbor追加理由とseed UID
- chunk token数、packing累計、除外理由
- degraded component
- queue/model load/embed/Dense/Lexical/Exact/fusion/postprocess/total時間

最終resultが「どのretrieverから入り、どの段階で残ったか」を追跡できない場合、検索品質の失敗を切り分けられない。

## 18. 追加推奨I/F

Copilot向け主I/Fは増やさない。運用・QA専用に以下を追加する。

```text
python ~/.copilot/rag/gen_db/status.py --db <db> --verify
python ~/.copilot/rag/gen_db/status.py --db <db> --verify --deep
```

`--verify`:

- SQLite quick/integrity check
- SQLite/Chroma UID集合差
- FTS/posting/file lookupの孤児
- VERSION/state generation
- model/profile mismatch

`--verify --deep`:

- 全content hash
- vector metadata
- 全参照

再構築対象:

```text
rebuild_component.py --component dense
rebuild_component.py --component exact
rebuild_component.py --component file
```

repairを作る場合、初期値は必ずdry-runとする。

```text
repair_db.py --db <db> --dry-run
repair_db.py --db <db> --apply
```

## 19. 実行順と中断条件

Day 0: 契約・fixture固定。

1. 対応OS/version/architecture/shellを確定。
2. 3DBの件数・hashをsnapshot。
3. 114 gold queriesをfreeze。
4. 一時fixtureとfailpointを準備。
5. 性能基準機を固定。

Day 1: 早期P0。

1. Git clean、DB一覧、削除済みDB非表示。
2. 両OSで3DBの最小検索。
3. JSON stdout純度。
4. argv/stdin一致。
5. daemon/no-daemon一致。
6. ac -> incident -> rfc -> ac。
7. 8同時cold start。
8. Dense rescue。

ここでP0が出たら後続の長時間試験を止めて修正する。

Day 2: 検索組み合わせ。

1. IF-CART 72件。
2. PAIR 24件。
3. TRI-DBQ 72件。
4. retriever/RRF/Postprocess。
5. メタモルフィック。
6. Mac/Windows差分。

Day 3: ライフサイクル。

1. setup/create。
2. build/resumeの全durable境界。
3. add/update。
4. lexical rebuild。
5. status/verify。
6. 破損fixture。

Day 4: 競合・性能。

1. daemon/add/rebuildのWindows競合。
2. クロスOS DB移送。
3. latency/RSS。
4. concurrency 2/4/8。

Night:

1. 実時間3時間idle。
2. 8時間または10,000 query soak。
3. daemon restart 50回。

## 20. リリース判定

以下が1件でもあればリリース不可。

- crash、未処理traceback、hang
- JSON parse失敗、stdoutへのlog混入
- 違うDBのchunk混入
- 一意Exact/filenameのtop1失敗
- argv/stdin、daemon/no-daemon、explain有無で同一OSの結果不一致
- Denseが黙って無効化
- RRF再計算不一致
- neighborが別documentへ越境
- duplicate chunk出力
- token budget超過
- build/add/rebuild後のSQLite/Chroma/FTS/state不一致
- resume後の重複・欠落
- 更新後の旧chunk/vector/posting残存
- writer二重起動によるlost update
- daemon複数常駐、stale endpointに手動削除が必要
- 削除済み`rfc-2000-rag`の再出現
- queryによるDB内容変更・永続肥大化
- 1DB破損で他DBも検索不能
- クロスOSで成功表示しながらDenseだけ無効
- soakで継続的メモリ増加
- Git管理対象がdirty

最終サインオフ表:

| Gate | Mac | Windows | 証跡 | 判定 |
|---|---|---|---|---|
| 3DB baseline / hash |  |  |  |  |
| I/F 72 |  |  |  |  |
| 検索組み合わせ |  |  |  |  |
| Dense rescue / RRF |  |  |  |  |
| daemon / concurrency |  |  |  |  |
| build / resume |  |  |  |  |
| add / rebuild |  |  |  |  |
| corruption / recovery |  |  |  |  |
| cross-OS |  |  |  |  |
| performance |  |  |  |  |
| soak / idle |  |  |  |  |
| Git clean |  |  |  |  |

## 21. 最優先10ケース

時間が足りなくなっても、次は省略しない。

1. `rfc-2000-rag`がlistにもdaemon cacheにも現れない。
2. JSON stdoutへlog/model loadが1文字も混ざらない。
3. 同一queryのargv/stdin、daemon/no-daemon、explain有無が一致。
4. ac -> incident -> rfc -> acで最後のacが最初と一致。
5. 8 process同時cold start後もdaemonが1つ。
6. Dense rescueが各DBで成立し、最終packingまでgoldが残る。
7. RRF値をexplainから外部再計算して一致。
8. SQLite commitとChroma persistの間でkillし、resume後にUID集合一致。
9. WindowsでdaemonがDBを開いたままadd/rebuildし、旧新混在しない。
10. `rfc-full-20k-rag`を含む長時間切替でRSSが収束し、誤DB応答が0。

## 22. Identifier / Exact偽陽性の水平展開

### 22.1 対象となる欠陥クラス

発見済みの事象:

```text
query: A2W
query alias: a2

DB term: A2L
DB alias: a2

a2同士が一致
  -> exact signal
  -> RRF加点
  -> anchor_rescue
  -> neighbor展開
  -> A2Wを含まない文書が最終contextへ入る
```

これは `A2W` 固有の問題ではなく、「完全なidentifierから情報を削って作ったaliasをExactとして扱う」欠陥である。compact IDだけをquery側で例外処理する修正は不十分とみなす。

本節では、次の各段階を独立に検査する。

1. queryからのidentifier抽出
2. exact dictionary lookup
3. posting取得
4. seed本文・metadataでの完全一致再検証
5. RRFへのsignal受渡し
6. anchor eligibility
7. anchor rescue
8. neighbor展開
9. token packing
10. prompt/JSONでの「見つかった/見つからない」の表現

### 22.2 Exactの推奨契約

Exactの目的は再現率ではなく、強い証拠を高精度で返すことである。表記揺れや類似語はBM25とDenseが担当する。

許可するmatch:

| match kind | 条件 | Exact signal | anchor eligible |
|---|---|---:|---:|
| `raw_exact` | 完全なidentifierが文字境界付きで一致 | 強 | Yes |
| `nfc_exact` | Unicode NFCだけを適用して全長一致 | 強 | Yes |
| `casefold_exact` | 全長casefold一致 | 弱または別signal | 原則No |
| `explicit_alias` | 出典付きで明示登録された同義語 | 弱 | 原則No |
| `metadata_exact` | 完全なfilename/path/title | Metadata強 | 規則次第 |

casefold_exactをanchor対象にする場合でも、casefold keyが一意で、identifier種別がcase-insensitiveと判定できる場合に限定する。一般の関数名・型名は大小文字が意味を持ち得るため、単にDB内で一意というだけでは強Exactにしない方が安全。

禁止するmatch:

- prefix切出し
- suffix切出し
- 共通数字部分
- token途中までの短縮
- internal punctuationの削除・置換
- snake/camelの部分語
- dotted nameの一部
- path segmentの一部
- substring
- stemming、単複変換
- 自動生成略称
- alias同士の推移的照合

基本不変条件:

```text
Exactとして返した全candidateについて:

normalized_full_query_identifier
    ==
normalized_full_index_identifier
```

さらに、posting先の該当fieldに完全なquery identifierが文字境界付きで存在することを確認する。旧DBのalias indexが残っていても、このraw verificationを最後の防壁にする。

### 22.3 判定用の共通field

`--explain` またはQA harnessは、Exact candidateごとに次を記録する。

```text
query_identifier
normalized_query_identifier
canonical_term
match_kind
matched_field
term_id
posting_chunk_uid / doc_id
casefold_collision_count
raw_boundary_verified
exact_rank
rrf_contribution
anchor_eligible
anchor_rescue
neighbor_seed_uid
drop_reason
```

query単位:

```text
extracted_identifiers
matched_identifiers
unmatched_identifiers
identifier_dominant
exact_candidate_count
related_context_only
```

### 22.4 一時micro fixture

本番DBへ合成語を追加せず、試験中だけ存在するmicro fixtureへ次を配置する。各identifierは原則として別chunkへ置き、明示した場合を除き、似た文字列を同じchunkへ置かない。

| fixture | 保存するidentifier |
|---|---|
| `FX-PREFIX` | `A2L`, `ABC123X`, `ERR-42-A`, `RFC9110` |
| `FX-SIBLING` | `ABC123Y`, `ERR-42-B`, `RFC9111` |
| `FX-DELIM` | `API.v1`, `API-v1`, `API_v1`, `API/v1` |
| `FX-CODE` | `getUser`, `getUsers`, `get_user`, `pkg.mod.func`, `pkg.mod2.func` |
| `FX-NUM` | `ERR-1204`, `ERR-1205`, `v1.2.3`, `v1.2.30`, `10.0.0.1`, `10.0.0.10` |
| `FX-HEX` | `0xA2`, `0xA2F`, `A2F` |
| `FX-UUID` | 末尾1文字だけ異なる2つのUUID |
| `FX-CASE` | `Foo`, `foo`, `FOO`, `UniqueCase42` |
| `FX-UNICODE` | NFC `cafe42` 相当、NFD等価形、全角 `Ａ２Ｌ`, Cyrillic `А2L` |
| `FX-CONTAIN` | `XABC-123Y`, `myA2Lvalue`, `prefixERR-42-Asuffix` |
| `FX-PATH` | `config/app.yml`, `config-app.yml`, `app.yml` |
| `FX-REPEAT` | 同じ `REP-777` を1 chunk内に100回 |
| `FX-MULTI` | `MULTI-101` と `MULTI-202` を別々の根拠spanへ配置 |

fixtureに存在しないquery:

```text
A2W
ABC123Z
ERR-42-C
RFC9112
API:v1
getUse
pkg.mod3.func
ERR-1206
v1.2.4
10.0.0.11
0xA2E
MULTI-303
```

fixture生成後に、これらが本文、metadata、canonical dictionaryのいずれにも存在しないことを事前assertする。

### 22.5 Prefix・Suffix・包含衝突

| ID | DBにある値 | query | Exact期待 | anchor |
|---|---|---|---|---|
| `XID-PFX-001` | `A2L` | `A2W` | 0 | false |
| `XID-PFX-002` | `ABC123X` | `ABC123Z` | 0 | false |
| `XID-PFX-003` | `ABC123X` | `ABC123` | 0 | false |
| `XID-PFX-004` | `ABC123` | `ABC123X` | 0 | false |
| `XID-PFX-005` | `ERR-42-A/B` | `ERR-42-C` | 0 | false |
| `XID-PFX-006` | `RFC9110/9111` | `RFC911` | 0 | false |
| `XID-PFX-007` | `RFC9110/9111` | `RFC9112` | 0 | false |
| `XID-SFX-001` | `PROD-42`, `TEST-42` | `DEV-42` | 0 | false |
| `XID-SFX-002` | `pkg.mod.func` | `mod.func` | 0 | false |
| `XID-SFX-003` | `ERR-42-A` | `42-A` | 0 | false |
| `XID-CONT-001` | `XABC-123Y` | `ABC-123` | 0 | false |
| `XID-CONT-002` | `myA2Lvalue` | `A2L` | 0 | false |
| `XID-CONT-003` | `prefixERR-42-Asuffix` | `ERR-42-A` | 0 | false |
| `XID-CONT-004` | standalone `A2L` と `myA2Lvalue` | `A2L` | standalone postingだけ | true |

`XID-CONT-004` は、substring occurrenceをpostingへ混ぜず、文字境界付きoccurrenceだけがExactになることを確認する。

### 22.6 数字・Version・区切り文字

| ID | DBにある値 | query | Exact期待 |
|---|---|---|---|
| `XID-NUM-001` | `ERR-1204/1205` | `ERR-1206` | 0 |
| `XID-NUM-002` | `ERR-1204` | `1204` | `1204`が独立termでない限り0 |
| `XID-NUM-003` | `v1.2.3` | `v1.2.4` | 0 |
| `XID-NUM-004` | `v1.2.3` | `v1.2` | 0 |
| `XID-NUM-005` | `v1.2.3` | `v1.2.30` | 0 |
| `XID-NUM-006` | `10.0.0.1` | `10.0.0.10` | 0 |
| `XID-HEX-001` | `0xA2F` | `0xA2` | `0xA2`が別termならそのpostingだけ |
| `XID-HEX-002` | `0xA2F` | `A2F` | `A2F`が別termでない限り0 |
| `XID-DEL-001` | `API.v1` | `API-v1` | `API-v1`自身のpostingだけ |
| `XID-DEL-002` | `API.v1` | `API_v1` | `API_v1`自身のpostingだけ |
| `XID-DEL-003` | `API.v1` | `API/v1` | `API/v1`自身のpostingだけ |
| `XID-DEL-004` | `API.v1` | `API:v1` | 0 |
| `XID-DEL-005` | `ERR_42` | `ERR-42` | 互いに一致しない |
| `XID-DEL-006` | `pkg.mod.func` | `pkg/mod/func` | 互いに一致しない |

internal punctuationの正規化はExactでは行わない。path separatorの統一が必要な場合はMetadata retrieverの規則として独立させる。

### 22.7 Code Identifier

| ID | DBにある値 | query | Exact期待 |
|---|---|---|---|
| `XID-CODE-001` | `getUser` | `getUsers` | `getUsers`自身だけ |
| `XID-CODE-002` | `getUser` | `getUse` | 0 |
| `XID-CODE-003` | `getUser` | `get` | 0 |
| `XID-CODE-004` | `getUser` | `User` | 0 |
| `XID-CODE-005` | `getUser` | `get_user` | `get_user`自身だけ |
| `XID-CODE-006` | `pkg.mod.func` | `pkg.mod2.func` | 対応する完全termだけ |
| `XID-CODE-007` | `pkg.mod.func` | `pkg.mod3.func` | 0 |
| `XID-CODE-008` | `Class.method` | `method` | standalone `method`がなければ0 |

camelCase、snake_case、qualified nameを分解した構成語は、BM25候補には使用してもExact signalに昇格させない。

### 22.8 Case・Unicode

| ID | 条件 | query | 期待 |
|---|---|---|---|
| `XID-CASE-001` | `UniqueCase42`だけ存在 | 同一表記 | `raw_exact`、anchor可 |
| `XID-CASE-002` | `UniqueCase42`だけ存在 | `uniquecase42` | `casefold_exact`、原則anchor不可 |
| `XID-CASE-003` | `Foo/foo/FOO`が存在 | `Foo` | raw `Foo`だけを強Exact |
| `XID-CASE-004` | `Foo/foo/FOO`が存在 | `fOo` | casefold曖昧。強Exact 0 |
| `XID-UNI-001` | NFC `cafe42`相当 | NFD等価形 | `nfc_exact` |
| `XID-UNI-002` | ASCII `A2L` | 全角 `Ａ２Ｌ` | NFKCを仕様にしない限り0 |
| `XID-UNI-003` | Latin `A2L` | Cyrillic `А2L` | 0 |
| `XID-UNI-004` | `A2L` | zero-width内包 | silent除去せず0または入力警告 |
| `XID-UNI-005` | `A2L` | `A 2 L` | 0 |

Unicode confusableを同一identifierへ寄せない。NFKCを採用する場合はcompatibility文字の衝突試験を別途行い、raw一致より低いsignalとして扱う。

### 22.9 Wrapper・自然文への埋込み

次は同じ `A2L` postingへ到達してよい。wrapperはquery tokenの外側だけを除去する。

| ID | query | 期待 |
|---|---|---|
| `XID-WRAP-001` | `A2L` | raw exact |
| `XID-WRAP-002` | `"A2L"` | raw exact |
| `XID-WRAP-003` | `(A2L)` | raw exact |
| `XID-WRAP-004` | ``A2L`` | raw exact |
| `XID-WRAP-005` | `A2Lについて教えて` | raw exact |
| `XID-WRAP-006` | `A2Lとは？` | raw exact |
| `XID-WRAP-007` | `What is A2L?` | raw exact |
| `XID-WRAP-008` | 複数行code block内の`A2L` | raw exact |

各変形で、queryから抽出されたidentifierが完全な `A2L` であり、`A2` や `2L` がlookup keyへ追加されていないことをassertする。

### 22.10 複数identifier

| ID | query | 期待 |
|---|---|---|
| `XID-MULTI-001` | `A2LとA2Wの違い` | A2L matched、A2W unmatched |
| `XID-MULTI-002` | `MULTI-101とMULTI-202` | 2 identifierを別々にmatch |
| `XID-MULTI-003` | `MULTI-101とMULTI-303` | 101 matched、303 unmatched |
| `XID-MULTI-004` | `A2L A2L A2L` | posting/RRFを三重加点しない |
| `XID-MULTI-005` | `A2L`がbodyとheadingに存在 | field provenanceは複数、chunkは重複しない |
| `XID-MULTI-006` | bodyのID + filenameのID | ExactとMetadataを区別 |
| `XID-MULTI-007` | 2つの有効IDの根拠が別文書 | budget内に両根拠を残す |
| `XID-MULTI-008` | 有効IDが3つ、budget不足 | どのIDの根拠を落としたか明示 |

query全体を単純な `exact_hit=true/false` だけで表さない。identifierごとのmatched/unmatched状態を保持する。

### 22.11 Identifier No-Hit Guard

`A2Wに関する情報を教えて` のように、queryの中心が存在しない強identifierの場合、一般語のLexicalや近いDense候補を「A2Wの根拠」として返さない。

identifier-dominant:

| ID | query | 期待 |
|---|---|---|
| `XID-NOHIT-001` | `A2Wとは` | unmatched、Exact 0、anchor false |
| `XID-NOHIT-002` | `A2Wについて教えて` | 同上 |
| `XID-NOHIT-003` | `What is A2W?` | 同上 |
| `XID-NOHIT-004` | quoted `A2W`だけ | 同上 |
| `XID-NOHIT-005` | `A2W` + 大量の一般的依頼文 | 同上 |

推奨出力:

```text
DB内では「A2W」の完全一致を確認できませんでした。
```

通常evidenceを0件にするか、関連候補を別field `related_context` へ隔離する。少なくとも、Lexical/Dense候補をA2Wそのものの根拠として表示しない。

identifier + 実質的topic:

| ID | query | 期待 |
|---|---|---|
| `XID-NOHIT-101` | `A2Wの冷媒規制について` | A2W unmatched。冷媒規制の関連候補はrelated扱い |
| `XID-NOHIT-102` | `A2Wとインバータ効率の関係` | A2W未確認を明示し、topic候補と区別 |
| `XID-NOHIT-103` | `A2W エラーコード 1204` | 抽出した各identifierの成否を個別表示 |

No-hit guardが強すぎて、query内の別の有効なtopicやidentifierまで捨てないことも確認する。

### 22.12 RRF・Anchor・Neighborへの伝播

| ID | 注入条件 | 合格条件 |
|---|---|---|
| `XID-FUS-001` | Lexicalが`A2L`文書を返すがqueryは`A2W` | Exact寄与0 |
| `XID-FUS-002` | Denseが`A2L`文書をtop1にする | Exact寄与0、anchor false |
| `XID-FUS-003` | legacy alias lookupが`A2L` postingを返す | raw verificationでdrop |
| `XID-FUS-004` | false alias候補がLexicalにも存在 | Exact weightを加算しない |
| `XID-FUS-005` | strict `A2L`がExact + Dense | 正しくRRF合算 |
| `XID-FUS-006` | casefold-only候補 + Dense | 強anchorに昇格しない |
| `XID-FUS-007` | ambiguous casefold + Lexical | strong Exact 0 |
| `XID-FUS-008` | 同じpostingを複数alias経路で返す | 有効なfull match1回だけ |
| `XID-FUS-009` | false seedの前後に高関連文 | seed drop後neighborも0 |
| `XID-FUS-010` | valid seedが最終budget外になりそう | strong anchor規則どおり保持 |
| `XID-FUS-011` | neighbor本文にはIDがない | neighborをExact occurrenceと表示しない |
| `XID-FUS-012` | chunkがseedかつ別seedのneighbor | score二重加算なし、理由は両方保持 |
| `XID-FUS-013` | cold lexical fast path | full identifierのraw boundary確認前にanchorしない |
| `XID-FUS-014` | daemon warm cacheに旧alias map | policy/generation差でinvalidate |

必須不変条件:

```text
unmatched identifierから:
  exact RRF contribution = 0
  anchor_eligible = false
  anchor_rescue = false
  neighbor expansion = 0
```

### 22.13 Field別の誤帰属

| ID | 条件 | 期待 |
|---|---|---|
| `XID-FIELD-001` | IDがbodyだけに存在 | `matched_field=body` |
| `XID-FIELD-002` | IDがheadingだけに存在 | heading postingだけ |
| `XID-FIELD-003` | IDがfilenameだけに存在 | Metadata signal。body Exactにしない |
| `XID-FIELD-004` | IDがpath segmentだけに存在 | Metadata規則だけ |
| `XID-FIELD-005` | 同じIDがbody/title/pathに存在 | result重複なし、field provenance保持 |
| `XID-FIELD-006` | basenameの一部だけ一致 | 完全filename Exactにしない |
| `XID-FIELD-007` | IDがneighborにだけ存在 | Exact seedは実際のoccurrence chunk |

### 22.14 Occurrence・DF・Score増幅

| ID | 条件 | 期待 |
|---|---|---|
| `XID-OCC-001` | 同じIDを1 chunkに100回 | candidateとRRF加点は1回 |
| `XID-OCC-002` | 同じIDが隣接3 chunk | postingは3件、final重複なし |
| `XID-OCC-003` | 同じIDが10文書 | candidate cap、文書多様化が正常 |
| `XID-OCC-004` | raw exactとcasefold exactが同じtermへ到達 | 強いmatch kindを1回だけ採用 |
| `XID-OCC-005` | identifier辞書に重複term row | integrity checkで検出 |
| `XID-OCC-006` | posting重複 | score増幅せずverifyで検出 |
| `XID-OCC-007` | posting先本文からIDを除去 | raw verificationでdrop、不整合として診断 |

### 22.15 Property-Based / Mutation試験

固定seedでidentifierを自動生成する。

```text
英字のみ
英字+数字
英字-数字
英字_数字
dotted.name
qualified::name
path/name.ext
camelCase
snake_case
version-like
UUID-like
hex-like
```

各canonical identifierへ次のmutationを1つずつ適用する。

- 1文字置換
- 1文字追加
- 1文字削除
- 隣接文字入替
- 末尾数字±1
- prefix追加
- suffix追加
- prefix切捨て
- suffix切捨て
- delimiter変更
- delimiter削除
- case変更
- Unicode confusable置換
- zero-width文字挿入
- wrapper追加

mutation後のtermが実在する場合はnegative testとして無効なので再生成する。

Property:

```text
P1: wrapperとNFC以外のmutationはraw Exactにならない
P2: Exact candidateのcanonical full keyはquery full keyと一致
P3: Exact postingの該当fieldに文字境界付きoccurrenceが存在
P4: negative mutationのExactFPRは0
P5: negative mutationのAnchorFPRは0
P6: queryを反復しても結果・drop reasonが同じ
P7: insertion orderを変えてindex buildしても論理結果が同じ
P8: daemon/no-daemonで同じ
P9: Mac/Windowsで同じ
```

micro fixtureで最低10,000 mutationを実行する。Exact lookupだけなら軽いため、通常のCIにも入れやすい。Full hybridは代表300件程度へ絞る。

意図的にprefix aliasを再有効化したmutation buildを1回作り、このsuiteが確実にFAILすることも確認する。テスト自体が欠陥を検出できるかの検査である。

### 22.16 現行3DBからの自動水平展開

合成fixtureだけでなく、各本番DBのcanonical identifierから層化抽出する。

層:

- 文字数2-3、4-8、9以上
- df=1、df=2-5、高df
- 英数字、hyphen、underscore、dot、slash、camel、version、filename
- body、heading、metadata

各DBから最低100 termを抽出し、termごとに次の不存在queryを生成する。

1. 末尾1文字置換
2. 末尾数字±1
3. 1文字削除
4. prefix切捨て
5. suffix追加
6. delimiter変更

生成queryがcanonical dictionaryや本文に実在しないことを事前確認する。

```text
3 DB x 100 terms x 6 mutations
= 1,800 negative Exact tests
```

追加:

- 各DB100件のcanonical positive test: 300件
- 各DB50件のcase variant: 150件
- 各DB30件をFull hybridで実行: 90件
- 各DB10件をdaemon cold/warm/no-daemonで実行: 90件

合否:

- negative 1,800件のExact candidate 0
- negative 1,800件のanchor rescue 0
- positive 300件のExact candidate recall 100%
- posting本文のraw boundary verification 100%
- Full hybridでfalse Exact signal 0

### 22.17 旧alias DBからの移行

自動aliasを廃止する場合は、runtime修正だけでなくindex policyをversion管理する。

推奨metadata:

```text
identifier_policy_version: strict-full-v2
exact_generation: <generation>
generated_aliases: false
```

移行試験:

| ID | 試験 | 合格条件 |
|---|---|---|
| `XID-MIG-001` | 旧DB + 新runtime | lossy aliasをlookupしない、またはraw verifyでdrop |
| `XID-MIG-002` | exact component side-by-side rebuild | query中は旧完全世代かBUSY |
| `XID-MIG-003` | alias dictionary/posting/index削除 | Exact positiveを維持、negative FPR 0 |
| `XID-MIG-004` | rebuild中kill | 旧/新の完全世代。混在なし |
| `XID-MIG-005` | publish直前/直後kill | resume後policy/generation一致 |
| `XID-MIG-006` | new DB + 旧runtime | mutationを拒否。unsafe alias復活なし |
| `XID-MIG-007` | daemonが旧exactをcache | db_hash + policy + generationでinvalidate |
| `XID-MIG-008` | no-op exact rebuild | UID、本文、vector、論理hash規則どおり |
| `XID-MIG-009` | 3DBをrebuild | Chroma UID/embedding不変 |
| `XID-MIG-010` | alias table/index除去前後 | catalogサイズが増えない |
| `XID-MIG-011` | Windowsでdaemon open中にpublish | lockで半端な世代を作らない |
| `XID-MIG-012` | Mac/Windowsで同じ入力をrebuild | canonical term/posting集合一致 |

物理schemaを変更するならschema versionを上げる。schemaを維持する場合でも、少なくとも `identifier_policy_version` を必須にする。

### 22.18 A2WとA2Lの正式な位置付け

旧 `AC_EXACT_001` は削除せず、役割を分離する。

Negative regression:

```text
ID: AC_EXACT_NEG_COLLISION_001
query: A2Wに関する情報を教えて

precondition:
  A2Wは本文、metadata、canonical identifierに存在しない
  A2Lは存在する

expected:
  extracted_identifiers = [A2W]
  exact_candidate_count = 0
  unmatched_identifiers = [A2W]
  exact RRF contribution = 0
  anchor_rescue = false
  A2L posting由来のneighbor = 0
  A2Wの根拠として通常contextを返さない
```

Positive low-DF:

```text
ID: AC_EXACT_LOWDF_001
query: A2Lに関する情報を教えて

expected:
  matched term = A2L
  match_kind = raw_exact
  seed fieldに文字境界付きA2Lが存在
  exact candidate rankが規定内
  anchor eligibilityが規則どおり
  neighborには元seed UIDが付く
  同じchunkをexact/neighborで二重加点しない
```

### 22.19 Release Gate

次は1件でもあればリリース不可。

- 存在しない近似identifierにExact candidateが1件以上
- query identifierより短いalias keyでExact lookup
- DB側の短縮alias経由でpostingへ到達
- seed本文・該当metadataに完全termがないのにExact signal
- casefold曖昧termを強Exactまたはanchor扱い
- unmatched identifierからRRF Exact寄与が発生
- unmatched identifierからanchor rescueまたはneighbor展開
- identifier-dominant no-hit queryへ、無関係contextを対象IDの根拠として返す
- 同じoccurrenceや複数alias経路によるscore二重加算
- exact policy変更後もdaemonが旧alias cacheを使用
- exact rebuildでvector、本文、chunk UIDが変化

計測する品質指標:

```text
Exact False Positive Rate
= negative queryでExact candidateが出た件数 / negative query総数
目標: 0

Anchor False Positive Rate
= negative queryでanchor_rescueした件数 / negative query総数
目標: 0

Raw Verification Failure
= Exact candidateだが該当fieldに完全termがない件数
目標: 0

Positive Exact Recall
= canonical positive queryで正しいpostingを取得した件数 / positive総数
目標: 100%
```

### 22.20 Dense/BM25 Baitを置いたNo-Hit攻撃試験

単に「Exactが0件」を確認するだけでは弱い。DenseとBM25が誤った候補を強く返すfixtureを意図的に作り、それでも最終contextへ入らないことを確認する。

一時fixture:

| chunk | 内容 |
|---|---|
| `G1-C0` | 対象serviceの一般説明 |
| `G1-C1` | `ERR-4207: handshake timeout caused by a stale token.` |
| `G1-C2` | `Remediation: rotate the token and restart the worker.` |
| `G2-C0` | `ERR-4208`とほぼ同じ説明 |
| `G3-C0` | `ERR-42070`。substring衝突用 |
| `G4-C0` | `handshake timeout stale token error`を高頻度で含むLexical bait。IDなし |
| `G5-C0` | 「認証情報が古くハンドシェイクがタイムアウトする」というDense bait。IDなし |
| `G6-C0` | pathだけが`runbooks/ERR-7311.md` |
| `G7-C0` | `pkg.auth.RefreshToken()`を本文に持つ |
| `G8-C0` | postingに`ZZQ-9F7C2D`があるがraw textにはないstale posting |
| `G9-C0` | raw textに`CFG.retry_limit_v2`があるがposting欠落 |
| `G10-C0` | G1と同じchunk_indexを持つ別document |

攻撃fixtureの成立条件:

- `ERR-4209` に対し、Dense baitがDense top3以内。
- 同じqueryに対し、Lexical baitがBM25 top3以内。
- guardを無効化すると、baitがRRF後の通常contextへ入る。

この条件を満たさないfixtureは、no-hit guardを攻撃できていないため試験無効とする。

| ID | query / 条件 | 合格条件 |
|---|---|---|
| `XID-BAIT-001` | `ERR-4209` | `no_evidence`、通常context 0 |
| `XID-BAIT-002` | `ERR-4209のhandshake timeout原因は？` | baitがDense/BM25 topでも通常context 0 |
| `XID-BAIT-003` | `ERR-42070`だけ存在し`ERR-4207`を検索 | substringをanchorにしない |
| `XID-BAIT-004` | `XERR-4207Z`だけ存在 | boundary不一致、no-hit |
| `XID-BAIT-005` | stale posting `ZZQ-9F7C2D` | raw verificationでdrop |
| `XID-BAIT-006` | DenseとBM25が同じbaitをtop1 | RRF scoreに関係なくno-hit |
| `XID-BAIT-007` | IDが別DBだけに存在 | 対象DBではno-hit |
| `XID-BAIT-008` | 存在DBをdaemonでwarm後に非存在DB | cache混線せずno-hit |
| `XID-BAIT-009` | `ERR-4207` + 不存在`ABC-9999` | `partial_evidence`、4207の根拠だけ |
| `XID-BAIT-010` | 不存在IDを2つ | unmatched 2件、通常context 0 |
| `XID-BAIT-011` | posting欠落だがraw textにはID | index不整合を明示。勝手なalias救済なし |
| `XID-BAIT-012` | 極小budget | verified seedなしでneighborだけ返さない |

推奨するno-hit出力契約:

```json
{
  "retrieval_status": "no_evidence",
  "matched_identifiers": [],
  "unmatched_identifiers": ["ERR-4209"],
  "guard_reason": "unmatched_strong_identifier",
  "contexts": []
}
```

no-hitは検索エラーではないためexit codeは0とする。Prompt形式も無関係なcontextを付けず、「指定identifierの根拠なし」を短く返す。

### 22.21 Strong Identifier Guardの過剰発火

No-hit guardを強くすると、通常のsemantic queryまで止める逆方向の不具合が起こり得る。ドメイン語辞書ではなく、構文的な強さで区別する。

強い候補:

```text
ERR-4207
ABC_9F17
A2W
foo_bar_v2
pkg.auth.RefreshToken
config.max_retries
src/auth/token.yaml
UUID / hex / SHA
RFC9110
引用符で明示されたidentifier
```

単独ではhard guard対象にしない候補:

```text
AC
API
error
空調
ブラジル
純粋な短い数字
通常のハイフン語
```

| ID | query | 期待 |
|---|---|---|
| `XID-GUARD-001` | `ACについて` | Exact missだけでsemantic検索を止めない |
| `XID-GUARD-002` | `API設計の方針` | 同上 |
| `XID-GUARD-003` | `空調の効率` | 通常semantic query |
| `XID-GUARD-004` | `2026について` | 純数字だけでhard guardにしない |
| `XID-GUARD-005` | `ERR-4209について` | hard no-hit guard |
| `XID-GUARD-006` | `"API"`と明示引用 | 引用による強指定の仕様どおり |
| `XID-GUARD-007` | semantic queryへ不存在strong IDを追加 | 元の一般contextをIDの根拠として返さない |
| `XID-GUARD-008` | 不存在IDを存在IDへ置換 | no-hitからverified anchor hitへ遷移 |

`XID-GUARD-007/008` は重要なメタモルフィック対である。

### 22.22 Field限定の正規化

あるfieldで安全な正規化を、全identifierへ水平適用してはいけない。

| ID | query | field | 期待 |
|---|---|---|---|
| `XID-FNORM-001` | `src\config.yaml` | path | `/`へ正規化しpath exact可 |
| `XID-FNORM-002` | `src\config.yaml` | API/body | `/`へ変換してAPI exactにしない |
| `XID-FNORM-003` | `config` | filename | stemはfile FTS。Exact/anchor不可 |
| `XID-FNORM-004` | `report.v1` | filename | `.pdf`を補完しない |
| `XID-FNORM-005` | `CONFIG.YAML` | filename | casefold weak、原則anchor不可 |
| `XID-FNORM-006` | basenameが2文書に存在 | filename | 両文書を返し、任意の1件へ決め打ちしない |
| `XID-FNORM-007` | `/api/v1/users/` | API | trailing slashを勝手に削除しない |
| `XID-FNORM-008` | `pkg.Client.getUser()` | code | 外側`()`だけwrapper扱い |
| `XID-FNORM-009` | `getUser` | qualified termだけ存在 | suffix aliasでhitしない |

### 22.23 `add_data`によるCollision発生

build時に安全でも、追加データによりaliasの一意性が崩れる。

| ID | 操作 | 合格条件 |
|---|---|---|
| `XID-ADDCASE-001` | `Foo`だけのDBへ`foo`を追加 | casefold keyをambiguousへ更新 |
| `XID-ADDCASE-002` | 追加後`FOO`を検索 | 任意の1termを強Exactにしない |
| `XID-ADDCASE-003` | raw `Foo`を検索 | raw `Foo`は維持 |
| `XID-ADDCASE-004` | raw `foo`を検索 | raw `foo`は維持 |
| `XID-ADDCASE-005` | `A2`だけのDBへ`A2L`追加 | query `A2`はA2だけ |
| `XID-ADDCASE-006` | `ABC-123`へ`ABC123`追加 | separatorなしaliasを作らない |
| `XID-ADDCASE-007` | collisionを生むadd中にkill/resume | ambiguous状態を片側だけ公開しない |
| `XID-ADDCASE-008` | collision文書を内容更新 | 古いcasefold/collision countを残さない |

### 22.24 Exact Migrationの原子性

旧indexを直接書き換えず、strict indexを別generationで作成し、検証後に公開する。

```text
1. legacy active generationを保持
2. strict temp generationをchunk本文から生成
3. term/posting/alias・raw occurrenceを検証
4. active generationをtransactionで切替
5. index_state、VERSION、hashを公開
6. daemonが次requestで差分を検知
7. success event後に旧generationを回収
```

metadataはschemaとは別に次を持つ。

```text
identifier_policy_version
exact_generation
exact_index_hash
content_hash
dense_index_hash
lexical_index_hash
```

Exact rebuildではcontent_hash、Dense、Lexicalは不変。`exact_index_hash` と、それを含む全体db_hashだけが成功公開時に変化する。

追加failpoint:

```text
EXK-01 migration start event後
EXK-02 temp schema作成後
EXK-03 最初のposting batch commit後
EXK-04 50% checkpoint後
EXK-05 全posting投入後・index作成前
EXK-06 index作成後・validation前
EXK-07 validation後・generation swap前
EXK-08 generation swap直後
EXK-09 index_state更新直後
EXK-10 VERSION/db_hash更新直後
EXK-11 daemon invalidation前
EXK-12 success event直前
```

| ID | 障害 | 合格条件 |
|---|---|---|
| `XID-EXR-001` | EXK-01-07でkill | legacyがactive、再実行で安全に継続 |
| `XID-EXR-002` | EXK-08でkill | authoritative stateからfinalizeまたはrollback |
| `XID-EXR-003` | EXK-09-10でkill | queryがpolicy/hash不一致を検知 |
| `XID-EXR-004` | EXK-11-12でkill | postingを再生成せずfinalize、重複なし |
| `XID-EXR-005` | 5回連続kill/resume | temp/旧generationが増殖しない |
| `XID-EXR-006` | disk full | 旧generationを維持 |
| `XID-EXR-007` | Windowsでdaemon open中 | BUSYまたは協調close/reopen |
| `XID-EXR-008` | rebuild二重起動 | writer 1つ |

daemon cache keyは、少なくとも次を含む。

```text
DB identity
db_hash
exact_generation
identifier_policy_version
```

mtimeだけによるcache invalidationは禁止する。同一秒内のrebuildやatomic replacementを検出できないためである。

### 22.25 旧DBの安全な扱い

policy fieldのないschema 2 DBはlegacyとして認識する。

- list/status/searchだけで暗黙migrationしない。
- legacy alias candidateはraw verificationを通過した場合だけExactへ入れる。
- raw verification不能ならExactをdegraded/disabledにする。
- `--explain`へlegacy policyと無効化理由を出す。
- Dense/BM25は利用可能な範囲で継続できる。
- strict DBを旧writerでadd/rebuildできないようversion guardする。

| ID | 試験 | 合格条件 |
|---|---|---|
| `XID-LEG-001` | policyなしDBをlist | legacy/rebuild推奨として表示 |
| `XID-LEG-002` | legacy DBで`A2W` | `A2L` alias candidateをraw verifyでdrop |
| `XID-LEG-003` | legacy DBのliteral ID | raw verify可能ならExact維持 |
| `XID-LEG-004` | list/status/search前後 | 自動migration・file変更なし |
| `XID-LEG-005` | strict DBを旧writerで更新 | 無変更で拒否 |
| `XID-LEG-006` | future policy DB | Exactを安全に無効化、mutation拒否 |

### 22.26 最終段の安全弁

RRF、dedup、多様化、neighbor、packingの完了後に、次を再検証する。

```text
すべてのstrong identifierがunmatched
  => final normal contextsは空

strong identifierがmatched
  => final contextsにverified anchor seedが最低1件残る
     またはbudget不足/no-evidenceを返す

neighborが存在
  => 対応するverified seedを同じresponseから追跡可能
```

scoreやrankはidentifierの存在証明ではない。DenseとBM25の両方が同じ文書へ合意しても、この不変条件を上書きしない。

## 23. 検索性能評価セットの改善

本節は実装単体試験ではなく、製品としての検索品質を測る性能試験である。`AC_EXACT_001` で起きた問題を、評価データ、指標、レポート、合否判定へ水平展開する。

### 23.1 A2W事例の評価上の扱い

旧評価:

```text
query_type: exact
question: A2Wに関する情報を教えて
top1が返った
exit code 0
=> PASS相当
```

問題:

- positive Exactの事前条件である「A2WがDBに存在する」を確認していなかった。
- top1文書だけを見て、Exact signalの正当性を検証していなかった。
- 存在しないidentifierに対して何か返ることを成功扱いしていた。
- Dense/BM25/Exact/Fusion/Postprocessのどこで誤ったか分離できなかった。

新しい分類:

| test ID | 役割 |
|---|---|
| `AC_EXACT_LOWDF_001` | `A2L`が実在するpositive Exact |
| `AC_EXACT_NEG_COLLISION_001` | `A2W`が不存在で、近い`A2L`へ誤一致しないnegative Exact |
| `AC_NOHIT_STRONGID_001` | A2Wについてシステムが誤った根拠を返さないno-hit |

同じqueryを、channel correctnessとsystem responseの2層で採点する。

```text
Channel:
  A2WにExact signalが付かないか

System:
  A2Wの根拠がないことを正しく扱えるか
```

最終contextに関連文書が入ったかだけでは、Exactの正誤を判断しない。

### 23.2 テストケース事前検証

評価実行前に全ケースをpreflightする。preflight不合格のケースを製品性能の分母へ入れない。

Positive Exact:

- query identifierが原文clean textまたは対象metadataに存在。
- identifier indexにも存在。
- gold documentと文字範囲または根拠文hashがある。
- queryが一意Exactかlow-DFかを記録。
- match fieldをbody/heading/file/pathに分類。

Negative Exact:

- query identifierが原文、clean records、canonical dictionary、metadataの全てに不存在。
- 文字列が似た既存identifierが最低1つ存在。
- 既存identifierとの関係をprefix/suffix/数字違い/delimiter違い等で記録。
- mutation後の文字列が偶然実在した場合は再生成。

Dense semantic:

- gold evidenceは存在。
- queryは原文の単純コピーではなく意味言い換え。
- BM25/Exact/Metadataのcandidate poolだけではgoldが取れないことを事前確認。
- この条件を満たさないqueryをDense rescueの分母へ入れない。

No-answer:

- identifierがないだけでなく、質問への根拠自体がcorpusに存在しない。
- 人手確認を含めてgoldなしを確定。
- 単なる検索失敗をno-answerとして登録しない。

preflight結果:

```text
VALID
INVALID_POSITIVE_NOT_IN_CORPUS
INVALID_NEGATIVE_EXISTS
INVALID_GOLD_SPAN
INVALID_DENSE_NOT_REQUIRED
INVALID_NOANSWER_UNCERTAIN
```

locked test setにinvalid caseが残っていたら、性能測定を開始しない。

### 23.3 Paired Query Family

実在する1つのcanonical identifierから、正例・負例を対にして作る。

例:

```text
canonical: A2L

P1: A2L
P2: A2Lに関する情報を教えて
P3: "A2L"
P4: 仕様で許可するcase variant

N1: A2W       # 1文字置換、不存在
N2: A2        # suffix切捨て、不存在
N3: A2LX      # suffix追加、不存在
N4: A-2L      # delimiter変更、不存在
```

相対的な期待値:

```text
P1/P2/P3:
  同じcanonical Exact postingを取得

N1/N2/N3/N4:
  canonicalへのExact signalは0

P1 -> N1:
  matchedからunmatchedへ変化

N1 -> P1:
  unmatchedからmatchedへ変化
```

このpaired設計により、個別文書名を大量に手書きせず、identifier境界の頑健性を測れる。

### 23.4 評価セットの規模

Fast setはcommitごとの短い回帰用。各DB40問、合計120問。

| class | DBあたり |
|---|---:|
| Exact positive | 8 |
| Exact negative collision | 8 |
| Dense semantic | 8 |
| BM25/phrase | 4 |
| Metadata | 4 |
| Mixed fusion | 4 |
| No-answer | 4 |

Release setは最終リリース用。各DB220問、合計660問。

| class | DBあたり | 合計 |
|---|---:|---:|
| Exact canonical positive | 30 | 90 |
| Exact negative near-collision | 60 | 180 |
| 許可された表記変形 | 15 | 45 |
| Metadata positive | 15 | 45 |
| Metadata negative near-miss | 15 | 45 |
| BM25/phrase | 20 | 60 |
| Dense semantic rescue | 25 | 75 |
| Mixed/RRF | 15 | 45 |
| Multi-evidence | 10 | 30 |
| Semantic no-answer | 10 | 30 |
| Boundary/long query | 5 | 15 |
| 合計 | 220 | 660 |

Nightly generated setでは、各DBの実identifierから合計3,000件以上のnegative mutationを生成する。

- false positive 0/3,000なら、「真のFPRの95%上限」は概算0.1%。
- release setのnegative 0/180だけでは95%上限は概算1.7%。

ゼロ件という結果だけでなく、分母を必ず併記する。

### 23.5 3DBへの層化配分

DBサイズ比例だけにするとRFCが支配するため、基本はDBごとに同数とする。その上で各DB内を層化する。

| 層 | 水準 |
|---|---|
| identifier長 | 2-3、4-8、9以上 |
| document frequency | 1、2-5、高DF |
| 形状 | 英数字、hyphen、underscore、dot、slash、camel、version、filename |
| field | body、heading、file、path |
| language | 日本語質問、英語質問、日英混在 |
| evidence位置 | chunk中央、境界、先頭、末尾 |

DBごとの役割:

- `ac-rag`: 日本語自然文、英語略語、PDF/Office混在。
- `incident-rag`: 英語長文、report identifier、類似事故記述。
- `rfc-full-20k-rag`: RFC番号、API/technical term、大規模candidate競合。

### 23.6 評価プロファイル

利用者向けCLIにはmode選択を追加しない。性能評価harness内部だけでablationする。

| profile | 構成 | 目的 |
|---|---|---|
| `H` | 製品版Hybrid | 最終性能 |
| `L` | BM25 + Exact + Metadata | Denseなしbaseline |
| `V` | Denseのみ | Dense単体能力 |
| `Oracle` | gold evidenceを直接context化 | generator上限 |

主比較:

```text
Vector効果 = H - L
```

VとLの単純勝敗ではなく、HybridへDenseを加えた際の救済と害を測る。

### 23.7 段階別に保存する結果

各queryで次のfunnelを保存する。

```text
query analyzer
  -> Dense candidates
  -> BM25 candidates
  -> Exact candidates
  -> Metadata candidates
  -> RRF
  -> dedup/diversity
  -> neighbor
  -> token packing
  -> final context
```

性能ログ:

```json
{
  "case_validity": "VALID",
  "answerable": true,
  "gold_doc_ids": [],
  "gold_spans": [],
  "expected_exact": false,
  "forbidden_exact_terms": ["A2L"],
  "retrievers": {
    "dense": [],
    "lexical": [],
    "exact": [],
    "metadata": []
  },
  "fusion": [],
  "final_contexts": [],
  "matched_identifiers": [],
  "unmatched_identifiers": ["A2W"],
  "latency_breakdown": {}
}
```

stdout hashだけでは性能評価できない。少なくともchunk UID、doc ID、match kind、各段階の順位を保持する。

### 23.8 検索品質指標

一般retrieval:

- Hit@1 / Hit@5
- MRR@10
- nDCG@10
- Recall@30
- Context Recall@token budget
- All-evidence Success@budget
- Evidence Density
- 重複率
- 同一文書占有率

異なるDBやchunk数を比較するときは、同じtop-kではなく同じ最終token budgetで比較する。

Exact:

```text
Exact Positive Recall
= positive queryで正しいExact postingが出た数 / positive総数

Exact False Positive Rate
= negative queryでExact signalが出た数 / negative総数

False Anchor Rate
= negative queryでanchor rescueした数 / negative総数

Exact Evidence Verification Rate
= Exact候補の該当fieldに完全termが実在した数 / Exact候補総数
```

一意Exactとlow-DFを分ける。

- 一意Exact: Hit@1
- low-DF: gold document Hit@k、posting recall、文書多様性

No-hit / abstention:

- Abstention Precision
- Abstention Recall
- Abstention F1
- False Evidence Rate
- Related-context誤表示率

Exactが0件でもcorpusに意味的回答がある場合と、corpus全体に回答がない場合を混同しない。

Vector:

```text
Vector Rescue Rate
= Lで失敗しHで成功 / Lで失敗

Vector Harm Rate
= Lで成功しHで失敗 / Lで成功

Dense Unique Evidence
= Denseだけがcandidate化したgold数
```

さらにgoldの生存段階を記録する。

```text
Dense topN
RRF後
Postprocess後
Final budget後
```

### 23.9 Failure Taxonomy

失敗をすべて「検索FAIL」にまとめない。

| category | 意味 |
|---|---|
| `TEST_DATA_INVALID` | goldや存在前提が誤り |
| `ANALYZER_MISS` | queryからidentifier/tokenを抽出できない |
| `EXACT_FALSE_POSITIVE` | 不存在IDへExact signal |
| `EXACT_FALSE_NEGATIVE` | 実在IDをExact候補化できない |
| `DENSE_MISS` | Dense candidate poolにgoldなし |
| `LEXICAL_MISS` | BM25 candidate poolにgoldなし |
| `FUSION_DROP` | candidateにはあるがRRF後に脱落 |
| `POSTPROCESS_DROP` | dedup/diversity/neighbor処理で脱落 |
| `PACKING_DROP` | token budget packingで脱落 |
| `NOHIT_FALSE_EVIDENCE` | 回答不能なのに根拠ありとして返却 |
| `WRONG_ABSTENTION` | 根拠があるのにno-hit |
| `OUTPUT_ERROR` | JSON/prompt/citation不正 |
| `PERFORMANCE_REGRESSION` | latency/RSS悪化 |

A2Wの旧結果は次のように記録する。

```text
positive testとして: TEST_DATA_INVALID
negative testとして: EXACT_FALSE_POSITIVE
system responseとして: NOHIT_FALSE_EVIDENCEの可能性
```

「テストケース不正」だけで閉じず、別の有効なnegative評価へ残す。

### 23.10 Before/After比較

alias修正前後で同じlocked setを実行する。

| 指標 | 期待 |
|---|---|
| Exact negative FPR | 低下し0 |
| False Anchor Rate | 0 |
| Canonical Exact Recall | 低下なし |
| 許可variant recall | 仕様範囲内で維持 |
| BM25/Dense Recall | 低下なし |
| Hybrid Context Recall | 低下なし |
| Vector Rescue/Harm | 悪化なし |
| no-hit F1 | 改善 |
| warm p95 | +20%以内 |
| peak RSS | +15%以内 |

同じqueryに対する修正前後のbinary成否はpaired dataとして扱う。件数が十分ならMcNemar検定、指標差はbootstrap confidence intervalを併記する。統計的有意差だけでなく、P0ケースが0件であることを優先する。

### 23.11 性能と品質の同時計測

query classごとにlatencyを分ける。

| class | 注意点 |
|---|---|
| Exact hit | fast pathが効くか |
| Exact miss | 無駄なalias/posting展開がないか |
| Dense semantic | embedding + Chroma時間 |
| Lexical high-DF | candidate爆発 |
| Mixed | 全retriever + RRF |
| No-answer | 無駄な大量context生成がないか |
| Long query | analyzerとtruncation |

測定matrix:

```text
3 DB
x daemon cold / warm / no-daemon
x query class
x Mac / Windows
```

各cell:

- warm-up 5回
- 最低30回
- p50/p95/p99
- peak RSS
- candidate数
- stage timing

query順をrandomizeし、同じ質問の連続実行によるcache偏りを避ける。

### 23.12 End-to-End

retrieval-onlyのrelease setとは別に、各DB30問、合計90問をgeneratorまで通す。

比較:

- No RAG
- Lexical RAG
- Hybrid RAG
- Oracle RAG

同じgenerator、prompt、temperature、context budgetを使う。

指標:

- gold claim correctness
- completeness
- faithfulness
- citation correctness
- abstention correctness

LLM judgeは補助とし、人手で確認したgold claim/spanを一次評価にする。Oracleでも失敗するqueryはgenerator側、Oracleは成功するがHybridで失敗するqueryはretrieval/context側と切り分ける。

### 23.13 Release Gate

Test set品質:

- locked setのinvalid case: 0
- positive caseのgold span欠落: 0
- negative queryの実在混入: 0
- Dense rescue条件未成立: 0

Exact / Metadata:

- canonical Exact candidate recall: 100%
- 一意Exact Hit@1: 100%
- negative Exact FPR: 0/180
- nightly negative Exact FPR: 0/3,000
- False Anchor Rate: 0
- Exact evidence verification: 100%
- 一意filename Hit@1: 100%

Hybrid / Vector:

- Semantic subsetでHがLを改善
- Vector Rescue Rateをreleaseごとに維持または改善
- Vector Harm Rate: 5%以下
- overall Context RecallがLより2 percentage point以上悪化しない
- Dense candidateに入ったgoldのFusion/Postprocess脱落を個別承認なしで増やさない

No-hit:

- 明確なno-answer queryのFalse Evidence: 0
- strong identifier negativeの偽Exact: 0
- 通常semantic queryへのhard guard過剰発火: 0

Runtime:

- warm p95: baseline +20%以内
- RSS: baseline +15%以内
- JSON error、timeout、DB混線: 0
- daemon/no-daemonで品質判定差: 0
- Mac/Windowsで一意Exact判定差: 0

### 23.14 レポート形式

class別:

| DB | class | N | Hit@1 | Hit@5 | MRR | Context Recall | FPR | False Anchor | p95 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|

Vector寄与:

| DB | semantic N | L success | H success | Rescue | Harm | Dense unique | final survival |
|---|---:|---:|---:|---:|---:|---:|---:|

Failure funnel:

| failure category | ac | incident | rfc | total |
|---|---:|---:|---:|---:|

Before/After:

| metric | before | after | delta | confidence interval | gate |
|---|---:|---:|---:|---:|---|

### 23.15 実行順

1. 全ケースpreflight。
2. Fast set 120問。
3. 問題がなければRelease set 660問。
4. H/L/V ablation。
5. Exact negative 3,000件。
6. Mac/Windowsとdaemon経路差。
7. latency/RSS反復測定。
8. End-to-End 90問。
9. Before/After差分とfailure taxonomy集計。

最初に見るべき数字:

```text
invalid test cases
Exact False Positive Rate
False Anchor Rate
Context Recall@budget
Vector Rescue Rate
Vector Harm Rate
No-hit False Evidence Rate
warm p95
```
