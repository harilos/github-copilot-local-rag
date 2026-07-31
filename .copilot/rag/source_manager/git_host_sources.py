from __future__ import annotations

import copy
import functools
import sys
from typing import Any

from . import git_source as _common
from .errors import SourceManagerError
from .git_host_runtime import install_git_host_runtime
from .git_host_urls import (
    GIT_SOURCE_TYPES,
    HOSTED_GIT_SOURCE_TYPES,
    LINK_STRATEGIES,
    SOURCE_LABELS,
    SOURCE_MENU_LABELS,
    derive_repository_web_url,
    make_repository_link,
    normalize_clone_url,
    normalize_repository_web_url,
    propose_repository_web_url,
)

_MANAGER_HOOK_MARKER = "_local_rag_git_host_manager_hook_installed"
_MANAGER_CLASS_MARKER = "_local_rag_git_host_manager_installed"
_LINK_PROVIDERS = {
    "github": "github",
    "gitlab": "gitlab",
    "azure-devops": "azure-devops",
}
_MISSING = object()


def install_git_host_source_runtime() -> None:
    """Install provider-specific Git Sources on the shared Git fetch engine."""

    from . import document_filter, execution, manager_connections, progress
    from . import providers, runner
    from . import store as store_module

    document_filter.FILE_SOURCE_TYPES = frozenset(
        set(document_filter.FILE_SOURCE_TYPES) | set(GIT_SOURCE_TYPES)
    )
    install_git_host_runtime(
        providers,
        runner,
        store_module,
        execution,
        progress,
    )
    _install_manager_hook(manager_connections)


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
    from . import document_filter

    module = sys.modules.get(manager_class.__module__)
    if module is not None:
        labels = getattr(module, "_PROVIDER_JA", None)
        if isinstance(labels, dict):
            labels.update(SOURCE_LABELS)
        module._GIT_PROVIDERS = (
            "github",
            "gitlab",
            "azure-devops",
        )
        strategies = getattr(module, "_GIT_STRATEGIES", None)
        if isinstance(strategies, dict):
            strategies["azure-devops"] = "azure-devops-item"

    original_ui_type = manager_class._ui_source_type
    original_menu = manager_class._print_menu
    original_show = manager_class._show_source_fetch_settings
    original_edit = manager_class._edit_source_fetch_settings
    original_failure = manager_class._source_failure_stage_label
    original_link_screen = manager_class._source_link_screen
    original_configure_link = manager_class._configure_source_link
    original_prompt_git_settings = (
        manager_class._prompt_git_repository_settings
    )

    @staticmethod
    def ui_source_type(value: Any) -> str:
        kind = str(value or "").strip().lower()
        return kind if kind in GIT_SOURCE_TYPES else original_ui_type(value)

    @functools.wraps(original_menu)
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
        return original_menu(
            self,
            title,
            tuple(updated),
            *args,
            **kwargs,
        )

    @functools.wraps(original_show)
    def show_source_fetch_settings(
        self: Any,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        kind = str(source.get("source_type") or "").strip().lower()
        if kind not in GIT_SOURCE_TYPES:
            return original_show(self, source)
        self.output(f"Gitサービス: {SOURCE_LABELS[kind]}")
        display = copy.deepcopy(source)
        display["source_type"] = "github"
        result = original_show(self, display)
        self.output(
            "検索結果リンク: "
            + (
                "ホスティング別に自動生成"
                if kind in HOSTED_GIT_SOURCE_TYPES
                else "自動生成なし（保存パスを表示）"
            )
        )
        return result

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        kind = str(source.get("source_type") or "").strip().lower()
        if kind not in GIT_SOURCE_TYPES:
            return original_edit(self, db_name, source)
        display = copy.deepcopy(source)
        display["source_type"] = "github"
        original_edit(self, db_name, display)
        if kind in HOSTED_GIT_SOURCE_TYPES and source.get("source_id"):
            self._print_info(
                "Webリンクの変更は、Sourceの"
                "「検索結果リンクを確認・変更する」から行います。"
            )

    @staticmethod
    def source_failure_stage_label(value: Any) -> str:
        stage = str(value or "").strip()
        if stage.startswith("fetch.github") or any(
            stage.startswith(f"fetch.{kind}")
            for kind in GIT_SOURCE_TYPES
        ):
            return "Gitリポジトリの取得"
        return original_failure(value)

    @functools.wraps(original_prompt_git_settings)
    def prompt_git_repository_settings(
        self: Any,
        provider: str,
        prior: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized = (
            "azure_devops"
            if str(provider or "").strip().lower() == "azure-devops"
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
                "ファイルURLを生成しません。"
                "検索結果にはRAG保存パスを表示します。"
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
                "「その他のGit」ではSource Linkを設定しません。"
            )
            return
        provider = _LINK_PROVIDERS.get(source_type)
        if provider is None:
            return original_configure_link(
                self,
                db_name,
                inventory,
                source_id,
            )

        previous_prompt = self.__dict__.get(
            "_prompt_source_link",
            _MISSING,
        )

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
                provider,
                dict(current.get("settings") or {}),
            )
            if settings is None:
                return None
            return {
                "display_name": display_name,
                "enabled": bool(current.get("enabled", True)),
                "provider": provider,
                "strategy": LINK_STRATEGIES[source_type],
                "settings": settings,
            }

        self._prompt_source_link = locked_prompt
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
                previous_prompt,
            )

    manager_class._ui_source_type = ui_source_type
    manager_class._print_menu = print_menu
    manager_class._show_source_fetch_settings = show_source_fetch_settings
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    manager_class._source_failure_stage_label = source_failure_stage_label
    manager_class._prompt_git_repository_settings = (
        prompt_git_repository_settings
    )
    manager_class._source_link_screen = source_link_screen
    manager_class._configure_source_link = configure_source_link
    manager_class._prompt_new_github_source = (
        document_filter._wrap_registration_form(
            prompt_new_git_host_source
        )
    )
    setattr(manager_class, _MANAGER_CLASS_MARKER, True)


