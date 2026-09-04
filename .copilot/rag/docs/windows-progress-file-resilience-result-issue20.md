# Issue #20 — Windows progress I/O 堅牢化の結果

> 2026-09-05追記: 以下は初回試験時点の記録です。以後、status/manageが古いprogressから再開条件を選ぶ制御依存が見つかり、main統合前NO-GOとなりました。追加修正・再試験は[正本scope追補](windows-status-scope-authority-followup-issue20.md)を参照してください。以下の旧progress fallback記述も追加修正で廃止しています。mainは未統合です。

実施日: 2026-09-04。判定: **GO_WITH_RESIDUAL**。

Windows実機で内蔵Watcherの自己競合を再現し、修正候補の関連236テストが全件PASSした。skip・FAIL・ERRORは0件。Windows 10の別実機、実運用AV/同期ソフト下、突然の電源断は未実施なので、全Windows環境への無条件GOとはしない。Issue #20を参照のみとし、重複Issue #21、Issue/PRコメント、main、Release、公開ZIPは操作していない。

## 開始状態とGit

- repository: `harilos/github-copilot-local-rag`
- 開始worktree: `main`、clean。既存未コミット変更はなかった。他worktreeは変更していない。
- fetch後のHEAD / origin/main: `435d794fe790a68be974db43e2b8e0bafcda1595`
- 上記から新規作成: `fix/windows-progress-file-resilience`
- push前remote main再照合も同じSHA、同名remote修正branchは未作成だった。
- 修正commit:
  - `741193619a1a60c4d266a56f6d036562f91fc68f`: Windows atomic replace retryと19テスト。
  - `32ac124c13065fc12ab5dd692007c538cb73ee4b`: 正本scope移行、snapshot Watcher、観測系分離、Source/UI結果伝播と試験。
  - `9faaaa0650beac303d940eabcd0db9a31c0e2dde`: 再現したstatus/inventory読取競合への限定修正とstress/contract試験。
- この報告・横断監査・軽量実行スクリプトは別の証拠commitにまとめる。通常pushの最終HEAD/remote readbackはタスク最終応答に記載する。force pushやmain統合は行わない。
- 製品修正→配布成果物再生成は0回。ZIP再生成/公開・実利用環境への上書きinstallなし。既存配布Pythonと依存を再利用して、branch上の候補コードを実行した。追加サイクル承認は消費していない。

## 根因の証明

`tools/windows_progress/reproduce_watcher.py`は開始commitから元の`add_data.py`と`atomic_io.py`を読み込む。synthetic `progress.json`だけを使い、元Watcherの通常`Path.open`直後にスレッドを待機させて競合窓を決定論的に拡大する。共有フラグは変更しない。

確認した順序:

1. Watcher threadが通常のread handleを開く。
2. producer threadが完全な一時JSONを書き、fsync後に`atomic_io.os.replace`する。
3. 置換がWinError 5で失敗する。旧targetのbytesは完全、一時ファイルは0件。
4. readerを解放し、Watcherを終了する。

これにより「同じプロセス内の表示用readerが置換を妨げ得る」機構は証明できた。DB writer lockはこのreaderを保護しない。元の障害発生時の全handle所有者を記録したトレースはないため、実障害のすべてをWatcherだけに帰属させるものではない。AV等の外部readerも同じ種類の競合を起こし得る。

別のnative childでdelete共有を許可しないhandleを保持した試験では、未修正版は即座にWinError 5、修正版は約300ms後のcloseから回復した。観測値の一例は旧版約3.4msで失敗、修正版約357.5msで成功。3秒保持では約2秒で打ち切り、旧JSONと一時ファイルcleanupを確認した。

## 修正と失敗ポリシー

| 対象 | 修正後の方針 |
| --- | --- |
| 共通atomic replacement | WindowsのWinError 5/32/33だけをmonotonic時計で最大約2秒再試行。10/20/40/80/100ms、以後100ms上限。非Windows・他エラーは即時raise。target先行削除/truncateなし。 |
| `progress.json` | ADD invocationごとのsnapshotを保持。永続化OSError後は当該sinkをdisableし、一度だけpathなし警告。以後もメモリsnapshotとmanager frameは更新する。 |
| gen_db `events.jsonl` | append-openだけ安全にretry可能。write/flushを再送して重複追記しない。永続化OSErrorで当該sinkを一度だけ警告してdisable。Windows CRTがwinerrorを付けないEACCESを返す場合は即時degraded。 |
| `index_state.json`、manifest、DB、設定、credential | best-effortへ変更していない。保存失敗はエラー。Source stateのrevision/etag再確認・CASは元のまま。 |
| status/inventory JSON reader | stressでCRT `PermissionError(errno=13, winerror=None)`を実再現。reader専用helperだけがWindowsのこの限定条件もretryする。writerの対象は拡張しない。期限後は元のraise/診断、JSON解析エラーも元の扱い。 |

`TypeError`、JSON serialization error等は握り潰さない。失敗状態を書こうとして生じた進捗/イベントOSErrorで、本体の元例外を置き換えない。sinkのdisable状態はContextVarでrun終了時に復元し、次回実行へ残さない。複数ADDを呼ぶSourceでは各ADDの劣化状態を最終結果へ集約する。

Watcherは`progress.json`をopen/statしない。producer snapshotをCondition付きsingle-slotへ渡し、timer threadでETAを更新する。run限定context managerが`incremental.write_progress`のpatchをfinallyで復元する。manager protocolのframe prefixとpayload契約は維持し、無効時はWatcher threadを作らない。

