from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from .errors import SourceManagerError
from . import gitlab_issues as _issues

_MARKER = "_local_rag_gitlab_issue_fixes_installed"


def parse_gitlab_api_project_web_url(web_url: Any, gitlab_url: Any) -> _issues.GitLabProject:
    """Validate API project identity by path while retaining configured origin."""
    response = _issues._canonical_web_root(web_url, field="GitLab project web_url")
    configured = _issues._canonical_web_root(gitlab_url, field="gitlab_url")
    response_split = urlsplit(response)
    configured_split = urlsplit(configured)
    identity_url = urlunsplit(
        (
            configured_split.scheme,
            configured_split.netloc,
            response_split.path,
            "",
            "",
        )
    )
    return _issues.parse_gitlab_project(identity_url, configured)


def _fetch_project_identity(
    project: _issues.GitLabProject,
    request: _issues.HttpRequest,
    headers: Mapping[str, str],
) -> _issues.GitLabProject:
    status, body, _response_headers = request(project.project_api_url, headers)
    if status != 200:
        raise _issues._gitlab_http_status_error(
            "GitLab project request failed", status
        )
    payload = _issues._decode_json(body, field="GitLab project")
    if not isinstance(payload, Mapping):
        raise SourceManagerError(
            "GitLab project response must be an object",
            stage="fetch.gitlab_issues",
        )
    project_id = _issues._positive_integer(
        payload.get("id"), field="GitLab project ID"
    )
    if project.project_id is not None and project_id != project.project_id:
        raise SourceManagerError(
            "GitLab project response has the wrong identity",
            stage="fetch.gitlab_issues",
        )
    verified = parse_gitlab_api_project_web_url(
        payload.get("web_url"), project.gitlab_url
    )
    response_path = str(payload.get("path_with_namespace") or "").strip()
    if (
        not response_path
        or response_path.casefold() != project.project_path.casefold()
        or verified.project_path.casefold() != project.project_path.casefold()
    ):
        raise SourceManagerError(
            "GitLab project response has the wrong path identity",
            stage="fetch.gitlab_issues",
        )
    return _issues.GitLabProject(
        gitlab_url=project.gitlab_url,
        api_base_url=project.api_base_url,
        project_url=project.project_url,
        project_path=project.project_path,
        project_id=project_id,
    )


def _emit_discussion_pagination_fallback(
    progress_callback: _issues.ProgressCallback | None,
    *,
    issue_iid: int,
    page: int,
    completed: int,
    expected_total: int | None,
    reason: str,
) -> None:
    """Report header drift without rejecting valid discussions already collected."""

    _issues._emit_progress(
        progress_callback,
        {
            "event": "provider.pagination_fallback",
            "provider": "gitlab_issues",
            "phase": "gitlab_issues.discussions",
            "label_ja": (
                "GitLab Issueコメント取得"
                "（ページング情報不整合・取得済み分で継続）"
            ),
            "completed": max(0, int(completed)),
            "total": expected_total,
            "unit": "スレッド",
            "total_kind": (
                "exact" if expected_total is not None else "unknown"
            ),
            "current_item": f"Issue #{int(issue_iid)} page={int(page)}",
            "status": "warning",
            "warning": "gitlab_discussions_pagination_inconsistent",
            "reason": str(reason),
            "checkpoint_saved": False,
        },
    )


