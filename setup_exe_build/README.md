# Windows Setup.exe builder (NSIS)

`build_setup.cmd`をダブルクリックすると、一般利用者向けの単一`Setup.exe`をこのフォルダ直下へ作成します。PowerShellから直接実行する場合は`build_setup.ps1`を使います。

## 最初に編集するもの

`installer-config.psd1`の次の項目を会社向けに変更してください。

- `Publisher`
- `WelcomeTitle`（初期値は`XXXの社内文書検索用RAGです`）
- `WelcomeTextLines`
- `ContactText`
- `FinishTextLines`

`WelcomeImagePath`へPNG、JPEG、GIF、BMPのいずれかを指定すると、インストーラーの初期画面へ表示します。空欄ではLocal RAG用の仮画像をビルド時に自動生成します。相対パスはこのフォルダを基準に解決されます。

## ビルド方法

1. Windows x64上で、開発用Local RAGの初期設定を済ませます。ビルドPCにだけPythonと準備済みONNXモデルが必要です。
2. `build_setup.cmd`をダブルクリックします。
3. ダイアログで、同封する`xxx-rag`辞書フォルダを選びます。
4. 完成した`CopilotLocalRAG-Setup-<version>.exe`をこのフォルダから取得します。

初回ビルドでは、必要に応じて次を`work/cache`へ取得します。

- ビルドPythonと同じバージョンのWindows x64埋め込みPython
- 検索実行時だけ必要なPythonパッケージ
- portable NSIS（初期値は3.12）

`work`と生成された`*.exe`はGit管理対象外です。キャッシュは次回ビルドで再利用されます。`-KeepWork`を指定した場合は、アプリと辞書のステージング結果も残します。

```powershell
.\build_setup.ps1 -KeepWork
```

辞書や画像をコマンドラインで指定することもできます。

```powershell
.\build_setup.ps1 `
  -DictionaryPath "$HOME\.copilot\rag\dbs\project-rag" `
  -WelcomeImagePath ".\company-welcome.png"
```

## 作られるSetup.exe

Setup.exeは管理者権限を要求せず、現在の利用者の`%USERPROFILE%\.copilot`へ次を配置します。

- 一般利用者向けの検索プログラム、Copilot instructions、検索Skill
- Windows x64埋め込みPython
- 検索実行時に必要なPython依存だけ
- 準備済みRuri ONNX INT8モデル
- 選択した初期辞書

同名の辞書がすでに存在するときは上書きしません。既存のDB、接続設定、秘密情報、ネットワーク設定も削除しません。`copilot-instructions.md`にはLocal RAG参照行が存在しない場合だけ追記します。更新時は旧`.venv`を削除してから検索専用ランタイムを入れ直すため、以前の重い依存も残しません。検索プロセスが動作中で削除できない場合は、終了後に再試行する画面を出します。

同梱版で「初期設定」を実行した場合は、重いモデル生成を再実行せず、埋め込み済みランタイムとモデルをオフライン検証します。破損時はSetup.exeをもう一度実行して修復します。

## 自動的に除外するもの

一般利用者は検索だけを行うため、辞書作成・Source管理・モデル生成にしか使わない次の配布物をSetup.exeから外します。

- ManagerおよびSource取得用の管理プログラム
- Source作業コピー、ログ、管理状態、秘密情報
- `sentence-transformers`、`torch`系
- `onnx`、`optimum`などのモデル生成依存
- `pypdf`、`python-docx`、`python-pptx`、`openpyxl`などの文書抽出依存
- `cryptography`などの管理用資格情報依存

辞書はLocal RAGの既存distribution契約でスナップショットされ、検索に必要な`catalog.sqlite`、`index`、プロファイル、リンク情報だけが同封されます。SQLiteはビルド中に安全なバックアップとして読み出されます。

## 補足

- ビルド時は依存パッケージ取得のためネットワーク接続が必要です。
- 完成EXEがNSISの単一ファイル上限へ近づく場合、ビルド中に警告します。
- `EmbeddedPythonVersion`を空欄にすると、検出したビルドPythonと同じパッチバージョンを使います。
- portable NSISのバージョンは`installer-config.psd1`の`NsisVersion`で変更できます。
