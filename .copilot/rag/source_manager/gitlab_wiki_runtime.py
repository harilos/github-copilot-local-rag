from __future__ import annotations

import copy
import functools
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError
from .gitlab_issues import gitlab_token_env, parse_gitlab_project
from .gitlab_wiki import (
    fetch_gitlab_wiki,
    generated_gitlab_wiki_link,
    repair_generated_gitlab_wiki_link,
    validate_gitlab_wiki_work_tree,
)
from .machine_connections import has_stored_gitlab_token
from .store import SourceStore

_PROVIDER_MARKER = "_local_rag_gitlab_wiki_provider_installed"
_MACHINE_MARKER = "_local_rag_gitlab_wiki_machine_installed"
_EXECUTION_MARKER = "_local_rag_gitlab_wiki_execution_installed"
_RUNNER_MARKER = "_local_rag_gitlab_wiki_runner_installed"
_MANAGER_HOOK_MARKER = "_local_rag_gitlab_wiki_manager_hook_installed"
_MANAGER_CLASS_MARKER = "_local_rag_gitlab_wiki_manager_installed"
_PACKAGE_MARKER = "_local_rag_gitlab_wiki_package_installed"


def install_gitlab_wiki_runtime() -> None:
    """Install GitLab Wiki acquisition while sharing the GitLab token registry."""
    from . import execution, machine_connections, manager_connections
    from . import packages, progress, providers, runner, store as store_module

    _install_provider_contract(providers, runner, store_module)
    _install_machine_environment(machine_connections)
    _install_execution(execution, runner)
    _install_runner(runner, providers)
    _install_package_contract(packages)
    progress._PROVIDER_LABELS["gitlab_wiki"] = "GitLab Wiki"
    _install_manager_hook(manager_connections)


def _validate_fetch(settings: Mapping[str, Any]) -> dict[str, Any]:
    supplied = dict(settings)
    unexpected = set(supplied) - {"gitlab_url", "project_url", "token_env"}
    if unexpected:
        raise SourceManagerError("GitLab Wiki settings contain unsupported fields")
    project = parse_gitlab_project(
        supplied.get("project_url"),
        supplied.get("gitlab_url"),
    )
    expected_env = gitlab_token_env(project.gitlab_url)
    requested_env = str(supplied.get("token_env") or expected_env).strip()
    if requested_env != expected_env:
        raise SourceManagerError(
            "GitLab token environment does not match the GitLab URL"
        )
    return {
        "gitlab_url": project.gitlab_url,
        "project_url": project.project_url,
        "token_env": expected_env,
    }


