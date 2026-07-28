# Local RAG Manager 日本語操作ガイド

## 1. Local RAG Managerとは

Local RAG Managerは、ローカルRAGの初期設定、DB作成、文書の取り込み、
検索確認、Source Link設定、状態確認、索引修復、DB削除を対話形式で行う
人間向けツールです。既存CLIへ処理を委譲するため、検索順位、文書ID、
DB schemaは変わりません。

## 2. 起動方法

macOS/Linux:

```bash
~/.copilot/rag/query/.venv/bin/python ~/.copilot/rag/manage.py
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\manage.py"
```

Git Bash:

```bash
"$HOME/.copilot/rag/query/.venv/Scripts/python.exe" \
  "$HOME/.copilot/rag/manage.py"
```

### 入力とキャンセルの共通ルール

- `【必須】` は入力が必要です。空欄の場合は理由と例を表示して再入力します。
- `【任意】` は空欄にすると未設定になります。
- 複数項目の設定中は `:q` で、保存せずに前の画面へ戻ります。
- 既存の任意値を消すときは `-` を入力します。Enterだけなら現在値を維持します。
- `Ctrl+C` または入力終端（EOF）では、未保存の変更を破棄して安全に終了します。

## 3. 初期設定

「初期設定・動作確認」では、仮想環境、必要ライブラリ、検索モデル、
検索可能なDBを確認できます。

- `初期設定: 完了`はLocal RAGの実行環境が利用できる状態です。
- `検索準備: 利用可能`は健康なDBが1つ以上ある状態です。
- 初期設定が完了していてもDBがない場合、検索準備は利用不可になります。

## 4. DBの作成

DB名は半角英数字で始まり、末尾を`-rag`にします。使用可能な文字は
半角英数字、`_`、`.`、`-`です。

```text
project-rag
incident-rag
product-manual-rag
```

表示名と検索ヒントは任意です。検索ヒントはCopilotがDBを選ぶ際に使う
短い説明で、文書本文には追加されません。

## 5. buildとaddの違い

- build: 取り込み元からDBを構築します。中断処理は同じ条件で再開できます。
- add: 既存DBへ新規・変更文書を追加し、選択範囲内の削除も反映します。

Managerは既存のcontent hashによる更新判定を使います。

## 6. Sourceとは

Sourceは、同じ取り込み元と同じURL生成設定を共有する文書のまとまりです。
build/addで文書が正常に索引登録された後に現れます。Source一覧から作成、
削除、Source ID変更はできません。

## 7. Source IDの決め方

Source IDは取り込み元を識別する、後から変更しないIDです。同じ取り込み元を
更新するときは同じIDを使います。

```text
github-repository
sharepoint-docs
redmine-issues
filesystem-docs
```

1 Sourceは1 Provider、1 URL生成単位です。異なるProviderを同じSource IDへ
混在させないでください。

## 8. 論理ルートとscan subdirectory

論理ルートは取り込むファイル群の基準ディレクトリです。論理ルート名は
RAG保存パスの先頭へ必ず含まれます。`scan subdirectory`は論理ルートの
一部だけを処理するときに使う任意値です。

```text
論理ルート: /path/to/source-root
scan subdirectory: manuals/ja
```

空欄の場合は論理ルート全体が対象です。

## 9. Source Linkとは

検索結果に元のGitHub、SharePoint、Redmine等を開くURLを付ける設定です。
検索順位、検索内容、回答可能性、DB内容には影響しません。URL生成に失敗した
場合もRAG保存パスは表示されます。

## 10. observed stored rootとは

「検出された保存ルート」は、現在有効な文書パスから自動検出した先頭
ディレクトリです。URL生成時に1回だけ取り除きます。利用者が入力する値では
ありません。

- `ready`: 1つ検出。ファイル単位URLを設定できます。
- `no_observed_root`: 未検出。トップページのみ設定できます。
- `multiple_observed_roots`: 複数検出。ProviderごとにSource IDを分けます。

## 11. GitHub設定

Managerは`.git`を調査せず、Gitコマンドも実行しません。

```text
Repository URL:
https://github.com/harilos/github-copilot-local-rag

Ref:
main

GitHubリポジトリ内の追加パス:
空欄

Commit:
666161c58fac1e0837ab39ce4f1b8b96943e9489
```

保存パス:

```text
github-copilot-local-rag/.copilot/rag/query/search.py
```

自動検出保存ルート:

```text
github-copilot-local-rag
```

Source相対パス:

```text
.copilot/rag/query/search.py
```

通常URL:

```text
https://github.com/harilos/github-copilot-local-rag/blob/main/.copilot/rag/query/search.py
```

固定URL:

```text
https://github.com/harilos/github-copilot-local-rag/blob/666161c58fac1e0837ab39ce4f1b8b96943e9489/.copilot/rag/query/search.py
```

## 12. SharePoint設定

Microsoft Graphは使用しません。

通常設定で入力するURLは、次の1項目だけです。

