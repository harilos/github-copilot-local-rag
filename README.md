# GitHub Copilot Local RAG

> 開発版: `1.0.1`
>
> GitHub Release: 未公開

ローカル文書や社内資料を、VS CodeのGitHub Copilotから自然な日本語で
検索するためのRAGパックです。利用者は専用Agentを選んで質問するだけで、
検索コマンドや検索方式を覚える必要はありません。

文書の抽出、索引作成、検索はPC内で行います。Copilotへ渡るのはDB全文ではなく、
質問に応じて選ばれた抜粋、出典、検索メタデータです。検索処理は元資料のURLを
自動で開きませんが、最終回答の生成にはGitHub Copilotへの接続が必要です。

現在、正式に案内している一般利用経路は、管理者が作成したWindows x64配布版、
VS Codeの3つの`LOCAL-RAG` Agent、およびPowerShell 7から起動する
GitHub Copilot CLIの3段階profileです。GitHub上のtag／Releaseから配布物を
公開する段階にはまだ進んでいません。

## まず選ぶ：2つの利用形態

このリポジトリには、別々の検索エンジンがあるのではなく、用途に応じた2つの
導入形態があります。

| | 配布版（利用者向け） | 管理版（管理者向けソース版） |
|---|---|---|
| 主な利用者 | 社内資料を検索する人 | DBと取得元を管理する人 |
| 入手方法 | 管理者からWindows x64 offline ZIPを受け取る | このリポジトリをcloneする |
| 検索 | 3つの`LOCAL-RAG` Agentを使う | 同じ検索機能を利用できる |
| DBの作成・更新 | できない | できる |
| Sourceの追加・再開 | できない | できる |
| 配布ZIPの作成 | できない | できる |
| 利用者PCのPython | 不要 | CPython 3.13.xが必要 |

配布版のDBは、管理版で作成した時点のsnapshotです。元資料が更新された場合は、
管理者がDBを更新して新しい配布ZIPを作ります。通常の検索AgentがDBやSourceを
変更することはありません。

資料を検索するだけなら、次の「配布版」から読んでください。DBを作成・更新する
場合は、後半の「管理版」へ進んでください。

## 配布版：インストールする

### 必要なもの

- Windows 10以降（x64）
- 次のいずれかのCopilot利用環境
  - VS CodeとGitHub Copilot Chat
  - PowerShell 7とGitHub Copilot CLI
- 管理者から受け取った配布ZIP
- ZIPを展開できる空き容量

インストール時にsystem Python、pip、PATH変更、管理者権限、network接続は
不要です。Agentで質問するときは、通常どおりGitHub Copilotへ接続できる必要が
あります。

### 初回インストール

1. 配布ZIPを通常のフォルダへすべて展開します。ZIP内から直接実行しないでください。
2. 展開したフォルダ直下の`install.cmd`をダブルクリックします。
3. 最後に`Local RAG インストール結果: 成功 (SUCCESS)`と表示されたことを確認します。
4. VS Codeを完全に終了し、もう一度起動します。

PowerShellから実行する場合は、展開先へ移動して次を実行します。

```powershell
.\install.cmd
```

配布版には固定Python、検索用package、ONNX model、選択されたDB、
VS Code用の3つのAgent、CLI用の3つのAgent、hash検証付きlauncher、
pinned MCP設定が含まれています。インストール後にCopilotへ
「初期設定をして」と依頼する必要はありません。

installerはLocal RAGを`%USERPROFILE%\.copilot`へ配置し、read-only MCPを
Copilot CLIの設定rootと通常のVS Code Default Profileへ別々のschemaで登録します。
既存の無関係なCopilot設定、別名のDB、system Python、VS Codeのapproval設定は
変更しません。
Copilotによる実地受入はinstallerや製品testでは実行しません。

### 同じDBを新しい配布版へ更新する

同名DBは、誤った上書きを防ぐため初期状態では置き換えません。管理者から同じDBの
更新版を受け取った場合だけ、次を実行します。