def _fetch_discussions(
    project: _issues.GitLabProject,
    issue_iid: int,
    request: _issues.HttpRequest,
    headers: Mapping[str, str],
    *,
    progress_callback: _issues.ProgressCallback | None,
) -> list[Mapping[str, Any]]:
    page = 1
    values: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    expected_total: int | None = None
    while page <= _issues._MAX_PAGES:
        query = urlencode({"per_page": 100, "page": page})
        status, body, response_headers = request(
            f"{project.discussions_api_url(issue_iid)}?{query}", headers
        )
        if status != 200:
            raise _issues._gitlab_http_status_error(
                "GitLab Issue discussions request failed", status
            )
        payload = _issues._decode_json(body, field="GitLab Issue discussions")
        if not isinstance(payload, list):
            raise SourceManagerError(
                "GitLab Issue discussions response must be an array",
                stage="fetch.gitlab_issues",
            )
        for raw in payload:
            if not isinstance(raw, Mapping):
                raise SourceManagerError(
                    "GitLab Issue discussions contain an invalid item",
                    stage="fetch.gitlab_issues",
                )
            discussion_id = str(raw.get("id") or "").strip()
            notes = raw.get("notes")
            if (
                not discussion_id
                or discussion_id in seen
                or not isinstance(notes, list)
                or any(not isinstance(note, Mapping) for note in notes)
            ):
                raise SourceManagerError(
                    "GitLab Issue discussion schema is invalid",
                    stage="fetch.gitlab_issues",
                )
            seen.add(discussion_id)
            values.append(dict(raw))

        fallback_reason: str | None = None
        total_text = _issues._header_value(response_headers, "X-Total")
        if total_text:
            if not total_text.isdigit():
                fallback_reason = "invalid_total"
            else:
                total = int(total_text)
                if expected_total is None:
                    expected_total = total
                elif expected_total != total:
                    fallback_reason = "total_changed"

        _issues._emit_progress(
            progress_callback,
            {
                "event": "provider.page",
                "provider": "gitlab_issues",
                "phase": "gitlab_issues.discussions",
                "label_ja": "GitLab Issueコメント取得",
                "completed": len(values),
                "total": expected_total,
                "unit": "スレッド",
                "total_kind": (
                    "exact" if expected_total is not None else "unknown"
                ),
                "current_item": f"Issue #{issue_iid} page={page}",
                "status": "running",
            },
        )

        next_page = _issues._header_value(response_headers, "X-Next-Page")
        if fallback_reason is None and expected_total is not None:
            if len(values) > expected_total:
                fallback_reason = "collected_exceeds_total"
            elif len(values) == expected_total and next_page:
                fallback_reason = "next_page_after_total"

        if fallback_reason is not None:
            _emit_discussion_pagination_fallback(
                progress_callback,
                issue_iid=issue_iid,
                page=page,
                completed=len(values),
                expected_total=expected_total,
                reason=fallback_reason,
            )
            break

        if next_page:
            if (
                not next_page.isdigit()
                or int(next_page) <= page
                or int(next_page) > _issues._MAX_PAGES
            ):
                _emit_discussion_pagination_fallback(
                    progress_callback,
                    issue_iid=issue_iid,
                    page=page,
                    completed=len(values),
                    expected_total=expected_total,
                    reason="invalid_next_page",
                )
                break
            if not payload:
                _emit_discussion_pagination_fallback(
                    progress_callback,
                    issue_iid=issue_iid,
                    page=page,
                    completed=len(values),
                    expected_total=expected_total,
                    reason="empty_page_with_next_page",
                )
                break
            page = int(next_page)
            continue

        if expected_total is not None:
            if len(values) != expected_total:
                _emit_discussion_pagination_fallback(
                    progress_callback,
                    issue_iid=issue_iid,
                    page=page,
                    completed=len(values),
                    expected_total=expected_total,
                    reason="no_next_page_before_total",
                )
            break
        if len(payload) < 100:
            break
        page += 1
    else:
        _emit_discussion_pagination_fallback(
            progress_callback,
            issue_iid=issue_iid,
            page=_issues._MAX_PAGES,
            completed=len(values),
            expected_total=expected_total,
            reason="safety_limit_reached",
        )
    return values


def install_gitlab_issue_fixes() -> None:
    if bool(getattr(_issues, _MARKER, False)):
        return
    _issues.parse_gitlab_api_project_web_url = parse_gitlab_api_project_web_url
    _issues._fetch_project_identity = _fetch_project_identity
    _issues._fetch_discussions = _fetch_discussions
    setattr(_issues, _MARKER, True)


install_gitlab_issue_fixes()
