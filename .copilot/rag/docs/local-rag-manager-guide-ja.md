# Local RAG Manager 日本語操作ガイド

## 1. Managerの役割

Local RAG Managerは、人間がDBとSourceを管理する対話画面です。

- DB作成、表示名・検索ヒント変更、削除
- Source追加、更新、再開、状態確認
- Source MetadataとSource Link設定
- 検索確認
- 検索索引の診断・修復
- 利用者向け配布package作成
- 管理PC引っ越しpackage作成・再開・取り込み
- runtime、proxy/CA、daemonの確認

Copilotの通常検索は読み取り専用です。管理依頼をした場合、CopilotはManagerの
利用を案内するだけで、Managerを自動起動しません。

## 2. 起動

macOS/Linux:

```bash
~/.copilot/rag/query/.venv/bin/python ~/.copilot/rag/manage.py
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.copilot\rag\manage.py"
```

Windows Git Bash:

```bash
"$HOME/.copilot/rag/query/.venv/Scripts/python.exe" \
"$HOME/.copilot/rag/manage.py"
```

初回セットアップは、依存パッケージと検索モデルの取得を含むため、通常は
10分程度かかります。ネットワーク速度や端末性能により前後します。

共通入力:

- `【必須】`: 空欄では進みません。
- `【任意】`: 空欄で未設定です。
- `:q`: 保存せず前の画面へ戻ります。
- Enter: 編集中の既存値を維持します。
- `-`: 編集中の任意値を消します。
- `Ctrl+C`またはEOF: 未保存変更を破棄して終了します。

入力例を組織向けに変更する場合は、
`config/manage-custom.example.json`を参考に、Git管理されない
`config/manage-custom.json`を作成します。schemaは
`local-rag.manage-custom.v1`です。必要な`examples` keyだけを上書きできます。

優先順位は、`LOCAL_RAG_MANAGE_CUSTOM_CONFIG`で指定した絶対パスの設定、
`manage-custom.json`、同梱exampleの順です。不正JSONはline、column、offsetを
含む警告を表示し、不正な項目だけを下位設定へ戻します。未知keyや型不正も
同様に警告し、ほかの正常項目は利用します。password、token、credential入り
URLなどの秘密値は入力例設定へ保存できません。

## 3. 最終メニュー

トップメニュー:

```text
1. 新しいDBを作る
2. DBを選んで管理する
3. 全DBの全Sourceを更新・再開する
4. 配布・管理PCの引っ越し
5. この端末の設定・動作確認
0. 終了
```

選択DBメニュー:

```text
1. Sourceを見る・更新する
2. 新しいSourceを追加する
3. このDBの全Sourceを更新・再開する
4. DBの名前・説明を変更する
5. 問題があるとき
6. このDBを削除する【危険】
0. 戻る
```

Source一覧では、検索へ反映済みの資料と、登録後まだ取得途中のSourceを1つの
一覧にまとめます。種類と「最新」「更新途中・再開可能」「既存データ」などの
状態を見て番号を選びます。

Source詳細:

```text
1. 更新・再開する
2. 取得設定を確認・変更する
3. 検索結果リンクを確認・変更する
4. 進捗・ログを見る
5. 技術情報
6. このSourceを削除する【危険】
0. 戻る
```

Otherの取り込み完了後は、1番が「ファイル／フォルダを選び直して再取り込み」
になります。macOSのSharePointでは更新できないことを明示します。

Source削除では、選択したSourceの検索済み文書、検索結果リンク設定、取得設定、
進捗、DB内の作業ファイルをまとめて削除します。DB自体とほかのSourceは削除
しません。Source名の完全入力と、既定Noの最終確認が必要です。途中で失敗した
場合は削除済みの範囲を表示し、同じ操作を再実行すると残りから再開します。

「問題があるとき」では診断結果を見て、全文・識別子索引、vector索引、または
全検索索引を再作成します。元文書の再取得を伴う操作とは分離されています。

## 4. DB作成

