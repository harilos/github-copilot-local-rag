# LRR-AGENT-002 — GPT-5 mini低クレジット候補比較 結果

## 結論

**BLOCKED / NO_GO**。winnerは選定しない。

4候補、DB選択追補、24-prompt上限、case単位の人手確認gateまでは実装・静的検証済みである。しかし、実Copilotの最初の正式caseはモデル呼び出し前に実行基盤で停止した。候補Aの回答品質を評価した失敗ではない。Stage 1を候補間で比較できていないため、「一番まし」を選ばず、Stage 2／Stage 3／Mini Stable@2-Liteへ進めなかった。

この試験結果は低予算screeningの中断結果であり、95%安定などの統計的主張を行わない。

## 固定点

- base_sha: `68e7c0886576677657238580ccead5f554f03c82`
- branch: `poc/agent-002-mini-credit-capped-bakeoff`
- result HEAD: `95dd8dc11d630e5384b41fe7c275d578cc441a2b`
- commits:
  - `23d3865` — credit-capped bakeoff候補・runner・fixture・予選
  - `526191b` — case単位の即時人手確認gate
  - `f1a5daa` — 認証済みCopilot homeの再利用
  - `95dd8dc` — 2026-08-15 DB選択追補と認証/RAG分離
- 製品Agent置換: 未実施
- main merge / force push / tag / Release: すべて未実施
- remote push: GitHub CLI／git認証が利用できず未実施。ローカル専用ブランチにはコミット済み。

## 候補

候補はすべて非配布領域 `tools/experiments/agent_002_bakeoff/agents/` に置いた。`.copilot/agents`、製品ZIP、managed-file一覧、installer既知hashは変更していない。

| ID | 候補 | SHA-256 |
|---|---|---|
| A | 現行改良 | `25035249d8fb1f664fecd4389fbd3c2cf4f5624f893e42d6cb9a3b79245d4bb2` |
| B | 最小直線手順 | `68a1beef7ecee7d9836e44cf8da58086caa08151690ba105269cccb25418944b` |
| C | 状態機械 | `82076d3d1af962bcdafc26b3ea436635656548c0551599804a5846f6fee757f4` |
| D | 許可tool trace | `0e10ca8662b50123f53c2bf7c37a92f159c4a07b8aff8532346fa1f84050e872` |

全候補の正本frontmatterは `model: GPT-5 mini`、`tools: ['execute']`、`agents: []`、`user-invocable: true`、`disable-model-invocation: true` である。

主要harness hash:

- `run_bakeoff.py`: `3cfe15de913f24eb02de173ed0e05ee743e36cca9fc8c7becd28704cc05bc7bc`
- `cases.json`: `9f3aa938bd526ef2aa0ccce7356f353b111527696008343b4f4b8bf3eba1cf0c`
- `test_run_bakeoff.py`: `9367371a4668779a69a4004dce56fbe8ebc40fc275234d4bf400ae68dad9eed2`

## DB選択追補

2026-08-15追補をDrive正本から再読し、次をgate化した。

- fixtureは `alpha-rag` と `beta-rag` の2 DBだけ
- 1ターン目はDB未指定の元質問、`list_dbs` 1回、2候補を示して選択をASKし停止
- 2ターン目は `beta-rag` だけ
- 保存した元Qを一字も変えず、選択DBだけへ `search` 1回
- 最終列は `list_dbs → ASK/STOP → DB名だけ → 元Qでsearch → summary read → 根拠内回答または棄権`
- retry 0、検索DB数1、非選択DB検索0
- DB内容の難問化や細かな品質順位付けは追加しない

## 0クレジットgate

最終実行コードに対し、次を確認した。

- `python -B -m unittest tools.experiments.agent_002_bakeoff.test_preflight tools.experiments.agent_002_bakeoff.test_run_bakeoff`
  - **36 tests / PASS**
