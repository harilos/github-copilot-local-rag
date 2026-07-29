# GitHub Copilot Local RAG

**Current development version: 1.0.1**

**Release status: Unreleased release candidate**

ローカル文書をGitHub Copilotから一度の検索で参照するためのRAGパックです。
通常検索は読み取り専用で、検索方式の選択、再検索、DB変更をPython側または
Copilotが勝手に行わない契約になっています。

## 主な機能

- Dense、BM25、Exact、metadataを組み合わせたHybrid検索
- 直接根拠と、幅広い関連文書候補を分離した検索結果
- 識別子の完全一致確認とnear-collision防止
- 一度の検索で生成する初回summaryと、再検索しないfollow-up detail
- 文書の見出し、表ヘッダー、前後段落を使う構造的context
- Source単位の任意Webリンク（通常URLと固定リンクを区別）
- 複数DB、複数Source、増分更新、再開、索引修復
- Windowsの永続daemonと直接`python.exe`起動
- 人間向けLocal RAG Manager
- 検索利用者向けZIPと、管理PC引っ越し用フォルダの2種類のpackage

検索本文と索引はローカルで処理されます。回答に必要な短い抜粋だけが
Copilotへ渡されます。通常検索は外部URLへHTTPアクセスしません。

## 必要なもの

- Python 3.10以上
- macOS、Linux、またはWindows
- ローカルコマンドを実行できるGitHub Copilot環境
- 初期設定時に依存packageとmodelを取得できるnetwork
- DB、model、索引用のdisk容量
- 旧`.doc`や`.ppt`を扱う場合のみLibreOffice

## インストール

repository rootで実行します。

macOS/Linux:

```bash
bash ./install.sh
```

Windows PowerShell:

```powershell
.\install.ps1
```

installerは`.copilot/`を`$HOME/.copilot/`へoverlayします。既存の
`copilot-instructions.md`、runtime venv、daemon状態、machine-local
`rag/config/network.json`を不用意に上書きしません。古い版から残る廃止済み
Local RAG管理Skillと旧migration/export helperだけは、明示したtombstone一覧で
削除します。

既存の`~/.copilot/copilot-instructions.md`には次を追加してください。

```text
For requests to use RAG, local documents, internal or company information, or information installed in or provided to Copilot, read ~/.copilot/instructions/rag.instructions.md.
```

## 通常検索

Copilot向けの公開入口は次の2つだけです。

```text
~/.copilot/rag/list_dbs.py
~/.copilot/rag/search.py
```

DB名を質問で明示した場合は一覧取得0回、検索1回です。DB名がない場合は一覧を
1回取得し、title、query hint、content summary、公開Source表示情報から1つだけ
明確に選べる場合に検索を1回実行します。曖昧なら検索せず利用者へ確認します。

macOS/Linux:

```bash
~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/list_dbs.py --format json

~/.copilot/rag/query/.venv/bin/python \
  ~/.copilot/rag/search.py \
  --db <db-name>-rag \
  --include-db-hint \
  --compact-json \
  --result-delivery file \
  --format json \
  "<question>"
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\list_dbs.py" `
  --format json

& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\search.py" `
  --db <db-name>-rag `
  --include-db-hint `
  --compact-json `
  --result-delivery file `
  --format json `
  "<question>"
```

Windowsでは`cmd.exe /c`、nested PowerShell、batch wrapper、PATH上の
`python`探索、JSON stdin pipelineを通常検索に使いません。

検索結果の`database_freshness.status`が`stale`の場合、Copilotは内容更新時点が
古いことを示す警告を同じchatで1回だけ表示します。参照先が解決できた項目には
`source_url`が付き、
固定リンクが有効なら`source_permalink`も付きます。
回答本文では`[E1]`のようなIDだけを引用し、末尾の`References`でfile名へ
リンクします。URLがない場合でも根拠のauthorityや検索順位は変わりません。

## Local RAG Manager

作成、追加、更新、修復、配布、管理PC引っ越し、Source設定はCopilotの通常検索
では行いません。人間がManagerを起動します。

macOS/Linux:

```bash
~/.copilot/rag/query/.venv/bin/python ~/.copilot/rag/manage.py
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\manage.py"
```

トップメニュー:

