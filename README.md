# GitHub Copilot Local RAG

> 開発版: `1.0.1` / GitHub Release: 未公開
>
> このREADMEは、[PR #19](https://github.com/harilos/github-copilot-local-rag/pull/19)の
> 個人Skill移行を取り込んだmainの開発版を説明しています（2026-09-04時点）。

ローカル文書や社内資料を、GitHub Copilotへ`/local-rag`に続けて質問して検索する
ためのパックです。専用のカスタムAgent、MCP server、専用launcherは使いません。

文書の抽出・索引作成・検索はPC内で行います。回答生成のため、質問に応じた抜粋・
出典・検索メタデータがGitHub Copilotへ渡ります。DB全文を送信したり、検索処理が
元資料のURLを自動で開いたりすることはありません。

Linux／Windowsの関連自動試験と実ユーザー環境でのrunner検索を確認済みです。
ただし、Copilot上の操作感・承認表示・回答の見え方は人力感応試験待ちです。
CLIの確認済み制約は[Copilot CLIで使う](#copilot-cliで使う)を参照してください。
自動試験の合格を、最終的な利用承認やRelease公開の完了とは扱っていません。

## すぐ使う

資料を検索する人は、管理者から受け取ったWindows x64配布ZIPを使います。
DBを作成・更新する人は、後半の[管理者向け](#管理者向け)へ進んでください。
配布版は検索専用で、ManagerやDB作成・更新機能は含みません。

### 必要なもの

- Windows 10以降（x64）とZIPを展開する空き容量
- サインイン済みのVS Code＋GitHub Copilot Chat、またはPowerShell 7＋GitHub Copilot CLI
- 管理者から受け取った配布ZIPと、検索に使うDB名

配布版には固定Python、依存package、ONNX model、選択されたDB、個人Skillが
含まれます。インストールにはsystem Python、pip、PATH変更、管理者権限、network
接続は不要です。Copilotで回答を生成するときはGitHub Copilotへの接続が必要です。

### インストールして最初の質問を送る

1. ZIPを通常のローカルフォルダへ**すべて展開**します。ZIP内から直接実行しません。
2. 展開先の`install.cmd`をダブルクリックします。
3. `Local RAG インストール結果: 成功 (SUCCESS)`を確認します。
4. VS Codeを完全に終了して再起動し、Copilot ChatをAgentモードにします。
5. 入力欄の`/`メニューから`/local-rag`を選び、その後ろへ質問を書きます。

次の`project-rag`は例です。配布元から案内されたDB名と、実際の資料に合う質問へ
置き換えてください。

```text
/local-rag project-ragで、このシステムの目的と採用理由を根拠付きで教えて
```

インストール先は`%USERPROFILE%\.copilot`です。Copilotへ初期設定を依頼する必要は
ありません。`/local-rag`は手動起動専用なので、通常の質問から勝手に検索しません。
PowerShellからインストールする場合は、展開先で`.\install.cmd`を実行します。

## 検索モードと根拠

### モードを選ぶ

| mode | 向いている質問 | 検索方針 |
|---|---|---|
| `standard`（既定） | 普段の質問 | 単純な質問は1回、広い質問は必要に応じ最大4回 |
| `savings` | 用語・識別子・単純な事実 | 1回だけ検索し、簡潔に回答 |
| `thorough` | 比較・矛盾確認・複数資料の調査 | 異なる観点で3〜4回検索し、回答前に不足を確認 |

```text
/local-rag mode=savings project-ragで、用語Aの意味を教えて
/local-rag mode=thorough project-ragで、方式Aと方式Bを設計・運用・障害対応の観点で比較して
```

どのモードも、VS CodeまたはCopilot CLIで現在選択中のモデル（Autoを含む）を
継承します。モードは検索方針の違いであり、モデルの切替や料金の保証ではありません。

DB名を省略すると一覧を1回確認し、説明と質問から明確に1つへ絞れる場合だけ
検索します。選べなければ利用者へ選択を求め、推測で検索しません。
1回の呼出しでは1つのDBを使います。質問には目的・期間・条件・比較軸を含めると、
意図に合った資料を探しやすくなります。内部の検索方式を指定する必要はありません。

### 回答と出典を見る

根拠のある主張にはEvidence IDが付き、出典は回答末尾の`## References`にまとまります。

- `[E…]`: 質問へ直接答える根拠
- `[B…]`: 背景情報
- `[D…]`: 関連資料候補。直接根拠とは限りません

複数回検索したときは`[R1-E1]`、`[R2-D1]`のように検索順も付きます。
Referencesでは、DBに元資料URLがあればクリックできるリンク、なければ資料名が
表示されます。URLやローカルpathを利用者が組み立てる必要はありません。

`partial`は根拠不足が残る状態、`no_hit`は直接根拠が見つからない状態です。
関連資料だけで確定した回答と受け取らず、不足や留保も確認してください。
抜粋だけでは足りない場合は、Skillが利用可能な範囲でキャッシュされた詳細を
取得します。詳細には有効期限があり、期限切れなら必要な質問を改めて送ってください。

### 承認画面について

**既定では承認設定を変更しません。既存の許可も自動解除しません。**
実行確認の有無はVS Code／Copilot CLIと組織の承認設定に従います。
このSkillを使うために包括的なterminal許可や組織MCPポリシーの変更は必要ありません。

Windowsのソース版・配布ZIP版installerでは、次のオプションを明示指定できます。
両方とも既定はOFFで、同時指定時は適用しません。

| オプション | 設定する範囲 |
| --- | --- |
| 指定なし | 既存設定を維持。実行確認はhostと既存規則に従う |
| `-ConfigureVSCodeRunnerApproval` | 固定venv Python＋`-I -X utf8 -B`＋固定`skill_runner.py`＋`list/search/detail/setup`の限定規則 |
| `-ConfigureVSCodeAutoApprove` | **危険・非推奨**。全workspaceのファイル操作・terminal・MCP等を含む全体の自動承認 |

```powershell
# 個別ランナー許可（ソース版 / ZIP版のどちらか）
.\install.ps1 -ConfigureVSCodeRunnerApproval
.\install.cmd -ConfigureVSCodeRunnerApproval

# 危険・非推奨：組織の許可を得た場合だけ
.\install.ps1 -ConfigureVSCodeAutoApprove
.\install.cmd -ConfigureVSCodeAutoApprove
```

個別規則は`chat.tools.terminal.autoApprove`、全体許可は
`chat.tools.global.autoApprove`を、通常版VS Codeの
`%APPDATA%\Code\User\settings.json`に設定します。
名前付きprofile、Insiders、Remote環境、Copilot CLIの`copilot -i`には適用しません。
個別規則はWindows PowerShellの固定絶対path形式と、既定install先の場合のみ
Skill記載の`$env:USERPROFILE`形式に対応します。環境変数の差替えを防ぐ仕組みではありません。
単一引用符の質問（内部の引用符は二重化）を使い、連結・パイプ・リダイレクト・
追加のPowerShell呼出しは対象外です。別形式は手動承認に戻ります。

**組織で許可されていない場合は有効にしないでください。** 組織ポリシーが禁止すれば
設定しても機能しません。installerは検出した禁止、既存のterminal許可無効、
不正な設定JSONを上書きせず、適用できない場合は警告します。
ポリシー未検出は組織の承認証明ではありません。VS Codeの初回確認は省略しません。
設定保存の成功は、実際の自動承認の成功を意味しません。
個別規則もbest-effortであり、OSの権限や安全な隔離を与える仕組みではありません。
runnerの固定DB root・child command・引数検証は維持します。
他の既存許可やhostの既定規則も残るため、git push等が必ず確認されるとは保証しません。
既存のglobal許可が有効なら個別モードは適用せず警告します。
解除する場合はVS Code設定から追加した規則を削除し、global設定はfalseへ戻してください。
詳細は[VS Code承認仕様](https://code.visualstudio.com/docs/agents/run/approvals)と
[組織ポリシー](https://code.visualstudio.com/docs/enterprise/policies)を参照してください。

通常検索は、固定のvenv Pythonと`skill_runner.py`を通じたread-only操作です。
確認が表示されたら、インストール先の`.copilot/rag/query/skill_runner.py`を実行する
コマンドであることと引数を確認してください。質問はcommand previewやshell履歴へ
表示される可能性があるので、そこへ残せない秘密値は含めないでください。
`setup_required`時だけrunner経由でsetupを1回試します。失敗した場合に利用者が
内部ファイルを探したり、別の低水準コマンドへ迂回したりする必要はありません。

## Copilot CLIで使う

通常の`copilot`を作業フォルダで起動し、**対話画面**へ同じ形式で入力します。

```text
/local-rag project-ragで、このシステムの目的を根拠付きで教えて
```

既存sessionでSkillが見えなければ、対話画面で次を順に実行します。

```text
/skills reload
/skills info local-rag
```

2026-09-02〜03のCopilot CLI **1.0.80**での確認では、非対話`copilot -p`に渡した
`/local-rag`はSkillとして展開されず、runnerも呼ばれませんでした。対話モードでは
Skill展開とrunner呼出しを確認していますが、実地試験には試験環境由来の残差があり、
最終的な操作確認は未完了です。ここでは対話モードを案内し、他のCLIバージョンでも
`-p`が非対応と断定しません。

旧`local-rag-copilot` launcher、専用profile、専用MCPは使用しません。

## 更新と困ったとき

### 配布版を更新する

配布DBは作成時点のsnapshotです。元資料を更新した場合は、管理者から新しい配布版を
受け取ります。同名DBは既定で置き換えません。置換してよい更新版と確認できた場合に
限り、新しいZIPの展開先で次を実行します。

```powershell
.\install.cmd -ReplaceExistingDatabases
```

置き換わるのはZIPに含まれる同名DBだけで、別名DBは保持します。
自動実行で終了待ちを省く場合は`-NoPause`を追加できます。

新規installはMCP設定・カスタムAgent・PowerShell profile・既定でのVS Code承認設定を
作成しません。旧版からの更新時だけ、所有manifestとhashを確認し、製品が配置した
旧Agent003／MCP／launcherを撤去します。無関係な設定・Agent・profile本文は保持し、
所有物と確認できない場合は勝手に削除しません。
Copilotによる実地受入はinstallerや製品testでは実行しません。

### よくある問題

| 状況 | 対応 |
|---|---|
| VS Codeの`/`メニューに出ない | 完全終了して再起動し、`Chat: Open Customizations`のSkillsで`local-rag`を確認 |
| CLIに出ない | `/skills reload` → `/skills info local-rag` |
| 同名DBがありinstallできない | 更新版と確認したうえで`-ReplaceExistingDatabases`を指定 |
| DBを選べない | 管理者から案内されたDB名を質問へ明記 |
| 根拠が不足する | 条件や比較軸を追加するか、`mode=thorough`で質問 |
| runtimeがない・setupが失敗する | インストール結果を確認し、同じinstallerでの修復を管理者へ相談 |
| installが`FAILED`になる | 画面に表示されたログを管理者へ渡す |

配布版installerのログは通常`%LOCALAPPDATA%\LocalRAG\logs`の
`portable-install-<timestamp>-<pid>.log`です。保存できない場合はTEMPへ切り替わるため、
成功・失敗どちらでも画面に表示されるログの絶対パスを確認してください。
`.copilot`全体の手動削除は、他の設定・Skill・DBも失うので行わないでください。

## 管理者向け

管理版はDB・Sourceの作成／更新、診断、配布版の作成を行うsource版です。
同じ`/local-rag` Skillを利用でき、管理操作はLocal RAG Managerから行います。

### 管理版の導入

必要なのはGit、CPython 3.13.x（`>=3.13,<3.14`）、初期設定時の依存package・model
取得用networkと空き容量です。Windows x64、macOS、Linuxで利用できます。
旧`.doc`／`.ppt`の取込みには別途LibreOfficeが必要です。
Windows配布ZIPの作成とSharePoint／TeamsのSource追加・更新はWindows限定です。

**mainを明示してclone**します。以下は新しいclone向けです。
既存の作業ツリーへ無理に適用しないでください。

Windows PowerShell:

```powershell
git clone --branch main --single-branch https://github.com/harilos/github-copilot-local-rag.git
Set-Location .\github-copilot-local-rag
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

PATH外のCPythonを使う場合は、最後のコマンドへ
`-BootstrapPython "C:\path\to\python.exe"`を追加します。
Windowsのruntimeは`%USERPROFILE%\.copilot\rag\query\.venv\Scripts\python.exe`です。

macOS／Linux:

```bash
git clone --branch main --single-branch https://github.com/harilos/github-copilot-local-rag.git
cd github-copilot-local-rag
bash ./install.sh
python3.13 -B ~/.copilot/rag/setup.py --format human
```

mainのcleanなcloneを更新するときは、`git pull --ff-only`後に上記の
installを再実行します。macOS／Linuxでruntime作成済みなら、setupには
`~/.copilot/rag/query/.venv/bin/python -B ~/.copilot/rag/setup.py --format human`を使います。
Windowsのsource installerは既存DBを上書きしません。端末固有のnetwork／Source
接続設定を保持し、旧製品統合の撤去には配布版と同じ所有権確認を行います。

### ManagerとSource

SourceにはGit、SVN、Redmine、SharePoint／Teams、GitLab Issue／Wiki、
GitHub Issues／Wiki、Confluence、手元のfile／folderを扱う実装があります。
対応条件、Managerの起動・メニュー、DB更新・再開、配布と管理PC移行、診断コマンドは
[Local RAG Manager 日本語操作ガイド](.copilot/rag/docs/local-rag-manager-guide-ja.md)へ
まとめています。通常の`/local-rag`へ管理を依頼しても、Managerを自動起動したり
DB・Sourceを変更したりはしません。

## データと詳細資料

- 配布ZIPは資料・抜粋・内部URLを含むため、元資料と同じ機密区分で扱ってください。
- credential、端末固有の接続設定、実行中の一時fileは配布ZIPへ含めません。
- 検索はPC内ですが、選ばれたEvidenceはGitHub Copilotへ渡ります。
- 技術仕様は[system design](.copilot/rag/docs/local-rag-system-design.md)と
  [`/local-rag` Skill移行設計](.copilot/rag/docs/local-rag-slash-skill-design-ja.md)を参照してください。

## License

[LICENSE](LICENSE)と、同梱dependency／modelのlicenseを確認して利用してください。
