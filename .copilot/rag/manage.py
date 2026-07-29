from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_MODULE_ROOT = Path(__file__).resolve().parent
if str(_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULE_ROOT))
from help_links import MANAGER_HELP_EPILOG, MANAGER_HELP_URL


RAG_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
GEN_DB_ROOT = RAG_ROOT / "gen_db"

TOP_MENU = (
    ("1", "新しいDBを作る"),
    ("2", "DBを選んで管理する"),
    ("3", "全DBの全Sourceを更新・再開する"),
    ("4", "配布・管理PCの引っ越し"),
    ("5", "この端末の設定・動作確認"),
    ("0", "終了"),
)
DATABASE_MENU = (
    ("1", "Sourceを見る・更新する"),
    ("2", "新しいSourceを追加する"),
    ("3", "このDBの全Sourceを更新・再開する"),
    ("4", "DBの名前・説明を変更する"),
    ("5", "問題があるとき"),
    ("6", "このDBを削除する【危険】"),
    ("0", "戻る"),
)
SOURCE_MENU = (
    ("1", "Source一覧から選択"),
    ("0", "戻る"),
)
SOURCE_DETAIL_MENU = (
    ("1", "更新・再開する"),
    ("2", "取得設定を確認・変更する"),
    ("3", "検索結果リンクを確認・変更する"),
    ("4", "進捗・ログを見る"),
    ("5", "技術情報"),
    ("0", "戻る"),
)
SOURCE_LINK_MENU = (
    ("1", "現在の設定を確認"),
    ("2", "新規設定・設定変更"),
    ("3", "有効・無効を切り替える"),
    ("4", "設定を削除する"),
    ("5", "生成URLを確認する"),
    ("6", "Source Linkヘルプを開く"),
    ("0", "戻る"),
)
REPAIR_COMPONENTS = {
    "1": "lexical",
    "2": "vector",
    "3": "all",
}
ALLOWED_SCRIPTS = frozenset(
    {
        "list_dbs.py",
        "search.py",
        "make_distribution_package.py",
        "make_admin_transfer_package.py",
        "query/setup.py",
        "query/list_dbs.py",
        "query/search.py",
        "gen_db/create_db.py",
        "gen_db/build_db.py",
        "gen_db/add_data.py",
        "gen_db/status.py",
        "gen_db/rebuild_component.py",
    }
)
DATABASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_ANSI = {
    "success": "\033[32m",
    "info": "\033[36m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "reset": "\033[0m",
}
_STATUS_JA = {
    "ready": "利用可能",
    "completed": "利用可能",
    "running": "処理中",
    "interrupted": "中断・再開可能",
    "failed": "失敗",
    "unknown": "状態不明",
    "setup_required": "初期設定が必要",
    "stale_running": "処理停止・再開確認が必要",
    "full": "十分",
    "partial": "一部のみ",
    "none": "根拠なし",
    "active": "有効",
    "disabled": "無効",
    "configured": "設定済み",
    "not_configured": "未設定",
    "type_only": "種別のみ設定",
    "manual_required": "手動対応が必要",
    "unconfigured": "未設定",
    "invalid": "不正",
}
_PROVIDER_JA = {
    "git_repository": "Gitリポジトリ",
    "unspecified": "未設定",
    "folder": "フォルダ",
    "git": "Gitリポジトリ（サービス未指定）",
    "github": "GitHub",
    "gitlab": "GitLab",
    "azure_devops": "Azure DevOps",
    "svn": "Subversion（SVN）",
    "sharepoint": "SharePoint",
    "redmine": "Redmine",
    "other": "その他のWebサイト",
}
_STRATEGY_JA = {
    "github-blob": "GitHubファイルリンク",
    "gitlab-blob": "GitLabファイルリンク",
    "azure-devops-item": "Azure DevOpsファイルリンク",
    "svn-http": "Apache HTTP(S)互換（各ファイルを直接開く）",
    "svn-web-root": "その他のSVN Web画面（トップページを開く）",
    "home-only": "トップページのみ",
    "append-relative-path": "相対パスをURL末尾へ追加",
    "regex-template": "正規表現テンプレート",
}
_GIT_PROVIDERS = ("github", "gitlab", "azure_devops")
_GIT_STRATEGIES = {
    "github": "github-blob",
    "gitlab": "gitlab-blob",
    "azure_devops": "azure-devops-item",
}
_BOOLEAN_CHOICE_JA = {
    "enabled": "有効",
    "disabled": "無効",
}


class ManagerError(RuntimeError):
    pass


class LocalRagManager:
    """Small human-facing orchestrator for the existing Local RAG CLIs."""

    def __init__(
        self,
        *,
        rag_root: Path = RAG_ROOT,
        dbs_root: Path | None = None,
        runtime_python: Path | None = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        runner: Callable[..., Any] = subprocess.run,
        color: bool | None = None,
    ) -> None:
        self.rag_root = Path(rag_root).expanduser().resolve()
        configured_dbs = os.getenv("RAG_DBS_ROOT", "").strip()
        self.dbs_root = (
            Path(dbs_root).expanduser().resolve()
            if dbs_root is not None
            else (
                Path(configured_dbs).expanduser().resolve()
                if configured_dbs
                else self.rag_root / "dbs"
            )
        )
        self._runtime_override = (
            Path(runtime_python).expanduser().resolve()
            if runtime_python is not None
            else None
        )
        self.input = input_fn
        self.output = output_fn
        self.runner = runner
        self.use_color = (
            False
            if os.getenv("NO_COLOR") is not None
            else (
                self._supports_color(sys.stdout)
                if color is None and output_fn is print
                else bool(color)
            )
        )
        self._sidecar_etags: dict[str, str] = {}
        self._sidecar_migrations: dict[str, bool] = {}
        self._sidecar_source_statuses: dict[str, dict[str, str]] = {}

    def run(self) -> int:
        self._print_info(f"ヘルプ: {MANAGER_HELP_URL}")
        while True:
            self._print_screen_header("メインメニュー")
            self._print_menu("操作を選択してください", TOP_MENU)
            self._print_info(f"詳しい使い方: {MANAGER_HELP_URL}")
            choice = self._ask("番号を入力してください: ")
            if choice is None or choice == "0":
                return 0
            if choice == "1":
                self._create_database()
            elif choice == "2":
                selected = self._select_database()
                if selected:
                    self._database_screen(selected)
            elif choice == "3":
                self._update_all_sources()
            elif choice == "4":
                self._package_and_transfer_screen()
            elif choice == "5":
                self._machine_setup_screen()
            else:
                self._invalid_selection("0～5")

    def _open_help(self) -> None:
        self._print_info(f"日本語操作ガイド: {MANAGER_HELP_URL}")
        try:
            opened = bool(webbrowser.open(MANAGER_HELP_URL))
        except Exception:
            opened = False
        if opened:
            self._print_success("既定のブラウザーでヘルプを開きました。")
        else:
            self._print_warning(
                "ブラウザーを自動で開けませんでした。上記URLを開いてください。"
            )

    def _machine_setup_screen(self) -> None:
        while True:
            self._print_screen_header("この端末の設定・動作確認")
            self._print_menu(
                "操作",
                (
                    ("1", "Local RAGを利用できるか確認する"),
                    ("2", "検索を試す"),
                    ("3", "Sourceへの接続状況を確認する"),
                    ("4", "技術情報"),
                    ("0", "戻る"),
                ),
            )
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._setup_or_verify()
            elif choice == "2":
                selected = self._select_database()
                if selected:
                    self._search(selected)
            elif choice == "3":
                self._show_source_connection_status()
            elif choice == "4":
                self._show_machine_technical_info()
            else:
                self._invalid_selection("0～4")

    def _show_source_connection_status(self) -> None:
        import shutil as _shutil

        self._print_screen_header("Sourceへの接続状況")
        statuses = {
            "GitHub": bool(_shutil.which("git")),
            "SVN": bool(_shutil.which("svn")),
            "Redmine": True,
            "SharePoint": (
                os.name == "nt"
                and bool(os.getenv("LOCAL_RAG_SHAREPOINT_ROOT", "").strip())
            ),
        }
        for label, available in statuses.items():
            self.output(
                f"{label:<10}: "
                f"{'利用可能' if available else '設定が必要'}"
            )
        self.output(
            "\n認証値や端末固有の保存場所は表示・保存しません。"
        )

    def _show_machine_technical_info(self) -> None:
        self._print_screen_header("技術情報")
        self.output(f"Python: {self._runtime_python()}")
        self.output(f"DB root: {self.dbs_root}")
        self.output(f"Platform: {sys.platform}")
        self.output(
            "SharePoint root environment: "
            + (
                "configured"
                if os.getenv("LOCAL_RAG_SHAREPOINT_ROOT", "").strip()
                else "not configured"
            )
        )

    def _problem_screen(self, db_name: str) -> None:
        while self._database_root(db_name).is_dir():
            self._print_screen_header(
                "問題があるとき",
                db_name=db_name,
            )
            self._print_menu(
                "操作",
                (
                    ("1", "検索を試す"),
                    ("2", "処理状況と最近のエラーを見る"),
                    ("3", "検索を修復する"),
                    ("4", "技術情報を表示する"),
                    ("0", "戻る"),
                ),
            )
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._search(db_name)
            elif choice == "2":
                self._show_source_progress_summary(db_name)
            elif choice == "3":
                self._repair_search_automatically(db_name)
            elif choice == "4":
                self._show_status(db_name)
            else:
                self._invalid_selection("0～4")

    def _repair_search_automatically(self, db_name: str) -> None:
        self._print_screen_header("検索を修復する", db_name=db_name)
        self._print_warning(
            "診断結果に基づき検索用データを再作成します。"
            "文書とSource設定は削除しません。"
        )
        if not self._confirm(f"DB「{db_name}」の検索を修復しますか？"):
            self._print_info("修復を開始しませんでした。")
            return
        result = self._invoke(
            "gen_db/rebuild_component.py",
            ["--db", db_name, "--component", "all"],
        )
        self._show_operation_result(result, "検索の修復")

    def _package_and_transfer_screen(self) -> None:
        while True:
            self._print_screen_header("配布・管理PCの引っ越し")
            self.output(
                "作成する内容には検索資料が含まれます。"
                "機密資料として安全に取り扱ってください。"
            )
            self._print_menu(
                "操作",
                (
                    ("1", "利用者向け検索パッケージを作る"),
                    ("2", "管理PCの引っ越し用フォルダを作る・再開する"),
                    ("3", "パッケージを取り込む・検証する"),
                    ("0", "戻る"),
                ),
            )
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return
            if choice in {"1", "2"}:
                self._create_portable_package(
                    "distribution" if choice == "1" else "admin-transfer"
                )
            elif choice == "3":
                self._verify_or_import_package()
            else:
                self._invalid_selection("0～3")

    def _update_database_sources(self, db_name: str) -> dict[str, Any]:
        if not self._guard_valid_database_target(db_name):
            return {"status": "invalid"}
        self._print_screen_header(
            "このDBの全Sourceを更新・再開する",
            db_name=db_name,
        )
        if not self._confirm(
            f"DB「{db_name}」で更新可能なSourceを順番に処理しますか？"
        ):
            self._print_info("処理を開始しませんでした。")
            return {"status": "cancelled"}
        try:
            from source_manager.runner import update_all_sources

            result = update_all_sources(
                self._database_root(db_name),
                python_executable=self._runtime_python(),
                rag_root=self.rag_root,
            )
        except Exception as exc:
            self._print_error(
                "Sourceの一括処理に失敗しました。"
                f"完了済みのSourceは保存されています（{type(exc).__name__}）。"
            )
            return {"status": "failed", "error": type(exc).__name__}
        self._show_source_update_result(result)
        if bool(result.get("snapshot_marker_eligible")):
            self._write_content_snapshot(
                db_name,
                reason="all_sources_updated",
            )
        return result

    def _update_all_sources(self) -> None:
        self._print_screen_header("全DBの全Sourceを更新・再開する")
        databases = self._database_summaries()
        if not databases:
            self._print_info("更新できるDBがありません。")
            return
        self.output(
            "すべてのDBについて、未完了のSourceは再開し、"
            "それ以外は更新可能なSourceだけを処理します。"
        )
        if not self._confirm("全DBの処理を開始しますか？"):
            self._print_info("処理を開始しませんでした。")
            return
        completed = failed = skipped = 0
        for index, database in enumerate(databases, start=1):
            db_name = str(database.get("name") or "")
            self.output(
                f"\n[{index}/{len(databases)}] DB「{db_name}」を処理します。"
            )
            try:
                from source_manager.runner import update_all_sources

                result = update_all_sources(
                    self._database_root(db_name),
                    python_executable=self._runtime_python(),
                    rag_root=self.rag_root,
                )
            except Exception as exc:
                failed += 1
                self._print_error(
                    f"DB「{db_name}」の処理に失敗しました"
                    f"（{type(exc).__name__}）。"
                )
                continue
            self._show_source_update_result(result)
            groups = self._source_update_groups(result)
            failed_items = groups["failed"]
            skipped_items = groups["skipped"]
            completed += len(groups["completed"])
            failed += len(failed_items)
            skipped += len(skipped_items)
            if bool(result.get("snapshot_marker_eligible")):
                self._write_content_snapshot(
                    db_name,
                    reason="all_sources_updated",
                )
        self.output("\n全DBの処理結果")
        self.output(f"成功: {completed} Source")
        self.output(f"失敗: {failed} Source")
        self.output(f"スキップ: {skipped} Source")

    def _show_source_update_result(self, result: dict[str, Any]) -> None:
        self.output("\nSource処理結果")
        groups = self._source_update_groups(result)
        for key, label in (
            ("completed", "成功"),
            ("failed", "失敗"),
            ("skipped", "スキップ"),
        ):
            values = groups[key]
            self.output(f"{label}: {len(values)} Source")
            for value in values:
                if isinstance(value, dict):
                    name = str(
                        value.get("display_name")
                        or value.get("name")
                        or "Source"
                    )
                    reason = str(
                        value.get("message")
                        or value.get("skip_reason")
                        or value.get("reason")
                        or value.get("error")
                        or ""
                    )
                    self.output(f"  - {name}" + (f": {reason}" if reason else ""))
                else:
                    self.output(f"  - {value}")

    @staticmethod
    def _source_update_groups(
        result: dict[str, Any],
    ) -> dict[str, list[Any]]:
        if isinstance(result.get("results"), list):
            values = [
                value
                for value in result["results"]
                if isinstance(value, dict)
            ]
            completed_statuses = {
                "complete",
                "completed",
                "ok",
                "success",
                "updated",
            }
            return {
                "completed": [
                    value
                    for value in values
                    if str(value.get("status") or "")
                    in completed_statuses
                ],
                "skipped": [
                    value
                    for value in values
                    if value.get("status") == "skipped"
                ],
                "failed": [
                    value
                    for value in values
                    if value.get("status") not in completed_statuses
                    and value.get("status") != "skipped"
                ],
            }
        return {
            key: list(result.get(key) or [])
            for key in ("completed", "failed", "skipped")
        }

    def _write_content_snapshot(self, db_name: str, *, reason: str) -> None:
        try:
            db_root = self._validated_database_root(db_name)
        except ManagerError:
            return
        path = db_root / "rag-wrapper.json"
        payload = {
            "schema_version": "local-rag.wrapper.v1",
            "content_snapshot_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "reason": reason,
        }
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    payload,
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            self._print_warning(
                "DBの全体更新日時を保存できませんでした。"
                "検索は引き続き利用できます。"
            )

    def _create_portable_package(self, kind: str) -> None:
        is_distribution = kind == "distribution"
        label = (
            "利用者向け検索パッケージ"
            if is_distribution
            else "管理PCの引っ越し用フォルダ"
        )
        destination = self._prompt_preserving_value(
            "保存先",
            "",
            required=True,
            description=(
                f"{label}の新しい保存先を指定します。"
                "既存のファイルやフォルダは上書きしません。"
            ),
            examples=(
                "local-rag-search-package.zip"
                if is_distribution
                else "local-rag-admin-transfer",
            ),
        )
        if destination is None:
            return
        output = Path(destination).expanduser()
        self.output("\n作成内容")
        self.output(f"種類: {label}")
        self.output(f"対象: 現在の全DB")
        self.output(
            "認証情報、端末設定、実行中の一時情報: 含めない"
        )
        if not self._confirm("この内容で作成しますか？"):
            self._print_info("パッケージを作成しませんでした。")
            return
        try:
            from source_manager.packages import (
                create_admin_transfer_package,
                create_distribution_package,
            )

            if is_distribution:
                result = create_distribution_package(
                    self.rag_root.parent,
                    output,
                )
            else:
                result = create_admin_transfer_package(
                    self.rag_root.parent,
                    output,
                )
        except Exception as exc:
            self._print_error(
                f"{label}を作成できませんでした"
                f"（{type(exc).__name__}）。"
            )
            self.output(
                "作成途中の内容は完成パッケージとして公開されていません。"
            )
            return
        self._print_success(f"{label}を作成しました。")
        manifest = result.get("manifest") or {}
        total = manifest.get("total") if isinstance(manifest, dict) else {}
        if isinstance(total, dict):
            self.output(f"ファイル数: {int(total.get('files') or 0):,}")
            self.output(f"合計サイズ: {int(total.get('bytes') or 0):,} bytes")

    def _verify_or_import_package(self) -> None:
        value = self._prompt_preserving_value(
            "パッケージ",
            "",
            required=True,
            description=(
                "利用者向けZIPまたは管理PC引っ越し用フォルダを指定します。"
                "最初に全ファイルの一覧とSHA-256を検証します。"
            ),
            examples=("local-rag-search-package.zip",),
        )
        if value is None:
            return
        package = Path(value).expanduser()
        try:
            from source_manager.packages import (
                validate_distribution_zip,
                validate_package_tree,
            )

            manifest = (
                validate_distribution_zip(package)
                if package.is_file()
                else validate_package_tree(package)
            )
        except Exception as exc:
            self._print_error(
                "パッケージの検証に失敗しました"
                f"（{type(exc).__name__}）。"
            )
            self.output("この端末のLocal RAGは変更されていません。")
            return
        self._print_success("パッケージの検証に成功しました。")
        self.output(f"種類: {manifest.get('kind') or '不明'}")
        self.output(
            f"ファイル数: "
            f"{int((manifest.get('total') or {}).get('files') or 0):,}"
        )
        database_names = [
            str(item.get("name") or "")
            for item in manifest.get("dbs", [])
            if isinstance(item, dict) and item.get("name")
        ]
        if database_names:
            self.output("対象DB: " + "、".join(database_names))
        existing = [
            name
            for name in database_names
            if (self.dbs_root / name).exists()
            or (self.dbs_root / name).is_symlink()
        ]
        if existing:
            self._print_warning(
                "同名DBを安全に差し替えます。"
                "全DBを一時場所で検証してからDB単位で公開し、"
                "失敗時は現在の同名DBを保持します。"
            )
            self.output("差し替えるDB: " + "、".join(existing))
        if not self._confirm("検証済みパッケージをこの端末へ取り込みますか？"):
            self._print_info("検証のみ完了しました。取り込みは行っていません。")
            return
        for name in existing:
            confirmation = self._ask(
                f"差し替えを確認するためDB名「{name}」を入力してください: "
            )
            if confirmation != name:
                self._print_info(
                    f"DB「{name}」の確認が一致しないため、"
                    "取り込みを開始しませんでした。"
                )
                return
        try:
            from source_manager.packages import import_package

            result = import_package(package, self.rag_root.parent)
        except Exception as exc:
            self._print_error(
                "パッケージを取り込めませんでした"
                f"（{type(exc).__name__}）。"
            )
            self.output(
                "既存の同名DBは保持されています。"
                "対象外のDBやファイルは削除していません。"
            )
            return
        imported = result.get("databases") if isinstance(result, dict) else []
        self._print_success("パッケージを取り込みました。")
        if isinstance(imported, list) and imported:
            self.output(
                "取り込んだDB: "
                + "、".join(str(name) for name in imported)
            )

    @staticmethod
    def _supports_color(stream: Any) -> bool:
        if os.getenv("NO_COLOR") is not None:
            return False
        try:
            if not stream.isatty():
                return False
        except (AttributeError, OSError):
            return False
        if os.name != "nt":
            return True
        return bool(
            os.getenv("WT_SESSION")
            or os.getenv("ANSICON")
            or os.getenv("TERM")
            or os.getenv("TERM_PROGRAM")
        )

    def _message(self, kind: str, text: str) -> None:
        labels = {
            "success": "成功",
            "info": "情報",
            "warning": "警告",
            "error": "エラー",
        }
        value = f"[{labels[kind]}] {text}"
        if self.use_color:
            value = f"{_ANSI[kind]}{value}{_ANSI['reset']}"
        self.output(value)

    def _print_success(self, text: str) -> None:
        self._message("success", text)

    def _print_info(self, text: str) -> None:
        self._message("info", text)

    def _print_warning(self, text: str) -> None:
        self._message("warning", text)

    def _print_error(self, text: str) -> None:
        self._message("error", text)

    def _print_screen_header(
        self,
        title: str,
        *,
        db_name: str | None = None,
        source_id: str | None = None,
    ) -> None:
        self.output("\n" + "=" * 60)
        self.output("Local RAG Manager")
        if db_name:
            self.output(f"データベース: {db_name}")
        if source_id:
            self.output(f"Source: {source_id}")
        self.output(f"画面: {title}")
        self.output("=" * 60)

    @staticmethod
    def _status_label(value: Any) -> str:
        internal = str(value or "unknown")
        translated = _STATUS_JA.get(internal, internal)
        return (
            translated
            if translated == internal
            else f"{translated}（{internal}）"
        )

    @staticmethod
    def _provider_label(value: Any) -> str:
        internal = str(value or "")
        if not internal:
            return "未設定"
        return f"{_PROVIDER_JA.get(internal, internal)}（{internal}）"

    @staticmethod
    def _strategy_label(value: Any) -> str:
        internal = str(value or "")
        if not internal:
            return "未設定"
        return f"{_STRATEGY_JA.get(internal, internal)}（{internal}）"

    def _invalid_selection(self, allowed: str) -> None:
        self._print_error("その選択値は存在しません。")
        self.output(f"{allowed}を入力してください。")
        self.output("設定は変更されていません。")

    def _setup_or_verify(self) -> None:
        self._print_screen_header("初期設定・動作確認")
        self.output(
            "初期設定はPython環境、必要ライブラリ、検索モデルを準備します。\n"
            "動作確認だけなら、既存ファイルを変更せずに検証できます。"
        )
        self._print_menu(
            "操作",
            (
                ("1", "現在の設定を確認する"),
                ("2", "初期設定を実行する"),
                ("0", "戻る"),
            ),
        )
        choice = self._ask("番号を入力してください: ")
        if choice in (None, "0"):
            return
        if choice == "1":
            if not self._runtime_python().is_file():
                self._print_error(
                    "初期設定が必要です。Local RAGの仮想環境がありません。"
                )
                self.output(
                    "「初期設定を実行する」を選び、実行環境を作成してください。"
                )
                return
            result = self._invoke(
                "query/setup.py",
                ["--verify-only", "--format", "json"],
                capture_output=True,
            )
            self._show_setup_result(result)
            return
        if choice == "2":
            self._print_warning(
                "必要なパッケージの導入と検索モデルの準備を行います。"
            )
            if not self._confirm("初期設定を実行しますか？"):
                return
            python = (
                self._runtime_python()
                if self._runtime_python().is_file()
                else Path(sys.executable).resolve()
            )
            result = self._invoke(
                "query/setup.py",
                ["--format", "json"],
                python=python,
                capture_output=True,
            )
            self._show_setup_result(result)
            return
        self._invalid_selection("0～2")

    def _show_setup_result(self, result: Any | None) -> None:
        if result is None:
            return
        try:
            payload = json.loads(str(result.stdout or ""))
        except json.JSONDecodeError:
            self._print_error("初期設定の結果を読み取れませんでした。")
            return
        if not isinstance(payload, dict):
            self._print_error("初期設定から想定外の結果が返されました。")
            return
        setup_complete = bool(payload.get("setup_complete"))
        lookup_ready = bool(payload.get("lookup_ready"))
        self.output("\n初期設定の結果")
        self.output(f"初期設定: {'完了' if setup_complete else '未完了'}")
        self.output(f"検索準備: {'利用可能' if lookup_ready else '利用不可'}")
        self.output(f"状態: {self._status_label(payload.get('status'))}")
        if setup_complete and lookup_ready:
            self._print_success("Local RAGを検索できます。")
        elif setup_complete:
            self._print_warning(
                "初期設定は完了していますが、検索可能なDBがまだありません。"
            )
            self.output("「新しいDBを作成」からDBを作成してください。")
        else:
            failed = payload.get("failed_check")
            if failed:
                self._print_error(f"確認項目「{failed}」で失敗しました。")
        if payload.get("next_action"):
            self.output(f"次の操作: {payload['next_action']}")

    def _select_database(self) -> str | None:
        self._print_screen_header("DBを選んで管理する")
        databases = self._database_summaries()
        if not databases:
            self._print_info("利用できるLocal RAGデータベースがありません。")
            self.output("メインメニューの「新しいDBを作成」を利用してください。")
            return None
        self.output("\nデータベース一覧")
        for index, item in enumerate(databases, start=1):
            name = str(item.get("name") or "")
            title = str(item.get("title") or name)
            self.output(f"\n{index}. {name}")
            self.output(f"   表示名: {title}")
            self.output(
                f"   内容: {str(item.get('content_summary') or '内容を確認できません')}"
            )
            if item.get("query_hint"):
                self.output(f"   検索向け: {item['query_hint']}")
        choice = self._ask("\nDB番号を入力してください（0: 戻る）: ")
        if choice in (None, "0"):
            return None
        try:
            index = int(choice) - 1
        except ValueError:
            self._invalid_selection(f"1～{len(databases)}、または0")
            return None
        if index < 0 or index >= len(databases):
            self._invalid_selection(f"1～{len(databases)}、または0")
            return None
        return str(databases[index]["name"])

    def _database_screen(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        while self._database_root(db_name).is_dir():
            self._print_screen_header("DB操作", db_name=db_name)
            self._show_database_overview(db_name)
            self._print_menu("操作", DATABASE_MENU)
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._sources_screen(db_name)
            elif choice == "2":
                self._add_source_screen(db_name)
            elif choice == "3":
                self._update_database_sources(db_name)
            elif choice == "4":
                self._edit_database_metadata(db_name)
            elif choice == "5":
                self._problem_screen(db_name)
            elif choice == "6":
                if self._delete_database_interactive(db_name):
                    return
            else:
                self._invalid_selection("0～6")

    def _show_database_overview(self, db_name: str) -> None:
        metadata = self._read_database_metadata(db_name)
        inventory = self._load_source_inventory(db_name)
        sources = self._inventory_sources(inventory) if inventory is not None else []
        content = "／".join(
            f"{_PROVIDER_JA.get(str(value.get('source_type') or 'other'), 'Other')}"
            f"「{str(value.get('display_name') or '既存データ')}」"
            for value in sources[:8]
        ) or "まだ検索へ反映された資料はありません"
        self.output(
            f"説明          : {metadata['title']}"
            + (
                f"／{metadata['query_hint']}"
                if metadata["query_hint"]
                else ""
            )
        )
        self.output(
            f"内容          : {content}"
        )

    def _add_source_screen(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        self._print_screen_header("新しいSourceを追加する", db_name=db_name)
        self._print_menu(
            "種類を選択してください",
            (
                ("1", "GitHubリポジトリ"),
                ("2", "SVN"),
                ("3", "Redmineプロジェクト"),
                ("4", "SharePoint同期フォルダ【追加・更新はWindowsのみ】"),
                ("5", "手元の資料を一度だけ取り込む（Other）"),
                ("0", "戻る"),
            ),
        )
        choice = self._ask("番号を入力してください: ")
        if choice in (None, "0"):
            return
        forms = {
            "1": self._prompt_new_github_source,
            "2": self._prompt_new_svn_source,
            "3": self._prompt_new_redmine_source,
            "4": self._prompt_new_sharepoint_source,
            "5": self._prompt_new_other_source,
        }
        form = forms.get(choice)
        if form is None:
            self._invalid_selection("0～5")
            return
        specification = form()
        if specification is None:
            self._print_info("Source設定は保存されていません。")
            return
        self.output("\n登録内容")
        self.output(f"取得元          : {specification['label']}")
        self.output(f"Sourceの名前    : {specification['display_name']}")
        for label, value in specification.get("summary") or []:
            self.output(f"{label:<16}: {value}")
        if specification["source_type"] == "other":
            self._print_menu(
                "確認",
                (
                    ("1", "保存して取り込みを開始"),
                    ("0", "中止"),
                ),
            )
        else:
            self._print_menu(
                "確認",
                (
                    ("1", "保存して取得を開始"),
                    ("2", "設定だけ保存"),
                    ("0", "中止"),
                ),
            )
        action = self._ask("番号を入力してください: ")
        if action in (None, "0"):
            self._print_info("Source設定は保存されていません。")
            return
        if action not in {"1", "2"} or (
            specification["source_type"] == "other" and action == "2"
        ):
            self._invalid_selection(
                "1、または0"
                if specification["source_type"] == "other"
                else "0～2"
            )
            return
        try:
            from source_manager.runner import register_source

            result = register_source(
                self._database_root(db_name),
                source_type=str(specification["source_type"]),
                display_name=str(specification["display_name"]),
                fetch=dict(specification["fetch"]),
                link=specification.get("link"),
                runtime_input=specification.get("runtime_input"),
                start=action == "1",
                python_executable=self._runtime_python(),
                rag_root=self.rag_root,
            )
        except Exception as exc:
            self._print_error(
                "Sourceを登録できませんでした。"
                f"設定または取得元を確認してください（{type(exc).__name__}）。"
            )
            self.output(
                "検索DBへ反映できていない内容は、次回このSourceから再開できます。"
            )
            return
        if action == "1":
            status = str(result.get("status") or "")
            if status == "updated":
                self._print_success(
                    "Sourceを保存し、検索へ反映しました。"
                )
            else:
                self._print_warning(
                    "Sourceを保存しましたが、処理は再開可能な位置で"
                    f"停止しています（状態: {status or '不明'}）。"
                )
        else:
            self._print_success("Sourceの取得設定を保存しました。")
            self.output(
                "検索へ反映されるまでは、Copilot向けDB内容一覧には表示されません。"
            )

    def _prompt_new_github_source(self) -> dict[str, Any] | None:
        self.output(
            "\n[1/2] GitHubリポジトリのURL【必須】\n"
            "リポジトリ全体をDB内の専用作業場所へ取得します。\n"
            "例: https://github.com/<owner>/<repository>.git"
        )
        url = self._prompt_preserving_value(
            "URL",
            "",
            required=True,
            examples=("https://github.com/<owner>/<repository>.git",),
        )
        if url is None:
            return None
        proposed = re.sub(r"\.git$", "", url.rstrip("/")).rsplit("/", 1)[-1]
        self.output(
            "\n[2/2] Sourceの名前【必須】\n"
            f"リポジトリ名から「{proposed}」を提案しました。"
        )
        name = self._prompt_preserving_value(
            "Sourceの名前",
            proposed,
            required=True,
            examples=("製品コード",),
        )
        if name is None:
            return None
        return {
            "source_type": "github",
            "label": "GitHub",
            "display_name": name,
            "fetch": {"repository_url": url},
            "summary": (
                ("対象", "リポジトリ全体"),
                ("Branch", "remoteの既定branch"),
                ("フォルダ範囲", "常に再帰"),
                ("作業場所", "DB内でLocal RAGが管理"),
            ),
        }

    def _prompt_new_svn_source(self) -> dict[str, Any] | None:
        url = self._prompt_preserving_value(
            "SVNのURL",
            "",
            required=True,
            description=(
                "HTTP(S)のSVN URLからDB内の専用作業場所へ取得します。"
            ),
            examples=("https://svn.example.invalid/repos/project/trunk",),
        )
        name = self._prompt_preserving_value(
            "Sourceの名前",
            "",
            required=True,
            examples=("製品仕様",),
        )
        if url is None or name is None:
            return None
        scope = self._select_value(
            "取り込む範囲",
            (
                ("1", "配下のフォルダも含める（再帰）【既定】"),
                ("2", "この階層のファイルだけ"),
            ),
            default="1",
        )
        if scope is None:
            return None
        link_strategy = self._select_value(
            "検索結果リンクの形式",
            (
                ("svn-http", "Apache HTTP(S)互換の各ファイル直リンク"),
                ("svn-web-root", "その他のSVN Web画面のトップページ"),
            ),
            default="1",
        )
        if link_strategy is None:
            return None
        if link_strategy == "svn-web-root":
            top_url = self._prompt_preserving_value(
                "SVN Web画面のトップURL",
                "",
                required=True,
                description=(
                    "製品固有のファイルURLは推測せず、"
                    "すべての検索結果からこのページを開きます。"
                ),
                examples=("https://svn-web.example.invalid/project#files",),
            )
            if top_url is None:
                return None
            link_settings = {"repository_url": top_url}
        else:
            link_settings = {
                "repository_url": url,
                "permalink_enabled": False,
            }
        return {
            "source_type": "svn",
            "label": "SVN",
            "display_name": name,
            "fetch": {
                "repository_url": url,
                "recursive": scope == "1",
            },
            "link": {
                "enabled": True,
                "strategy": link_strategy,
                "settings": link_settings,
            },
            "summary": (
                (
                    "取得範囲",
                    "再帰" if scope == "1" else "この階層のファイルだけ",
                ),
                ("過去文書の自動削除", "行わない"),
                ("途中再開", "可能"),
            ),
        }

    def _prompt_new_redmine_source(self) -> dict[str, Any] | None:
        url = self._prompt_preserving_value(
            "RedmineプロジェクトのURL",
            "",
            required=True,
            examples=("https://redmine.example.invalid/projects/project-id",),
        )
        name = self._prompt_preserving_value(
            "Sourceの名前",
            "",
            required=True,
            examples=("製品サポート",),
        )
        if url is None or name is None:
            return None
        period = self._select_value(
            "どこまでさかのぼって取得しますか？（Issueの更新日時）",
            (
                ("1", "過去1年【おすすめ】"),
                ("2", "過去90日"),
                ("3", "過去30日"),
                ("4", "期間を指定"),
                ("5", "制限しない"),
            ),
            default="1",
        )
        if period is None:
            return None
        days: int | None = {"1": 365, "2": 90, "3": 30}.get(period)
        if period == "4":
            raw_days = self._prompt_preserving_value(
                "日数",
                "",
                required=True,
                description="1以上の日数を入力します。",
                examples=("180",),
            )
            if raw_days is None:
                return None
            try:
                days = int(raw_days)
            except ValueError:
                self._print_error("日数は1以上の整数で入力してください。")
                return None
            if days < 1:
                self._print_error("日数は1以上の整数で入力してください。")
                return None
        base_url = re.sub(r"/projects/[^/?#]+/?$", "", url.rstrip("/"))
        return {
            "source_type": "redmine",
            "label": "Redmine",
            "display_name": name,
            "fetch": {
                "project_url": url,
                "updated_within_days": days,
                "api_key_env": "LOCAL_RAG_REDMINE_API_KEY",
            },
            "link": {
                "enabled": True,
                "strategy": "regex-template",
                "settings": {
                    "path_pattern": (
                        r"^issues/(?P<issue_id>[0-9]+)\.md$"
                    ),
                    "url_template": f"{base_url}/issues/{{issue_id}}",
                },
            },
            "summary": (
                ("期間", "制限なし" if days is None else f"過去{days}日"),
                ("Issue状態", "完了済みを含む"),
                ("取得方式", "Issueを1件ずつ直列取得"),
                ("検索への反映", "5件保存するごと"),
                ("添付", "ファイル名とURLだけ保存"),
                ("固定待機", "なし"),
                ("自動削除", "行わない"),
                ("途中再開", "可能"),
            ),
        }

    def _prompt_new_sharepoint_source(self) -> dict[str, Any] | None:
        if os.name != "nt":
            self._print_warning(
                "SharePoint Sourceの追加・更新はWindowsだけで利用できます。"
                "既存DBの検索とWebリンク表示はこのOSでも利用できます。"
            )
            return None
        if not os.getenv("LOCAL_RAG_SHAREPOINT_ROOT", "").strip():
            self._print_error(
                "SharePoint同期ルートの端末設定が必要です。"
                "DBには絶対パスを保存しません。"
            )
            return None
        relative = self._prompt_preserving_value(
            "SharePoint rootからの相対フォルダ",
            "",
            required=True,
            examples=("Team Documents/manuals",),
        )
        browser = self._prompt_preserving_value(
            "SharePointのbrowser URL",
            "",
            required=True,
            examples=(
                "https://tenant.example.invalid/sites/project/"
                "Shared%20Documents/manuals",
            ),
        )
        name = self._prompt_preserving_value(
            "Sourceの名前",
            "",
            required=True,
            examples=("設計資料",),
        )
        if relative is None or browser is None or name is None:
            return None
        return {
            "source_type": "sharepoint",
            "label": "SharePoint",
            "display_name": name,
            "fetch": {
                "relative_path": relative,
                "root_env": "LOCAL_RAG_SHAREPOINT_ROOT",
            },
            "link": {
                "enabled": True,
                "strategy": "append-relative-path",
                "settings": {"source_web_root": browser},
            },
            "summary": (
                ("同期フォルダ", relative),
                ("Webリンク", "ファイル直接リンク"),
                ("追加・更新", "Windowsのみ"),
            ),
        }

    def _prompt_new_other_source(self) -> dict[str, Any] | None:
        path = self._prompt_preserving_value(
            "ファイルまたはフォルダ",
            "",
            required=True,
            description=(
                "今回だけ取り込みます。完了後、入力した絶対パスは保存しません。"
            ),
            examples=("選択したファイルまたはフォルダ",),
        )
        name = self._prompt_preserving_value(
            "Sourceの名前",
            "",
            required=True,
            examples=("引継ぎ資料",),
        )
        if path is None or name is None:
            return None
        return {
            "source_type": "other",
            "label": "Other",
            "display_name": name,
            "fetch": {"one_shot": True},
            "runtime_input": path,
            "summary": (
                ("方式", "今回だけ取り込む"),
                ("自動更新", "なし"),
                ("元資料リンク", "なし"),
            ),
        }

    def _search(self, db_name: str) -> None:
        self._print_screen_header("検索を試す", db_name=db_name)
        self._print_menu(
            "検索方法",
            (
                ("1", "通常検索"),
                ("2", "診断情報付き検索"),
                ("0", "戻る"),
            ),
        )
        mode = self._ask("番号を入力してください: ")
        if mode in (None, "0"):
            return
        if mode not in {"1", "2"}:
            self._invalid_selection("0～2")
            return
        question = self._prompt_preserving_value(
            "質問",
            "",
            required=True,
            description=(
                "選択中のDBで確認したい内容を、自然な文章で入力します。"
            ),
            examples=("この製品の運用上の注意点を教えて",),
        )
        if question is None:
            return
        arguments = ["--db", db_name, "--compact-json"]
        if mode == "2":
            arguments.append("--explain")
        arguments.append(question)
        # Exactly one search process is started for either presentation mode.
        self._print_info("検索を実行中です。完了までお待ちください。")
        result = self._invoke(
            "search.py",
            [*arguments, "--format", "json", "--result-delivery", "stdout"],
            capture_output=True,
            report_nonzero=False,
        )
        if result is None:
            return
        raw = str(result.stdout or "")
        if raw.strip():
            self._show_search_result(raw)
        elif int(result.returncode) != 0:
            self._print_error("検索処理が結果を返さずに失敗しました。")

    def _show_search_result(self, raw_output: str) -> None:
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            self._print_error("検索結果をJSONとして読み取れませんでした。")
            if raw_output.strip():
                self.output(raw_output.strip())
            return
        if not isinstance(payload, dict):
            self._print_error("検索から想定外の結果が返されました。")
            return
        if (
            payload.get("schema_version") == "rag-result-pointer-v1"
            and payload.get("summary_file")
        ):
            try:
                summary_path = Path(str(payload["summary_file"]))
                payload = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                self._print_error(
                    "検索結果ファイルを読み取れませんでした。"
                )
                return
        self.output("\n検索結果")
        self.output(f"状態: {self._status_label(payload.get('status'))}")
        self.output(
            f"回答可能性: "
            f"{self._status_label(payload.get('answerability') or 'unknown')}"
        )
        status = str(payload.get("status") or "")
        if status in {"busy", "error"}:
            error_kind = str(payload.get("error") or "unknown")
            self._print_error(f"検索に失敗しました: {error_kind}")
        initial = (
            payload.get("initial_response")
            if isinstance(payload.get("initial_response"), dict)
            else {}
        )
        draft = str(initial.get("answer_draft_markdown") or "").strip()
        if draft:
            self.output("\n回答案")
            self.output(draft)
        key_points = initial.get("key_points") or []
        if key_points:
            self.output("\n要点")
            for point in key_points:
                if isinstance(point, dict):
                    text = str(point.get("text") or "").strip()
                    source_ids = [
                        str(value)
                        for value in point.get("source_ids") or []
                        if str(value)
                    ]
                    suffix = (
                        f" [{'、'.join(source_ids)}]"
                        if source_ids
                        else ""
                    )
                else:
                    text = str(point).strip()
                    suffix = ""
                if text:
                    self.output(f"- {text}{suffix}")
        limitations = initial.get("limitations") or []
        if limitations:
            self.output("\n制限事項")
            for limitation in limitations:
                if isinstance(limitation, dict):
                    text = str(
                        limitation.get("text")
                        or limitation.get("message")
                        or ""
                    ).strip()
                else:
                    text = str(limitation).strip()
                if text:
                    self._print_warning(text)
        evidence = [
            item
            for item in payload.get("evidence") or []
            if isinstance(item, dict)
        ]
        if evidence:
            self.output("\n直接根拠")
            for item in evidence[:4]:
                source = item.get("source")
                path = (
                    str(source.get("path") or "")
                    if isinstance(source, dict)
                    else str(item.get("path") or "")
                )
                self.output(
                    f"- {item.get('id') or '根拠'}: "
                    f"{item.get('title') or path or '名称不明'}"
                )
                excerpt = str(
                    item.get("excerpt")
                    or item.get("matched_excerpt")
                    or item.get("text")
                    or ""
                ).strip()
                if excerpt:
                    self.output(f"  抜粋: {excerpt}")
        documents = [
            item
            for item in payload.get("document_results") or []
            if isinstance(item, dict)
        ]
        if documents:
            self.output("\n関連文書")
            for item in documents[:10]:
                self.output(
                    f"- {item.get('id') or '文書'}: "
                    f"{item.get('title') or item.get('path') or '名称不明'}"
                    f"（関連度: {item.get('support_level') or '不明'}）"
                )
        for warning in payload.get("warnings") or []:
            self._print_warning(str(warning))
        preferred: list[str] = []
        for key in ("evidence", "background_context", "document_results"):
            for item in payload.get(key) or []:
                if not isinstance(item, dict):
                    continue
                source = item.get("source")
                path = (
                    str(source.get("path") or "")
                    if isinstance(source, dict)
                    else str(item.get("path") or "")
                )
                value = str(
                    item.get("uri")
                    or item.get("source_permalink")
                    or item.get("source_url")
                    or path
                )
                if value and value not in preferred:
                    preferred.append(value)
        if preferred:
            self.output("\n参照先")
            for value in preferred[:10]:
                self.output(f"- {value}")

    def _create_database(self) -> None:
        self._print_screen_header("新しいDBを作成")
        name = self._prompt_preserving_value(
            "DB名",
            "",
            required=True,
            description=(
                "末尾が -rag になる半角英数字名です。\n"
                "使用可能な文字: 半角英数字、_、.、-"
            ),
            examples=("project-rag", "incident-rag", "product-manual-rag"),
        )
        if name is None:
            return
        name = name.strip()
        if not self._valid_database_name(name):
            self._print_error(
                "DB名は半角英数字で始まり、使用可能な文字だけを使い、"
                "末尾を -rag にしてください。"
            )
            self.output("入力例: project-rag")
            self.output("DBは作成されていません。")
            return
        if self._database_root(name).exists():
            self._print_error(f"DB「{name}」は既に存在します。")
            return
        title = self._prompt_preserving_value(
            "表示名",
            "",
            required=False,
            description="人間が一覧で識別しやすい名前です。",
            examples=("製品A 設計資料",),
            empty_help="DB名を表示名として利用",
        )
        if title is None:
            return
        query_hint = self._prompt_preserving_value(
            "検索ヒント",
            "",
            required=False,
            description=(
                "CopilotがDBを選ぶときに使う短い説明です。"
                "文書本文には入りません。"
            ),
            examples=("製品Aの設計、障害、運用、会議記録を収録",),
            empty_help="検索ヒントなし",
        )
        if query_hint is None:
            return
        self.output("\n作成内容")
        self.output(f"  DB名: {name}")
        self.output(f"  表示名: {title.strip() or '未設定'}")
        self.output(f"  検索ヒント: {query_hint.strip() or '未設定'}")
        if not self._confirm("この内容で作成しますか？"):
            self._print_info("DB作成をキャンセルしました。")
            return
        arguments = ["--db", name]
        if title and title.strip():
            arguments.extend(["--title", title.strip()])
        if query_hint and query_hint.strip():
            arguments.extend(["--query-hint", query_hint.strip()])
        result = self._invoke(
            "gen_db/create_db.py",
            arguments,
            capture_output=True,
        )
        if result is not None and int(result.returncode) == 0:
            self._print_success(f"DB「{name}」を作成しました。")
            self._print_info(
                "続いて「DBを選んで管理する」からこのDBを開き、"
                "「新しいSourceを追加する」を選んでください。"
            )

    def _edit_database_metadata(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        self._print_screen_header(
            "DBの表示名・検索ヒントを変更",
            db_name=db_name,
        )
        self.output(
            "DB名、文書、索引、検索順位は変更しません。\n"
            "表示名は人間向け、検索ヒントはCopilotがDBを選ぶ際の説明です。"
        )
        current = self._read_database_metadata(db_name)
        title = self._prompt_preserving_value(
            "表示名",
            current["title"],
            required=False,
            description="DB一覧で人間が識別しやすい名前です。",
            examples=("製品A 設計・運用資料",),
            empty_help="DB名を表示名として利用",
        )
        if title is None:
            self._print_info("DB情報の変更をキャンセルしました。")
            return
        query_hint = self._prompt_preserving_value(
            "検索ヒント",
            current["query_hint"],
            required=False,
            description=(
                "Copilotが複数のDBから選ぶときに使う短い説明です。"
                "文書本文や検索索引には入りません。"
            ),
            examples=("製品Aの設計、障害、運用手順を収録",),
            empty_help="検索ヒントなし",
        )
        if query_hint is None:
            self._print_info("DB情報の変更をキャンセルしました。")
            return
        normalized_title = title.strip() or db_name
        normalized_hint = query_hint.strip()
        self.output("\n変更内容")
        self.output(f"  DB名（変更不可）: {db_name}")
        self.output(f"  表示名: {current['title']} → {normalized_title}")
        self.output(
            "  検索ヒント: "
            f"{current['query_hint'] or '未設定'} → "
            f"{normalized_hint or '未設定'}"
        )
        if not self._confirm("この内容で保存しますか？"):
            self._print_info("DB情報の変更をキャンセルしました。")
            return
        try:
            self._import_dbs().update_db_metadata(
                self._database_root(db_name),
                db_name,
                title=normalized_title,
                query_hint=normalized_hint,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._print_error(
                f"DB情報を保存できませんでした: {type(exc).__name__}: {exc}"
            )
            return
        self._print_success("DBの表示名と検索ヒントを保存しました。")

    def _build_or_resume(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        status = self._status_json(db_name)
        self._print_screen_header("DBを構築・再開", db_name=db_name)
        self.output(
            "buildは取り込み元からDBを初めて構築します。"
            "中断済み処理がある場合は同じ条件で再開できます。"
        )
        entries = [("1", "新しく構築する")]
        if status and status.get("can_resume"):
            entries.append(("2", "保存済み処理を再開する"))
        entries.extend((("3", "強制的に再構築する【危険】"), ("0", "戻る")))
        self._print_menu("操作", entries)
        choice = self._ask("番号を入力してください: ")
        if choice in (None, "0"):
            return
        if choice == "2" and status and status.get("can_resume"):
            self.output("\n再開する保存済み処理")
            self.output(f"  DB: {db_name}")
            self.output(f"  操作: {status.get('operation') or 'build'}")
            self.output(f"  論理ルート: {status.get('root') or '不明'}")
            self.output(f"  Source ID: {status.get('source_id') or '不明'}")
            self.output(
                "  読込範囲: "
                f"{status.get('scan_subdir') or '論理ルート全体'}"
            )
            self.output(
                "  確定単位: "
                f"{int(status.get('batch_size_files') or 5)}文書"
            )
            if self._confirm(f"DB「{db_name}」の保存済み処理を再開しますか？"):
                self._resume_saved_operation(db_name, status)
            return
        if choice not in {"1", "3"}:
            self._invalid_selection("表示された番号、または0")
            return
        values = self._prompt_ingestion_values()
        if values is None:
            return
        root, source_id, scan_subdir = values
        arguments = [
            "--db",
            db_name,
            "--root",
            root,
            "--source-id",
            source_id,
            "--include-root-name-in-path",
        ]
        if scan_subdir:
            arguments.extend(["--scan-subdir", scan_subdir])
        self._print_ingestion_summary(
            operation=(
                "DB強制再構築" if choice == "3" else "DB構築"
            ),
            db_name=db_name,
            root=root,
            source_id=source_id,
            scan_subdir=scan_subdir,
            retry_errors=False,
        )
        if choice == "3":
            self._print_warning(
                "強制再構築は既存の抽出結果と検索索引を作り直します。"
            )
            confirmation = self._ask(
                f"続行するにはDB名「{db_name}」を入力してください: "
            )
            if confirmation != db_name:
                self._print_info("強制再構築をキャンセルしました。")
                return
            arguments.append("--force-rebuild")
        if choice != "3" and not self._confirm(
            f"DB「{db_name}」の構築を開始しますか？"
        ):
            return
        result = self._invoke(
            "gen_db/build_db.py",
            arguments,
            capture_output=True,
        )
        if result is not None and int(result.returncode) == 0:
            self._show_operation_result(result, "DB構築")
            self._print_success(f"DB「{db_name}」の構築が完了しました。")

    def _resume_saved_operation(
        self,
        db_name: str,
        status: dict[str, Any],
    ) -> None:
        # Never execute the resume_command stored in progress. Reconstruct the
        # allowlisted argv from individually validated status fields.
        root = str(status.get("root") or "")
        source_id = str(status.get("source_id") or "")
        scan_subdir = str(status.get("scan_subdir") or ".")
        batch_size_files = int(status.get("batch_size_files") or 5)
        if not root or not source_id:
            self._print_error(
                "再開に必要な論理ルートまたはSource IDが保存されていません。"
            )
            return
        operation = str(status.get("operation") or "build")
        script = (
            "gen_db/add_data.py"
            if operation == "add"
            else "gen_db/build_db.py"
        )
        arguments = [
            "--db",
            db_name,
            "--root",
            root,
            "--source-id",
            source_id,
            "--include-root-name-in-path",
            "--resume",
            "--batch-size-files",
            str(batch_size_files),
        ]
        if scan_subdir and scan_subdir != ".":
            arguments.extend(["--scan-subdir", scan_subdir])
        self._print_info(
            "保存済み処理を再開しています。完了までお待ちください。"
        )
        result = self._invoke(script, arguments, capture_output=True)
        if result is not None and int(result.returncode) == 0:
            self._show_operation_result(result, "処理再開")
            self._print_success(f"DB「{db_name}」の処理を再開・完了しました。")

    def _add_or_update(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        source_id = self._select_ingestion_source_id(db_name)
        if source_id is None:
            return
        values = self._prompt_ingestion_values(source_id=source_id)
        if values is None:
            return
        root, source_id, scan_subdir = values
        arguments = [
            "--db",
            db_name,
            "--root",
            root,
            "--source-id",
            source_id,
            "--include-root-name-in-path",
        ]
        if scan_subdir:
            arguments.extend(["--scan-subdir", scan_subdir])
        retry_errors = self._confirm(
            "前回の抽出エラーをもう一度処理しますか？"
        )
        if retry_errors:
            arguments.append("--retry-errors")
        self._print_ingestion_summary(
            operation="文書追加・更新",
            db_name=db_name,
            root=root,
            source_id=source_id,
            scan_subdir=scan_subdir,
            retry_errors=retry_errors,
        )
        if not self._confirm(f"DB「{db_name}」へ追加・更新しますか？"):
            return
        self._print_info(
            "文書を追加・更新しています。完了までこの画面でお待ちください。"
        )
        result = self._invoke(
            "gen_db/add_data.py",
            arguments,
            capture_output=True,
        )
        if result is not None and int(result.returncode) == 0:
            self._show_operation_result(result, "文書追加・更新")
            self._print_success(f"DB「{db_name}」の文書を更新しました。")

    def _prompt_ingestion_values(
        self,
        *,
        source_id: str | None = None,
    ) -> tuple[str, str, str] | None:
        root = self._prompt_preserving_value(
            "論理ルートディレクトリ",
            "",
            required=True,
            description=(
                "RAGへ取り込むファイル群の基準ディレクトリです。\n"
                "このディレクトリ名は保存パスの先頭へ必ず含まれます。"
            ),
            examples=(
                r"C:\path\to\project-docs",
                "/path/to/project-docs",
            ),
        )
        if root is None:
            return None
        if source_id is None:
            source_id = self._prompt_preserving_value(
                "Source ID",
                "",
                required=True,
                description=(
                    "文書の取り込み元を識別する、変更しないIDです。\n"
                    "同じ取り込み元を更新するときは同じIDを使用します。\n"
                    "1 Sourceは1つのProvider・1つのURL生成単位です。"
                    "GitHub、GitLab、Azure DevOps、SharePoint、Redmineを"
                    "同じIDへ混在させないでください。"
                ),
                examples=(
                    "github-repository",
                    "gitlab-repository",
                    "azure-repository",
                    "svn-repository",
                    "sharepoint-docs",
                    "redmine-issues",
                    "filesystem-docs",
                ),
            )
            if source_id is None:
                return None
        scan_subdir = self._prompt_preserving_value(
            "読込サブディレクトリ（scan subdirectory）",
            "",
            required=False,
            description=(
                "論理ルート全体ではなく、一部だけを読み込む場合に指定します。"
            ),
            examples=("docs", "manuals/ja"),
            empty_help="論理ルート全体を対象",
        )
        if scan_subdir is None:
            return None
        return root.strip(), source_id.strip(), str(scan_subdir).strip()

    def _print_ingestion_summary(
        self,
        *,
        operation: str,
        db_name: str,
        root: str,
        source_id: str,
        scan_subdir: str,
        retry_errors: bool,
    ) -> None:
        self.output("\n実行内容")
        self.output(f"  操作: {operation}")
        self.output(f"  DB: {db_name}")
        self.output(f"  Source ID: {source_id}")
        self.output(f"  論理ルート: {root}")
        self.output(f"  読込範囲: {scan_subdir or '論理ルート全体'}")
        self.output(
            "  抽出エラーの再試行: "
            f"{'する' if retry_errors else 'しない'}"
        )

    def _select_ingestion_source_id(self, db_name: str) -> str | None:
        inventory = self._load_source_inventory(db_name)
        source_ids = (
            [
                str(source["source_id"])
                for source in self._inventory_sources(inventory)
            ]
            if inventory is not None
            else []
        )
        self._print_screen_header("Source IDを選択", db_name=db_name)
        self.output(
            "同じ取り込み元を更新するときは既存のSource IDを選びます。\n"
            "新しいProviderや取り込み元は、別のSource IDにしてください。"
        )
        for index, source_id in enumerate(source_ids, start=1):
            self.output(f"{index}. 既存のSource: {source_id}")
        new_index = len(source_ids) + 1
        self.output(
            f"{new_index}. 新しいSource IDを入力"
        )
        self.output(
            "例: sharepoint-docs、redmine-issues、github-repository、"
            "gitlab-repository、azure-repository、svn-repository、"
            "filesystem-docs"
        )
        choice = self._ask("番号を入力してください（0: キャンセル）: ")
        if choice in (None, "0"):
            return None
        try:
            index = int(choice)
        except ValueError:
            self._invalid_selection(f"1～{new_index}、または0")
            return None
        if 1 <= index <= len(source_ids):
            return source_ids[index - 1]
        if index != new_index:
            self._invalid_selection(f"1～{new_index}、または0")
            return None
        value = self._prompt_preserving_value(
            "新しいSource ID",
            "",
            required=True,
            description=(
                "Providerごとに分けた、今後変更しない識別子です。"
            ),
            examples=(
                "github-repository",
                "sharepoint-docs",
                "redmine-issues",
            ),
        )
        if value is None:
            return None
        return value.strip()

    def _repair_index(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        self._print_screen_header("検索索引を修復", db_name=db_name)
        self._print_warning(
            "索引を再作成します。RAG文書とSource Link設定は変更しません。"
        )
        self.output(
            "全文・識別子索引: 単語検索、Exact、識別子検索に使います。\n"
            "ベクトル索引: 意味検索に使う埋め込み索引です。\n"
            "すべて: 上記2種類を再作成します。"
        )
        self._print_menu(
            "修復する索引",
            (
                ("1", "全文・識別子索引"),
                ("2", "ベクトル索引"),
                ("3", "すべての検索索引"),
                ("0", "戻る"),
            ),
        )
        choice = self._ask("番号を入力してください: ")
        component = REPAIR_COMPONENTS.get(str(choice or ""))
        if component is None:
            if choice not in (None, "0"):
                self._invalid_selection("0～3")
            return
        component_ja = {
            "lexical": "全文・識別子",
            "vector": "ベクトル",
            "all": "すべて",
        }[component]
        if not self._confirm(
            f"DB「{db_name}」の{component_ja}索引を再作成しますか？"
        ):
            return
        self._print_info(
            "検索索引を再作成しています。完了までお待ちください。"
        )
        result = self._invoke(
            "gen_db/rebuild_component.py",
            ["--db", db_name, "--component", component],
            capture_output=True,
        )
        if result is not None and int(result.returncode) == 0:
            self._show_operation_result(result, "検索索引修復")
            self._print_success(f"{component_ja}索引を再作成しました。")

    def _show_operation_result(self, result: Any, operation: str) -> None:
        raw = str(getattr(result, "stdout", "") or "").strip()
        if not raw:
            return
        payload = self._last_json_object(raw)
        if payload is None:
            self._print_info(
                f"{operation}は終了しましたが、件数の要約を読み取れませんでした。"
            )
            return
        self.output(f"\n{operation}の結果")
        labels = {
            "indexed_files": "索引登録ファイル",
            "new_files": "新規ファイル",
            "changed_files": "変更ファイル",
            "skipped_files": "未変更・スキップ",
            "failed_files": "失敗ファイル",
            "error_files": "失敗ファイル",
            "deleted_files": "削除反映ファイル",
            "upserted_records": "更新レコード",
            "deleted_records": "削除レコード",
            "collection_count": "ベクトル索引レコード",
            "rebuilt_records": "再構築レコード",
        }
        shown = False
        for key, label in labels.items():
            if key in payload:
                self.output(f"{label}: {payload[key]}")
                shown = True
        if not shown:
            self.output("処理は正常に完了しました。")

    @staticmethod
    def _last_json_object(raw: str) -> dict[str, Any] | None:
        text = raw[-2_000_000:]
        decoder = json.JSONDecoder()
        candidates = [
            index
            for index, character in enumerate(text)
            if character == "{"
            and (index == 0 or text[index - 1] in "\r\n")
        ]
        for index in reversed(candidates):
            try:
                value, _end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    def _sources_screen(self, db_name: str) -> None:
        while self._database_root(db_name).is_dir():
            self._print_screen_header("Source", db_name=db_name)
            self.output(
                "Sourceとは:\n"
                "同じ取得元と検索結果リンク設定を共有する資料のまとまりです。"
            )
            inventory = self._load_source_inventory(db_name)
            catalog_sources = (
                self._inventory_sources(inventory)
                if inventory is not None
                else []
            )
            sources = self._combined_source_records(
                db_name,
                catalog_sources,
            )
            if not sources:
                self._print_info("Sourceはまだありません。")
                return
            for index, source in enumerate(sources, start=1):
                name = str(source.get("display_name") or "既存データ")
                source_type = self._ui_source_type(source.get("source_type"))
                status = self._source_manager_status(source)
                self.output(
                    f"{index}. {name:<18} "
                    f"{_PROVIDER_JA.get(source_type, 'Other'):<12} {status}"
                )
            self.output("\n0. 戻る")
            choice = self._ask("Source番号を入力してください: ")
            if choice in (None, "0"):
                return
            try:
                selected_index = int(choice) - 1
            except ValueError:
                self._invalid_selection(f"1～{len(sources)}、または0")
                continue
            if not 0 <= selected_index < len(sources):
                self._invalid_selection(f"1～{len(sources)}、または0")
                continue
            self._source_detail_screen(
                db_name,
                inventory,
                sources[selected_index],
            )

    def _load_source_inventory(self, db_name: str) -> Any | None:
        try:
            module = self._import_source_inventory()
            return module.build_source_inventory(
                self._database_root(db_name),
                db_name,
            )
        except Exception as exc:
            self._print_error(
                "Source一覧を読み取れませんでした: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    @staticmethod
    def _inventory_sources(inventory: Any) -> list[dict[str, Any]]:
        payload = inventory.to_dict()
        return [
            dict(value)
            for value in payload.get("sources") or []
            if isinstance(value, dict) and value.get("source_id")
        ]

    def _source_manager_records(self, db_name: str) -> list[dict[str, Any]]:
        try:
            root = self._validated_database_root(db_name) / "sources"
        except ManagerError:
            return []
        if not root.is_dir() or root.is_symlink():
            return []
        records: list[dict[str, Any]] = []
        for directory in sorted(root.iterdir(), key=lambda value: value.name):
            source_path = directory / "source.json"
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or source_path.is_symlink()
                or not source_path.is_file()
            ):
                continue
            try:
                source = json.loads(source_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(source, dict):
                continue
            value = dict(source)
            value["_local_source_key"] = str(
                source.get("local_source_key")
                or source.get("source_key")
                or directory.name
            )
            state_path = directory / "state.json"
            if state_path.is_file() and not state_path.is_symlink():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    state = {}
                if isinstance(state, dict):
                    value["_state"] = state
            records.append(value)
        return records

    def _combined_source_records(
        self,
        db_name: str,
        catalog_sources: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        managed = self._source_manager_records(db_name)
        by_source_id = {
            str(value.get("source_id")): value
            for value in managed
            if str(value.get("source_id") or "")
        }
        combined: list[dict[str, Any]] = []
        included_keys: set[str] = set()
        for catalog in catalog_sources:
            source_id = str(catalog.get("source_id") or "")
            value = dict(catalog)
            configured = by_source_id.get(source_id)
            if configured is not None:
                merged = dict(configured)
                merged.update(value)
                merged["_state"] = configured.get("_state") or {}
                merged["_local_source_key"] = configured.get(
                    "_local_source_key"
                )
                value = merged
                included_keys.add(str(configured.get("_local_source_key") or ""))
            value["_catalog_present"] = True
            combined.append(value)
        for configured in managed:
            local_key = str(configured.get("_local_source_key") or "")
            if local_key in included_keys:
                continue
            value = dict(configured)
            value["_catalog_present"] = False
            combined.append(value)
        combined.sort(
            key=lambda value: (
                0 if value.get("_catalog_present") else 1,
                -int(value.get("document_count") or 0),
                str(value.get("display_name") or "").casefold(),
                str(value.get("_local_source_key") or ""),
            )
        )
        return combined

    @staticmethod
    def _ui_source_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return (
            normalized
            if normalized in {"github", "svn", "redmine", "sharepoint", "other"}
            else "other"
        )

    def _source_manager_status(self, source: dict[str, Any]) -> str:
        source_type = self._ui_source_type(source.get("source_type"))
        if source_type == "sharepoint" and os.name != "nt":
            return "このOSでは更新不可"
        local_key = str(source.get("_local_source_key") or "")
        if not local_key:
            return "既存データ"
        state = source.get("_state")
        state = state if isinstance(state, dict) else {}
        if bool(source.get("metadata_sync_pending")) or bool(
            state.get("metadata_sync_pending")
        ):
            return "更新途中・再開可能"
        if bool(state.get("can_resume")):
            if not source.get("_catalog_present"):
                return "初回取得途中・再開可能"
            if state.get("last_error"):
                return "前回失敗・再開可能"
            return "更新途中・再開可能"
        if not source.get("_catalog_present"):
            return "更新可能"
        if source_type == "other":
            return "最新"
        return "最新"

    def _select_source(self, inventory: Any) -> dict[str, Any] | None:
        sources = self._inventory_sources(inventory)
        inventory_payload = inventory.to_dict()
        self.output("\nSource一覧（読み取り専用）")
        missing = int(
            inventory_payload.get("documents_without_source_id")
            or inventory_payload.get("missing_source_document_count")
            or 0
        )
        if missing:
            self._print_warning(
                f"Source IDがない索引済み文書が{missing:,}件あります。"
            )
        if not sources:
            self._print_info("索引済みのSourceがありません。")
            return None
        for index, source in enumerate(sources, start=1):
            source_id = str(source["source_id"])
            label = str(source.get("display_name") or source_id)
            source_type = str(source.get("source_type") or "")
            link_setting = source.get("source_link_setting") or {}
            roots = source.get("observed_stored_roots") or []
            self.output(f"\n{index}. {label}")
            self.output(f"   Source ID: {source_id}")
            self.output(
                f"   文書数: {int(source.get('document_count') or 0):,}"
            )
            self.output(
                f"   チャンク数: {int(source.get('chunk_count') or 0):,}"
            )
            self.output(
                f"   Source種別: {self._provider_label(source_type)}"
            )
            self.output(
                f"   Source Link: "
                f"{self._status_label(source.get('link_status') or 'not_configured')}"
            )
            self.output(
                f"   保存ルート: {', '.join(str(v) for v in roots) or '未検出'}"
            )
        self._print_info(
            "Source IDは読み取り専用です。追加・削除・名称変更はできません。"
        )
        choice = self._ask("Source番号を入力してください（0: 戻る）: ")
        if choice in (None, "0"):
            return None
        try:
            index = int(choice) - 1
        except ValueError:
            self._invalid_selection(f"1～{len(sources)}、または0")
            return None
        if not 0 <= index < len(sources):
            self._invalid_selection(f"1～{len(sources)}、または0")
            return None
        return sources[index]

    def _source_detail_screen(
        self,
        db_name: str,
        inventory: Any | None,
        source: dict[str, Any],
    ) -> None:
        source_id = str(source.get("source_id") or "")
        while True:
            source_type = self._ui_source_type(source.get("source_type"))
            display_name = str(source.get("display_name") or "既存データ")
            state_label = self._source_manager_status(source)
            self._print_screen_header(
                "Source詳細",
                db_name=db_name,
            )
            self.output(
                f"Source: {display_name}\n"
                f"種類  : {_PROVIDER_JA.get(source_type, 'Other')}\n"
                f"状態  : {state_label}"
            )
            update_label = (
                "ファイル／フォルダを選び直して再取り込み"
                if source_type == "other" and state_label == "最新"
                else "更新・再開する"
            )
            entries = (
                ("1", update_label),
                ("2", "取得設定を確認・変更する"),
                ("3", "検索結果リンクを確認・変更する"),
                ("4", "進捗・ログを見る"),
                ("5", "技術情報"),
                ("0", "戻る"),
            )
            self._print_menu("操作", entries)
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._update_single_source(db_name, source)
            elif choice == "2":
                self._edit_source_fetch_settings(db_name, source)
            elif choice == "3":
                if not source_id or inventory is None:
                    self._print_warning(
                        "初回の検索反映が完了してからリンクを設定できます。"
                    )
                else:
                    self._source_link_screen(db_name, inventory, source_id)
            elif choice == "4":
                self._show_source_progress(source)
            elif choice == "5":
                self._show_source_technical_info(source)
            else:
                self._invalid_selection("0～5")

    def _update_single_source(
        self,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        local_key = str(source.get("_local_source_key") or "")
        if not local_key:
            self._print_warning(
                "このSourceには自動取得の設定がありません。"
                "既存の資料はそのまま検索できます。"
            )
            return
        source_type = self._ui_source_type(source.get("source_type"))
        if source_type == "sharepoint" and os.name != "nt":
            self._print_warning(
                "このOSではSharePointの追加・更新はできません。"
                "検索とWebリンク表示は利用できます。"
            )
            return
        runtime_input: str | None = None
        if (
            source_type == "other"
            and self._source_manager_status(source) == "最新"
        ):
            runtime_input = self._prompt_preserving_value(
                "ファイルまたはフォルダ",
                "",
                required=True,
                description=(
                    "今回取り込み直す資料を選びます。完了後、"
                    "この端末の絶対パスは保存しません。"
                ),
                examples=("選択したファイルまたはフォルダ",),
            )
            if runtime_input is None:
                self._print_info("再取り込みを開始しませんでした。")
                return
        if not self._confirm("このSourceの更新または再開を開始しますか？"):
            self._print_info("処理を開始しませんでした。")
            return
        try:
            from source_manager.runner import update_source

            result = update_source(
                self._database_root(db_name),
                local_key,
                python_executable=self._runtime_python(),
                rag_root=self.rag_root,
                runtime_input=runtime_input,
            )
        except Exception as exc:
            self._print_error(
                "Sourceの処理に失敗しました。"
                f"次回このSourceから再開できます（{type(exc).__name__}）。"
            )
            return
        if str(result.get("status") or "") in {
            "ok",
            "complete",
            "completed",
            "success",
            "updated",
        }:
            self._print_success("Sourceを検索へ反映しました。")
        else:
            self._print_warning(
                str(result.get("message") or "処理は再開可能な位置で停止しました。")
            )

    def _show_source_fetch_settings(
        self,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        self.output("\n取得設定")
        self.output(
            f"取得元: {_PROVIDER_JA.get(self._ui_source_type(source.get('source_type')), 'Other')}"
        )
        fetch = source.get("fetch")
        if not isinstance(fetch, dict):
            fetch = source.get("provider_settings")
        fetch = fetch if isinstance(fetch, dict) else {}
        public_labels = {
            "repository_url": "取得URL",
            "project_url": "プロジェクトURL",
            "relative_path": "同期ルートからの相対フォルダ",
            "recursive": "配下フォルダ",
            "updated_within_days": "取得期間（日）",
            "one_shot": "取り込み方式",
        }
        shown = False
        for key, label in public_labels.items():
            if key not in fetch:
                continue
            value = fetch[key]
            if key == "recursive":
                value = "含める" if bool(value) else "この階層だけ"
            elif key == "one_shot":
                value = "今回だけ取り込む"
            elif value is None:
                value = "制限なし"
            self.output(f"{label}: {value}")
            shown = True
        if not shown:
            self.output("取得設定: 既存データ（自動更新なし）")
        self._print_info(
            "取得設定の編集は、現在の処理が完了している場合だけ安全に行えます。"
        )
        return dict(fetch)

    def _edit_source_fetch_settings(
        self,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        fetch = self._show_source_fetch_settings(source)
        local_key = str(source.get("_local_source_key") or "")
        if not local_key:
            self._print_info(
                "このSourceには変更できる取得設定がありません。"
            )
            return
        source_type = self._ui_source_type(source.get("source_type"))
        if source_type == "other":
            self._print_info(
                "Otherは今回だけ取り込む方式です。"
                "再取り込み時にファイルまたはフォルダを選び直します。"
            )
            return
        if source_type == "sharepoint" and source.get("source_id"):
            self._print_warning(
                "検索へ反映済みのSharePoint Sourceでは、"
                "同期ルートからの相対フォルダを変更できません。"
            )
            self._print_info(
                "別のフォルダを取り込む場合は、"
                "「新しいSourceを追加する」から登録してください。"
                "検索結果リンクは別メニューで変更できます。"
            )
            return
        if source_type == "sharepoint" and os.name != "nt":
            self._print_warning(
                "このOSではSharePointの取得設定を変更できません。"
                "検索とWebリンク表示は利用できます。"
            )
            return
        updated = dict(fetch)
        summary: list[tuple[str, Any]] = []
        if source_type in {"github", "svn"}:
            label = (
                "GitHubリポジトリのURL"
                if source_type == "github"
                else "SVNのURL"
            )
            repository_url = self._prompt_preserving_value(
                label,
                str(fetch.get("repository_url") or ""),
                required=True,
                examples=(
                    ("https://github.com/<owner>/<repository>.git",)
                    if source_type == "github"
                    else (
                        "https://svn.example.invalid/repos/project/trunk",
                    )
                ),
            )
            if repository_url is None:
                return
            updated["repository_url"] = repository_url
            summary.append((label, repository_url))
            if source_type == "svn":
                recursive = self._select_value(
                    "取り込む範囲",
                    (
                        ("recursive", "配下のフォルダも含める（再帰）"),
                        ("direct", "この階層のファイルだけ"),
                    ),
                    default=(
                        "recursive"
                        if bool(fetch.get("recursive", True))
                        else "direct"
                    ),
                )
                if recursive is None:
                    return
                updated["recursive"] = recursive == "recursive"
                summary.append(
                    (
                        "取り込む範囲",
                        "再帰"
                        if updated["recursive"]
                        else "この階層のファイルだけ",
                    )
                )
        elif source_type == "redmine":
            # base URL and project identifier are validated derivatives of
            # project_url, not independently editable settings.
            updated.pop("base_url", None)
            updated.pop("project_id", None)
            project_url = self._prompt_preserving_value(
                "RedmineプロジェクトのURL",
                str(fetch.get("project_url") or ""),
                required=True,
                examples=(
                    "https://redmine.example.invalid/projects/project-id",
                ),
            )
            if project_url is None:
                return
            updated["project_url"] = project_url
            current_days = fetch.get("updated_within_days")
            days = self._prompt_preserving_value(
                "取得期間（日）",
                "" if current_days is None else str(current_days),
                required=False,
                description=(
                    "Issueの更新日時を基準にします。空欄は制限なしです。"
                ),
                examples=("365", "90", "30"),
                empty_help="制限なし",
            )
            if days is None:
                return
            if days:
                try:
                    parsed_days = int(days)
                except ValueError:
                    self._print_error(
                        "取得期間は1以上の整数で入力してください。"
                    )
                    return
                if parsed_days < 1:
                    self._print_error(
                        "取得期間は1以上の整数で入力してください。"
                    )
                    return
                updated["updated_within_days"] = parsed_days
            else:
                updated["updated_within_days"] = None
            summary.extend(
                (
                    ("RedmineプロジェクトのURL", project_url),
                    (
                        "取得期間",
                        "制限なし"
                        if updated["updated_within_days"] is None
                        else f"{updated['updated_within_days']}日",
                    ),
                )
            )
        elif source_type == "sharepoint":
            relative = self._prompt_preserving_value(
                "SharePoint rootからの相対フォルダ",
                str(fetch.get("relative_path") or ""),
                required=True,
                examples=("Team Documents/manuals",),
            )
            if relative is None:
                return
            updated["relative_path"] = relative
            summary.append(("同期ルートからの相対フォルダ", relative))
        self.output("\n変更後の取得設定")
        for label, value in summary:
            self.output(f"{label}: {value}")
        if not self._confirm("この内容で取得設定を保存しますか？"):
            self._print_info("取得設定は変更されていません。")
            return
        try:
            from source_manager.runner import update_source_configuration

            update_source_configuration(
                self._database_root(db_name),
                local_key,
                fetch=updated,
            )
        except Exception as exc:
            self._print_error(
                "取得設定を保存できませんでした。"
                f"処理状況を確認してください（{type(exc).__name__}）。"
            )
            return
        self._print_success("取得設定を保存しました。")

    def _show_source_progress(self, source: dict[str, Any]) -> None:
        state = source.get("_state")
        state = state if isinstance(state, dict) else {}
        self.output("\n処理状況")
        self.output(f"状態: {self._source_manager_status(source)}")
        self.output(f"取得済み: {int(state.get('fetched_count') or 0):,}件")
        self.output(
            f"検索へ反映済み: "
            f"{int(state.get('indexed_confirmed_count') or 0):,}件"
        )
        self.output(f"未反映: {int(state.get('pending_count') or 0):,}件")
        self.output(
            f"最後の再開位置: {state.get('last_completed_item') or '未記録'}"
        )
        if state.get("last_error"):
            self._print_warning(
                "前回の処理でエラーがありました。"
                "秘密情報と端末固有パスを除いた内容だけを記録しています。"
            )

    def _show_source_technical_info(self, source: dict[str, Any]) -> None:
        self.output("\n技術情報")
        self.output(f"Source ID: {source.get('source_id') or '未確定'}")
        self.output(
            f"内部ディレクトリ: "
            f"{source.get('_local_source_key') or '管理情報なし'}"
        )
        self.output(
            f"文書数: {int(source.get('document_count') or 0):,}"
        )
        self.output(
            f"内部種別: {str(source.get('source_type') or 'unspecified')}"
        )

    def _source_link_screen(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        while True:
            self._print_screen_header(
                "Source Link設定",
                db_name=db_name,
                source_id=source_id,
            )
            self.output(
                "Source Linkとは:\n"
                "RAGの検索結果に、元のGitHub・GitLab・Azure DevOps・"
                "Subversion・SharePoint・Redmine等を開くURLを付ける設定です。\n"
                "検索順位や検索内容には影響しません。設定できない場合も"
                "RAG内の保存パスは表示されます。"
            )
            self._print_menu(
                "操作",
                SOURCE_LINK_MENU,
            )
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._show_source_link(db_name, source_id)
            elif choice == "2":
                self._configure_source_link(db_name, inventory, source_id)
            elif choice == "3":
                self._toggle_source_link(db_name, inventory, source_id)
            elif choice == "4":
                self._remove_source_link(db_name, inventory, source_id)
            elif choice == "5":
                self._preview_source_link(db_name, inventory, source_id)
            elif choice == "6":
                self._open_help()
            else:
                self._invalid_selection("0～6")

    def _configure_source_metadata(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, payload, source = loaded
        current = source or {}
        display_name = self._prompt_preserving_value(
            "Source表示名",
            str(current.get("display_name") or ""),
            required=False,
            description=(
                "Manager上でSourceを識別しやすくする表示専用の名前です。"
                "Source IDは変更されません。"
            ),
            examples=("設計資料", "障害管理", "ソースコード"),
            empty_help="Source IDを表示",
        )
        if display_name is None:
            return
        source_types = [
            "unspecified",
            "folder",
            "git",
            "github",
            "gitlab",
            "azure_devops",
            "svn",
            "sharepoint",
            "redmine",
            "other",
        ]
        current_type = str(current.get("source_type") or "unspecified")
        source_type = self._prompt_choice_preserving(
            "Source種別",
            source_types,
            current_type,
            required=False,
        )
        if source_type is None:
            return
        source_type = "" if source_type == "unspecified" else source_type
        if isinstance(current.get("link"), dict):
            current_link_type = str(current.get("source_type") or "")
            if not source_type or source_type != current_link_type:
                self._print_error(
                    "Source Linkが設定されているため、種別だけを変更できません。"
                    "先にSource Linkを削除するか、Source Link設定から"
                    "Providerを変更してください。"
                )
                return
        self.output("\n変更内容")
        self.output(
            f"  Source表示名: {display_name or '未設定'}"
        )
        self.output(
            f"  Source種別: {self._provider_label(source_type)}"
        )
        if not self._confirm(
            f"DB「{db_name}」のSource「{source_id}」へ保存しますか？"
        ):
            self._print_info("Source情報は変更されていません。")
            return
        if (
            not current
            and not display_name
            and not source_type
        ):
            self._print_info("保存するSource情報はありません。")
            return
        target = self._source_entry(payload, source_id, create=True)
        assert target is not None
        if display_name:
            target["display_name"] = display_name
        else:
            target.pop("display_name", None)
        if source_type:
            target["source_type"] = source_type
        else:
            target.pop("source_type", None)
        if (
            not target.get("display_name")
            and not target.get("source_type")
            and not target.get("link")
        ):
            payload["sources"] = [
                value
                for value in payload.get("sources") or []
                if value.get("source_id") != source_id
            ]
        if self._save_sidecar(
            db_name,
            inventory,
            source_links,
            payload,
        ):
            self._print_success("Source情報を保存しました。")

    def _load_sidecar_payload(
        self,
        db_name: str,
    ) -> tuple[Any, dict[str, Any]] | None:
        source_links = self._import_source_links()
        try:
            db_root = self._validated_database_root(db_name)
        except ManagerError as exc:
            self._print_error(f"DBを安全に開けません: {exc}")
            return None
        loaded = source_links.load_source_links(
            db_root, db_name
        )
        if loaded.status == "invalid":
            self._print_error(
                "Source Link設定ファイルが不正です。変更は保存していません。"
            )
            return None
        if loaded.status == "manual_required":
            self._print_error(
                "既存のSource設定に自動変換できない内容があります。"
                "検索は相対パス表示で継続できます。"
                "詳細はSourceの「技術情報」で確認してください。"
            )
            return None
        self._sidecar_etags[db_name] = str(
            getattr(loaded, "etag", "missing")
        )
        self._sidecar_migrations[db_name] = bool(
            getattr(loaded, "migration_required", False)
        )
        self._sidecar_source_statuses[db_name] = dict(
            getattr(loaded, "source_statuses", ())
        )
        if self._sidecar_migrations[db_name]:
            statuses = self._sidecar_source_statuses[db_name]
            summary = ", ".join(
                f"{source_id}={status}"
                for source_id, status in sorted(statuses.items())
            )
            self._print_warning(
                "互換形式のSharePointリンク設定を読み取り専用で"
                "開きました。次にSource設定を保存すると現行形式で"
                "保存されます。"
                + (f" 状態: {summary}" if summary else "")
            )
        if loaded.status == "configured" and loaded.payload is not None:
            return source_links, copy.deepcopy(loaded.payload)
        return source_links, {
            "schema_version": source_links.SCHEMA_VERSION,
            "revision": 0,
            "sources": [],
        }

    @staticmethod
    def _source_entry(
        payload: dict[str, Any],
        source_id: str,
        *,
        create: bool,
    ) -> dict[str, Any] | None:
        sources = payload.setdefault("sources", [])
        for source in sources:
            if isinstance(source, dict) and source.get("source_id") == source_id:
                return source
        if not create:
            return None
        source = {"source_id": source_id}
        sources.append(source)
        return source

    def _inventory_ids_paths(
        self,
        inventory: Any,
    ) -> tuple[list[str], dict[str, list[str]]]:
        sources = self._inventory_sources(inventory)
        ids = [str(value["source_id"]) for value in sources]
        if hasattr(inventory, "observed_paths_by_source"):
            observed = inventory.observed_paths_by_source()
            return ids, {
                str(key): [str(path) for path in value]
                for key, value in observed.items()
            }
        observed: dict[str, list[str]] = {}
        for source in sources:
            source_id = str(source["source_id"])
            values = (
                source.get("observed_paths")
                or source.get("sample_documents")
                or source.get("document_samples")
                or source.get("paths")
                or []
            )
            observed[source_id] = [
                str(value.get("path") if isinstance(value, dict) else value)
                for value in values
                if value
            ]
        return ids, observed

    def _save_sidecar(
        self,
        db_name: str,
        inventory: Any,
        source_links: Any,
        payload: dict[str, Any],
    ) -> bool:
        ids, _observed = self._inventory_ids_paths(inventory)
        previous_revision = int(payload.get("revision") or 0)
        payload["revision"] = previous_revision + 1
        kwargs: dict[str, Any] = {
            "db_name": db_name,
            "existing_sources": ids,
            "allow_unmatched_sources": True,
            "expected_revision": previous_revision,
            "expected_etag": self._sidecar_etags.get(db_name, "missing"),
        }
        try:
            db_root = self._validated_database_root(db_name)
            source_links.save_source_links(
                db_root, payload, **kwargs
            )
        except Exception as exc:
            self._print_error(
                "Source Link設定を保存できませんでした: "
                f"{type(exc).__name__}: {exc}。設定は変更されていません。"
            )
            return False
        self._sidecar_migrations[db_name] = False
        return True

    def _source_link(
        self,
        db_name: str,
        source_id: str,
    ) -> tuple[Any, dict[str, Any], dict[str, Any] | None] | None:
        loaded = self._load_sidecar_payload(db_name)
        if loaded is None:
            return None
        source_links, payload = loaded
        source = self._source_entry(payload, source_id, create=False)
        return source_links, payload, source

    @staticmethod
    def _flat_source_link(
        source: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(source, dict):
            return {}
        nested = source.get("link")
        if isinstance(nested, dict):
            return {
                "display_name": str(source.get("display_name") or ""),
                "provider": str(source.get("source_type") or ""),
                "enabled": bool(nested.get("enabled")),
                "strategy": str(nested.get("strategy") or ""),
                "settings": copy.deepcopy(nested.get("settings") or {}),
            }
        return {
            key: copy.deepcopy(source[key])
            for key in (
                "display_name",
                "provider",
                "enabled",
                "strategy",
                "settings",
            )
            if key in source
        }

    def _show_source_link(self, db_name: str, source_id: str) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source = loaded[2]
        flat = self._flat_source_link(source)
        if not flat.get("provider"):
            self._print_info("このSourceにはSource Linkが設定されていません。")
            return
        self._print_source_link_summary(source_id, flat)

    def _print_source_link_summary(
        self,
        source_id: str,
        source: dict[str, Any],
    ) -> None:
        source = self._flat_source_link(source)
        settings = source.get("settings") or {}
        provider = str(source.get("provider") or "")
        self.output("\nSource Link設定")
        self.output(f"Source ID: {source_id}")
        self.output(
            f"表示名: {source.get('display_name') or '未設定'}"
        )
        self.output(f"Provider: {self._provider_label(provider)}")
        self.output(
            f"状態: {'有効' if source.get('enabled') else '無効'}"
        )
        self.output(
            f"リンク方式: {self._strategy_label(source.get('strategy'))}"
        )
        if provider in _GIT_PROVIDERS:
            provider_label = self._provider_label(provider)
            self.output(
                f"リポジトリ: {settings.get('repository_url') or '未設定'}"
            )
            self.output(
                f"通常表示版: {settings.get('ref') or '未設定'}"
            )
            self.output(
                f"{provider_label}リポジトリ内の追加パス: "
                f"{settings.get('repository_path_prefix') or '未設定'}"
            )
            self.output(
                "固定リンク: "
                f"{'有効' if settings.get('permalink_enabled') else '無効'}"
            )
            if settings.get("commit"):
                self.output(f"固定コミット: {settings['commit']}")
        elif provider == "svn":
            self.output(
                f"SVN URL: {settings.get('repository_url') or '未設定'}"
            )
            if source.get("strategy") == "svn-http":
                self.output(
                    "SVNリポジトリ内の追加パス: "
                    f"{settings.get('repository_path_prefix') or '未設定'}"
                )
                self.output(
                    "固定リビジョンリンク: "
                    f"{'有効' if settings.get('permalink_enabled') else '無効'}"
                )
                if settings.get("revision") is not None:
                    self.output(f"リビジョン: {settings['revision']}")
            else:
                self.output(
                    "検索結果ごとのファイルURLは生成せず、"
                    "すべて同じトップURLを開きます。"
                )
        elif provider == "sharepoint":
            self.output(
                f"SharePoint上の基準フォルダURL: "
                f"{settings.get('source_web_root') or '未設定'}"
            )
        elif settings.get("source_home_url"):
            self.output(f"トップURL: {settings['source_home_url']}")
        elif settings.get("source_web_root"):
            self.output(f"基準URL: {settings['source_web_root']}")
        if settings.get("path_pattern"):
            self.output(f"パス正規表現: {settings['path_pattern']}")
            self.output(f"URLテンプレート: {settings.get('url_template')}")

    def _configure_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, payload, source = loaded
        current = self._flat_source_link(source)
        link = self._prompt_source_link(existing=current or None)
        if link is None:
            self._print_info("設定を保存せずに戻ります。")
            return
        display_name = str(link.pop("display_name", "") or "").strip()
        try:
            link = source_links.validate_source_link(link)
        except Exception as exc:
            self._print_error(
                f"Source Link設定が不正です: {type(exc).__name__}: {exc}"
            )
            self.output("設定は保存されていません。入力例を確認してください。")
            return
        source_payload = next(
            (
                value
                for value in self._inventory_sources(inventory)
                if value.get("source_id") == source_id
            ),
            {},
        )
        root_status = str(
            source_payload.get("observed_root_status") or "no_observed_root"
        )
        if (
            link.get("strategy") not in {"home-only", "svn-web-root"}
            and root_status != "ready"
        ):
            if link.get("provider") == "sharepoint":
                self._print_error("SharePointのファイルURLを生成できません。")
                self.output(
                    "SharePoint上の基準フォルダURL、または自動検出された"
                    "保存ルートを確認してください。"
                )
            else:
                self._print_error(
                    "ファイル単位のSource Linkには、検出された保存ルートが"
                    f"1つ必要です。現在の状態: {root_status}"
                )
            self.output(f"保存ルートの状態: {root_status}")
            self.output(
                "複数ルートの場合は、Providerごとに別のSource IDで"
                "文書を追加し直してください。\n設定は保存されていません。"
            )
            return
        _, observed = self._inventory_ids_paths(inventory)
        self._show_representative_preview(
            source_links,
            link,
            observed.get(source_id, []),
        )
        self.output("\n変更内容")
        if current:
            self._print_warning(
                "既存のSource Link設定を置き換えます。"
                "RAG文書や索引は変更されません。"
            )
            self.output("変更前")
            self._print_source_link_summary(source_id, current)
        self.output("変更後")
        self._print_source_link_summary(source_id, {**link, "display_name": display_name})
        if not self._confirm(
            f"DB「{db_name}」のSource「{source_id}」へ保存しますか？"
        ):
            self._print_info("Source Link設定は変更されていません。")
            return
        target = self._source_entry(payload, source_id, create=True)
        assert target is not None
        if display_name:
            target["display_name"] = display_name
        else:
            target.pop("display_name", None)
        target["source_type"] = str(link["provider"])
        target["link"] = {
            key: copy.deepcopy(link[key])
            for key in ("enabled", "strategy", "settings")
        }
        if self._save_sidecar(
            db_name,
            inventory,
            source_links,
            payload,
        ):
            self._print_success("Source Linkを保存しました。")

    def _toggle_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, payload, source = loaded
        if source is None or not isinstance(source.get("link"), dict):
            self._print_info("このSourceにはSource Linkが設定されていません。")
            return
        new_state = not bool(source["link"].get("enabled"))
        label = "有効化" if new_state else "無効化"
        if new_state:
            self.output("有効化すると、検索結果へURLを再び付与します。")
        else:
            self.output(
                "無効化すると、設定を残したまま検索結果へのURL付与を停止します。"
            )
        if not self._confirm(
            f"DB「{db_name}」のSource「{source_id}」を{label}しますか？"
        ):
            return
        source["link"]["enabled"] = new_state
        if self._save_sidecar(
            db_name,
            inventory,
            source_links,
            payload,
        ):
            self._print_success(f"Source Linkを{label}しました。")

    def _remove_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, payload, source = loaded
        if source is None or not isinstance(source.get("link"), dict):
            self._print_info("このSourceにはSource Linkが設定されていません。")
            return
        self._print_warning(
            "削除するとSource Link設定をsidecarから取り除きます。"
            "索引済み文書、Source、DBは削除されません。"
        )
        self.output("削除対象")
        self.output(f"  DB: {db_name}")
        self.output(f"  Source: {source_id}")
        self.output(
            f"  Provider: {self._provider_label(source.get('source_type'))}"
        )
        if not self._confirm(
            "このSource Link設定を削除しますか？"
        ):
            return
        source.pop("link", None)
        if not source.get("display_name") and not source.get("source_type"):
            payload["sources"] = [
                value
                for value in payload.get("sources") or []
                if value.get("source_id") != source_id
            ]
        if self._save_sidecar(
            db_name,
            inventory,
            source_links,
            payload,
        ):
            self._print_success("Source Link設定を削除しました。")

    def _preview_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, _payload, source = loaded
        if source is None or not isinstance(source.get("link"), dict):
            self._print_info("このSourceにはSource Linkが設定されていません。")
            return
        _, observed = self._inventory_ids_paths(inventory)
        preview = source_links.resolve_mapping_preview(
            source,
            observed.get(source_id, []),
        )
        _, observed = self._inventory_ids_paths(inventory)
        self._print_source_link_preview(
            source_links,
            self._flat_source_link(source),
            observed.get(source_id, []),
            preview=preview,
        )

    def _show_representative_preview(
        self,
        source_links: Any,
        source_link: dict[str, Any],
        paths: list[str],
    ) -> None:
        representative = list(paths[:5])
        self.output("\n生成URLの確認")
        if not representative:
            self._print_warning("確認に使える保存パスがありません。")
            return
        preview = source_links.resolve_mapping_preview(
            source_link,
            representative,
        )
        self._print_source_link_preview(
            source_links,
            source_link,
            representative,
            preview=preview,
        )

    def _print_source_link_preview(
        self,
        source_links: Any,
        source_link: dict[str, Any],
        paths: list[str],
        *,
        preview: list[dict[str, Any]],
    ) -> None:
        root_independent = (
            source_link.get("provider") == "svn"
            and source_link.get("strategy") == "svn-web-root"
        )
        if root_independent:
            roots: tuple[str, ...] = ()
        else:
            try:
                roots = tuple(source_links.observed_root_from_paths(paths))
            except Exception:
                roots = ()
        settings = source_link.get("settings") or {}
        for index, item in enumerate(preview, start=1):
            stored_path = str(item.get("path") or "")
            self.output(f"\n文書{index}")
            self.output(f"RAG保存パス: {stored_path or '不明'}")
            if root_independent:
                self.output(
                    "自動検出された保存ルート: "
                    "このリンク方式では使用しません"
                )
                self.output(
                    "Source相対パス: このリンク方式では使用しません"
                )
            elif len(roots) == 1:
                self.output(f"自動除去された保存ルート: {roots[0]}")
                try:
                    relative = source_links.source_relative_path(
                        stored_path,
                        roots[0],
                    )
                except Exception:
                    relative = ""
                self.output(f"Source相対パス: {relative or '生成不可'}")
            elif not roots:
                self.output("自動除去された保存ルート: 未検出")
            else:
                self.output(
                    "自動除去された保存ルート: "
                    + ", ".join(roots)
                )
            if settings.get("repository_path_prefix"):
                provider_label = self._provider_label(
                    str(source_link.get("provider") or "")
                )
                self.output(
                    f"{provider_label}上の追加パス: "
                    f"{settings['repository_path_prefix']}"
                )
            generated = (
                item.get("source_permalink")
                or item.get("source_url")
            )
            if generated:
                self.output(f"生成URL: {generated}")
                self._print_success("URLを生成できました。")
            else:
                self._print_error("URLを生成できませんでした。")
                self.output(f"理由: {item.get('status') or '不明'}")

    def _unmatched_source_settings(self, db_name: str) -> None:
        inventory = self._load_source_inventory(db_name)
        if inventory is None:
            return
        ids, _ = self._inventory_ids_paths(inventory)
        loaded = self._load_sidecar_payload(db_name)
        if loaded is None:
            return
        source_links, payload = loaded
        unmatched = [
            dict(value)
            for value in payload.get("sources") or []
            if isinstance(value, dict) and value.get("source_id") not in ids
        ]
        if not unmatched:
            self._print_info(
                "対応するSourceがないSource Link設定はありません。"
            )
            return
        self._print_warning(
            "以下は現在の索引済みSourceと一致しない設定です。"
            "検索には適用されません。"
        )
        for index, source in enumerate(unmatched, start=1):
            self.output(f"\n{index}. Source ID: {source.get('source_id')}")
            self.output(
                f"   Source種別: "
                f"{self._provider_label(source.get('source_type'))}"
            )
            link = source.get("link")
            self.output(
                "   Source Link: "
                + (
                    ("有効" if link.get("enabled") else "無効")
                    if isinstance(link, dict)
                    else "未設定"
                )
            )
        choice = self._ask("確認する番号を入力してください（0: 戻る）: ")
        if choice in (None, "0"):
            return
        try:
            selected = unmatched[int(choice) - 1]
        except (ValueError, IndexError):
            self._invalid_selection(f"1～{len(unmatched)}、または0")
            return
        self._print_source_link_summary(
            str(selected.get("source_id") or ""),
            selected,
        )
        if not self._confirm(
            f"DB「{db_name}」から、この未対応設定を削除しますか？"
        ):
            return
        payload["sources"] = [
            value
            for value in payload.get("sources") or []
            if value.get("source_id") != selected.get("source_id")
        ]
        # Saving after removing unmatched settings is validated against the
        # current read-only inventory.
        if self._save_sidecar(db_name, inventory, source_links, payload):
            self._print_success("未対応のSource Link設定を削除しました。")

    def _print_values(self, title: str, values: Iterable[Any]) -> None:
        self.output(f"\n{title}")
        printed = False
        for value in values:
            if isinstance(value, dict):
                text = value.get("path") or value.get("scan_root") or value
            else:
                text = value
            self.output(f"- {text}")
            printed = True
        if not printed:
            self.output("（記録なし）")

    @staticmethod
    def _compact_values(values: Iterable[Any]) -> str:
        output: list[str] = []
        for value in values:
            if isinstance(value, dict):
                text = value.get("prefix") or value.get("path") or str(value)
            else:
                text = str(value)
            if text:
                output.append(str(text))
        return ", ".join(output[:4]) if output else "なし"

    def _prompt_source_link(
        self,
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = dict(existing or {})
        current_settings = dict(current.get("settings") or {})
        display_name = self._prompt_preserving_value(
            "Source表示名",
            str(current.get("display_name") or ""),
            required=False,
            description=(
                "Manager上でSourceを識別しやすくする表示専用の名前です。"
                "Source IDは変更されません。"
            ),
            examples=("GitHubソースコード", "SharePoint設計資料"),
            empty_help="Source IDを表示",
        )
        if display_name is None:
            return None
        current_provider = str(current.get("provider") or "")
        current_category = (
            "git_repository"
            if current_provider in _GIT_PROVIDERS
            else current_provider
        )
        provider_categories = [
            "sharepoint",
            "git_repository",
            "redmine",
            "other",
            "svn",
        ]
        if existing is not None:
            provider_category = self._prompt_choice_preserving(
                "Provider",
                provider_categories,
                current_category,
            )
        else:
            provider_category = self._select_value(
                "Providerを選択",
                provider_categories,
            )
        if provider_category is None:
            return None
        if provider_category == "git_repository":
            if existing is not None and current_provider in _GIT_PROVIDERS:
                provider = self._prompt_choice_preserving(
                    "Gitホスティングサービス",
                    list(_GIT_PROVIDERS),
                    current_provider,
                )
            else:
                provider = self._select_value(
                    "Gitホスティングサービスを選択",
                    _GIT_PROVIDERS,
                )
            if provider is None:
                return None
        else:
            provider = provider_category
        if provider == "sharepoint":
            strategy = "append-relative-path"
            choices: list[str] = []
        elif provider in _GIT_PROVIDERS:
            strategy = _GIT_STRATEGIES[provider]
            choices = []
        elif provider == "svn":
            choices = ["svn-http", "svn-web-root"]
        else:
            choices = ["home-only", "append-relative-path", "regex-template"]
        if provider not in {"sharepoint", *_GIT_PROVIDERS}:
            if existing is not None and provider == current_provider:
                current_strategy = self._infer_source_link_strategy(
                    str(current.get("provider") or ""),
                    current_settings,
                )
                strategy = self._prompt_choice_preserving(
                    "リンク方式",
                    choices,
                    current_strategy,
                )
            else:
                strategy = self._select_value("リンク方式を選択", choices)
            if strategy is None:
                return None
        settings: dict[str, Any] = {}
        same_shape = (
            provider == current.get("provider")
            and strategy
            == self._infer_source_link_strategy(
                str(current.get("provider") or ""),
                current_settings,
            )
        )
        prior = current_settings if same_shape else {}
        if provider == "sharepoint":
            self.output(
                "\nSharePointではリンク方式を自動設定します。\n"
                "自動検出した保存ルートを1回だけ除去し、残った相対パスを"
                "次の基準フォルダURLへ追加します。Microsoft Graphは使用しません。"
            )
            root = self._prompt_preserving_value(
                "SharePoint上の基準フォルダURL",
                str(prior.get("source_web_root") or ""),
                required=True,
                description=(
                    "検索結果から個別ファイルを開くための基準URLです。\n"
                    "RAGが自動計算した相対パスを、このURLの末尾に追加します。"
                ),
                examples=(
                    "https://contoso.sharepoint.com/sites/manual/"
                    "Shared%20Documents",
                ),
            )
            if root is None:
                return None
            settings["source_web_root"] = root
        elif provider in _GIT_PROVIDERS:
            git_settings = self._prompt_git_repository_settings(
                provider,
                prior,
            )
            if git_settings is None:
                return None
            settings = git_settings
        elif provider == "svn":
            svn_settings = self._prompt_svn_settings(strategy, prior)
            if svn_settings is None:
                return None
            settings = svn_settings
        elif strategy == "home-only":
            value = self._prompt_preserving_value(
                "SourceトップURL",
                str(prior.get("source_home_url") or ""),
                required=True,
                description=(
                    "Source全体の入口としてManagerで確認するURLです。"
                    "ファイル単位のURLは生成しません。"
                ),
                examples=("https://example.com/project",),
            )
            if value is None:
                return None
            settings = {"source_home_url": value}
        elif strategy == "append-relative-path":
            value = self._prompt_preserving_value(
                "ファイルURLの基準URL",
                str(prior.get("source_web_root") or ""),
                required=True,
                description=(
                    "このURLの末尾へSource相対パスを追加します。"
                ),
                examples=("https://example.com/documents",),
            )
            if value is None:
                return None
            settings = {"source_web_root": value}
        else:
            self._print_warning(
                "正規表現テンプレートは上級者向けです。"
                "通常は「相対パスをURL末尾へ追加」を選んでください。"
            )
            self.output(
                "正規表現の (?P<name>...) で値を取り出し、"
                "URLテンプレートの {name} と対応させます。\n"
                "一致しないパスはURLなしで安全に表示されます。"
            )
            pattern = self._prompt_preserving_value(
                "Source相対パスの正規表現",
                str(prior.get("path_pattern") or ""),
                required=True,
                description=(
                    "Source相対パス全体に一致する、安全なnamed group付き"
                    "正規表現です。"
                ),
                examples=(
                    r"^issues/(?P<issue_id>[0-9]+)\.md$",
                    r"^wiki/(?P<page>.+)\.md$",
                ),
            )
            template = self._prompt_preserving_value(
                "URLテンプレート",
                str(prior.get("url_template") or ""),
                required=True,
                description=(
                    "正規表現のnamed groupと同じ名前を{name}で指定します。"
                ),
                examples=(
                    "https://redmine.example.com/issues/{issue_id}",
                    "https://redmine.example.com/projects/project/wiki/{page}",
                ),
            )
            if pattern is None or template is None:
                return None
            settings = {"path_pattern": pattern, "url_template": template}
        return {
            "display_name": display_name,
            "enabled": bool(current.get("enabled", True)),
            "provider": provider,
            "strategy": strategy,
            "settings": settings,
        }

    def _prompt_git_repository_settings(
        self,
        provider: str,
        prior: dict[str, Any],
    ) -> dict[str, Any] | None:
        provider_label = _PROVIDER_JA.get(provider, provider)
        if provider == "github":
            repository_examples = (
                "https://github.com/owner/repository",
                "https://git.example.com/owner/repository",
            )
            repository_help = (
                "GitHubまたはGitHub EnterpriseのリポジトリトップURLです。"
                "/blob/や/tree/以下のURLは入力しません。"
            )
            ref_help = (
                "GitHub上で通常表示するブランチ、タグ、またはコミットです。"
            )
            ref_examples = ("main", "develop", "release/v2", "v1.2.3")
        elif provider == "gitlab":
            repository_examples = (
                "https://gitlab.com/group/subgroup/repository",
                "https://gitlab.example.com/group/repository",
            )
            repository_help = (
                "GitLab.comまたはセルフホストGitLabのプロジェクトトップURLです。"
                "/-/blob/や/-/tree/以下のURLは入力しません。"
            )
            ref_help = (
                "GitLab上で通常表示するブランチ、タグ、またはコミットです。"
            )
            ref_examples = ("main", "develop", "release/v2", "v1.2.3")
        else:
            repository_examples = (
                "https://dev.azure.com/organization/project/_git/repository",
                "https://organization.visualstudio.com/project/_git/repository",
            )
            repository_help = (
                "Azure DevOps ReposのリポジトリルートURLです。"
                "ファイル表示用のqueryやfragmentは入力しません。"
            )
            ref_help = (
                "通常リンクで開くブランチ名です。Azure DevOpsでは今回、"
                "通常refをブランチとして扱います。"
            )
            ref_examples = ("main", "develop", "release/v2")

        repository = self._prompt_preserving_value(
            f"{provider_label}リポジトリURL",
            str(prior.get("repository_url") or ""),
            required=True,
            description=repository_help,
            examples=repository_examples,
        )
        ref = self._prompt_preserving_value(
            "ブランチ・タグ・コミット（ref）"
            if provider != "azure_devops"
            else "ブランチ（ref）",
            str(prior.get("ref") or ""),
            required=True,
            description=ref_help,
            examples=ref_examples,
        )
        if repository is None or ref is None:
            return None
        settings: dict[str, Any] = {
            "repository_url": repository,
            "ref": ref,
            "permalink_enabled": False,
        }
        repository_prefix = self._prompt_preserving_value(
            f"{provider_label}リポジトリ内の追加パス",
            str(prior.get("repository_path_prefix") or ""),
            required=False,
            description=(
                "RAGのSource相対パスより、リポジトリ上の実ファイルが"
                "さらに深い場所にある場合だけ指定します。通常は空欄です。\n"
                "これはRAG保存パスから取り除くprefixではありません。"
                "保存ルートの除去はManagerが自動で行います。"
            ),
            examples=(
                "RAG: docs/manual.md / リポジトリ: "
                "product-a/docs/manual.md → product-a",
            ),
            empty_help=f"{provider_label}リポジトリ直下として扱う",
        )
        commit = self._prompt_preserving_value(
            "固定リンク用コミット",
            str(prior.get("commit") or ""),
            required=False,
            description=(
                "将来内容が変わらない固定URLを付ける場合に、"
                "完全なコミットSHAを指定します。回答では固定リンクが優先されます。"
            ),
            examples=("0123456789abcdef0123456789abcdef01234567",),
            empty_help="通常のref URLだけを生成",
        )
        if repository_prefix is None or commit is None:
            return None
        if repository_prefix:
            settings["repository_path_prefix"] = repository_prefix
        if commit:
            settings["commit"] = commit
            settings["permalink_enabled"] = True
        return settings

    def _prompt_svn_settings(
        self,
        strategy: str,
        prior: dict[str, Any],
    ) -> dict[str, Any] | None:
        if strategy == "svn-web-root":
            self.output(
                "\n検索結果ごとのファイルURLは生成されません。\n"
                "どの検索結果から開いても、設定したSVN Web画面の"
                "トップページへ移動します。製品固有URLの推測は行いません。"
            )
            repository = self._prompt_preserving_value(
                "SVN Web画面のトップURL",
                str(prior.get("repository_url") or ""),
                required=True,
                description=(
                    "VisualSVN、ViewVC、WebSVN、Trac等のトップURLです。"
                    "query、fragment、末尾の/は入力どおり保持します。"
                ),
                examples=(
                    "https://svn-web.example.com/project?view=summary#files",
                ),
            )
            return (
                {"repository_url": repository}
                if repository is not None
                else None
            )

        self.output(
            "\nApache HTTP(S)＋mod_dav_svn互換URLとして、"
            "各ファイルを直接開くリンクを生成します。\n"
            "checkout、認証、リビジョン自動取得は行いません。"
        )
        repository = self._prompt_preserving_value(
            "SVNリポジトリURL",
            str(prior.get("repository_url") or ""),
            required=True,
            description=(
                "取り込んだローカルルートに対応するHTTP(S) URLです。"
                "trunk、branches、tagsを含むURLもそのまま使用できます。"
            ),
            examples=(
                "https://svn.example.com/repos/project/trunk",
                "https://svn.example.com/repos/project/branches/release-2",
            ),
        )
        repository_prefix = self._prompt_preserving_value(
            "SVNリポジトリ内の追加パス",
            str(prior.get("repository_path_prefix") or ""),
            required=False,
            description=(
                "SVN URLとSource相対パスの間へ追加するディレクトリです。"
                "通常は空欄です。"
            ),
            examples=("docs", "manuals/ja"),
            empty_help="SVNリポジトリURL直下として扱う",
        )
        current_choice = (
            "enabled"
            if prior.get("permalink_enabled") is True
            else "disabled"
        )
        permalink_choice = self._prompt_choice_preserving(
            "固定リビジョンリンク",
            ["disabled", "enabled"],
            current_choice,
        )
        if (
            repository is None
            or repository_prefix is None
            or permalink_choice is None
        ):
            return None
        settings: dict[str, Any] = {
            "repository_url": repository,
            "permalink_enabled": permalink_choice == "enabled",
        }
        if repository_prefix:
            settings["repository_path_prefix"] = repository_prefix
        if permalink_choice == "enabled":
            revision = self._prompt_preserving_value(
                "SVNリビジョン番号",
                str(prior.get("revision") or ""),
                required=True,
                description=(
                    "固定リンクのpとrへ使用する1以上の整数です。"
                    "HEADやBASEは使用できません。\n"
                    "混在リビジョンの作業コピーでは、単一revisionの固定リンクが"
                    "各ファイルの実際の版と一致しない場合があります。"
                ),
                examples=("1234",),
            )
            if revision is None:
                return None
            settings["revision"] = revision
        return settings

    @staticmethod
    def _infer_source_link_strategy(
        provider: str,
        settings: dict[str, Any],
    ) -> str:
        if provider in _GIT_PROVIDERS:
            return _GIT_STRATEGIES[provider]
        if provider == "svn":
            return (
                "svn-http"
                if (
                    "permalink_enabled" in settings
                    or "repository_path_prefix" in settings
                    or "revision" in settings
                )
                else "svn-web-root"
            )
        if settings.get("path_pattern") or settings.get("url_template"):
            return "regex-template"
        if settings.get("source_web_root"):
            return "append-relative-path"
        return "home-only"

    def _prompt_preserving_value(
        self,
        label: str,
        current: str,
        *,
        required: bool,
        description: str = "",
        examples: Iterable[str] = (),
        empty_help: str = "",
    ) -> str | None:
        while True:
            self.output(
                f"\n{label}{'【必須】' if required else '【任意】'}"
            )
            if description:
                self.output(description)
            example_values = [str(value) for value in examples if str(value)]
            if example_values:
                self.output("入力例:")
                for example in example_values:
                    self.output(f"  {example}")
            self.output(f"現在値: {current or '未設定'}")
            if current:
                self.output("空欄: 現在値を維持")
            elif empty_help:
                self.output(f"空欄: {empty_help}")
            elif not required:
                self.output("空欄: 未設定")
            self.output(
                "操作: :q で保存せず戻る"
                + ("、- で現在の任意値を削除" if not required else "")
            )
            value = self._ask("> ")
            if value is None or value.strip() == ":q":
                return None
            if not value:
                if current:
                    return current
                if required:
                    self._print_error(f"{label}は必須です。")
                    if example_values:
                        self.output(f"入力例: {example_values[0]}")
                    self.output("設定は保存されていません。")
                    continue
                return ""
            if value == "-":
                if required:
                    self._print_error(f"{label}は削除できない必須項目です。")
                    self.output("設定は保存されていません。")
                    continue
                return ""
            return value.strip()

    def _prompt_choice_preserving(
        self,
        label: str,
        choices: list[str],
        current: str,
        *,
        required: bool = True,
    ) -> str | None:
        while True:
            self.output(
                f"\n{label}{'【必須】' if required else '【任意】'}"
            )
            for index, choice in enumerate(choices, start=1):
                self.output(
                    f"{index}. {self._choice_label(choice)}"
                )
            self.output(f"現在値: {self._choice_label(current)}")
            self.output("空欄: 現在値を維持 / :q: 保存せず戻る")
            value = self._ask("> ")
            if value is None or value.strip() == ":q":
                return None
            selected = value.strip() or current
            if selected.isdigit():
                index = int(selected) - 1
                selected = (
                    choices[index]
                    if 0 <= index < len(choices)
                    else ""
                )
            if selected not in choices:
                self._print_error(f"{label}の選択値が不正です。")
                self.output(
                    "許容値: "
                    + ", ".join(choices)
                    + "。設定は保存されていません。"
                )
                continue
            return selected

    def _delete_database_interactive(self, db_name: str) -> bool:
        if not self._guard_valid_database_target(db_name):
            return False
        try:
            root = self._validated_database_root(db_name)
        except Exception as exc:
            self._print_error(
                f"DBを削除できません: {type(exc).__name__}: {exc}"
            )
            return False
        documents = 0
        inventory = self._load_source_inventory(db_name)
        if inventory is not None:
            inventory_payload = inventory.to_dict()
            total_documents = inventory_payload.get("document_count")
            if (
                isinstance(total_documents, int)
                and not isinstance(total_documents, bool)
                and total_documents >= 0
            ):
                documents = total_documents
            else:
                for source in self._inventory_sources(inventory):
                    documents += int(source.get("document_count") or 0)
                documents += int(
                    inventory_payload.get("documents_without_source_id")
                    or inventory_payload.get("missing_source_document_count")
                    or 0
                )
        size = self._directory_size_without_following_links(root)
        metadata = self._read_database_metadata(db_name)
        self._print_screen_header("DB削除【危険】", db_name=db_name)
        self._print_error(
            "この操作は選択したDBディレクトリを完全に削除します。"
        )
        self.output(
            f"DB名: {db_name}\n"
            f"表示名: {metadata['title']}\n"
            f"文書数: {documents:,}\n"
            f"サイズ: {size:,} bytes"
        )
        try:
            active_states = self._in_progress_source_states(root)
        except ManagerError as exc:
            self._print_error(
                "Sourceの処理状態を安全に確認できないため、"
                "DBは削除されませんでした。"
            )
            self.output(str(exc))
            return False
        if active_states:
            self._print_warning(
                f"更新途中のSourceが{len(active_states):,}件あります。"
            )
            self.output(
                "削除前に、安全な再開位置を残した中断状態として記録します。"
            )
            if not self._confirm(
                "更新途中のSourceを中断状態として記録しますか？"
            ):
                self._print_info(
                    "中断状態へ変更せず、DB削除をキャンセルしました。"
                )
                return False
            interrupted = 0
            try:
                for state_path, state_payload in active_states:
                    self._write_interrupted_source_state(
                        root,
                        state_path,
                        state_payload,
                    )
                    interrupted += 1
            except (ManagerError, OSError) as exc:
                self._print_error(
                    "Sourceの中断記録を完了できないため、"
                    "DBは削除されませんでした。"
                )
                self.output(
                    f"中断状態を保存済み: {interrupted:,}件 / "
                    f"未保存: {len(active_states) - interrupted:,}件"
                )
                self.output(f"理由: {type(exc).__name__}: {exc}")
                return False
            self._print_success(
                f"{interrupted:,}件のSourceを中断状態として記録しました。"
            )
            self.output("続けてDB削除をもう一度確認します。")
        confirmation = self._ask(
            f"続行するにはDB名「{db_name}」を正確に入力してください: "
        )
        if confirmation is None:
            return False
        try:
            self._delete_database(db_name, confirmation)
        except Exception as exc:
            if root.exists():
                self._print_error(
                    "DB削除は完了していません。"
                    "DBフォルダの一部が残っている可能性があります。"
                )
            else:
                self._print_error(
                    "DB削除中にエラーが発生しました。"
                    "削除結果を正常完了として確認できません。"
                )
            self.output(f"理由: {type(exc).__name__}: {exc}")
            return False
        self._print_success(f"DB「{db_name}」を削除しました。")
        return True

    def _in_progress_source_states(
        self,
        db_root: Path,
    ) -> list[tuple[Path, dict[str, Any]]]:
        root = Path(db_root)
        sources_root = root / "sources"
        if not sources_root.exists():
            return []
        if sources_root.is_symlink() or not sources_root.is_dir():
            raise ManagerError("Source保存先が通常のディレクトリではありません。")
        try:
            resolved_sources = sources_root.resolve(strict=True)
            resolved_sources.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ManagerError(
                "Source保存先がDBフォルダの外を参照しています。"
            ) from exc
        active: list[tuple[Path, dict[str, Any]]] = []
        for source_dir in sorted(
            sources_root.iterdir(),
            key=lambda value: value.name,
        ):
            if source_dir.is_symlink() or not source_dir.is_dir():
                continue
            state_path = source_dir / "state.json"
            if not state_path.exists():
                continue
            if state_path.is_symlink() or not state_path.is_file():
                raise ManagerError(
                    "Sourceの処理状態が通常のファイルではありません。"
                )
            try:
                state_path.resolve(strict=True).relative_to(resolved_sources)
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise ManagerError(
                    "Sourceの処理状態を読み取れません。"
                ) from exc
            if not isinstance(payload, dict):
                raise ManagerError("Sourceの処理状態が不正です。")
            if self._source_state_is_in_progress(payload):
                active.append((state_path, payload))
        return active

    @staticmethod
    def _source_state_is_in_progress(state: dict[str, Any]) -> bool:
        phase = str(
            state.get("phase") or state.get("status") or ""
        ).strip().casefold()
        phase = phase.replace("-", "_").replace(" ", "_")
        if not phase:
            return False
        terminal = {
            "idle",
            "ready",
            "completed",
            "complete",
            "succeeded",
            "success",
            "interrupted",
            "failed",
            "error",
            "not_started",
            "configured",
            "metadata_sync_pending",
        }
        if phase in terminal:
            return False
        active = {
            "active",
            "running",
            "starting",
            "resuming",
            "fetch",
            "fetching",
            "download",
            "downloading",
            "materialize",
            "materializing",
            "ingest",
            "ingesting",
            "index",
            "indexing",
            "reflect",
            "reflecting",
            "updating",
            "metadata_sync",
            "metadata_syncing",
        }
        return (
            phase in active
            or phase.endswith("_in_progress")
            or phase.startswith("running_")
        )

    @staticmethod
    def _write_interrupted_source_state(
        db_root: Path,
        state_path: Path,
        state_payload: dict[str, Any],
    ) -> None:
        root = Path(db_root).resolve(strict=True)
        path = Path(state_path)
        try:
            path.resolve(strict=True).relative_to(root)
            parent = path.parent.resolve(strict=True)
            parent.relative_to(root)
            metadata = os.lstat(path)
        except (OSError, ValueError) as exc:
            raise ManagerError(
                "Sourceの処理状態がDBフォルダ内にありません。"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ManagerError(
                "Sourceの処理状態が通常のファイルではありません。"
            )
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise ManagerError(
                "Sourceの処理状態を再確認できません。"
            ) from exc
        try:
            current = json.loads(original.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ManagerError("Sourceの処理状態が不正です。") from exc
        if current != state_payload:
            raise ManagerError(
                "Sourceの処理状態が確認中に変更されました。"
            )
        value = dict(current)
        if "phase" in value or "status" not in value:
            value["phase"] = "interrupted"
        if "status" in value:
            value["status"] = "interrupted"
        value["can_resume"] = True
        value["updated_at"] = datetime.now(timezone.utc).isoformat()
        revision = value.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            value["revision"] = revision + 1
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        temporary = parent / (
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != original:
                raise ManagerError(
                    "Sourceの処理状態が保存直前に変更されました。"
                )
            if (
                path.is_symlink()
                or path.parent.resolve(strict=True) != parent
                or path.resolve(strict=True).parent != parent
            ):
                raise ManagerError(
                    "Sourceの処理状態の保存先が変更されました。"
                )
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass

    @staticmethod
    def _directory_size_without_following_links(root: Path) -> int:
        total = 0
        for current, directory_names, file_names in os.walk(
            root, followlinks=False
        ):
            current_path = Path(current)
            directory_names[:] = [
                name
                for name in directory_names
                if not (current_path / name).is_symlink()
            ]
            for name in file_names:
                path = current_path / name
                if path.is_symlink():
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _delete_database(
        self,
        db_name: str,
        typed_name: str,
    ) -> None:
        if typed_name != db_name:
            raise ManagerError("typed confirmation did not match")
        root = self._validated_database_root(db_name)
        try:
            shutil.rmtree(root)
        except OSError as exc:
            raise ManagerError(str(exc)) from exc

    def _validated_database_root(self, db_name: str) -> Path:
        if not self._valid_database_name(db_name):
            raise ManagerError("invalid database name")
        dbs_root = self.dbs_root.resolve(strict=True)
        candidate = self.dbs_root / db_name
        if candidate.parent != self.dbs_root:
            raise ManagerError("database is not a direct child")
        if candidate.is_symlink():
            raise ManagerError("database root cannot be a symlink")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != dbs_root or resolved.name != db_name:
            raise ManagerError("database target is outside the DB root")
        if not resolved.is_dir():
            raise ManagerError("database target is not a directory")
        return resolved

    @staticmethod
    def _valid_database_name(db_name: str) -> bool:
        return bool(DATABASE_NAME_PATTERN.fullmatch(str(db_name)))

    def _status_json(self, db_name: str) -> dict[str, Any] | None:
        result = self._invoke(
            "gen_db/status.py",
            ["--db", db_name, "--json"],
            capture_output=True,
        )
        if result is None or int(result.returncode) != 0:
            return None
        try:
            payload = json.loads(str(result.stdout or ""))
        except json.JSONDecodeError:
            self._print_error("DB状態を読み取れませんでした。")
            return None
        return payload if isinstance(payload, dict) else None

    def _guard_valid_database_target(self, db_name: str) -> bool:
        try:
            self._validated_database_root(db_name)
        except Exception as exc:
            self._print_error(
                f"DB「{db_name}」を安全な操作対象として確認できません: "
                f"{type(exc).__name__}: {exc}"
            )
            return False
        return True

    def _show_status(self, db_name: str) -> None:
        self._print_screen_header("詳細状態", db_name=db_name)
        status = self._status_json(db_name)
        if status is None:
            return
        catalog = (
            status.get("catalog")
            if isinstance(status.get("catalog"), dict)
            else {}
        )
        documents = status.get("document_count")
        chunks = status.get("chunk_count")
        if documents is None:
            documents = catalog.get("documents") or 0
        if chunks is None:
            chunks = catalog.get("chunks") or 0
        inventory = self._load_source_inventory(db_name)
        sources = (
            self._inventory_sources(inventory)
            if inventory is not None
            else []
        )
        self.output("DB状態")
        self.output(f"DB名: {db_name}")
        self.output(f"状態: {self._status_label(status.get('status'))}")
        self.output(f"処理段階: {status.get('phase') or 'なし'}")
        self.output(f"操作: {status.get('operation') or 'なし'}")
        self.output(f"論理ルート: {status.get('root') or '未設定'}")
        self.output(
            f"ルート表示名: {status.get('root_display_name') or '未設定'}"
        )
        self.output(
            "読込サブディレクトリ: "
            f"{status.get('scan_subdir') or '論理ルート全体'}"
        )
        self.output(f"読込ルート: {status.get('scan_root') or '未設定'}")
        self.output(
            f"保存パス接頭辞: "
            f"{status.get('stored_path_prefix') or '未設定'}"
        )
        self.output(f"Source ID: {status.get('source_id') or '未設定'}")
        self.output(
            f"確定単位: {int(status.get('batch_size_files') or 5):,}文書"
        )
        self.output(f"文書数: {int(documents or 0):,}")
        self.output(f"チャンク数: {int(chunks or 0):,}")
        self.output(f"Source数: {len(sources):,}")
        self.output(
            f"ファイル進捗: {int(status.get('files_done') or 0):,}"
            f" / {int(status.get('files_total') or 0):,}"
        )
        self.output(
            f"索引登録: {int(status.get('indexed_files') or 0):,} / "
            f"スキップ: {int(status.get('skipped_files') or 0):,} / "
            f"失敗: {int(status.get('error_files') or 0):,}"
        )
        self.output(
            f"更新レコード: {int(status.get('upserted_records') or 0):,} / "
            f"削除レコード: {int(status.get('deleted_records') or 0):,}"
        )
        self.output(
            f"ベクトル索引レコード: "
            f"{int(status.get('collection_count') or 0):,}"
        )
        self.output(f"現在のファイル: {status.get('current_file') or 'なし'}")
        current_batch = status.get("current_batch_files") or []
        self.output(
            f"現在のバッチ: "
            f"{', '.join(str(v) for v in current_batch) if current_batch else 'なし'}"
        )
        self.output(f"最終更新: {status.get('updated_at') or '不明'}")
        self.output(
            f"中断処理: "
            f"{'再開可能' if status.get('can_resume') else 'なし'}"
        )
        self.output(
            f"抽出エラー: {int(status.get('error_count_total') or 0):,}"
        )
        self.output(f"最後のエラー: {status.get('last_error') or 'なし'}")
        events = status.get("events") or []
        if events:
            self.output("最近のイベント:")
            for event in events[-5:]:
                if isinstance(event, dict):
                    self.output(
                        "  - "
                        + str(
                            event.get("message")
                            or event.get("event")
                            or event.get("phase")
                            or event
                        )
                    )
                else:
                    self.output(f"  - {event}")
        if status.get("resume_command"):
            self.output(f"安全な再開コマンド: {status['resume_command']}")
        if status.get("force_rebuild_command"):
            self.output(
                f"強制再構築コマンド: {status['force_rebuild_command']}"
            )
        if self._confirm("診断用の詳細JSONを表示しますか？"):
            self.output(
                json.dumps(
                    status,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )

    def _database_summaries(self) -> list[dict[str, Any]]:
        result = self._invoke(
            "list_dbs.py",
            ["--format", "json"],
            capture_output=True,
        )
        if result is None or int(result.returncode) != 0:
            return []
        try:
            payload = json.loads(str(result.stdout or ""))
        except json.JSONDecodeError:
            self._print_error("DB一覧を読み取れませんでした。")
            return []
        databases = payload.get("databases") if isinstance(payload, dict) else []
        if not isinstance(databases, list):
            return []
        return [
            dict(item)
            for item in databases
            if isinstance(item, dict) and item.get("name")
        ]

    def _invoke(
        self,
        relative_script: str,
        arguments: Iterable[str],
        *,
        capture_output: bool = False,
        python: Path | None = None,
        report_nonzero: bool = True,
    ) -> Any | None:
        normalized = Path(relative_script).as_posix()
        if normalized not in ALLOWED_SCRIPTS:
            raise ManagerError("script is not allowlisted")
        script = (self.rag_root / normalized).resolve(strict=False)
        try:
            script.relative_to(self.rag_root)
        except ValueError as exc:
            raise ManagerError("script is outside the RAG root") from exc
        runtime = Path(python or self._runtime_python())
        if not runtime.is_file():
            self._print_error(
                "初期設定が必要です。Local RAGの仮想環境がありません。"
            )
            return None
        argv = [str(runtime), str(script), *[str(value) for value in arguments]]
        kwargs: dict[str, Any] = {
            "shell": False,
            "check": False,
            "cwd": str(self.rag_root),
            "env": {
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "RAG_DBS_ROOT": str(self.dbs_root),
            },
        }
        if capture_output:
            kwargs.update(
                {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                }
            )
        try:
            completed = self.runner(argv, **kwargs)
        except OSError as exc:
            self._print_error(
                f"処理を開始できませんでした: {type(exc).__name__}: {exc}"
            )
            return None
        if int(completed.returncode) != 0 and report_nonzero:
            self._print_error(
                f"処理が終了コード{int(completed.returncode)}で失敗しました。"
            )
            if capture_output and completed.stderr:
                self.output(str(completed.stderr).strip())
        return completed

    def _runtime_python(self) -> Path:
        if self._runtime_override is not None:
            return self._runtime_override
        relative = (
            Path("query/.venv/Scripts/python.exe")
            if sys.platform.startswith("win")
            else Path("query/.venv/bin/python")
        )
        return self.rag_root / relative

    def _database_root(self, db_name: str) -> Path:
        return self.dbs_root / db_name

    def _read_database_metadata(self, db_name: str) -> dict[str, str]:
        root = self._database_root(db_name)
        try:
            dbs = self._import_dbs()
            config = dbs.read_db_config(root)
            title = str(config.get("title") or db_name)
            query_hint = str(dbs.read_profile_hint(root, max_chars=2_000))
        except (OSError, ValueError, json.JSONDecodeError):
            title = db_name
            query_hint = ""
        return {
            "title": title,
            "query_hint": query_hint,
        }

    def _import_dbs(self) -> Any:
        candidates = (
            self.rag_root / "gen_db" / "software_rag_tool",
            TOOL_ROOT,
        )
        for tool_root in candidates:
            if tool_root.is_dir() and str(tool_root) not in sys.path:
                sys.path.insert(0, str(tool_root))
        from software_rag_tool import dbs

        return dbs

    def _import_source_inventory(self) -> Any:
        tool_root = self.rag_root / "gen_db" / "software_rag_tool"
        if str(tool_root) not in sys.path:
            sys.path.insert(0, str(tool_root))
        from software_rag_tool import source_inventory

        return source_inventory

    def _import_source_links(self) -> Any:
        tool_root = self.rag_root / "gen_db" / "software_rag_tool"
        if str(tool_root) not in sys.path:
            sys.path.insert(0, str(tool_root))
        from software_rag_tool import source_links

        return source_links

    def _select_value(
        self,
        title: str,
        values: Iterable[str | tuple[str, str]],
        *,
        default: str | None = None,
    ) -> str | None:
        choices: list[tuple[str, str]] = []
        for value in values:
            if isinstance(value, tuple):
                internal, label = value
                choices.append((str(internal), str(label)))
            else:
                internal = str(value)
                choices.append((internal, self._choice_label(internal)))
        self.output(f"\n{title}")
        for index, (value, label) in enumerate(choices, start=1):
            self.output(f"{index}. {label}")
            description = {
                "git_repository": (
                    "GitHub、GitLab、Azure DevOps Reposから"
                    "ホスティングサービスを選びます。"
                ),
                "github": "GitHubリポジトリ内のファイルへリンクします。",
                "gitlab": "GitLabリポジトリ内のファイルへリンクします。",
                "azure_devops": (
                    "Azure DevOps Repos内のファイルへリンクします。"
                ),
                "svn": (
                    "SubversionのApache HTTP(S)ファイルリンク、または"
                    "製品固有Web画面のトップリンクを設定します。"
                ),
                "sharepoint": (
                    "SharePointのサイト、文書ライブラリ、"
                    "フォルダ内のファイルへリンクします。"
                ),
                "redmine": "RedmineのIssue、Wiki、文書等へリンクします。",
                "other": (
                    "URL末尾への相対パス追加、または"
                    "正規表現テンプレートを使います。"
                ),
                "home-only": "Source全体のトップページだけを設定します。",
                "append-relative-path": (
                    "基準URLの末尾へSource相対パスを追加します。"
                ),
                "regex-template": (
                    "named group付き正規表現からURLを作る上級者向け方式です。"
                ),
                "github-blob": (
                    "リポジトリURL、ref、Source相対パスからGitHub URLを作ります。"
                ),
                "gitlab-blob": (
                    "リポジトリURL、ref、Source相対パスからGitLab URLを作ります。"
                ),
                "azure-devops-item": (
                    "リポジトリURL、ブランチ、Source相対パスから"
                    "Azure DevOps URLを作ります。"
                ),
                "svn-http": (
                    "mod_dav_svn互換URLで各ファイルを直接開きます。"
                ),
                "svn-web-root": (
                    "製品固有のファイルURLを推測せず、"
                    "設定したトップページを開きます。"
                ),
            }.get(value)
            if description:
                self.output(f"   {description}")
        suffix = (
            f"（Enter: {default}、0: キャンセル）"
            if default is not None
            else "（0: キャンセル）"
        )
        choice = self._ask(f"番号を入力してください{suffix}: ")
        if choice == "" and default is not None:
            choice = default
        if choice in (None, "0"):
            return None
        try:
            index = int(choice) - 1
        except ValueError:
            self._invalid_selection(f"1～{len(choices)}、または0")
            return None
        if index < 0 or index >= len(choices):
            self._invalid_selection(f"1～{len(choices)}、または0")
            return None
        return choices[index][0]

    @staticmethod
    def _choice_label(value: str) -> str:
        if value in _PROVIDER_JA:
            return f"{_PROVIDER_JA[value]}（{value}）"
        if value in _STRATEGY_JA:
            return f"{_STRATEGY_JA[value]}（{value}）"
        if value in _BOOLEAN_CHOICE_JA:
            return _BOOLEAN_CHOICE_JA[value]
        return value or "未設定"

    def _confirm(self, question: str) -> bool:
        answer = self._ask(f"{question} [y/N]: ")
        return bool(answer and answer.casefold() in {"y", "yes"})

    def _ask(self, prompt: str) -> str | None:
        try:
            return self.input(prompt)
        except KeyboardInterrupt:
            self.output("")
            self._print_info("操作をキャンセルしました。変更は保存されていません。")
            return None
        except EOFError:
            self.output("")
            self._print_info("入力が終了したため、前の画面へ戻ります。")
            return None

    def _print_menu(
        self,
        title: str,
        entries: Iterable[tuple[str, str]],
    ) -> None:
        self.output(f"\n{title}")
        for key, label in entries:
            self.output(f"{key}. {label}")


def main() -> int:
    _configure_standard_streams()
    parser = argparse.ArgumentParser(
        add_help=False,
        description=(
            "Local RAG Manager\n"
            "DB作成、Sourceの追加・更新・再開、検索結果リンク、"
            "配布、管理PCの引っ越し、動作確認を対話形式で行います。"
        ),
        epilog=MANAGER_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="このヘルプを表示して終了します",
    )
    parser._optionals.title = "オプション"
    parser.parse_args()
    manager = LocalRagManager()
    return manager.run()


def _configure_standard_streams() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