DB名は半角英数字で始め、`-rag`で終わらせます。

```text
<db-name>-rag
```

titleとquery hintはCopilotが一覧からDBを選ぶための公開情報です。機密情報を
書かず、収録分野を短く示してください。DB作成直後にはSourceはありません。

## 5. Sourceの管理

Sourceは、同じ取得元、同じ更新方法、同じURL生成設定を共有する単位です。
Source追加画面では次から選びます。

1. GitHub repository
2. SVN
3. Redmine project
4. SharePoint同期folder
5. 手元の資料を一度だけ取り込むOther

Sourceの安定IDは索引と更新stateを結びます。後から変更しません。新しいSourceは
登録だけでは検索対象にならず、取得・索引登録が成功した後にSource inventoryへ
現れます。

Source取得は固定のwork pathへ行い、providerごとのcheckpointを保存します。
中断後は同じ設定とcheckpointから再開します。credentialやlocal absolute pathを
Source設定へ永続化せず、必要な値は環境変数またはmachine-local設定から解決します。

### GitHub

初回登録で入力するのはrepository URLとSourceの名前です。リポジトリ全体を
DB内の固定作業場所へ取得し、remoteが示す既定branchを実際の取得結果から確認
します。`main`や`master`を推測しません。

検索結果リンクは取得設定と別に確認します。安全に導出できるGitHub browser URLと
実際に取得したbranchを候補にし、commit permalinkやrepository内追加pathを使う
場合だけリンク画面で設定します。tokenやGit credentialはDBへ保存しません。

### SVN

SVN URL、Source名、再帰または直下ファイルだけの範囲を選びます。checkout/update
はDB内の専用領域で行い、credentialはURLへ埋め込みません。検索結果リンクは
Apache HTTP(S)のファイル直リンク、または製品固有Web画面のトップページから
明示的に選びます。製品固有URLは推測しません。

### Redmine

projectのIssueを直列に取得し、各Issueを`issues/<issue-id>.md`へ保存します。
取得対象と各Issueの完了位置をcheckpointへ保存し、5件保存するごとに検索へ
反映します。中断時にやり直すのは最後に反映確認できていない最大5件です。
一時的なHTTP失敗だけを上限付きでretryし、`Retry-After`があれば従います。

### SharePoint

追加・更新はWindowsだけです。SharePoint同期clientが作ったlocal folderを、
machine-local環境変数で指定したrootから直接検索へ反映します。同期実体をDB内へ
copyしません。

```text
root環境変数: LOCAL_RAG_SHAREPOINT_ROOT
相対folder: <relative-subdirectory>
```

Source設定にWindowsのabsolute pathは保存しません。作成済みDBは利用者向け
packageまたは管理PC引っ越しpackageでmacOSへ移せます。macOSではそのDBを検索
できますが、SharePoint Sourceの追加・更新はできません。

### Other

人間が選択したlocal file/folderを一度だけcopyして索引化します。継続同期を
意味しません。

## 6. root、scan subdirectory、stored path

logical rootはSource work treeの基準です。scan subdirectoryを指定すると一部だけ
処理しますが、stored pathは常にlogical root基準です。

```text
logical root: <source-root>
scan subdirectory: <relative-subdirectory>
stored path: <root-name>/<relative-subdirectory>/<document>
```

root名は必ずstored pathへ含まれ、separatorは`/`です。別scopeから同じ物理fileへ
到達しても、同じstored pathとdocument identityになります。

## 7. Source MetadataとSource Link

Source inventoryはcatalogの現在有効なdocumentから読み取ります。sidecarだけで
架空Sourceは作りません。Source IDがないdocumentは診断件数としてだけ表示します。

任意のmetadata:

- display name
- `source_type`
- enabled/disabled Link 1件

保存先:

```text
<db-root>/source-links.json
```

canonical schemaは`rag-source-metadata-v1`です。1 Sourceは最大1 Provider、
最大1 URL設定です。path prefixやlongest-prefix選択はありません。