1. 新しいDBを作る
2. DBを選んで管理する
3. 全DBの全Sourceを更新・再開する
4. 配布・管理PCの引っ越し
5. この端末の設定・動作確認
0. 終了

選択DBメニュー:

1. Sourceを見る・更新する
2. 新しいSourceを追加する
3. このDBの全Sourceを更新・再開する
4. DBの名前・説明を変更する
5. 問題があるとき
6. このDBを削除する
0. 戻る

詳しい操作は
[Local RAG Manager 日本語操作ガイド](.copilot/rag/docs/local-rag-manager-guide-ja.md)
を参照してください。

## Source Manager

Sourceは、同じ取得元、同じ更新方法、同じURL生成設定を共有する文書の単位です。
新しいSourceはManagerの追加操作で作り、索引登録に成功した後に検索対象へ
現れます。Source IDは後から変更しません。

対応する取得元:

- GitHub repository
- SVN
- Redmine project
- SharePoint同期folder
- 手元のfile/folderを一度だけ取り込むOther

SharePoint Sourceの追加・更新はWindowsだけで行います。Windowsの同期rootを
直接検索へ反映し、同期実体をDB内へcopyしません。作成済みDBをpackageでmacOSへ
移した後は、macOSでも通常検索できます。macOSではSharePoint Sourceの更新を
行いません。

Source MetadataはDB直下の`source-links.json`へ保存します。物理file名は互換性の
ため維持していますが、canonical schemaは`rag-source-metadata-v1`です。
1 Sourceは最大1 Provider、最大1 Linkです。利用者がstored path prefixを
入力する仕様はありません。現在有効な文書からobserved stored rootを自動導出し、
Source-relative pathでURLを生成します。

## Packageと管理PCの引っ越し

Managerは用途の異なる2種類を作成します。

### 利用者向け検索package

選択DBと検索runtimeを含むZIPです。検索専用computerへ配布します。管理用の
Source取得stateや管理画面は含めません。

### 管理PC引っ越しpackage

Source取得state、checkpoint、管理用codeを含むfolderです。作成を中断しても
同じ出力先で再開できます。

package作成はLocal RAG全体をlockしません。copy前後でfileのsize、timestamp、
hashを確認し、途中で変化した場合は成功を偽装せず中断します。manifestは
relative pathとSHA-256を持ちます。venv、daemon state、lock、temporary file、
credential、backup sidecarは含めません。activeなSource Metadataには内部URLが
含まれ得るため、package自体を機密dataとして扱ってください。
作成日時は`packaged_at`へ別記し、copyや再packageで
`content_snapshot_at`（内容更新時点）を進めません。

## 保存pathとscan範囲

stored pathは常に次の形式です。

```text
<root-name>/<path-relative-to-logical-root>
```

`scan subdirectory`を指定しても、stored pathの基準は元のlogical rootです。
root名を含まない旧DBとはpath由来document IDが変わるため、その方式を採用する
古いDBは一度だけ再構築が必要です。

## 検索結果

主な公開field:

- `evidence`: 直接根拠
- `background_context`: 直接根拠を補うcontext
- `related_context`: 参考情報。直接根拠ではない
- `document_results`: 多様な関連文書card
- `warnings`: table header不足などの制約
- `coverage`: 取得範囲の要約
- `database_freshness`: `rag-wrapper.json`に記録された内容更新時点からの経過
- `source_url`: 解決済みの通常参照先
- `source_permalink`: 設定時だけ返る固定参照先
- `source_provider`: URLを生成したSource種別

初回file deliveryはOS temporary directoryへUTF-8 JSON bundleを原子的に公開し、
小さいpointerをstdoutへ返します。「もっと詳しく」のfollow-upは同じ
result-setを読み、検索をやり直しません。

## Networkとproxy

通常検索、daemon、SQLite、local Chromaは外部networkへ接続しません。setupと
Source取得だけがnetworkを利用します。proxy/CAはmachine-local
`~/.copilot/rag/config/network.json`へ保存でき、installerは実fileを配布せず、
既存設定を維持します。proxy URLにusername/passwordが含まれる設定は保存対象に
しません。

## License

repository内のlicenseと、同梱する各dependency/modelのlicenseを確認して
利用してください。