def _install_provider_contract(providers: Any, runner: Any, store_module: Any) -> None:
    if bool(getattr(providers, _PROVIDER_MARKER, False)):
        return
    original_validate = providers.validate_provider_config
    original_build = providers.build_fetch_plan

    @functools.wraps(original_validate)
    def validate_provider_config(
        provider: str,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        kind = str(provider or "").strip().lower()
        if kind == "gitlab_wiki":
            return _validate_fetch(settings)
        return original_validate(kind, settings)

    @functools.wraps(original_build)
    def build_fetch_plan(
        *,
        source_key: str,
        provider: str,
        settings: Mapping[str, Any],
        logical_root: str,
        work_path: str,
    ) -> Any:
        kind = str(provider or "").strip().lower()
        if kind != "gitlab_wiki":
            return original_build(
                source_key=source_key,
                provider=provider,
                settings=settings,
                logical_root=logical_root,
                work_path=work_path,
            )
        normalized = validate_provider_config(kind, settings)
        normalized_root = providers.validate_relative_path(
            logical_root,
            field="logical_root",
            allow_empty=False,
        )
        normalized_work = providers.validate_relative_path(
            work_path,
            field="work_path",
            allow_empty=False,
        )
        if normalized_root != normalized_work:
            raise SourceManagerError(
                "logical_root and work_path must use the fixed Source work path"
            )
        step = providers.FetchStep(
            "wiki",
            "gitlab_fetch_wiki",
            True,
            normalized_work,
            normalized,
            "gitlab_wiki_full_refresh",
        )
        body = {
            "schema_version": "local-rag.fetch-plan.v1",
            "source_key": str(source_key),
            "provider": kind,
            "logical_root": normalized_root,
            "work_path": normalized_work,
            "steps": [step.to_dict()],
        }
        providers.validate_persistable(body, field="fetch_plan")
        digest = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return providers.FetchPlan(
            schema_version=body["schema_version"],
            source_key=str(source_key),
            provider=kind,
            logical_root=normalized_root,
            work_path=normalized_work,
            steps=(step,),
            plan_etag=digest,
        )

    providers.SUPPORTED_PROVIDERS = frozenset(
        set(providers.SUPPORTED_PROVIDERS) | {"gitlab_wiki"}
    )
    providers.validate_provider_config = validate_provider_config
    providers.build_fetch_plan = build_fetch_plan
    runner.validate_provider_config = validate_provider_config
    store_module.validate_provider_config = validate_provider_config
    store_module.build_fetch_plan = build_fetch_plan
    setattr(providers, _PROVIDER_MARKER, True)


def _install_machine_environment(machine_connections: Any) -> None:
    if bool(getattr(machine_connections, _MACHINE_MARKER, False)):
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
        if str(source_payload.get("source_type") or "").strip().lower() != "gitlab_wiki":
            return result
        fetch = source_payload.get("fetch")
        settings = dict(fetch) if isinstance(fetch, Mapping) else {}
        gitlab_url = settings.get("gitlab_url")
        name = str(settings.get("token_env") or "").strip()
        if name and gitlab_url:
            secret = machine_connections.resolve_gitlab_token(
                rag_root,
                gitlab_url=gitlab_url,
                token_env=name,
                environ=result,
            )
            if secret:
                result[name] = secret
        return result

    machine_connections.source_runtime_environment = source_runtime_environment
    setattr(machine_connections, _MACHINE_MARKER, True)


def _install_execution(execution: Any, runner: Any) -> None:
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
        if str(plan.get("provider") or "").strip().lower() != "gitlab_wiki":
            return original(plan, work_directory, state, **kwargs)
        del state
        work = Path(work_directory)
        if not work.is_dir() or work.is_symlink():
            raise SourceManagerError("work directory is unsafe")
        step = dict((plan.get("steps") or [{}])[0])
        parameters = dict(step.get("parameters") or {})
        getter = kwargs.get("http_get") or execution._http_get
        environment = kwargs.get("environment")
        env = execution.os.environ if environment is None else environment
        progress_callback = kwargs.get("progress_callback")
        execution._emit_provider_progress(progress_callback, "gitlab_wiki", "started")

        def request(
            url: str,
            headers: Mapping[str, str],
        ) -> tuple[int, bytes, Mapping[str, str]]:
            return execution._get_with_retry_response(
                getter,
                url,
                headers,
                provider="gitlab_wiki",
                provider_label="GitLab Wiki",
                progress_callback=progress_callback,
            )

        try:
            result = fetch_gitlab_wiki(
                parameters,
                work,
                request,
                env,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            execution._emit_provider_progress(
                progress_callback,
                "gitlab_wiki",
                "failed",
                error=exc,
            )
            raise
        execution._emit_provider_progress(
            progress_callback,
            "gitlab_wiki",
            "completed",
            documents=int(result.get("documents") or 0),
        )
        return result

    execution.execute_fetch_plan = execute_fetch_plan
    runner.execute_fetch_plan = execute_fetch_plan
    setattr(execution, _EXECUTION_MARKER, True)


def _install_runner(runner: Any, providers: Any) -> None:
    if bool(getattr(runner, _RUNNER_MARKER, False)):
        return
    original_register = runner.register_source
    original_update = runner.update_source
    original_update_configuration = runner.update_source_configuration
    original_reflect = runner._reflect_and_sync

    @functools.wraps(original_register)
    def register_source(
        db_root: Path,
        *,
        source_type: str,
        display_name: str,
        fetch: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if str(source_type or "").strip().lower() == "gitlab_wiki":
            normalized = providers.validate_provider_config("gitlab_wiki", fetch)
            kwargs["link"] = generated_gitlab_wiki_link(
                normalized["project_url"],
                normalized["gitlab_url"],
            )
            fetch = normalized
        return original_register(
            db_root,
            source_type=source_type,
            display_name=display_name,
            fetch=fetch,
            **kwargs,
        )

    @functools.wraps(original_update)
    def update_source(
        db_root: Path,
        local_source_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        store = SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        if str(source.payload.get("source_type") or "").strip().lower() != "gitlab_wiki":
            return original_update(db_root, local_source_key, **kwargs)
        normalized = providers.validate_provider_config(
            "gitlab_wiki",
            source.payload.get("fetch") or {},
        )
        if normalized != source.payload.get("fetch"):
            payload = copy.deepcopy(source.payload)
            payload["fetch"] = normalized
            pending = payload.get("pending_metadata")
            if isinstance(pending, dict) and isinstance(pending.get("link"), Mapping):
                repaired = repair_generated_gitlab_wiki_link(
                    normalized["project_url"],
                    normalized["gitlab_url"],
                    pending["link"],
                )
                if repaired is not None:
                    pending = copy.deepcopy(pending)
                    pending["link"] = repaired
                    payload["pending_metadata"] = pending
            source = store.save_source(
                payload,
                expected_revision=source.revision,
                expected_etag=source.etag,
            )
        state = store.read_state(local_source_key)
        if (
            state.payload.get("phase") == "reflect"
            and int(state.payload.get("pending_count") or 0) > 0
        ):
            validate_gitlab_wiki_work_tree(
                normalized,
                store.paths(local_source_key).work_directory,
                expected_documents=int(state.payload.get("fetched_count") or 0),
            )
        if (
            kwargs.get("executor") is None
            and kwargs.get("command_runner") is None
            and kwargs.get("http_get") is None
            and kwargs.get("rag_root") is not None
        ):
            route = runner.resolve_source_network_route(
                Path(kwargs["rag_root"]),
                environment=kwargs.get("environment"),
                progress_callback=kwargs.get("progress_callback"),
            )
            kwargs["command_runner"] = route.command_runner
            kwargs["http_get"] = route.http_get
            kwargs["environment"] = route.environment
        return original_update(db_root, local_source_key, **kwargs)

    @functools.wraps(original_update_configuration)
    def update_source_configuration(
        db_root: Path,
        local_source_key: str,
        *,
        fetch: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        store = SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        if (
            source.payload.get("source_id")
            and str(source.payload.get("source_type") or "").strip().lower()
            == "gitlab_wiki"
        ):
            current = providers.validate_provider_config(
                "gitlab_wiki",
                source.payload.get("fetch") or {},
            )
            normalized = providers.validate_provider_config("gitlab_wiki", fetch)
            if any(
                normalized.get(key) != current.get(key)
                for key in ("gitlab_url", "project_url")
            ):
                raise SourceManagerError(
                    "gitlab_wiki_project_is_immutable_add_new_source"
                )
        return original_update_configuration(
            db_root,
            local_source_key,
            fetch=fetch,
            **kwargs,
        )

    @functools.wraps(original_reflect)
    def reflect_and_sync(
        store: Any,
        source: Any,
        state: Any,
        *,
        add_root: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if str(source.payload.get("source_type") or "").strip().lower() == "gitlab_wiki":
            validate_gitlab_wiki_work_tree(
                source.payload.get("fetch") or {},
                Path(add_root),
                expected_documents=int(state.payload.get("fetched_count") or 0),
            )
        return original_reflect(
            store,
            source,
            state,
            add_root=add_root,
            **kwargs,
        )

    runner.register_source = register_source
    runner.update_source = update_source
    runner.update_source_configuration = update_source_configuration
    runner._reflect_and_sync = reflect_and_sync
    setattr(runner, _RUNNER_MARKER, True)
    package = sys.modules.get(__package__)
    if package is not None:
        setattr(package, "register_source", register_source)
        setattr(package, "update_source", update_source)
        setattr(package, "update_source_configuration", update_source_configuration)


def _install_package_contract(packages: Any) -> None:
    if bool(getattr(packages, _PACKAGE_MARKER, False)):
        return
    packages._DISTRIBUTION_TOOL_MODULES = frozenset(
        set(packages._DISTRIBUTION_TOOL_MODULES) | {"gitlab_wiki_links.py"}
    )
    setattr(packages, _PACKAGE_MARKER, True)


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
        module._PROVIDER_JA["gitlab_wiki"] = "GitLab Wiki"

    original_ui_type = manager_class._ui_source_type
    original_edit = manager_class._edit_source_fetch_settings
    original_failure_label = manager_class._source_failure_stage_label

    @staticmethod
    def ui_source_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return "gitlab_wiki" if normalized == "gitlab_wiki" else original_ui_type(value)

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        if self._ui_source_type(source.get("source_type")) != "gitlab_wiki":
            return original_edit(self, db_name, source)
        self._show_source_fetch_settings(source)
        self._print_warning(
            "GitLab Wiki SourceのGitLab本体とプロジェクトは変更できません。"
        )
        self._print_info(
            "別のWikiを取り込む場合は「新しいSourceを追加する」から登録してください。"
        )

    @staticmethod
    def source_failure_stage_label(value: Any) -> str:
        stage = str(value or "").strip()
        if stage.startswith("fetch.gitlab_wiki"):
            return "GitLab Wikiの取得"
        if stage.startswith("reflect.gitlab_wiki"):
            return "GitLab Wikiの検索反映"
        return original_failure_label(value)

    manager_class._ui_source_type = ui_source_type
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    manager_class._source_failure_stage_label = source_failure_stage_label
    manager_class._prompt_new_gitlab_wiki_source = prompt_new_gitlab_wiki_source
    manager_class._add_source_screen = add_source_screen
    setattr(manager_class, _MANAGER_CLASS_MARKER, True)


def prompt_new_gitlab_wiki_source(self: Any) -> dict[str, Any] | None:
    gitlab_url = self._prompt_preserving_value(
        "GitLab本体のURL",
        "",
        required=True,
        description=(
            "GitLab.comなら https://gitlab.com、社内GitLabがサブパス配下なら"
            "そのパスまで入力します。"
        ),
    )
    project_url = self._prompt_preserving_value(
        "GitLabプロジェクトのURL",
        "",
        required=True,
        description="Wikiを取得するプロジェクトのトップURLです。",
        examples=self._examples("gitlab_repository_web_url"),
    )
    if gitlab_url is None or project_url is None:
        return None
    try:
        project = parse_gitlab_project(project_url, gitlab_url)
    except Exception as exc:
        self._print_internal_diagnostic(
            exc,
            operation="GitLab Wiki接続先の確認",
            stage="machine_connections.gitlab_wiki.validate",
        )
        return None
    if not has_stored_gitlab_token(self.rag_root, project.gitlab_url):
        self._print_info(
            "このGitLabのaccess tokenが未登録のため、共通のSource接続設定を開きます。"
        )
        if not self._source_connection_settings_screen(
            required="gitlab",
            gitlab_url=project.gitlab_url,
        ):
            return None
    checked = self._confirm_gitlab_project_connection(
        gitlab_url=project.gitlab_url,
        project_url=project.project_url,
    )
    if checked is None:
        return None
    name = self._prompt_preserving_value(
        "Sourceの名前",
        "",
        required=True,
        examples=self._examples("gitlab_source_display_name"),
    )
    if name is None:
        return None
    location = checked.location
    return {
        "source_type": "gitlab_wiki",
        "label": "GitLab Wiki",
        "display_name": name,
        "fetch": {
            "gitlab_url": location.gitlab_url,
            "project_url": location.project_url,
            "token_env": gitlab_token_env(location.gitlab_url),
        },
        "link": generated_gitlab_wiki_link(
            location.project_url,
            location.gitlab_url,
        ),
        "summary": (
            ("対象", "プロジェクトWiki全ページ"),
            ("access token", "この端末に登録済み（GitLab Issueと共通）"),
            ("更新方式", "全ページを比較し、削除済みページも反映"),
            ("途中再開", "可能"),
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
            ("6", "GitLab Issue"),
            ("7", "GitLab Wiki"),
            ("8", "手元の資料を一度だけ取り込む（Other）"),
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
        "6": self._prompt_new_gitlab_issues_source,
        "7": self._prompt_new_gitlab_wiki_source,
        "8": self._prompt_new_other_source,
    }
    form = forms.get(choice)
    if form is None:
        self._invalid_selection("0～8")
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
        self._print_menu("確認", (("1", "保存して取り込みを開始"), ("0", "中止")))
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
            "1、または0" if specification["source_type"] == "other" else "0～2"
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
            self._print_source_result_failure(result, operation="Source登録後の初回処理")
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