初版のSource Metadata編集はsingle-editorです。同じDBを2つのManager processから
同時に編集しないでください。一時file、revision／etag再確認、atomic replaceで
保存しますが、永続lock fileは作らず、厳密なmulti-process CASは保証しません。

現在有効なdocumentからtop-level stored rootを自動導出します。

- `ready`: rootが1つ。per-file URLを設定できます。
- `no_observed_root`: rootなし。SVN Web画面やOtherのトップページ方式
  以外ではファイル単位URLを生成できません。
- `multiple_observed_roots`: 複数。Provider単位でSource IDを分けて追加します。

URL解決は検索順位とevidence分類が終わった後に行われます。成功時は公開結果へ
通常リンクを`source_url`、固定リンクを`source_permalink`として追加します。
回答で参照する際は固定リンクを優先します。設定なし、破損、path不正、
生成失敗時はstored pathだけを返し、検索statusは変わりません。

## 8. DBの更新と再開

「このDBの全Sourceを更新・再開する」は各Sourceのprovider設定とcheckpointを
使います。Manager独自のmtime判定は行わず、既存のcontent hashとchunker設定へ
委譲します。

処理中または中断時は、対象DB、Source、現在の段階、資料件数、最近のエラー、
再開可否を確認できます。検索修復の内部対象は診断結果からManagerが決めます。

## 9. Package

「配布・管理PCの引っ越し」には2種類あります。

### 利用者向け検索package

- ZIP
- 選択した検索可能DB
- 公開wrapperと検索runtime
- 必要なmodel
- 現行schemaとして検証済みのSource Metadata全体
- 管理用取得stateは含めない

### 管理PC引っ越しpackage

- resumable folder
- DBと管理用code
- Source設定とcheckpoint
- 途中中断後に同じ出力先で再開可能

package作成はdaemonやDB全体へglobal lockを取りません。copy元を2回確認し、
途中変更を検出したら失敗として終了します。出力はrelative pathだけのmanifestと
SHA-256で検証します。
package作成日時は内容更新日時と分けて記録します。配布、copy、再packageだけで
`rag-wrapper.json`の`content_snapshot_at`を新しくしません。

除外対象:

- `.venv`
- daemon/run state
- `*.lock`、SQLite WAL/journal、temporary file
- credential、secret、private key
- `source-links.json.bak`

activeな`source-links.json`には内部URLが含まれ得ます。packageを機密資料として
安全に保管してください。

## 10. この端末の設定・動作確認

通常画面には、Local RAGを利用できるか、検索を試す、Sourceへの接続が
「利用可能」か「設定が必要」かだけを表示します。runtime Python、model、
proxy/CA、credential参照、環境変数名などは「技術情報」だけに表示します。

setup completeとlookup readyは別です。runtimeが正常でも健康なDBがなければ
lookup readyはfalseです。

## 11. 安全な削除

DB削除では、対象がDB root直下の通常directoryであることを検証します。Source処理
の中断状態を安全に保存してからもう一度確認し、DB名、表示名、資料数、sizeを表示
します。人間がDB名を完全一致で入力した場合だけdirectory全体を削除します。
削除用lock、trash、quarantineは作りません。Windowsでfile handleが残る場合は
成功を偽装しません。

## 12. よくある質問

### Source Linkを変更すると再索引が必要ですか？

不要です。Source Linkは検索結果の表示段階だけに適用されます。

### package作成中に検索できますか？

package作成はDB管理lockやmaintenance状態を作りません。ただしcopy開始前後で
対象が変わった場合や、現在のManagerが実writerを直接保持している場合はpublish
せず中断します。検索daemonが読んでいるだけでは中断しません。

### macOSでSharePoint DBを検索できますか？

できます。Windowsで更新・索引化したDBを移してください。macOSではSharePoint
同期folderからの更新はできません。

### CopilotへDB追加を依頼できますか？

通常RAG Skillは読み取り専用です。Managerを人間が起動してください。
