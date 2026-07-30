from __future__ import annotations

import functools
import getpass
import inspect
import os
import sys
from typing import Any

from .machine_connections import (
    SHAREPOINT_ROOT_ENV,
    check_gitlab_project,
    clear_sharepoint_root,
    gitlab_project_location,
    gitlab_token_env,
    has_stored_gitlab_token,
    has_stored_redmine_api_key,
    list_gitlab_registrations,
    list_redmine_registrations,
    redmine_api_key_env,
    register_gitlab_token,
    register_redmine_api_key,
    set_sharepoint_root,
    sharepoint_root_status,
)
from .redmine import generated_redmine_link, parse_redmine_project_url


_HOOK_MARKER = "_local_rag_connection_ui_hook_installed"
_CLASS_MARKER = "_local_rag_connection_ui_installed"


def install_manage_custom_hook() -> None:
    """Install the Manager extension before manage.py imports load_manage_custom.

    manage.py intentionally remains the stable public entry point. Its constructor
    already calls load_manage_custom, so this compatibility hook installs the
    connection UI on the concrete Manager class at that point without changing
    database or Source contracts.
    """

    from . import manage_custom

    if bool(getattr(manage_custom, _HOOK_MARKER, False)):
        return
    original = manage_custom.load_manage_custom

    @functools.wraps(original)
    def load_manage_custom(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        manager = caller.f_locals.get("self") if caller is not None else None
        if manager is not None and manager.__class__.__name__ == "LocalRagManager":
            install_manager_connection_ui(manager.__class__)
        return value

    manage_custom.load_manage_custom = load_manage_custom
    setattr(manage_custom, _HOOK_MARKER, True)


def install_manager_connection_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _CLASS_MARKER, False)):
        return

    def machine_setup_screen(self: Any) -> None:
        while True:
            self._print_screen_header("この端末の設定・動作確認")
            self._print_menu(
                "操作",
                (
                    ("1", "Local RAGを利用できるか確認する"),
                    ("2", "検索を試す"),
                    (
                        "3",
                        "Source接続設定"
                        "（Redmine API・GitLab API・SharePoint）",
                    ),
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
                self._source_connection_settings_screen()
            elif choice == "4":
                self._show_machine_technical_info()
            else:
                self._invalid_selection("0～4")

    def source_connection_settings_screen(
        self: Any,
        *,
        required: str | None = None,
        redmine_project_url: str | None = None,
        gitlab_url: str | None = None,
    ) -> bool:
        if required == "sharepoint":
            self._print_screen_header("Source接続設定")
            self._show_source_connection_summary()
            self._print_info(
                "SharePoint Sourceを登録するには同期ルートの端末設定が必要です。"
            )
            return bool(self._register_sharepoint_root_setting())
        if required == "redmine":
            self._print_screen_header("Source接続設定")
            self._show_source_connection_summary()
            self._print_info(
                "このRedmineを取得するAPIキーを、この端末へ登録します。"
            )
            return bool(
                self._register_redmine_api_key_setting(
                    project_url=redmine_project_url,
                )
            )
        if required == "gitlab":
            self._print_screen_header("Source接続設定")
            self._show_source_connection_summary()
            self._print_info(
                "このGitLabを取得するaccess tokenを、"
                "この端末へ登録します。"
            )
            return bool(
                self._register_gitlab_token_setting(
                    gitlab_url=gitlab_url,
                )
            )

        while True:
            self._print_screen_header("Source接続設定")
            self._show_source_connection_summary()
            self._print_menu(
                "操作",
                (
                    ("1", "SharePoint同期ルートを登録・変更する"),
                    ("2", "SharePoint同期ルートの端末設定を削除する"),
                    ("3", "Redmine APIキーを登録・更新する"),
                    ("4", "Redmine APIキーの登録状況を見る"),
                    ("5", "GitLab access tokenを登録・更新する"),
                    ("6", "GitLab access tokenの登録状況を見る"),
                    ("7", "GitLabプロジェクトへの接続を確認する"),
                    ("0", "戻る"),
                ),
            )
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return True
            if choice == "1":
                self._register_sharepoint_root_setting()
            elif choice == "2":
                self._clear_sharepoint_root_setting()
            elif choice == "3":
                self._register_redmine_api_key_setting()
            elif choice == "4":
                self._show_redmine_api_key_registrations()
            elif choice == "5":
                self._register_gitlab_token_setting()
            elif choice == "6":
                self._show_gitlab_token_registrations()
            elif choice == "7":
                self._check_gitlab_project_setting()
            else:
                self._invalid_selection("0～7")

    def show_source_connection_summary(self: Any) -> None:
        status = sharepoint_root_status(self.rag_root)
        if status.configured:
            source_label = (
                "Manager設定" if status.source == "manager" else "環境変数"
            )
            self.output(f"SharePoint同期ルート: 登録済み（{source_label}）")
            self.output(f"  {status.root}")
        else:
            self.output("SharePoint同期ルート: 未登録")
        registrations = list_redmine_registrations(self.rag_root)
        registered = sum(1 for item in registrations if item.registered)
        self.output(f"Redmine APIキー: {registered:,}件登録済み")
        gitlab_registrations = list_gitlab_registrations(self.rag_root)
        gitlab_registered = sum(
            1 for item in gitlab_registrations if item.registered
        )
        self.output(
            f"GitLab access token: {gitlab_registered:,}件登録済み"
        )
        self.output(
            "APIキーとtokenの値は登録・更新だけ可能です。"
            "画面やログには表示しません。"
        )

    def register_sharepoint_root_setting(self: Any) -> bool:
        current = sharepoint_root_status(self.rag_root)
        value = self._prompt_preserving_value(
            "SharePoint同期ルートの絶対フォルダ",
            current.root or "",
            required=True,
            description=(
                "OneDriveで同期しているSharePoint全体の基準フォルダです。"
                "DBには保存せず、この端末の設定として保持します。"
            ),
            examples=self._examples("ingestion_root"),
        )
        if value is None:
            self._print_info("SharePoint同期ルートは変更されていません。")
            return False
        try:
            root = set_sharepoint_root(self.rag_root, value)
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="SharePoint同期ルートの保存",
                stage="machine_connections.sharepoint.save",
            )
            return False
        self._print_success("SharePoint同期ルートをこの端末へ登録しました。")
        self.output(f"登録先: {root}")
        return True

    def clear_sharepoint_root_setting(self: Any) -> bool:
        status = sharepoint_root_status(self.rag_root)
        if status.source != "manager":
            if status.source == "environment":
                self._print_info(
                    f"現在は環境変数 {SHAREPOINT_ROOT_ENV} を使用しています。"
                    "この画面から環境変数は削除できません。"
                )
            else:
                self._print_info("Managerに登録されたSharePoint同期ルートはありません。")
            return False
        if not self._confirm("Managerに登録したSharePoint同期ルートを削除しますか？"):
            self._print_info("SharePoint同期ルートは変更されていません。")
            return False
        clear_sharepoint_root(self.rag_root)
        self._print_success("ManagerのSharePoint同期ルート設定を削除しました。")
        return True

    def register_redmine_api_key_setting(
        self: Any,
        *,
        project_url: str | None = None,
    ) -> bool:
        url = project_url
        if not url:
            url = self._prompt_preserving_value(
                "RedmineプロジェクトのURL",
                "",
                required=True,
                description=(
                    "APIキーを使うRedmineを特定します。"
                    "同じRedmine配下の別プロジェクトでも同じ登録を利用できます。"
                ),
                examples=self._examples("redmine_project_url"),
            )
        if url is None:
            self._print_info("Redmine APIキーは変更されていません。")
            return False
        try:
            project = parse_redmine_project_url(url)
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="Redmine接続先の確認",
                stage="machine_connections.redmine.validate",
            )
            return False
        self.output(f"登録対象: {project.api_root}")
        self.output(
            "APIキーは入力中も、保存後も、この画面から読み出せません。"
        )
        try:
            api_key = getpass.getpass("Redmine APIキー【必須・非表示】: ")
        except (KeyboardInterrupt, EOFError):
            self.output("")
            self._print_info("Redmine APIキーは変更されていません。")
            return False
        if not api_key.strip():
            self._print_error("APIキーを入力してください。")
            return False
        try:
            registration = register_redmine_api_key(
                self.rag_root,
                project.project_url,
                api_key,
            )
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="Redmine APIキーの保存",
                stage="machine_connections.redmine.save",
            )
            return False
        finally:
            api_key = ""
        self._print_success("Redmine APIキーをこの端末へ登録しました。")
        self.output(f"接続先: {registration.api_root}")
        self.output("APIキーの値は再表示できません。変更時は上書き登録します。")
        return True

    def show_redmine_api_key_registrations(self: Any) -> None:
        self.output("\nRedmine APIキー登録状況")
        registrations = list_redmine_registrations(self.rag_root)
        if not registrations:
            self.output("登録はありません。")
            return
        for index, item in enumerate(registrations, start=1):
            state = "登録済み" if item.registered else "再登録が必要"
            self.output(f"{index}. {item.api_root} — {state}")
        self.output("APIキーの値は表示しません。")

    def register_gitlab_token_setting(
        self: Any,
        *,
        gitlab_url: str | None = None,
    ) -> bool:
        url = gitlab_url
        if not url:
            url = self._prompt_preserving_value(
                "GitLab本体のURL",
                "",
                required=True,
                description=(
                    "GitLab.comなら https://gitlab.com、"
                    "社内GitLabがサブパス配下ならそのパスまで入力します。"
                ),
            )
        if url is None:
            self._print_info(
                "GitLab access tokenは変更されていません。"
            )
            return False
        try:
            # Validate and canonicalize the instance without requiring a
            # project path. gitlab_token_env performs the same strict parse
            # used by the persistent connection identity.
            gitlab_token_env(url)
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="GitLab接続先の確認",
                stage="machine_connections.gitlab.validate",
            )
            return False
        self.output(f"登録対象: {url.rstrip('/')}")
        self.output(
            "access tokenは入力中も、保存後も、"
            "この画面から読み出せません。"
        )
        self.output(
            "Issueの取得にはread_api権限を持つtokenが必要です。"
        )
        try:
            token = getpass.getpass(
                "GitLab access token【必須・非表示】: "
            )
        except (KeyboardInterrupt, EOFError):
            self.output("")
            self._print_info(
                "GitLab access tokenは変更されていません。"
            )
            return False
        if not token.strip():
            self._print_error("access tokenを入力してください。")
            return False
        try:
            registration = register_gitlab_token(
                self.rag_root,
                url,
                token,
            )
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="GitLab access tokenの保存",
                stage="machine_connections.gitlab.save",
            )
            return False
        finally:
            token = ""
        self._print_success(
            "GitLab access tokenをこの端末へ登録しました。"
        )
        self.output(f"接続先: {registration.gitlab_url}")
        self.output(
            "tokenの値は再表示できません。変更時は上書き登録します。"
        )
        return True

    def show_gitlab_token_registrations(self: Any) -> None:
        self.output("\nGitLab access token登録状況")
        registrations = list_gitlab_registrations(self.rag_root)
        if not registrations:
            self.output("登録はありません。")
            return
        for index, item in enumerate(registrations, start=1):
            state = "登録済み" if item.registered else "再登録が必要"
            self.output(f"{index}. {item.gitlab_url} — {state}")
        self.output("access tokenの値は表示しません。")

    def confirm_gitlab_project_connection(
        self: Any,
        *,
        gitlab_url: str,
        project_url: str,
    ) -> Any | None:
        try:
            location = gitlab_project_location(
                gitlab_url,
                project_url,
            )
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="GitLabプロジェクトURLの確認",
                stage="machine_connections.gitlab.project_url",
            )
            return None
        if not has_stored_gitlab_token(
            self.rag_root,
            location.gitlab_url,
        ):
            self._print_info(
                "このGitLabのaccess tokenが未登録のため、"
                "共通のSource接続設定を開きます。"
            )
            if not self._source_connection_settings_screen(
                required="gitlab",
                gitlab_url=location.gitlab_url,
            ):
                return None
        try:
            from .networking import resolve_source_network_route

            route = resolve_source_network_route(self.rag_root, environment=None)
            checked = check_gitlab_project(
                self.rag_root,
                gitlab_url=location.gitlab_url,
                project_url=location.project_url,
                token_env=gitlab_token_env(location.gitlab_url),
                environ=route.environment,
                http_get=route.http_get,
            )
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="GitLabプロジェクトへの接続確認",
                stage="machine_connections.gitlab.connection_check",
            )
            return None
        self._print_success(
            "GitLabプロジェクトへaccess tokenで接続できました。"
        )
        self.output(f"確認対象: {checked.name}")
        return checked

    def check_gitlab_project_setting(self: Any) -> bool:
        gitlab_url = self._prompt_preserving_value(
            "GitLab本体のURL",
            "",
            required=True,
            description=(
                "社内GitLabがサブパス配下なら、"
                "そのサブパスまで含めます。"
            ),
        )
        if gitlab_url is None:
            self._print_info("GitLab接続確認を中止しました。")
            return False
        project_url = self._prompt_preserving_value(
            "GitLabプロジェクトのURL",
            "",
            required=True,
            description=(
                "確認するプロジェクトのトップURLです。"
                "/-/issues 以降は付けません。"
            ),
            examples=self._examples("gitlab_repository_web_url"),
        )
        if project_url is None:
            self._print_info("GitLab接続確認を中止しました。")
            return False
        return (
            self._confirm_gitlab_project_connection(
                gitlab_url=gitlab_url,
                project_url=project_url,
            )
            is not None
        )

    def show_source_connection_status(self: Any) -> None:
        self._print_screen_header("Source接続設定")
        self._show_source_connection_summary()

    def show_machine_technical_info(self: Any) -> None:
        self._print_screen_header("技術情報")
        self.output(f"Python: {self._runtime_python()}")
        self.output(f"DB root: {self.dbs_root}")
        self.output(f"Platform: {sys.platform}")
        sharepoint = sharepoint_root_status(self.rag_root)
        self.output(
            "SharePoint root setting: "
            + (sharepoint.source if sharepoint.configured else "not configured")
        )
        self.output(
            "Redmine API key registrations: "
            + str(
                sum(
                    1
                    for item in list_redmine_registrations(self.rag_root)
                    if item.registered
                )
            )
        )
        self.output(
            "GitLab access token registrations: "
            + str(
                sum(
                    1
                    for item in list_gitlab_registrations(self.rag_root)
                    if item.registered
                )
            )
        )

    def prompt_new_sharepoint_source(self: Any) -> dict[str, Any] | None:
        if os.name != "nt":
            self._print_warning(
                "SharePoint Sourceの追加・更新はWindowsだけで利用できます。"
                "既存DBの検索とWebリンク表示はこのOSでも利用できます。"
            )
            return None
        if not sharepoint_root_status(self.rag_root).configured:
            self._print_info(
                "SharePoint同期ルートが未登録のため、共通のSource接続設定を開きます。"
            )
            if not self._source_connection_settings_screen(required="sharepoint"):
                return None
        relative = self._prompt_preserving_value(
            "SharePoint rootからの相対フォルダ",
            "",
            required=True,
            examples=self._examples("sharepoint_relative_path"),
        )
        browser = self._prompt_preserving_value(
            "SharePointのbrowser URL",
            "",
            required=True,
            examples=self._examples("sharepoint_browser_url"),
        )
        name = self._prompt_preserving_value(
            "Sourceの名前",
            "",
            required=True,
            examples=self._examples("sharepoint_source_display_name"),
        )
        if relative is None or browser is None or name is None:
            return None
        return {
            "source_type": "sharepoint",
            "label": "SharePoint",
            "display_name": name,
            "fetch": {
                "relative_path": relative,
                "root_env": SHAREPOINT_ROOT_ENV,
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

    def prompt_new_redmine_source(self: Any) -> dict[str, Any] | None:
        url = self._prompt_preserving_value(
            "RedmineプロジェクトのURL",
            "",
            required=True,
            examples=self._examples("redmine_project_url"),
        )
        if url is None:
            return None
        try:
            parse_redmine_project_url(url)
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="Redmine接続先の確認",
                stage="machine_connections.redmine.validate",
            )
            return None
        if not has_stored_redmine_api_key(self.rag_root, url):
            self._print_info(
                "このRedmineのAPIキーが未登録のため、共通のSource接続設定を開きます。"
            )
            if not self._source_connection_settings_screen(
                required="redmine",
                redmine_project_url=url,
            ):
                return None
        name = self._prompt_preserving_value(
            "Sourceの名前",
            "",
            required=True,
            examples=self._examples("redmine_source_display_name"),
        )
        if name is None:
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
                examples=self._examples("redmine_days"),
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
        return {
            "source_type": "redmine",
            "label": "Redmine",
            "display_name": name,
            "fetch": {
                "project_url": url,
                "updated_within_days": days,
                "api_key_env": redmine_api_key_env(url),
            },
            "link": generated_redmine_link(url),
            "summary": (
                ("期間", "制限なし" if days is None else f"過去{days}日"),
                ("Issue状態", "完了済みを含む"),
                ("取得方式", "Issueを1件ずつ直列取得"),
                ("検索への反映", "5件保存するごと"),
                ("添付", "ファイル名とURLだけ保存"),
                ("APIキー", "この端末に登録済み（値は非表示）"),
                ("固定待機", "なし"),
                ("自動削除", "行わない"),
                ("途中再開", "可能"),
            ),
        }

    def prompt_new_gitlab_issues_source(
        self: Any,
    ) -> dict[str, Any] | None:
        gitlab_url = self._prompt_preserving_value(
            "GitLab本体のURL",
            "",
            required=True,
            description=(
                "GitLab.comなら https://gitlab.com、"
                "社内GitLabがサブパス配下ならそのパスまで入力します。"
            ),
        )
        if gitlab_url is None:
            return None
        project_url = self._prompt_preserving_value(
            "GitLabプロジェクトのURL",
            "",
            required=True,
            description=(
                "Issueを取得するプロジェクトのトップURLです。"
                "/-/issues 以降は付けません。"
            ),
            examples=self._examples("gitlab_repository_web_url"),
        )
        if project_url is None:
            return None
        checked = self._confirm_gitlab_project_connection(
            gitlab_url=gitlab_url,
            project_url=project_url,
        )
        if checked is None:
            return None
        name = self._prompt_preserving_value(
            "Sourceの名前",
            "",
            required=True,
            examples=self._examples("source_display_name"),
        )
        if name is None:
            return None
        period = self._select_value(
            "どこまでさかのぼって取得しますか？"
            "（Issueの更新日時）",
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
                description="1～3650の日数を入力します。",
                examples=self._examples("redmine_days"),
            )
            if raw_days is None:
                return None
            try:
                days = int(raw_days)
            except ValueError:
                self._print_error(
                    "日数は1～3650の整数で入力してください。"
                )
                return None
            if not 1 <= days <= 3650:
                self._print_error(
                    "日数は1～3650の整数で入力してください。"
                )
                return None
        location = checked.location
        return {
            "source_type": "gitlab_issues",
            "label": "GitLab Issue",
            "display_name": name,
            "fetch": {
                "gitlab_url": location.gitlab_url,
                "project_url": location.project_url,
                "updated_within_days": days,
                "token_env": gitlab_token_env(location.gitlab_url),
            },
            "link": location.issue_link,
            "summary": (
                (
                    "期間",
                    "制限なし" if days is None else f"過去{days}日",
                ),
                ("Issue状態", "open／closed両方"),
                ("コメント", "Discussionとシステム履歴を含む"),
                ("取得方式", "Issueを1件ずつ直列取得"),
                ("検索への反映", "5件保存するごと"),
                (
                    "履歴保持",
                    "削除・閲覧不可になった既存Issueも保持",
                ),
                (
                    "project変更",
                    "初回反映後は不可（別Sourceとして追加）",
                ),
                ("access token", "この端末に登録済み（値は非表示）"),
                ("途中再開", "可能"),
            ),
        }

    manager_class._machine_setup_screen = machine_setup_screen
    manager_class._source_connection_settings_screen = source_connection_settings_screen
    manager_class._show_source_connection_summary = show_source_connection_summary
    manager_class._register_sharepoint_root_setting = register_sharepoint_root_setting
    manager_class._clear_sharepoint_root_setting = clear_sharepoint_root_setting
    manager_class._register_redmine_api_key_setting = register_redmine_api_key_setting
    manager_class._show_redmine_api_key_registrations = show_redmine_api_key_registrations
    manager_class._register_gitlab_token_setting = register_gitlab_token_setting
    manager_class._show_gitlab_token_registrations = show_gitlab_token_registrations
    manager_class._confirm_gitlab_project_connection = confirm_gitlab_project_connection
    manager_class._check_gitlab_project_setting = check_gitlab_project_setting
    manager_class._show_source_connection_status = show_source_connection_status
    manager_class._show_machine_technical_info = show_machine_technical_info
    manager_class._prompt_new_sharepoint_source = prompt_new_sharepoint_source
    manager_class._prompt_new_redmine_source = prompt_new_redmine_source
    manager_class._prompt_new_gitlab_issues_source = prompt_new_gitlab_issues_source
    setattr(manager_class, _CLASS_MARKER, True)
