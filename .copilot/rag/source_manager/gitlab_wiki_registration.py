from __future__ import annotations

import functools
from typing import Any

from .gitlab_issues import gitlab_token_env, parse_gitlab_project
from .gitlab_wiki import generated_gitlab_wiki_link
from .machine_connections import (
    gitlab_project_location,
    has_stored_gitlab_token,
)

_HOOK_MARKER = "_local_rag_gitlab_wiki_registration_hook_installed"
_CLASS_MARKER = "_local_rag_gitlab_wiki_registration_installed"


def install_gitlab_wiki_registration_runtime() -> None:
    """Keep Wiki registration independent from the GitLab Issues API check."""

    from . import manager_connections

    if bool(getattr(manager_connections, _HOOK_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_ui(manager_class)

    manager_connections.install_manager_connection_ui = install_manager_connection_ui
    setattr(manager_connections, _HOOK_MARKER, True)


def _install_manager_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _CLASS_MARKER, False)):
        return
    manager_class._prompt_new_gitlab_wiki_source = prompt_new_gitlab_wiki_source
    setattr(manager_class, _CLASS_MARKER, True)


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
        parsed = parse_gitlab_project(project_url, gitlab_url)
        location = gitlab_project_location(
            parsed.gitlab_url,
            parsed.project_url,
        )
    except Exception as exc:
        self._print_internal_diagnostic(
            exc,
            operation="GitLab Wiki接続先の確認",
            stage="machine_connections.gitlab_wiki.validate",
        )
        return None
    if not has_stored_gitlab_token(self.rag_root, location.gitlab_url):
        self._print_info(
            "このGitLabのaccess tokenが未登録のため、"
            "共通のSource接続設定を開きます。"
        )
        if not self._source_connection_settings_screen(
            required="gitlab",
            gitlab_url=location.gitlab_url,
        ):
            return None
    name = self._prompt_preserving_value(
        "Sourceの名前",
        "",
        required=True,
        examples=self._examples("gitlab_source_display_name"),
    )
    if name is None:
        return None
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
            (
                "access token",
                "この端末に登録済み（GitLab Issueと共通）",
            ),
            ("接続確認", "Wiki APIは取得開始時に確認"),
            ("更新方式", "全ページを比較し、削除済みページも反映"),
            ("途中再開", "可能"),
        ),
    }
