# Local RAG Windows persistent ACL prevention report

作成日: 2026-08-01 JST

## 要約

Windows Python 3.13で永続directoryへmode=0o700を指定した場合にprotected DACLが作られ、別主体をロックアウトする問題を、信頼済み親rootのACL継承へ変更した。POSIXは0700を維持する。既存pathのOwner/DACLは変更せず、ACL修復・takeown・grant処理は追加していない。

実DB、インストール済みLocal RAG、実設定、credential、外部GitHub/Redmine/SVNにはアクセスしていない。試験はrepository fixture、TemporaryDirectory、固定prefixとUUID sentinelを持つ隔離rootだけで実施し、終了後に削除した。

## 完了表

| 項目 | 結果 |
|---|---|
| 作業前branch／HEAD／origin/main | fix/manager-production-recovery / 5e3707804256cd05e3c2958cf61366b5927463ec / d22e031638359188dd59af5052c2f95ff03e7f1f |
| 作業後branch／HEAD | fix/manager-production-recovery / 7f00750（report作成前） |
| 変更ファイル | persistent policy 1、呼出側5、test 1、二主体受入script 1、本report |
| persistent経路の棚卸し | 指定6系統すべてpersistentまたはpersistent stagingとして修正 |
| 修正前の隔離再現 | Windows Python 3.13、別主体listでWinError 5、Protected=true、Inherited ACE=0 |
| unit／contract test | Windows 13 test、12 pass、symlink作成権限不足1 skip。reparse属性拒否testはpass |
| 機能別隔離回帰 | Other snapshot、GitHub Issues初回/再更新、DB copy、copy-only import/差替え、rollbackがpass |
| Windows Python 3.13二主体受入 | A作成→B利用、B作成→A利用の両方向で全操作成功、WinError 5=0 |
| POSIX 0700回帰 | WSL Ubuntuで13 test pass、skip 0。実mode 0700を確認 |
| 全test file／test／skip／失敗 | 修正前64/848/17/7 failed files、修正後65/861/18/同じ7 failed files |
| 実DBへのアクセス・変更 | なし |
| commit SHA | cd67da9（実装）、7f00750（test/受入script） |
| push／PR／merge | なし |

## 変更ファイル

### 実装

- .copilot/rag/source_manager/persistent_paths.py
- .copilot/rag/source_manager/store.py
- .copilot/rag/source_manager/execution.py
- .copilot/rag/source_manager/github_content.py
- .copilot/rag/source_manager/database_copy_core.py
- .copilot/rag/source_manager/copy_only_packages.py

### テスト・受入

- .copilot/rag/source_manager/tests/test_persistent_paths.py
- tools/windows_acl_acceptance.py

## 6候補経路の判断

| 候補 | 分類 | 対応 |
|---|---|---|
| SourceStore.ensure_work_directory() | persistent directory | 共通directory policyへ移行。Windowsはmode省略、POSIXは0700 |
| execution._ensure_real_directory() | persistent directory | 親を検証後、共通policyで1 componentずつ作成 |
| execution._materialize_snapshot() | persistent staging | 親直下の一意stageを共通staging policyで確保し、従来のreplace/rollbackを維持 |
| github_content.fetch_github_issues() | persistent staging | tempfile.mkdtemp(dir=work parent)を廃止。Issues子directoryも共通policy化 |
| database_copy_core.copy_database() | persistent staging | private temporary parentを廃止し、DB親直下の継承stageから公開 |
| copy_only_packages._publish_copy_tree() | persistent staging | DB親直下の継承stageへ移行。DB rootと子directoryも共通policy化 |

execution._copy_tree()が作る公開子directoryも共通policyを通すようにした。copy-only importが作るrag/dbs、DB stage、子directoryも同じpolicyを使う。

## policy契約

create_persistent_directory:

- Windowsではmkdirへmodeを渡さない。
- POSIXではmode=0o700を明示する。
- Python minor versionではなくOSで分岐する。
- parents、exist_ok、既存fileの契約を保持する。
- trusted root逸脱、symlink、junction、reparse point、非directoryを拒否する。
- 既存directoryへexist_ok=Trueで再入場してもACL操作をしない。

create_persistent_staging_directory:

- 信頼済み永続親直下に暗号学的nonceを含む一意名を作る。
- exist_ok=Falseで原子的に確保する。
- 衝突時だけ有限回再試行する。
- 作成後もreal directoryとroot内を再検証する。
- Windowsでは親ACLを継承し、POSIXでは0700を使う。

PermissionError:

- Source一覧取得でPermissionErrorを欠損として隠さない。
- DB識別子と管理root相対pathだけを含む安全なSourceManagerErrorへ変換する。
- ACL owner相違／継承無効の可能性と、対象限定の管理者確認が必要なことを示す。
- Managerの常時管理者実行を案内しない。

## private temp分類

次はprivateのまま維持した。

- query/result_bundle.pyのresult spool、items、responses。
- package ZIP展開用TemporaryDirectory。
- 同一process内だけで使い、公開せず最後に削除する一時領域。