```powershell
.\install.cmd -ReplaceExistingDatabases
```

この指定で置き換わるのは、配布ZIPに含まれる同名DBだけです。別名のDBは保持されます。
自動試験などで終了待ちを省く場合は`-NoPause`も指定できます。

## 配布版：3つのAgentで検索する

### 最初の質問

1. VS CodeでCopilot Chatを開きます。
2. ChatをAgentモードにします。
3. Agent選択欄から、まず`LOCAL-RAG-標準`を選びます。
4. 普通の文章で知りたいことを質問します。

専用Agentを選んだ時点でLocal RAGの使用は必須になるため、毎回「RAGを使って」と
付ける必要はありません。

```text
project-ragで、A2Lの目的と採用理由を根拠付きで教えて
```

### Agentの選び方

| Agent | 向いている質問 | 現在のmodel設定 |
|---|---|---|
| `LOCAL-RAG-標準` | 普段の質問。必要な量だけ検索して答えてほしい | VS CodeのAuto選択を継承 |
| `LOCAL-RAG-節約` | 用語、識別子、単純な事実を短く確認したい | GPT-5 mini (copilot) |
| `LOCAL-RAG-徹底検索` | 複数観点の比較、矛盾確認、複雑な調査をしたい | GPT-5.3-Codex (copilot) |

迷った場合は`LOCAL-RAG-標準`を使ってください。`LOCAL-RAG-節約`は検索回数と
根拠確認を最小限にし、`LOCAL-RAG-徹底検索`は同じDBを異なる観点から検索して
Evidenceを突き合わせます。

3 Agentが利用できるのは、Local RAGの次の2つのread-only toolだけです。

- `local_rag_search`
- `local_rag_get_evidence`

terminal、PowerShell、Workspace file、Web、別のtoolは使いません。DB、Source、
設定、fileの作成・変更・削除も行いません。徹底検索も公開Webへ迂回せず、
配布されたLocal RAGのEvidenceだけで回答します。

### 質問の例

| やりたいこと | 質問例 |
|---|---|
| 用語を確認する | `project-ragで、A2Lとは何か根拠付きで教えて` |
| 採用理由を調べる | `project-ragで、方式Aを採用した理由と制約を整理して` |
| 方式を比較する | `project-ragで、方式Aと方式Bを設計・運用・障害対応の観点で比較して` |
| 関連資料を広く探す | `project-ragで、この障害に関係する仕様・実装・運用資料を観点別に調べて` |
| 根拠を詳しく読む | `さっきの[E2]の前後を詳しく確認して` |

DB名を省略した場合、AgentはDBの説明と質問が明確に一致するときだけ自動で
選びます。選べない場合は推測で検索しません。配布元から案内された
`<DB名>-rag`を質問に付けて、もう一度実行してください。

キーワードだけを並べるより、目的、期間、条件、比較軸を含めて質問するほうが
意図に合った結果になります。ベクトル検索、全文検索、完全一致検索などの
内部方式を利用者が指定する必要はありません。

### 回答と出典の見方

Agentは、根拠のある主張へEvidence IDを付け、回答末尾の`## References`へ
出典をまとめます。

- `[E…]`: 質問へ直接答える根拠
- `[B…]`: 理解を補う背景情報
- `[D…]`: 関連文書の候補。直接根拠とは限らない

複数回検索した場合は`[R1-E1]`、`[R2-D1]`のように、何回目の検索結果かも
表示します。元資料の安全なリンクがDBへ設定されていれば、Referencesから
GitHub、SVN、Redmine、SharePointなどの資料を開けます。

### GitHub Copilot CLIから使う

PowerShell 7から標準profileを起動します。

```powershell
local-rag-copilot
```

三段階は`-Tier`で選べます。

| `-Tier` | 用途 | model |
|---|---|---|
| `savings` | 単純な事実を短く確認する | `claude-haiku-4.5` |
| `standard` | 普段の質問へ標準的に回答する | `auto` |
| `thorough` | 複数観点の比較や矛盾確認を行う | `auto` |