def prompt_new_git_host_source(self: Any) -> dict[str, Any] | None:
    source_type = self._select_value(
        "Gitのサービスを選択してください",
        tuple(SOURCE_MENU_LABELS.items()),
        default="github",
    )
    if source_type is None:
        return None
    kind = str(source_type)
    self._print_info(f"選択したGitサービス: {SOURCE_LABELS[kind]}")
    specification = _common.prompt_new_git_source(self)
    if specification is None:
        return None
    clone_url = str(
        (specification.get("fetch") or {}).get("repository_url") or ""
    )
    try:
        clone_url = normalize_clone_url(kind, clone_url)
    except SourceManagerError as exc:
        self._print_error(str(exc))
        return None
    specification = copy.deepcopy(specification)
    specification["source_type"] = kind
    specification["label"] = f"{SOURCE_LABELS[kind]}リポジトリ"
    specification["fetch"]["repository_url"] = clone_url
    link: dict[str, Any] | None = None
    if kind in HOSTED_GIT_SOURCE_TYPES:
        proposed = propose_repository_web_url(kind, clone_url)
        web_url = self._prompt_preserving_value(
            "リポジトリのWeb URL",
            proposed,
            required=True,
            description=(
                "検索結果からファイルを開くためのリポジトリトップURLです。"
                "clone URLから推定した値を確認してください。"
            ),
            examples=self._examples(
                "gitlab_repository_web_url"
                if kind == "gitlab"
                else (
                    "azure_repository_web_url"
                    if kind == "azure-devops"
                    else "github_repository_web_url"
                )
            ),
        )
        if web_url is None:
            return None
        try:
            link = make_repository_link(kind, web_url, ref="HEAD")
        except SourceManagerError as exc:
            self._print_error(str(exc))
            return None
    specification["link"] = link
    specification["summary"] = (
        ("Gitサービス", SOURCE_LABELS[kind]),
        *tuple(specification.get("summary") or ()),
        (
            "Webリンク",
            "初回取得時に既定branchで自動設定"
            if link is not None
            else "自動生成なし（保存パスを表示）",
        ),
    )
    return specification


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
    "HOSTED_GIT_SOURCE_TYPES",
    "derive_repository_web_url",
    "install_git_host_source_runtime",
    "make_repository_link",
    "normalize_repository_web_url",
]