`SharePoint上の基準フォルダURL【必須】`

検索結果から個別ファイルを開くための、文書ライブラリまたはフォルダの
URLです。Managerが自動検出した保存ルートを文書パスから1回だけ除去し、
残ったSource相対パスをこのURLの末尾へ追加します。

```text
https://contoso.sharepoint.com/sites/project/Shared%20Documents/manuals
```

リンク方式はファイル直接リンクに自動設定されるため、選択する必要は
ありません。保存ルートも読み取り専用の自動検出値であり、入力しません。
基準URLまたは保存ルートを確認できない場合は、曖昧なトップページURLへ
フォールバックせず、リンクを生成しません。credential入りURLや個別
ファイルURLは入力しません。

以前のsidecarにあるSourceトップURLは読み込み可能ですが、新しく設定する
項目ではありません。基準フォルダURLを設定して保存し直すと、旧Homeキーは
新しい設定から除去されます。

## 13. Redmine設定

Issue:

```text
保存パス: issues/12345.md
正規表現: ^issues/(?P<issue_id>[0-9]+)\.md$
テンプレート: https://redmine.example.com/issues/{issue_id}
生成URL: https://redmine.example.com/issues/12345
```

Wiki:

```text
保存パス: wiki/Installation_Guide.md
正規表現: ^wiki/(?P<page>.+)\.md$
テンプレート: https://redmine.example.com/projects/project/wiki/{page}
```

named groupとtemplate placeholderは同じ名前にします。

## 14. その他URL設定

通常は「相対パスをURL末尾へ追加」を使います。正規表現テンプレートは
上級者向けです。一致しない文書はURLなしで安全に表示されます。

## 15. Refとは

RefはGitHub上で通常表示する版です。

- ブランチ: `main`、`develop`、`release/v2`
- タグ: `v1.2.3`
- コミット: 完全なcommit SHA

ブランチが更新されるとリンク先の表示内容も更新されます。

## 16. GitHubリポジトリ内の追加パスとは

RAGのSource相対パスよりGitHub上の実ファイルが深い場合だけ指定します。

```text
RAG上: manuals/setup.md
GitHub上: product-a/manuals/setup.md
設定値: product-a
生成URL: https://github.com/owner/repo/blob/main/product-a/manuals/setup.md
```

これはstored path prefixではありません。保存ルートの除去はManagerが
自動で行います。

## 17. Commit permalinkとは

完全なcommit SHAを指定すると、通常のref URLに加えて内容が将来変わらない
固定リンクを生成します。回答では固定リンクが優先されます。

## 18. 設定previewの読み方

previewにはRAG保存パス、自動除去された保存ルート、Source相対パス、
Provider固有の追加パス、生成URL、成功・失敗理由が表示されます。

## 19. 無効化と削除の違い

- 無効化: 設定を残し、検索結果へのURL付与だけを停止します。
- 削除: Source Link設定をsidecarから削除します。

どちらも索引済み文書、Source、DBを削除しません。

## 20. build/addと検索の同時実行

Local RAGは独自のDBメンテナンス状態を永続管理しません。build、add、
repair、searchを過去の実行状態だけで拒否することもありません。処理に
失敗した場合は、その実行がエラーで終了します。原因を修正した後、そのまま
再実行してください。SQLite、Chroma、OSのファイル操作が同時実行の競合を
検出した場合は、その処理の通常エラーとして表示されます。

## 21. エラー別対処

- 初期設定が必要: 初期設定を実行します。
- 保存ルート未検出: 文書とSource IDを確認します。
- 複数ルート: ProviderごとにSource IDを分けます。
- URL設定不正: 表示された入力例と許容値を確認します。
- revision conflict: 設定を読み直して再編集します。

## 22. Windows PowerShellでの起動

PATH上の`python`ではなく、インストール済みvenvの`python.exe`を直接
起動します。`cmd.exe /c`や`Start-Process`は不要です。

## 23. Git Bashでの起動

PowerShellの`&`や`$env:`を使わず、`$HOME`からvenvの`python.exe`を
直接指定します。

## 24. macOS/Linuxでの起動

`~/.copilot/rag/query/.venv/bin/python`を直接使用します。

## 25. FAQ

### Source Linkを変更すると再構築が必要ですか？

不要です。sidecarだけが更新されます。

### URLが生成できないと検索精度が下がりますか？

下がりません。URLは検索処理の後に付与されます。

### SourceをManagerから作れますか？

作れません。新しいSource IDでbuild/addが成功した後に現れます。

### 詳細JSONはどこで見られますか？

状態画面で選択できます。通常画面は人間向け要約です。

## 26. セキュリティ上の注意

- URLへユーザー名、パスワード、token、cookieを埋め込まないでください。
- DBの`source-links.json`は内部URLを含む可能性があります。
- DBを外部へ渡す前にsidecarを確認してください。
- 通常検索は外部URLへHTTPアクセスしません。
- DB削除は復元機能のない危険操作です。