```powershell
local-rag-copilot -Tier savings
local-rag-copilot -Tier standard
local-rag-copilot -Tier thorough
```

専用launcherは現在の作業folderを維持し、検証済みのAgentとpinned
`localragagent003` MCPを使用します。session内で自動許可するのは
`local_rag_search`と`local_rag_get_evidence`だけです。通常の`copilot`
command、認証、既存session、永続permissions、VS Codeのapproval設定は
変更しません。

### 困ったとき

| 状況 | 対応 |
|---|---|
| Agent選択欄に`LOCAL-RAG`がない | VS Codeを完全に終了して再起動する。直らなければinstallerの最終結果を確認する |
| 同名DBがあるためinstallできない | 更新版であることを確認し、`-ReplaceExistingDatabases`を付けて再実行する |
| installが`FAILED`になった | 画面に表示されたlogを管理者へ渡す |
| 質問に合うDBを選べない | 配布元から案内されたDB名を質問へ明記する |
| 根拠が不足している | 条件や比較軸を追加するか、`LOCAL-RAG-徹底検索`で質問し直す |

installerは毎回、`%LOCALAPPDATA%\LocalRAG\logs`へ
`portable-install-<timestamp>-<pid>.log`を1つ作り、成功時も失敗時も画面に
absolute pathを表示します。

## 管理版：インストールする

管理版は、DBとSourceの作成・更新、処理の再開、診断、配布版の作成を行う端末へ
source cloneから導入します。日常の管理操作は対話式のLocal RAG Managerを使います。

### 必要なもの

- Windows x64、macOS、またはLinux
- CPython 3.13.x（`>=3.13,<3.14`）
- Git
- 初期設定時に依存packageとmodelを取得できるnetwork
- DB、取得資料、model、索引用のdisk容量
- 旧`.doc`／`.ppt`を取り込む場合だけLibreOffice

Windows利用者向けoffline ZIPの作成はWindows上で行います。SharePoint／Teamsの
Source追加・更新もWindowsだけです。

### Windows PowerShell

```powershell
git clone https://github.com/harilos/github-copilot-local-rag.git
Set-Location .\github-copilot-local-rag
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

PATH外のCPython 3.13.xを使う場合は、最後のコマンドを次に置き換えます。

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -BootstrapPython "C:\path\to\python.exe"
```

更新時はrepositoryで次を実行します。

```powershell
git pull --ff-only
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Windowsのruntime Pythonは次の固定pathに作られます。

```text
%USERPROFILE%\.copilot\rag\query\.venv\Scripts\python.exe
```

### macOS／Linux

```bash
git clone https://github.com/harilos/github-copilot-local-rag.git
cd github-copilot-local-rag
bash ./install.sh
python3.13 -B ~/.copilot/rag/setup.py --format human
```

更新時はrepositoryで次を実行します。

```bash
git pull --ff-only
bash ./install.sh
~/.copilot/rag/query/.venv/bin/python -B ~/.copilot/rag/setup.py --format human
```

source installerは、端末固有のnetwork設定とSource接続設定を保持します。
Windowsの`install.ps1`は、cloneに含まれる同名DBで既存DBを上書きしません。
Copilot CLI側は`COPILOT_HOME`、未設定時は`%USERPROFILE%\.copilot`の
`mcp-config.json`へ`mcpServers` schemaでmergeし、VS Code側はDefault Profileの
`mcp.json`へ`servers` schemaでmergeします。同名の利用者所有設定とは衝突として
停止し、無関係なserver、comment、BOM、改行は保持します。

## 管理版：Managerを使う

### 起動する

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe" -B "$env:USERPROFILE\.copilot\rag\manage.py"
```

macOS／Linux:

```bash
~/.copilot/rag/query/.venv/bin/python -B ~/.copilot/rag/manage.py
```

Managerでは次の操作を行えます。

