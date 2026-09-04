# Issue #20 追補 — 再開条件を正本に限定

実施日: 2026-09-05。対象branch: `fix/windows-progress-file-resilience`。
開始HEAD / remote branch: `1be58af4fd7de0db047dbcc33ed75299a6e84215`、作業ツリーclean。
開始remote main: `435d794fe790a68be974db43e2b8e0bafcda1595`。

## 問題と修正

観測系の保存失敗を許容すると、旧runの`progress.json`が残ることがある。
従来のstatusはprogressのroot/source/operation/batchを優先しており、manageもその表示用値から再開していた。
したがって、初回のI/O試験だけではmain統合を許可できなかった。

- `index_state.json.ingestion`を唯一の保存済み制御情報とする共通validatorを追加。
- root、source_id、scan_subdir、operation、batch_size_filesは正本に必須。progressから補完しない。
- chunk条件は正本の値（overlap=0を含む）を保持する。新しいchunk属性を持たない旧正本だけ、従来の抽出既定値1400/160を使う。scope/operation/batchの既定値による穴埋めはしない。
- scope、operation、batch、chunk条件、保存identity、privacy属性が型も含め一致したprogressだけ表示する。片側だけにあるscope属性も不一致とする。
- 不一致/欠損/破損progressは`progress_unavailable`とし、旧phase、current file、error、進捗件数を流用しない。正本stateやmanifestから分かる件数は引き続き表示する。
- resume/force rebuildの生成条件は正本だけから取得する。manageは生のcommandやflat表示値を実行せず、検証済みingestionからallowlist argvを再構成する。
- extract rebuildの旧progress fallbackも撤廃。保存batchと異なる明示CLI指定は拒否する。
- build wrapperへ保存済みroot identityを渡し、再開時の同一性を保持する。別の物理rootを、同じidentity指定だけで許可することはしない。
- 現行の終端状態`success`/`partial`/`failure`も正本からの再開対象として扱う。稼働中の表示では再開を提示せず、writer lockは引き続き最終防御となる。
- private指定とplaintext rootが混在する不正scopeは表示前に拒否。未知のlegacy属性は公開するingestionへ転載せず、既知の制御属性だけを返す。正常なredacted rootから実pathを推測したcommandも生成しない。

正本が欠損、不完全、型不正の場合、保存済み再開とextract再構築を拒否する。
元のSource更新経路で明示した条件から再実行する必要があり、古いprogressによる自動復元は行わない。
正本JSONの読取失敗は引き続き失敗扱いであり、成功へ変換しない。

## 検証

Windows native x64、OS build `10.0.26200`、配布CPython `3.13.15`を使い、branch上の候補を実行する。
実DB・利用者profileを変更せず、synthetic fixtureのみ使用する。

恒久回帰には次を含める。

- 旧progressと新正本のroot/source/operation/batch競合。
- 同じscopeでoperationだけ旧値の場合、batchだけ異なる場合、型不一致。
- optional scope属性の不一致/欠損、正本欠損/不正、progress欠損/不正。
- 一致progressの表示維持、現行/旧終端status、running時の再開禁止。
- flat値と正本が異なるmanage再開、任意command不実行、build wrapperの引数転送。
- extract rebuildの保存batch、旧progress非依存、privacy安全停止。
- 保存identityを指定した実再開と、異なるroot/scopeの拒否。

実行コマンド（repository root、依存入り配布Python）:

```powershell
$issue20Python = Join-Path $env:USERPROFILE '.copilot\rag\query\.venv\Scripts\python.exe'
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py status_scope
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py core
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py regression
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py scope
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py source
git diff --check
```

最終候補での結果:

| group | 件数 | FAIL / ERROR / skip | 所要秒 |
| --- | ---: | --- | ---: |
| status_scope | 25 | 0 / 0 / 0 | 1.228 |
| core | 57 | 0 / 0 / 0 | 58.687 |
| regression | 85 | 0 / 0 / 0 | 10.167 |
| scope | 34 | 0 / 0 / 0 | 3.153 |
| source | 71 | 0 / 0 / 0 | 2.747 |
| 合計 | **272 PASS** | **0 / 0 / 0** | 各group並列実行 |

初回236件の関連groupを再利用し、status/manage・rebuild・identity回帰を追加した。
同じtestを集計上重複加算していない。全repository suiteの再実行ではない。
Windowsの実handle、1000回更新stress、正本保存障害、crash/resume、実配布Python子processとPowerShell 5.1を含む既存coreも再実行してPASSした。
新identity 8件は実checkpoint/resume関数経路を実行し、store操作をmockした契約試験である。既存coreの実SQLite/Chroma試験とは区別する。

開発中の189件PASS後、独立レビューで現行終端statusの判定漏れ、保存identityの再開検証漏れ、private scopeの表示境界が見つかった。
同じ修正範囲で対処し、影響がSource/resumeに及んだため、上表を静止した最終候補に対して再実行した。旧候補のPASSを最終候補の結果へ流用していない。
`git diff --check`もPASS。独立した最終read-onlyレビューで既報3点の解消を確認し、対象差分の新規P0/P1は0件。
今回の追加修正の機械試験はPASS。main統合の承認・実行は行わず、branch上の成果として確定する。

## 変更ファイル

- 製品: `ingestion_paths.py`（共通validator）、`status.py`（正本/表示分離）、`manage.py`（正本argv）、`rebuild_component.py`（正本必須）、`build_db.py`（identity転送）、`incremental.py`（identity検証とprivate移行順序）。
- 恒久試験: `test_status_scope_authority.py`、`test_manager_contracts.py`、`test_rebuild_scope_authority.py`、`test_resume_root_identity.py`。
- 試験実行: `tools/windows_progress/run_acceptance.py`の関連group登録。
- 報告: 本追補と、初回結果報告の失効条件・追補リンク。

## 残事項とGit範囲

- この追補はmain統合の承認ではない。main merge、Issue/PRコメント、Release、公開ZIPは行わない。
- 既存のWindows 10別実機、実運用AV/同期環境、突然の電源断、横断監査P1/P2は未解決のまま。今回の修正で解消したとは扱わない。
- private scopeは元のSource更新経路で扱う。正本に物理rootが残らないcustom identityの同一性を証明できない場合は安全停止する。
- 製品修正→配布成果物再生成は0回。ZIP/installer再生成や実利用環境への上書きは行っていない。
- 対象変更だけを1件の追加commitにし、同じbranchへ通常pushする。最終HEADとremote一致は最終応答に報告する。
