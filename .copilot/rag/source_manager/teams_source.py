from __future__ import annotations

import copy
import functools
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError
from .machine_connections import SHAREPOINT_ROOT_ENV, sharepoint_root_status
from .store import SourceStore


_PROVIDER_MARKER = "_local_rag_teams_provider_installed"
_EXECUTION_MARKER = "_local_rag_teams_execution_installed"
_RUNNER_MARKER = "_local_rag_teams_runner_installed"
_MANAGER_HOOK_MARKER = "_local_rag_teams_manager_hook_installed"
_MANAGER_CLASS_MARKER = "_local_rag_teams_manager_installed"


def install_teams_source_runtime() -> None:
    """Install the Teams shared-folder Source as a separate provider."""

    from . import execution, machine_connections, manager_connections, metadata, progress
    from . import providers, runner, store as store_module

    _install_provider_runtime(providers, runner, store_module)
    _install_machine_environment(machine_connections)
    _install_source_links_runtime(metadata)
    _install_execution_runtime(execution, runner)
    _install_runner_runtime(runner, providers)
    progress._PROVIDER_LABELS["teams"] = "Teams"
    _install_manager_hook(manager_connections)


def _install_provider_runtime(providers: Any, runner: Any, store_module: Any) -> None:
    if bool(getattr(providers, _PROVIDER_MARKER, False)):
        return
    original_validate = providers.validate_provider_config
    original_resolve = providers.resolve_environment_root

    @functools.wraps(original_validate)
    def validate_provider_config(
        provider: str,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        kind = str(provider or "").strip().lower()
        if kind == "teams":
            return original_validate("sharepoint", settings)
        return original_validate(kind, settings)

    @functools.wraps(original_resolve)
    def resolve_environment_root(
        settings: Mapping[str, Any],
        *,
        provider: str,
        environment: Mapping[str, str] | None = None,
    ) -> Path:
        kind = str(provider or "").strip().lower()
        if kind == "teams":
            return original_resolve(
                settings,
                provider="sharepoint",
                environment=environment,
            )
        return original_resolve(
            settings,
            provider=provider,
            environment=environment,
        )

    providers.SUPPORTED_PROVIDERS = frozenset(
        set(providers.SUPPORTED_PROVIDERS) | {"teams"}
    )
    providers.validate_provider_config = validate_provider_config
    providers.resolve_environment_root = resolve_environment_root
    runner.validate_provider_config = validate_provider_config
    store_module.validate_provider_config = validate_provider_config
    setattr(providers, _PROVIDER_MARKER, True)


def _enable_teams_source_links() -> Any:
    tool_root = (
        Path(__file__).resolve().parents[1]
        / "gen_db"
        / "software_rag_tool"
    )
    value = str(tool_root)
    if value not in sys.path:
        sys.path.insert(0, value)
    from software_rag_tool import source_links

    source_links.ALLOWED_SOURCE_TYPES = frozenset(
        set(source_links.ALLOWED_SOURCE_TYPES) | {"teams"}
    )
    return source_links


def _install_source_links_runtime(metadata: Any) -> None:
    marker = "_local_rag_teams_source_links_installed"
    if bool(getattr(metadata, marker, False)):
        return
    _enable_teams_source_links()
    original = metadata._source_links_module

    @functools.wraps(original)
    def source_links_module(rag_root: Path) -> Any:
        module = original(rag_root)
        module.ALLOWED_SOURCE_TYPES = frozenset(
            set(module.ALLOWED_SOURCE_TYPES) | {"teams"}
        )
        return module

    metadata._source_links_module = source_links_module
    setattr(metadata, marker, True)


def _install_machine_environment(machine_connections: Any) -> None:
    marker = "_local_rag_teams_machine_environment_installed"
    if bool(getattr(machine_connections, marker, False)):
        return
    original = machine_connections.source_runtime_environment

    @functools.wraps(original)
    def source_runtime_environment(
        rag_root: str | Path,
        source_payload: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        result = original(rag_root, source_payload, environ=environ)
        source_type = str(source_payload.get("source_type") or "").strip().lower()
        if source_type != "teams":
            return result
        fetch = source_payload.get("fetch")
        settings = dict(fetch) if isinstance(fetch, Mapping) else {}
        name = str(settings.get("root_env") or SHAREPOINT_ROOT_ENV).strip()
        if name:
            root = machine_connections.configured_sharepoint_root(
                rag_root,
                environ=result,
            )
            if root is not None:
                result[name] = str(root)
        return result

    machine_connections.source_runtime_environment = source_runtime_environment
    setattr(machine_connections, marker, True)


def _install_execution_runtime(execution: Any, runner: Any) -> None:
    if bool(getattr(execution, _EXECUTION_MARKER, False)):
        return
    original = execution.execute_fetch_plan

    @functools.wraps(original)
    def execute_fetch_plan(
        plan: Mapping[str, Any],
        work_directory: Path,
        state: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if str(plan.get("provider") or "").strip().lower() != "teams":
            return original(plan, work_directory, state, **kwargs)
        translated = copy.deepcopy(dict(plan))
        translated["provider"] = "sharepoint"
        callback = kwargs.get("progress_callback")
        kwargs["progress_callback"] = _teams_progress_callback(callback)
        return original(translated, work_directory, state, **kwargs)

    execution.execute_fetch_plan = execute_fetch_plan
    runner.execute_fetch_plan = execute_fetch_plan
    setattr(execution, _EXECUTION_MARKER, True)


def _teams_progress_callback(callback: Any) -> Any:
    if callback is None:
        return None

    def emit(event: Mapping[str, Any]) -> None:
        value = dict(event)
        if str(value.get("provider") or "") == "sharepoint":
            value["provider"] = "teams"
        phase = str(value.get("phase") or "")
        if phase.startswith("sharepoint."):
            value["phase"] = "teams." + phase.split(".", 1)[1]
        label = str(value.get("label_ja") or "")
        if "SharePoint" in label:
            value["label_ja"] = label.replace("SharePoint", "Teams")
        callback(value)

    return emit


def _install_runner_runtime(runner: Any, providers: Any) -> None:
    if bool(getattr(runner, _RUNNER_MARKER, False)):
        return
    original_update = runner.update_source
    original_update_all = runner.update_all_sources
    original_update_configuration = runner.update_source_configuration

    @functools.wraps(original_update)
    def update_source(
        db_root: Path,
        local_source_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        store = SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        if str(source.payload.get("source_type") or "").strip().lower() == "teams":
            if os.name != "nt":
                raise SourceManagerError("Teams Source updates require Windows")
            _prepare_external_root_resume(store, local_source_key)
        return original_update(db_root, local_source_key, **kwargs)

    @functools.wraps(original_update_all)
    def update_all_sources(db_root: Path, **kwargs: Any) -> dict[str, Any]:
        result = original_update_all(db_root, **kwargs)
        if os.name == "nt":
            return result
        return normalize_update_all_result(result)

    @functools.wraps(original_update_configuration)
    def update_source_configuration(
        db_root: Path,
        local_source_key: str,
        *,
        fetch: Mapping[str, Any],
        display_name: str | None = None,
        pending_link: Any = runner._UNSET,
    ) -> dict[str, Any]:
        store = SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        if (
            source.payload.get("source_id")
            and str(source.payload.get("source_type") or "").strip().lower()
            == "teams"
        ):
            normalized = providers.validate_provider_config("teams", fetch)
            if normalized != source.payload.get("fetch"):
                raise SourceManagerError(
                    "teams_ingestion_root_is_immutable_add_new_source"
                )
        arguments: dict[str, Any] = {
            "fetch": fetch,
            "display_name": display_name,
        }
        if pending_link is not runner._UNSET:
            arguments["pending_link"] = pending_link
        return original_update_configuration(
            db_root,
            local_source_key,
            **arguments,
        )

    runner.update_source = update_source
    runner.update_all_sources = update_all_sources
    runner.update_source_configuration = update_source_configuration
    setattr(runner, _RUNNER_MARKER, True)
    package = sys.modules.get(__package__)
    if package is not None:
        setattr(package, "update_source", update_source)
        setattr(package, "update_all_sources", update_all_sources)
        setattr(package, "update_source_configuration", update_source_configuration)


def _prepare_external_root_resume(store: SourceStore, local_source_key: str) -> None:
    state = store.read_state(local_source_key)
    if not state.payload:
        return
    if (
        state.payload.get("phase") != "reflect"
        or int(state.payload.get("pending_count") or 0) <= 0
    ):
        return
    value = copy.deepcopy(state.payload)
    value.update(
        {
            "status": "interrupted",
            "phase": "fetch",
            "can_resume": True,
            "last_error": None,
        }
    )
    store.save_state(
        local_source_key,
        value,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )


def normalize_update_all_result(result: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(result))
    items = value.get("results")
    if not isinstance(items, list):
        return value
    for item in items:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("source_type") or "").strip().lower() == "teams"
            and item.get("status") == "failed"
            and "teams source updates require windows"
            in str(item.get("error") or "").casefold()
        ):
            item["status"] = "skipped"
            item["skip_reason"] = "teams_update_requires_windows"
            for key in (
                "failure_stage",
                "error_type",
                "error",
                "failure_diagnostic",
                "process_diagnostic",
            ):
                item.pop(key, None)
    failed = [
        item
        for item in items
        if isinstance(item, dict) and item.get("status") == "failed"
    ]
    blocking_reasons = {
        "sharepoint_update_requires_windows",
        "teams_update_requires_windows",
    }
    blocking = [
        item
        for item in items
        if isinstance(item, dict) and item.get("skip_reason") in blocking_reasons
    ]
    updateable = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("skip_reason") != "one_shot_source_complete"
    ]
    successful = {"updated", "complete", "success"}
    completed = sum(
        1 for item in updateable if item.get("status") in successful
    )
    value.update(
        {
            "status": "ok" if not failed else "partial",
            "source_count": len(items),
            "updateable_source_count": len(updateable),
            "completed_source_count": completed,
            "snapshot_marker_eligible": (
                not failed and not blocking and completed == len(updateable)
            ),
        }
    )
    return value


def _install_manager_hook(manager_connections: Any) -> None:
    if bool(getattr(manager_connections, _MANAGER_HOOK_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_ui(manager_class)

    manager_connections.install_manager_connection_ui = install_manager_connection_ui
    setattr(manager_connections, _MANAGER_HOOK_MARKER, True)


def _install_manager_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _MANAGER_CLASS_MARKER, False)):
        return
    module = sys.modules.get(manager_class.__module__)
    if module is not None and isinstance(getattr(module, "_PROVIDER_JA", None), dict):
        module._PROVIDER_JA["teams"] = "Microsoft Teams共有フォルダ"

    original_ui_type = manager_class._ui_source_type
    original_status = manager_class._source_manager_status
    original_update_single = manager_class._update_single_source
    original_edit = manager_class._edit_source_fetch_settings
    original_connections = manager_class._source_connection_settings_screen
    original_failure_label = manager_class._source_failure_stage_label

    @staticmethod
    def ui_source_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return "teams" if normalized == "teams" else original_ui_type(value)

    @functools.wraps(original_status)
    def source_manager_status(self: Any, source: dict[str, Any]) -> str:
        if self._ui_source_type(source.get("source_type")) == "teams" and os.name != "nt":
            return "このOSでは更新不可"
        return original_status(self, source)

    @functools.wraps(original_update_single)
    def update_single_source(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        if self._ui_source_type(source.get("source_type")) == "teams" and os.name != "nt":
            self._print_warning(
                "このOSではTeams共有フォルダの追加・更新はできません。"
                "既存DBの検索は利用できます。"
            )
            return
        return original_update_single(self, db_name, source)

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        if self._ui_source_type(source.get("source_type")) != "teams":
            return original_edit(self, db_name, source)
        fetch = self._show_source_fetch_settings(source)
        local_key = str(source.get("_local_source_key") or "")
        if not local_key:
            self._print_info("このSourceには変更できる取得設定がありません。")
            return
        if source.get("source_id"):
            self._print_warning(
                "検索へ反映済みのTeams Sourceでは、"
                "同期ルートからの相対フォルダを変更できません。"
            )
            self._print_info(
                "別の共有フォルダを取り込む場合は、"
                "「新しいSourceを追加する」から登録してください。"
            )
            return
        if os.name != "nt":
            self._print_warning(
                "このOSではTeams共有フォルダの取得設定を変更できません。"
            )
            return
        relative = self._prompt_preserving_value(
            "SharePoint同期ルートからのTeams共有フォルダ相対パス",
            str(fetch.get("relative_path") or ""),
            required=True,
            examples=self._examples("sharepoint_relative_path"),
        )
        if relative is None:
            return
        updated = dict(fetch)
        updated["relative_path"] = relative
        self.output("\n変更後の取得設定")
        self.output(f"同期ルートからの相対フォルダ: {relative}")
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
            self._print_internal_diagnostic(
                exc,
                operation="Source取得設定の保存",
                stage="source_config.save",
                db_name=db_name,
                source_name=str(source.get("display_name") or ""),
                source_key=local_key,
                provider="teams",
                can_resume=True,
            )
            return
        self._print_success("取得設定を保存しました。")

    @functools.wraps(original_connections)
    def source_connection_settings_screen(
        self: Any,
        *,
        required: str | None = None,
        redmine_project_url: str | None = None,
    ) -> bool:
        if required != "teams":
            return original_connections(
                self,
                required=required,
                redmine_project_url=redmine_project_url,
            )
        self._print_screen_header("Source接続設定")
        self._show_source_connection_summary()
        self._print_info(
            "Teams共有フォルダSourceを登録するには、"
            "SharePoint同期ルートの端末設定が必要です。"
        )
        return bool(self._register_sharepoint_root_setting())

    @staticmethod
    def source_failure_stage_label(value: Any) -> str:
        stage = str(value or "").strip()
        if stage.startswith("fetch.teams"):
            return "Teams共有フォルダの確認"
        return original_failure_label(value)

    manager_class._ui_source_type = ui_source_type
    manager_class._source_manager_status = source_manager_status
    manager_class._update_single_source = update_single_source
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    manager_class._source_connection_settings_screen = source_connection_settings_screen
    manager_class._source_failure_stage_label = source_failure_stage_label
    manager_class._prompt_new_teams_source = prompt_new_teams_source
    manager_class._add_source_screen = add_source_screen
    setattr(manager_class, _MANAGER_CLASS_MARKER, True)


def prompt_new_teams_source(self: Any) -> dict[str, Any] | None:
    if os.name != "nt":
        self._print_warning(
            "Teams共有フォルダSourceの追加・更新はWindowsだけで利用できます。"
            "既存DBの検索はこのOSでも利用できます。"
        )
        return None
    if not sharepoint_root_status(self.rag_root).configured:
        self._print_info(
            "SharePoint同期ルートが未登録のため、"
            "共通のSource接続設定を開きます。"
        )
        if not self._source_connection_settings_screen(required="teams"):
            return None
    relative = self._prompt_preserving_value(
        "SharePoint同期ルートからのTeams共有フォルダ相対パス",
        "",
        required=True,
        description=(
            "OneDriveで同期済みのTeams共有フォルダを指定します。"
            "Teamsのチャネル名ではなく、端末上の相対フォルダです。"
        ),
        examples=self._examples("sharepoint_relative_path"),
    )
    name = self._prompt_preserving_value(
        "Sourceの名前",
        "",
        required=True,
        examples=self._examples("sharepoint_source_display_name"),
    )
    if relative is None or name is None:
        return None
    return {
        "source_type": "teams",
        "label": "Microsoft Teams共有フォルダ",
        "display_name": name,
        "fetch": {
            "relative_path": relative,
            "root_env": SHAREPOINT_ROOT_ENV,
        },
        "summary": (
            ("同期フォルダ", relative),
            ("同期方式", "OneDriveで同期済みのローカルフォルダ"),
            ("Webリンク", "初期設定なし"),
            ("追加・更新", "Windowsのみ"),
        ),
    }


def add_source_screen(self: Any, db_name: str) -> None:
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
            ("5", "Teams共有フォルダ【OneDrive同期・Windowsのみ】"),
            ("6", "手元の資料を一度だけ取り込む（Other）"),
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
        "5": self._prompt_new_teams_source,
        "6": self._prompt_new_other_source,
    }
    form = forms.get(choice)
    if form is None:
        self._invalid_selection("0～6")
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
            (("1", "保存して取り込みを開始"), ("0", "中止")),
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
            progress_callback=self._progress_callback(
                "Source追加",
                provider=str(specification.get("source_type") or ""),
            ),
        )
    except Exception as exc:
        self._print_source_exception(
            exc,
            operation="Source登録",
            db_name=db_name,
            source_name=str(specification.get("display_name") or ""),
            provider=str(specification.get("source_type") or ""),
        )
        return
    if action == "1":
        status = str(result.get("status") or "")
        if status == "updated":
            self._print_success("Sourceを保存し、検索へ反映しました。")
        elif status in {"failed", "error"}:
            self._print_source_result_failure(
                result,
                operation="Source登録後の初回処理",
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
