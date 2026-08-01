from __future__ import annotations

import functools
import sys
from typing import Any

from .github_content import (
    generated_github_issues_link,
    generated_github_wiki_link,
    parse_github_repository_url,
)


_HOOK_MARKER = "_local_rag_github_content_registration_hook_installed"
_CLASS_MARKER = "_local_rag_github_content_registration_installed"


def install_github_content_registration_runtime() -> None:
    from . import manager_connections

    if bool(getattr(manager_connections, _HOOK_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_ui(manager_class)

    manager_connections.install_manager_connection_ui = (
        install_manager_connection_ui
    )
    setattr(manager_connections, _HOOK_MARKER, True)


def _install_manager_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _CLASS_MARKER, False)):
        return
    manager_class._prompt_new_github_issues_source = (
        prompt_new_github_issues_source
    )
    manager_class._prompt_new_github_wiki_source = prompt_new_github_wiki_source
    original_ui_source_type = manager_class._ui_source_type

    def ui_source_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"github_issues", "github_wiki"}:
            return normalized
        return original_ui_source_type(value)

    manager_class._ui_source_type = staticmethod(ui_source_type)
    module = sys.modules.get(manager_class.__module__)
    if module is not None and hasattr(module, "_PROVIDER_JA"):
        module._PROVIDER_JA["github_issues"] = "GitHub Issues"
        module._PROVIDER_JA["github_wiki"] = "GitHub Wiki"
    setattr(manager_class, _CLASS_MARKER, True)


def prompt_new_github_issues_source(self: Any) -> dict[str, Any] | None:
    repository_url = self._prompt_preserving_value(
        "GitHubリポジトリのURL",
        "",
        required=True,
        description="Issueを取得するGitHubリポジトリのトップURLです。",
        examples=self._examples("github_repository_web_url"),
    )
    if repository_url is None:
        return None
    try:
        repository = parse_github_repository_url(repository_url)
    except Exception as exc:
        self._print_internal_diagnostic(
            exc,
            operation="GitHub Issues接続先の確認",
            stage="machine_connections.github_issues.validate",
        )
        return None
    name = self._prompt_preserving_value(
        "Sourceの名前",
        f"{repository.name} Issues",
        required=True,
        examples=self._examples("github_source_display_name"),
    )
    if name is None:
        return None
    return {
        "source_type": "github_issues",
        "label": "GitHub Issues",
        "display_name": name,
        "fetch": {
            "repository_url": repository.repository_url,
            "state": "all",
            "include_comments": True,
        },
        "link": generated_github_issues_link(repository.repository_url),
        "summary": (
            ("対象", "Issue本文とコメント"),
            ("認証", "gh CLIのログインを使用"),
            ("更新方式", "全Issueを同期"),
        ),
    }


def prompt_new_github_wiki_source(self: Any) -> dict[str, Any] | None:
    repository_url = self._prompt_preserving_value(
        "GitHubリポジトリのURL",
        "",
        required=True,
        description="Wikiを取得するGitHubリポジトリのトップURLです。",
        examples=self._examples("github_repository_web_url"),
    )
    if repository_url is None:
        return None
    try:
        repository = parse_github_repository_url(repository_url)
    except Exception as exc:
        self._print_internal_diagnostic(
            exc,
            operation="GitHub Wiki接続先の確認",
            stage="machine_connections.github_wiki.validate",
        )
        return None
    name = self._prompt_preserving_value(
        "Sourceの名前",
        f"{repository.name} Wiki",
        required=True,
        examples=self._examples("github_source_display_name"),
    )
    if name is None:
        return None
    return {
        "source_type": "github_wiki",
        "label": "GitHub Wiki",
        "display_name": name,
        "fetch": {"repository_url": repository.repository_url},
        "link": generated_github_wiki_link(repository.repository_url),
        "summary": (
            ("対象", "Wiki全ページ"),
            ("認証", "Git HTTPS認証を使用"),
            ("更新方式", "Wiki Gitリポジトリを同期"),
        ),
    }
