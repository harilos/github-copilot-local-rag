from __future__ import annotations

import functools
import json
from typing import Any, Mapping

from .errors import SourceManagerError
from .gitlab_issue_fixes import parse_gitlab_api_project_web_url
from .gitlab_issues import gitlab_token_env, parse_gitlab_project
from .gitlab_wiki import generated_gitlab_wiki_link
from .gitlab_wiki_path_fix import install_gitlab_wiki_path_fix
from .machine_connections import (
    gitlab_project_location,
    has_stored_gitlab_token,
)

_HOOK_MARKER = "_local_rag_gitlab_wiki_registration_hook_installed"
_CLASS_MARKER = "_local_rag_gitlab_wiki_registration_installed"
_CONNECTION_MARKER = "_local_rag_gitlab_connection_identity_fix_installed"


def install_gitlab_wiki_registration_runtime() -> None:
    """Keep Wiki registration independent from the GitLab Issues API check."""

    from . import machine_connections, manager_connections

    install_gitlab_wiki_path_fix()
    _install_connection_identity_fix(machine_connections, manager_connections)
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


def _install_connection_identity_fix(
    machine_connections: Any,
    manager_connections: Any,
) -> None:
    if bool(getattr(machine_connections, _CONNECTION_MARKER, False)):
        return

    def check_gitlab_project(
        rag_root: str | Any,
        *,
        gitlab_url: Any,
        project_url: Any,
        token_env: str | None = None,
        environ: Mapping[str, str] | None = None,
        http_get: Any = None,
    ) -> Any:
        location = machine_connections.gitlab_project_location(
            gitlab_url,
            project_url,
        )
        environment = (
            machine_connections.os.environ
            if environ is None
            else environ
        )
        environment_name = str(
            token_env or gitlab_token_env(location.gitlab_url)
        ).strip()
        token = machine_connections.resolve_gitlab_token(
            rag_root,
            gitlab_url=location.gitlab_url,
            token_env=environment_name,
            environ=environment,
        )
        if not token:
            raise SourceManagerError(
                "GitLab access token is not registered on this computer"
            )
        getter = http_get or machine_connections._default_http_get
        headers = {
            "PRIVATE-TOKEN": token,
            "Accept": "application/json",
        }
        try:
            response = getter(location.project_api_url, headers, 10.0)
            status = int(response[0])
            body = bytes(response[1])
            if status != 200:
                raise SourceManagerError(
                    f"GitLab project connection check failed (HTTP {status})"
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SourceManagerError(
                    "GitLab project connection check returned invalid JSON"
                ) from exc
            if not isinstance(payload, Mapping):
                raise SourceManagerError(
                    "GitLab project connection check returned an invalid project"
                )
            project_id = payload.get("id")
            if (
                isinstance(project_id, bool)
                or not str(project_id).isdigit()
                or not 1 <= int(project_id) <= machine_connections._MAX_PROJECT_ID
            ):
                raise SourceManagerError(
                    "GitLab project connection check returned an invalid project ID"
                )
            returned = parse_gitlab_api_project_web_url(
                payload.get("web_url"),
                location.gitlab_url,
            )
            if returned.project_path != location.project_path:
                raise SourceManagerError(
                    "GitLab project connection check returned a different project"
                )
            issues_url = (
                f"{location.api_base_url}/projects/{int(project_id)}/issues"
                "?scope=all&state=all&per_page=1&page=1"
            )
            issues_response = getter(issues_url, headers, 10.0)
            issues_status = int(issues_response[0])
            issues_body = bytes(issues_response[1])
            if issues_status != 200:
                raise SourceManagerError(
                    "GitLab Issues API connection check failed "
                    f"(HTTP {issues_status})"
                )
            try:
                issues_payload = json.loads(issues_body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SourceManagerError(
                    "GitLab Issues API connection check returned invalid JSON"
                ) from exc
            if not isinstance(issues_payload, list):
                raise SourceManagerError(
                    "GitLab Issues API connection check returned an invalid response"
                )
            name = str(
                payload.get("name_with_namespace")
                or payload.get("name")
                or ""
            )
            return machine_connections.GitLabProjectCheck(
                location=location,
                project_id=int(project_id),
                name=name.strip() or location.project_path,
            )
        finally:
            token = ""
            headers["PRIVATE-TOKEN"] = ""

    machine_connections.check_gitlab_project = check_gitlab_project
    manager_connections.check_gitlab_project = check_gitlab_project
    setattr(machine_connections, _CONNECTION_MARKER, True)


def _install_manager_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _CLASS_MARKER, False)):
        return
    manager_class._prompt_new_gitlab_wiki_source = (
        prompt_new_gitlab_wiki_source
    )
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
