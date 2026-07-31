from __future__ import annotations

import copy
import functools
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError

GIT_SOURCE_TYPES = frozenset(
    {"github", "gitlab", "azure-devops", "other-git"}
)
_ADDITIONAL_GIT_SOURCE_TYPES = frozenset(
    GIT_SOURCE_TYPES - {"github"}
)
_SOURCE_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "azure-devops": "Azure DevOps",
    "other-git": "その他のGit",
}
_LINK_PROVIDERS = {
    "github": "github",
    "gitlab": "gitlab",
    "azure-devops": "azure_devops",
}
_PROVIDER_MARKER = "_local_rag_git_provider_types_provider_installed"
_EXECUTION_MARKER = "_local_rag_git_provider_types_execution_installed"
_RUNNER_MARKER = "_local_rag_git_provider_types_runner_installed"
_MANAGER_HOOK_MARKER = "_local_rag_git_provider_types_manager_hook_installed"
_MANAGER_CLASS_MARKER = "_local_rag_git_provider_types_manager_installed"


def install_git_provider_types_runtime() -> None:
    """Install distinct persisted Source types on the shared Git fetch engine."""

    from . import (
        document_filter,
        execution,
        manager_connections,
        progress,
        providers,
        runner,
    )
    from . import store as store_module

    document_filter.FILE_SOURCE_TYPES = frozenset(
        set(document_filter.FILE_SOURCE_TYPES) | set(GIT_SOURCE_TYPES)
    )
    _install_provider_contract(providers, runner, store_module)
    _install_execution_contract(execution, runner)
    _install_runner_contract(runner)
    progress._PROVIDER_LABELS.update(
        {
            "github": "GitHub",
            "gitlab": "GitLab",
            "azure-devops": "Azure DevOps",
            "other-git": "その他のGit",
        }
    )
    _install_manager_hook(manager_connections)


def _install_provider_contract(
    providers: Any,
    runner: Any,
    store_module: Any,
) -> None:
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
        if kind not in _ADDITIONAL_GIT_SOURCE_TYPES:
            return original_validate(kind, settings)
        # The generic Git validator, including file_selection support, is the
        # single authority for all hosted and unclassified Git repositories.
        return dict(original_validate("github", settings))

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
        if kind not in _ADDITIONAL_GIT_SOURCE_TYPES:
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
            "repository",
            "git_fetch",
            True,
            normalized_work,
            normalized,
            "repository_revision",
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
        set(providers.SUPPORTED_PROVIDERS) | set(GIT_SOURCE_TYPES)
    )
    providers.validate_provider_config = validate_provider_config
    providers.build_fetch_plan = build_fetch_plan
    runner.validate_provider_config = validate_provider_config
    runner.build_fetch_plan = build_fetch_plan
    store_module.validate_provider_config = validate_provider_config
    store_module.build_fetch_plan = build_fetch_plan
    setattr(providers, _PROVIDER_MARKER, True)


def _install_execution_contract(execution: Any, runner: Any) -> None:
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
        kind = str(plan.get("provider") or "").strip().lower()
        if kind not in _ADDITIONAL_GIT_SOURCE_TYPES:
            return dict(original(plan, work_directory, state, **kwargs))

        translated = copy.deepcopy(dict(plan))
        translated["provider"] = "github"
        callback = kwargs.get("progress_callback")
        if callback is not None:
            kwargs = dict(kwargs)
            kwargs["progress_callback"] = _provider_progress_proxy(
                callback,
                source_type=kind,
            )
        return dict(original(translated, work_directory, state, **kwargs))

    execution.execute_fetch_plan = execute_fetch_plan
    runner.execute_fetch_plan = execute_fetch_plan
    setattr(execution, _EXECUTION_MARKER, True)


def _provider_progress_proxy(callback: Any, *, source_type: str) -> Any:
    def emit(event: Mapping[str, Any]) -> None:
        value = dict(event)
        if str(value.get("provider") or "").strip().lower() == "github":
            value["provider"] = source_type
        callback(value)

    return emit