| やりたいこと | Managerの入口 |
|---|---|
| DBを作る | `1. 新しいDBを作る` |
| DBごとのSourceを追加・更新・再開する | `2. DBを選んで管理する` |
| 全DBのSourceをまとめて更新する | `3. 全DBの全Sourceを更新・再開` |
| 配布版や管理PC引っ越しpackageを扱う | `4. 配布・管理PCの引っ越し` |
| 端末設定と検索動作を確認する | `5. この端末の設定・動作確認` |
| 検索daemonを終了する | `6. 検索daemonを終了` |

Sourceは、Git repository（GitHub・GitLab・Azure DevOps・その他のGit）、SVN、
Redmine、SharePoint、Teams、
GitLab Issue／Wiki、GitHub Issues／Wiki、手元のfile／folderなどの取得元を管理する
単位です。入力項目、更新、再開、除外、
配布、管理PCの移行は
[Local RAG Manager 日本語操作ガイド](.copilot/rag/docs/local-rag-manager-guide-ja.md)
を参照してください。

### Windows配布版をコマンドで作る

出力ZIPと同じpathが既に存在する場合は上書きしません。`--db`を繰り返すと複数DBを
含められ、省略すると全DBが対象になります。

```powershell
$python = "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe"
& $python -B "$env:USERPROFILE\.copilot\rag\make_distribution_package.py" --output "C:\LocalRAG\local-rag-distribution.zip" --db project-rag
```

完成したZIPを展開すると、利用者向けの`install.cmd`と`README-WINDOWS.md`が
入っています。package作成時に固定Python、依存package、model、DB、manifest、
checksumを検証します。利用者PCではPythonやnetworkを使いません。installerは
3つのVS Code Agent、3つのCLI Agent、pinned MCP、専用launcherをtransaction内で
配置・更新し、CLIまたはMCP登録に失敗した場合はそれらを元へ戻します。

### 管理PC引っ越しpackageをコマンドで作る

```powershell
$python = "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe"
& $python -B "$env:USERPROFILE\.copilot\rag\make_admin_transfer_package.py" --output "C:\LocalRAG\admin-transfer"
```

管理PC引っ越しpackageにはDBだけでなく、Source設定と再開情報も含まれます。
credentialと端末固有設定は含めません。

### コマンドで動作確認する

Windows PowerShell:

```powershell
$python = "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe"
& $python -B "$env:USERPROFILE\.copilot\rag\list_dbs.py" --format text
& $python -B "$env:USERPROFILE\.copilot\rag\search.py" --db project-rag --include-db-hint --result-delivery stdout --format prompt "A2Lの目的と採用理由を教えて"
```

macOS／Linux:

```bash
~/.copilot/rag/query/.venv/bin/python -B ~/.copilot/rag/list_dbs.py --format text
~/.copilot/rag/query/.venv/bin/python -B ~/.copilot/rag/search.py --db project-rag --include-db-hint --result-delivery stdout --format prompt "A2Lの目的と採用理由を教えて"
```

この直接CLIはDB一覧と検索結果を確認する診断用です。利用者向けの最終回答は
作文しません。通常の利用者は3つのAgentを使ってください。

## データの扱い

- 配布ZIPには検索対象の資料、抜粋、内部URLが含まれる場合があります。元資料と同じ機密区分で扱ってください。
- credential、端末固有の接続設定、実行中の一時fileは配布ZIPへ含めません。
- Local RAGの検索はPC内で完結しますが、選ばれたEvidenceは回答生成のためGitHub Copilotへ渡ります。
- 検索Agentはread-onlyです。DBやSourceの変更は、管理者がManagerから明示的に行います。

検索の内部構造、MCP、結果contract、Source Metadata、package検証については
[Local RAG system design](.copilot/rag/docs/local-rag-system-design.md)を参照してください。

## License

repositoryの[LICENSE](LICENSE)と、同梱する各dependency／modelのlicenseを確認して
利用してください。
