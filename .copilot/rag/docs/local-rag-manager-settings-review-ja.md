# Local RAG Manager 設定項目レビュー

この文書は、通常利用者が入力する設定を最小化するための棚卸し結果です。
分類は、A: 削除、B: 自動化、C: 読み取り専用、D: 詳細設定、
E: 通常設定として維持、を表します。

| 画面 | Provider | 旧項目名 | 新しい表示名 | 分類 | 対応内容 | 実際の参照箇所 | 理由・互換性 |
|---|---|---|---|---|---|---|---|
| Source詳細 | 共通 | `source_id` | Source ID（読み取り専用） | C | catalog由来の値を表示のみ | Source inventory、検索結果 | 文書identityと取り込み状態に関係するため変更しない |
| Source詳細 | 共通 | `display_name` | Source表示名 | E | 任意の表示用値として維持 | Managerの一覧・詳細 | identityや検索には影響しない |
| Source詳細 | 共通 | `observed_root` | 自動検出された保存ルート | B/C | 現在有効なcatalog文書から自動導出し、入力不可 | `source_inventory.py`、`source_links.py` | URL生成時にpath component単位で1回だけ除去する内部状態 |
| Source情報 | 共通 | — | Source種別 | E（任意） | 未設定、folder、git、GitHub、GitLab、Azure DevOps、SVN、SharePoint、Redmine、その他から選択 | `source-links.json`の`source_type` | Linkなしでも設定でき、未設定をfolderへ推測しない |
| Source Link | 共通 | Provider | Source種別から自動 | B | Link設定時に選んだProviderを`source_type`へ保存し、nested `link`には重複保存しない | `validate_source_link()`、URL resolver | Provider二重管理を避ける。Linkなしなら種別も任意 |
| Source Link | 共通 | `enabled` | 有効・無効 | E | 専用の切替操作を維持 | URL resolver | 設定を消さず一時停止できる |
| Source Link | SharePoint | `source_home_url` / Home Root | — | A | UI・通常保存・summaryから削除 | legacy sidecar readerのみ | ファイル直接リンクで未使用。旧値は読めるが、基準URLを保存し直すと除去する |
| Source Link | SharePoint | `home-only` | — | A | Managerで新規作成不可。明示migrationでは旧設定を保持 | legacy sidecar reader／migration | 曖昧なトップページへフォールバックせずpath-only。移行で既存metadataを捨てない |
| Source Link | SharePoint | strategy選択 | ファイル直接リンク（自動） | B | `append-relative-path`へ固定し、選択画面を出さない | `manage.py`、`source_links.py` | 通常利用者の判断が不要 |
| Source Link | SharePoint | `source_web_root` | SharePoint上の基準フォルダURL | E | 唯一のURL入力として必須 | SharePoint URL generator | Source相対パスを追加する基準として必要 |
| Source Link | SharePoint | Forms URL正規化・path encode | — | B | 内部で自動処理 | SharePoint URL normalizer | 利用者が判断する項目ではない |
| Source Link | GitHub | strategy選択 | GitHubファイルリンク（自動） | B | `github-blob`へ固定し、選択画面を出さない | GitHub URL generator | Provider選択から一意に決まる |
| Source Link | GitHub | `repository_url` | GitHubリポジトリURL | E | 必須 | GitHub URL generator | リポジトリは自動推測しない |
| Source Link | GitHub | `ref` | ブランチ・タグ・コミット（ref） | E | 必須 | GitHub URL generator | 表示する版を自動推測しない |
| Source Link | GitHub | `repository_path_prefix` | GitHubリポジトリ内の追加パス | E | 任意 | GitHub URL generator | 外部repository内の配置差を補う。RAG保存rootとは別物 |
| Source Link | GitHub | `commit` | 固定リンク用コミット | E | 任意 | permalink generator | 固定版を必要とする場合だけ入力する |
| Source Link | GitHub | `permalink_enabled` | — | B | commit入力の有無から自動設定 | Manager保存処理 | 重複するYes/No入力を不要にする |
| Source Link | GitLab | strategy選択 | GitLabファイルリンク（自動） | B | `gitlab-blob`へ固定し、選択画面を出さない | GitLab URL generator | Provider選択から一意に決まる |
| Source Link | GitLab | repository/ref/追加path/commit | GitLabリポジトリ設定 | E | Git共通フォームで必要な値だけ入力 | GitLab URL generator | GitLab.com、subgroup、セルフホストを同じ設定で扱う |
| Source Link | Azure DevOps | strategy選択 | Azure DevOpsファイルリンク（自動） | B | `azure-devops-item`へ固定し、選択画面を出さない | Azure DevOps URL generator | 通常refはbranch（GB）、固定commitはGCとして生成する |
| Source Link | Azure DevOps | repository/ref/追加path/commit | Azure DevOpsリポジトリ設定 | E | Git共通フォームで必要な値だけ入力 | Azure DevOps URL generator | queryはManagerで組み立て、入力URLのquery/fragmentは拒否する |
| Source Link | Subversion | strategy | SVNリンク形式 | E | `svn-http`と`svn-web-root`を人が明示選択 | SVN URL generator | mod_dav_svn直リンクと製品固有Web画面を自動判定しない |
| Source Link | Subversion | repository URL / path / revision | Apache HTTP(S)設定 | E | URL、任意追加path、固定link有無、revisionだけ表示 | SVN HTTP URL generator | ref/commitや`.svn`には依存しない |
| Source Link | Subversion | Web root URL | SVN Web画面のトップURL | E | `svn-web-root`ではこの1項目だけ表示 | SVN Web-root resolver | query/fragment/trailing slashを保持し全結果へ同じURLを付ける |
| Source Link | Redmine | strategy | リンク方式 | E | home、相対path、正規表現から用途に応じ選択 | Web URL generator | Issue、Wiki、単一入口で生成方法が異なる |
| Source Link | Redmine | `path_pattern` / `url_template` | 正規表現 / URLテンプレート | D | 上級者向け表示とpreviewを維持 | regex-template resolver | ID抽出が必要な場合のみ使用 |
| Source Link | その他Web | strategyと対応URL | リンク方式と基準URL | E/D | 選択した方式に必要な欄だけ表示 | Web URL generator | 汎用Providerの用途差を吸収する |
| Source Link preview | 共通 | stored/relative path、生成URL | 生成URLの確認 | D/C | 読み取り専用preview | resolver preview | 保存前の確認用で、設定値ではない |
| 状態 | 共通 | 生JSON | 診断用の詳細JSON | D | 標準画面から外し、選択時だけ表示 | `status.py --json` | 通常利用者には人間向け要約で十分 |
| build/add | 共通 | `--include-root-name-in-path` | — | B | 常時有効としてUIへ出さない | build/add CLI | 固定値であり選択不要 |
| build/add | 共通 | root / Source ID / scan subdirectory | 論理ルート / Source ID / 読込サブディレクトリ | E | 必須・任意と例を明示 | build/add CLI | 取り込みscopeとidentityを決めるため必要 |
| add | 共通 | retry errors | 抽出エラーを再試行 | E | 明示確認時だけ有効 | add CLI | 通常の更新と失敗再試行を区別する |
| DB作成 | 共通 | title / query hint | 表示名 / 検索ヒント | E | 任意 | create CLI、DB選択 | 人間表示とDB選択に用途がある |

## 互換性

- DB schema、検索・build・addのCLI引数は変更しません。物理ファイル名は
  `source-links.json`のまま、schemaは`rag-source-metadata-v1`です。
- 旧SharePoint `home-only` は明示migrationで既存metadataを保持できますが、
  検索では従来どおりpath-onlyへfail-openします。Managerからの新規作成は
  できません。
- `append-relative-path`の旧SharePoint設定に`source_home_url`が含まれていても
  読み込めます。canonicalな再保存では`source_web_root`だけを残します。
- GitHubの旧`append-relative-path`表記は読み取り時に`github-blob`へ正規化し、
  新規保存では受け付けません。
- observed root、Source相対path、Provider別strategyの自動化は、文書ID、
  chunk ID、検索順位、検索statusを変更しません。