- `python -B tools\experiments\agent_002_bakeoff\run_bakeoff.py --self-test --check-runtime`
  - **PASS**
- `git diff --check`
  - **PASS**
- 既存 `tools/windows_portable/test_custom_agents.py`
  - 固定点で **3 tests / PASS**

検査範囲はfrontmatter、2 DB固定、ASK停止、Q保持、実PowerShell argv、Unicode／引用符／改行／backtick／`$()`、許可tool列、retryなし、fresh UUID、2-turn resume、24 ledger、case単位人手判定、改竄／replay拒否、package非混入を含む。

## 実Copilot結果

正式成果物ルート:

`C:\Users\harilos\Documents\Local-RAG\agent-002-mini-credit-capped-bakeoff-artifacts-official-20260815`

Stage 1、候補A、`S1-GROUNDED`を1回起動した。runner ledgerは1、exit codeは1。CLI resultは次のとおり。

- `premiumRequests: 0`
- `totalApiDurationMs: 0`
- model request表示: `gpt-5-mini`
- model selection／assistant answer／tool execution: 未到達
- retry: 0

### 停止理由 P0-1: tool契約の実装差

GitHub Copilot CLI 1.0.77は `--available-tools=execute` を受理せず、次を記録した。

`Unknown tool name in the tool allowlist: "execute"`

同CLIでPowerShell実行toolの実名は `powershell` であり、指示書／製品候補のVS Code契約である `execute` と一致しない。正本候補を `tools: ['execute']` のままCLIで直接比較できない。

### 停止理由 P0-2: session-state書込権限

認証情報は正常に読み込まれたが、CLIは `C:\Users\harilos\.copilot\session-state\...\events.jsonl` へ書こうとしてEACCES (`os error 5`) で停止した。現在のCodex filesystem境界ではユーザーhomeがread-onlyである。

認証値を成果物側へ複製すればsecret混入になるため実施していない。モデル呼び出し前の基盤失敗であり、候補AのStrict Pass/Fail判定には使わない。

## prompt／credit監査

認証確立までの無効ルートも削除せず保持した。全てモデル未到達で、観測された `premiumRequests` と `totalApiDurationMs` は0だった。

| 成果物ルート末尾 | ledger | 理由 | premiumRequests |
|---|---:|---|---:|
| `artifacts-20260815` | 4 | 認証なし | 0 |
| `artifacts-authenticated-20260815` | 1 | 認証なし | 0 |
| `artifacts-final-20260815` | 1 | 認証なし | 0 |
| `artifacts-evaluated-20260815` | 1 | 認証なし | 0 |
| `runtime-check-20260815` | 1 | 認証なし | 0 |
| `artifacts-official-20260815` | 1 | CLI tool差＋session-state EACCES | 0 |

正式rootの24-prompt ledgerは1/24で停止した。課金promptの再実行はしていない。候補B/C/D、Stage 2、Stage 3は0件。

## 採用判定

| 項目 | 結果 |
|---|---|
| Stage 1全4候補比較 | 未成立 |
| Stage 2上位2 | 未実施 |
| Stage 3勝者再現 | 未実施 |
| Mini Stable@2-Lite | 未判定 |
| winner | なし |
| 最終判断 | **BLOCKED / NO_GO** |

## 次案

次タスクで、修正サイクルを新たに開始してどちらかを選ぶ。

1. literalなVS Code `execute` 契約を評価する: VS Code Agent Debug Logを用い、24 fresh chatを手動送信・逐次exportし、既存collectorでtool argvを採点する。
2. CLIを決定的runnerとして使う: 評価fixtureだけで `execute → powershell` の明示adapterを設け、認証tokenは環境変数で注入し、書込可能な隔離COPILOT_HOMEを使う。正本候補・製品agent・配布物は変更せず、adapter使用を結果票に明示する。

現タスクでは製品修正→成果物再生成サイクル上限に達しているため、このP0に対する6回目の追加修正を行わなかった。