result_bundle.pyにはpersistent policyを導入していない。mode=0o700が残ることを静的testで確認し、result bundle contract 33 testもpassした。異主体共有の明示製品契約は確認されなかったため、共有化commitは作成していない。

## 修正前の隔離再現

実DBと無関係な固定prefixの隔離rootを、主体AがWindows Python 3.13のPath.mkdir(mode=0o700)で作成した。

結果:

- 主体Bのdirectory list: PermissionError / WinError 5。
- DACL Protected: true。
- Inherited ACE count: 0。
- fixtureはUUID sentinel確認後、作成主体で削除。

この再現により、データ欠損ではなく作成時ACLの問題を隔離環境で固定した。

## 二主体受入

tools/windows_acl_acceptance.pyをcreate、consume、cleanupの3 phaseで使用した。password、credential、account作成、権限昇格、ACL変更はscript内に存在しない。

### 主体A作成 → 主体B利用

成功した操作:

- list
- read
- write
- atomic replace
- rename
- delete

WinError 5: 0。

### 主体B作成 → 主体A利用

同じ6操作が成功。WinError 5: 0。

### DACL読取り確認

両方向fixtureとも次を確認した。

- Protected: false。
- Inherited ACE count: 6。
- Everyone、Authenticated Users、BUILTIN Usersへの新規explicit broad grant: 0。
- exist_ok=True再入場前後のSDDL SHA-256: 一致。

Owner名、SID、SDDL本文は報告へ保存していない。

許可していない第三主体による実access拒否は、第三の非昇格tokenを用意していないため未実施。ただし製品コードと受入scriptにACL grant処理はなく、隔離DACL上の新規explicit broad grantは0だった。

## 機能別隔離回帰

- Source work: mode分岐、parents/exist_ok、PermissionError契約をunitで確認。
- Git/Wiki/SVN provider control: execution._ensure_real_directory()の多段作成を隔離rootで確認。
- Other snapshot:新tree公開、旧leftover削除、stage/backup cleanupを確認。
- Other rollback: stage公開失敗を注入し、旧work復元とstage cleanupを確認。
- GitHub Issues: local command runner fixtureで初回更新と同内容再更新を実施。
- DB copy:隔離DB fixtureをpersistent stageから公開し、stage残存なしを確認。
- copy-only import:新規importと同名fixture差替えを実施し、backup/stage残存なしを確認。
- 外部GitHub、Redmine、SVNへの通信は行っていない。

## テスト結果

### 新規ACL test

Windows:

- Ran 13 tests。
- pass 12。
- skip 1: OS権限によりdirectory symlinkを作成できないため。
- junction/reparse拒否はst_file_attributes fixtureで別途pass。

WSL Ubuntu:

- Ran 13 tests。
- pass 13。
- skip 0。
- POSIX実directory mode 0700を確認。

### focused regression

- Redmine incremental、SVN validation、DB copy、GitHub content: 39 pass。
- package/copy-only contracts: 7 pass。
- result bundle contracts: 33 pass。
- Windows SVN protocol integration: svn executable不在によりbaselineと同じ1 failure。

### 全回帰baseline比較

修正前HEAD 5e37078を一時detached worktreeで同一Python・同一依存・同一runnerにより測定した。

| 測定 | test files | tests | skip | failed files |
|---|---:|---:|---:|---:|
| 修正前baseline | 64 | 848 | 17 | 7 |
| 修正後 | 65 | 861 | 18 | 7 |
| 差分 | +1 | +13 | +1 | 0 |

failed filesは修正前後で同一:

- query/test_completion_marker_repair_contracts.py
- query/test_distribution_package_smoke.py
- query/test_lightweight_routing_contracts.py
- query/test_manager_contracts.py
- query/test_manager_source_menu_routing.py
- source_manager/tests/test_contracts.py
- source_manager/tests/test_svn_protocol_windows.py

主な既知環境要因はWindows symlink権限不足、svn executable不在、既存branch由来のquery contract差分。baselineは更新していない。

## その他のgate

- compileall: pass。
- git diff --check: pass。
- credential assignment scan: 0。
- persistent対象モジュールの限定AST再発防止: pass。
- persistent親をdir=に渡すtempfile.mkdtemp: 0。
- persistent対象での直接mkdir(mode=0o700): 0。
- ACL変更API、takeown、icacls、Set-Acl、chmodによる修復: 追加なし。
- 一時baseline worktree、wheelhouse、dependency directory、ACL fixture: 検証後に削除済み。

## 未完了・警告

- 第三の非昇格security tokenによるnegative access testは未実施。
- Windows環境にsvn executableがなく、SVN protocol実integrationはbaseline同様failure。
- Windowsのsymlink作成権限がなく、実symlink unit 1件はskip。reparse属性fixtureと既存fail-closed処理で補完した。
- origin/mainは前回portable setup変更によりd22e031へ進んでいるが、本branchは既存A〜Cをrebaseせず、その後続commitとして維持した。
- push、PR、mergeは実施していない。