Source結果は`observability_degraded`と、`progress`/`events`だけの`observability_failed_sinks`を検証して保持する。privacy-safe経路、Redmine/GitLab/Confluence batch経路でも集約する。Managerには固定文言で「観測表示が古い可能性」を警告し、DB更新結果とは別に表示する。任意のsink名・path・例外本文はこのmetadataへ通さない。

## progressの制御依存除去

`index_state.json.ingestion`にoperation/chunk条件も保存し、最初の進捗通知より前に正本として確定する。`rebuild_component --component extract`はこのscopeを優先する。scopeキーのない旧DBだけprogressをfallbackとして使い、present-but-invalidの正本を古いprogressで置換しない。

旧正本に新しいchunk条件がない場合は歴史的既定値を使い、overlap=0は保持する。privacy-safe rootから実pathを推測せず、元のSource更新経路でrootを与えるよう安全停止する。同一private scopeのresume/custom root identity、reset失敗が最初のpersistent writeより前である既存契約も検証した。

## 最終試験

Windows native x64、OS build `10.0.26200`（25H2、UBR 9278）、既存配布CPython `3.13.15`。native handle競合試験は通常sandbox実行であり、管理者権限で競合を回避していない。GitHub認証/commit/pushだけはユーザーkeyringを利用する実行環境で別途実施する。

| 最終group | 件数 | FAIL/ERROR/skip | 所要秒 |
| --- | ---: | --- | ---: |
| core | 54 | 0 / 0 / 0 | 58.680 |
| regression | 85 | 0 / 0 / 0 | 10.124 |
| source | 71 | 0 / 0 / 0 | 2.562 |
| scope | 26 | 0 / 0 / 0 | 2.572 |
| 合計（重複加算なし） | **236** | **0 / 0 / 0** | 並列group実行 |

再実行コマンド（repository root、既存依存入り配布runtimeを使う）:

```powershell
$issue20Python = Join-Path $env:USERPROFILE '.copilot\rag\query\.venv\Scripts\python.exe'
& $issue20Python -X utf8 -B tools/windows_progress/reproduce_watcher.py
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py core
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py regression
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py source
& $issue20Python -X utf8 -B tools/windows_progress/run_acceptance.py scope
git diff --check
```

試験はsynthetic DB/input・hash embedding・既存tokenizer fixtureを使用し、実SQLite/Chromaへの書込を検証する。実利用DB、社内資料、認証情報は読まない。実行スクリプトは診断を収集してpathをredactし、件数と結果をJSONで出力する。

主な受入証拠:

- WinError 5→5→成功、32/33→成功、対象外/非Windows即時raise、backoff/期限、old/new完全JSON、一時ファイル0。
- 実handle短時間保持から回復、progress/events長時間保持はDB成功＋当該警告1回。正本state長時間保持は子CLI非0、旧bytes保持、解放後resume成功。
- Watcher稼働中に1000回進捗更新し、statusと実inventory readerを並行反復。続く実ADDも完了、4ストアID一致、壊れたJSON・共有違反停止・orphan temp・警告spamは0。別の実Source子CLI試験でprogress/result frameを取得・検証。
- delete/upsert/catalog/clean/stateの5境界に観測保存障害を注入し、4ストアID集合が一致。
- 同5境界の本体失敗は終了1、元WinError 112を保持。失敗progressの二次例外に隠されず、各resumeが終了0で収束。
- 実vector upsert直後に専用child PIDをterminate。不一致を実確認してから、別processの`--resume`で4ストアIDが収束。
- filesystem Source登録→実fetch/copy→実配布Python子process ADD→Source state complete/metadata CAS commitまで成功。進捗故障時もupdatedと劣化警告を分離。
- Windows PowerShell **5.1**から配布Pythonでupdate/resume/statusを実行し、終了0、status成功・件数1。
- 既存DB writer lock、20 competing processes、CAS、reparse防御、UTF-8、privacy、scope/resume、Source batch/partial回帰がPASS。
- `git diff --check` PASS。独立レビューで今回の差分に新規P0/P1指摘なし。

最初のstressではstatus読み取りのerrno13とinventory診断でFAILした。再送だけで隠さず、限定reader修正を別commitにした。開発時にはtest fixtureのimport/configurationやeventの「必ず2秒待つ」という誤assertも修正した。最終候補の上記groupは各1回のみ実行し、合格済みgroupの再送で結果を選別していない。

## 横断監査と残事項

全表は [WindowsファイルI/O横断監査](windows-file-io-audit-issue20.md)。要求された全moduleと、追加のruntime置換/直接write経路を、artifact役割・並行性・retry/rollback・枯渇時方針別に記録した。

- P1: stable clean JSONL、Redmine本文、DB_PROFILEの直接write中断対策。
- P1: provider/DB copy/packageのdirectory publishとrollback、destination ownership。
- P1: Source削除・設定・connection pairの保存/rollbackとdaemon IPC。
- P1調査候補（既存、未確定）: private scope移行がresume検証より先で、不一致を隠す可能性。今回新設ではなく、変更せず監査へ残した。
- P2: private staging cleanup、表示情報、retry条件の整理。

観測保存がdisableされたrunでは、保存済みprogress/statusは古い場合がある。最終Source/ADD結果と固定警告を一次結果とし、progressファイルだけでDB成功を判定しない。劣化状態は正本へ新しい書込を追加して保存しておらず、run終了後の全画面に永続警告を追加する変更までは行っていない。

人力追加確認は、実運用の通常ユーザー権限でSource更新中にstatus表示を使い、劣化警告が理解できるかを見ること。別Windows 10実機、実際のAV/同期環境、ネットワークproviderの長時間運用は追加検証対象。native terminate/resumeはPASSしたが、突然の電源断やcleanへのwrite途中kill全境界まで保証したとは扱わない。