def _install_runner_contract(runner: Any) -> None:
    if bool(getattr(runner, _RUNNER_MARKER, False)):
        return
    original = runner.update_source

    @functools.wraps(original)
    def update_source(
        db_root: Path,
        local_source_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        store = runner.SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        source_type = str(
            source.payload.get("source_type") or ""
        ).strip().lower()
        if (
            source_type in _ADDITIONAL_GIT_SOURCE_TYPES
            and kwargs.get("executor") is None
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
        return dict(original(db_root, local_source_key, **kwargs))

    runner.update_source = update_source
    setattr(runner, _RUNNER_MARKER, True)
    package = sys.modules.get(__package__)
    if package is not None:
        setattr(package, "update_source", update_source)


def _install_manager_hook(manager_connections: Any) -> None:
    if bool(getattr(manager_connections, _MANAGER_HOOK_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_ui(manager_class)

    manager_connections.install_manager_connection_ui = (
        install_manager_connection_ui
    )
    setattr(manager_connections, _MANAGER_HOOK_MARKER, True)


def _install_manager_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _MANAGER_CLASS_MARKER, False)):
        return
    module = sys.modules.get(manager_class.__module__)
    if module is not None:
        provider_labels = getattr(module, "_PROVIDER_JA", None)
        if isinstance(provider_labels, dict):
            provider_labels.update(_SOURCE_LABELS)
        git_providers = tuple(getattr(module, "_GIT_PROVIDERS", ()))
        if "azure-devops" not in git_providers:
            module._GIT_PROVIDERS = (*git_providers, "azure-devops")
        git_strategies = getattr(module, "_GIT_STRATEGIES", None)
        if isinstance(git_strategies, dict):
            git_strategies["azure-devops"] = "azure-devops-item"

    original_print_menu = manager_class._print_menu
    original_ui_source_type = manager_class._ui_source_type
    original_prompt = manager_class._prompt_new_github_source
    original_show = manager_class._show_source_fetch_settings
    original_edit = manager_class._edit_source_fetch_settings
    original_link_screen = manager_class._source_link_screen
    original_configure_link = manager_class._configure_source_link
    original_prompt_git_settings = (
        manager_class._prompt_git_repository_settings
    )

    @functools.wraps(original_print_menu)
    def print_menu(
        self: Any,
        title: Any,
        options: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        updated = []
        for item in options:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                values = list(item)
                if str(values[1]) in {
                    "GitHubリポジトリ",
                    "Gitリポジトリ（GitHub・GitLab等）",
                }:
                    values[1] = "Gitリポジトリ"
                item = tuple(values) if isinstance(item, tuple) else values
            updated.append(item)
        return original_print_menu(
            self,
            title,
            tuple(updated),
            *args,
            **kwargs,
        )

    @staticmethod
    def ui_source_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in GIT_SOURCE_TYPES:
            return normalized
        return original_ui_source_type(value)

    @functools.wraps(original_prompt)
    def prompt_new_git_source(self: Any) -> dict[str, Any] | None:
        source_type = self._select_value(
            "Gitホスティングサービス",
            (
                ("github", "GitHub"),
                ("gitlab", "GitLab"),
                ("azure-devops", "Azure DevOps"),
                ("other-git", "その他のGitサーバー"),
            ),
            default="github",
        )
        if source_type is None:
            return None
        self._print_info(
            "取得処理は共通のGit clone／sparse checkoutを使用し、"
            "Source種別とURL生成方式だけを分けます。"
        )
        specification = original_prompt(self)
        if specification is None:
            return None
        value = copy.deepcopy(dict(specification))
        value["source_type"] = str(source_type)
        value["label"] = _SOURCE_LABELS[str(source_type)]
        value["summary"] = (
            ("Gitサービス", _SOURCE_LABELS[str(source_type)]),
            *tuple(value.get("summary") or ()),
        )
        return value

    @functools.wraps(original_show)
    def show_source_fetch_settings(
        self: Any,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        source_type = str(source.get("source_type") or "").strip().lower()
        if source_type not in _ADDITIONAL_GIT_SOURCE_TYPES:
            return original_show(self, source)
        alias = copy.deepcopy(source)
        alias["source_type"] = "github"
        fetch = original_show(self, alias)
        self.output(f"Gitサービス: {_SOURCE_LABELS[source_type]}")
        return fetch

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        source_type = str(source.get("source_type") or "").strip().lower()
        if source_type not in _ADDITIONAL_GIT_SOURCE_TYPES:
            return original_edit(self, db_name, source)
        self._print_info(
            f"Gitサービス: {_SOURCE_LABELS[source_type]} "
            "（Source種別は変更しません）"
        )
        alias = copy.deepcopy(source)
        alias["source_type"] = "github"
        return original_edit(self, db_name, alias)

    @functools.wraps(original_prompt_git_settings)
    def prompt_git_repository_settings(
        self: Any,
        provider: str,
        prior: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized = (
            "azure_devops"
            if str(provider).strip().lower() == "azure-devops"
            else provider
        )
        return original_prompt_git_settings(self, normalized, prior)

    @functools.wraps(original_link_screen)
    def source_link_screen(
        self: Any,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        source_type = _managed_source_type(self, db_name, source_id)
        if source_type == "other-git":
            self._print_screen_header(
                "検索結果リンク",
                db_name=db_name,
            )
            self._print_info(
                "「その他のGit」はWeb画面のURL規則を安全に特定できないため、"
                "ファイルURLを自動生成しません。検索結果にはRAG保存パスを表示します。"
            )
            return
        return original_link_screen(self, db_name, inventory, source_id)

    @functools.wraps(original_configure_link)
    def configure_source_link(
        self: Any,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        source_type = _managed_source_type(self, db_name, source_id)
        if source_type == "other-git":
            self._print_info(
                "「その他のGit」ではSource Linkを自動設定できません。"
            )
            return
        expected_provider = _LINK_PROVIDERS.get(source_type)
        if expected_provider is None:
            return original_configure_link(
                self,
                db_name,
                inventory,
                source_id,
            )

        prior_prompt = self.__dict__.get("_prompt_source_link", _MISSING)
        prior_save = self.__dict__.get("_save_sidecar", _MISSING)
        base_save = self._save_sidecar

        def locked_prompt(
            *,
            existing: dict[str, Any] | None = None,
        ) -> dict[str, Any] | None:
            current = dict(existing or {})
            display_name = self._prompt_preserving_value(
                "Source表示名",
                str(current.get("display_name") or ""),
                required=False,
                description=(
                    "Manager上でSourceを識別しやすくする表示専用の名前です。"
                    "Source IDとGitサービス種別は変更されません。"
                ),
                examples=self._examples("source_display_name"),
                empty_help="Source IDを表示",
            )
            if display_name is None:
                return None
            settings = self._prompt_git_repository_settings(
                expected_provider,
                dict(current.get("settings") or {}),
            )
            if settings is None:
                return None
            strategy = {
                "github": "github-blob",
                "gitlab": "gitlab-blob",
                "azure_devops": "azure-devops-item",
            }[expected_provider]
            return {
                "display_name": display_name,
                "enabled": bool(current.get("enabled", True)),
                "provider": expected_provider,
                "strategy": strategy,
                "settings": settings,
            }

        def save_sidecar(
            selected_db: str,
            selected_inventory: Any,
            source_links: Any,
            payload: dict[str, Any],
        ) -> bool:
            target = self._source_entry(
                payload,
                source_id,
                create=False,
            )
            if isinstance(target, dict):
                target["source_type"] = source_type
            return bool(
                base_save(
                    selected_db,
                    selected_inventory,
                    source_links,
                    payload,
                )
            )

        self._prompt_source_link = locked_prompt
        self._save_sidecar = save_sidecar
        try:
            return original_configure_link(
                self,
                db_name,
                inventory,
                source_id,
            )
        finally:
            _restore_instance_attribute(
                self,
                "_prompt_source_link",
                prior_prompt,
            )
            _restore_instance_attribute(
                self,
                "_save_sidecar",
                prior_save,
            )

    manager_class._print_menu = print_menu
    manager_class._ui_source_type = ui_source_type
    manager_class._prompt_new_github_source = prompt_new_git_source
    manager_class._show_source_fetch_settings = show_source_fetch_settings
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    manager_class._prompt_git_repository_settings = (
        prompt_git_repository_settings
    )
    manager_class._source_link_screen = source_link_screen
    manager_class._configure_source_link = configure_source_link
    setattr(manager_class, _MANAGER_CLASS_MARKER, True)


def _managed_source_type(
    manager: Any,
    db_name: str,
    source_id: str,
) -> str:
    try:
        values = manager._source_manager_records(db_name)
    except Exception:
        return ""
    for source in values:
        if str(source.get("source_id") or "") == str(source_id):
            return str(source.get("source_type") or "").strip().lower()
    return ""


_MISSING = object()


def _restore_instance_attribute(
    target: Any,
    name: str,
    previous: Any,
) -> None:
    if previous is _MISSING:
        target.__dict__.pop(name, None)
    else:
        target.__dict__[name] = previous


__all__ = [
    "GIT_SOURCE_TYPES",
    "install_git_provider_types_runtime",
]
