from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from .errors import SourceManagerError
from .security import validate_web_url


GITLAB_ISSUES_CUTOFF_STATE_KEY = "gitlab_issues_updated_after"
GITLAB_ISSUE_IDS_STATE_KEY = "gitlab_issue_iids"
GITLAB_PROJECT_ID_STATE_KEY = "gitlab_project_id"
GITLAB_ISSUES_BATCH_SIZE = 5
_AUTO_ISSUE_PATTERN = r"^issues/(?P<issue_iid>[0-9]+)\.md$"
_LOCAL_METADATA = re.compile(
    r"(?m)^<!-- local-rag-gitlab-issue: (\{[^\r\n]+\}) -->$"
)
_MAX_PAGES = 100_000
_MAX_PROJECT_ID = 9_223_372_036_854_775_807
_GITLAB_ENV_PREFIX = "LOCAL_RAG_GITLAB_TOKEN_"
_WINDOWS_FILE_RETRY_SECONDS = 2.0
_WINDOWS_TRANSIENT_FILE_ERRORS = frozenset({5, 32, 33})


HttpRequest = Callable[
    [str, Mapping[str, str]],
    tuple[int, bytes, Mapping[str, str]],
]
ProgressCallback = Callable[[Mapping[str, Any]], None]
ItemCallback = Callable[[int, int], None]
InventorySnapshotCallback = Callable[[int, list[int]], None]


@dataclass(frozen=True)
class GitLabProject:
    gitlab_url: str
    api_base_url: str
    project_url: str
    project_path: str
    project_id: int | None = None

    @property
    def api_project_reference(self) -> str:
        if self.project_id is not None:
            return str(self.project_id)
        return quote(self.project_path, safe="")

    @property
    def issues_api_url(self) -> str:
        return (
            f"{self.api_base_url}/projects/"
            f"{self.api_project_reference}/issues"
        )

    @property
    def project_api_url(self) -> str:
        return (
            f"{self.api_base_url}/projects/"
            f"{self.api_project_reference}"
        )

    def issue_api_url(self, issue_iid: int) -> str:
        return f"{self.issues_api_url}/{int(issue_iid)}"

    def discussions_api_url(self, issue_iid: int) -> str:
        return f"{self.issue_api_url(issue_iid)}/discussions"

    @property
    def issue_link_template(self) -> str:
        return f"{self.project_url}/-/issues/{{issue_iid}}"


@dataclass(frozen=True)
class GitLabIssueInventoryItem:
    iid: int
    issue_id: int
    updated_at: datetime
    updated_at_text: str
    user_notes_count: int


def gitlab_connection_id(gitlab_url: Any) -> str:
    """Return the machine-connection identity for one GitLab installation."""

    instance = _canonical_web_root(gitlab_url, field="gitlab_url")
    digest = hashlib.sha256(instance.encode("utf-8")).hexdigest()
    return f"gitlab-{digest[:20]}"


def gitlab_token_env(gitlab_url: Any) -> str:
    """Return the only environment name allowed for this GitLab token."""

    suffix = gitlab_connection_id(gitlab_url).removeprefix("gitlab-").upper()
    return f"{_GITLAB_ENV_PREFIX}{suffix}"


def parse_gitlab_project(
    project_url: Any,
    gitlab_url: Any,
) -> GitLabProject:
    """Parse a GitLab installation root and one project below that root."""

    project_text = _canonical_web_root(project_url, field="project_url")
    gitlab_text = _canonical_web_root(gitlab_url, field="gitlab_url")
    project_split = urlsplit(project_text)
    gitlab_split = urlsplit(gitlab_text)
    if (
        project_split.scheme.casefold() != gitlab_split.scheme.casefold()
        or project_split.netloc.casefold() != gitlab_split.netloc.casefold()
    ):
        raise SourceManagerError(
            "project_url and gitlab_url must use the same origin"
        )
    mount_path = gitlab_split.path.rstrip("/")
    if mount_path.casefold().endswith("/api/v4"):
        raise SourceManagerError(
            "gitlab_url must identify the GitLab top page, not /api/v4"
        )
    project_path = project_split.path.rstrip("/")
    prefix = f"{mount_path}/" if mount_path else "/"
    if not project_path.startswith(prefix) or project_path == mount_path:
        raise SourceManagerError(
            "project_url must be below gitlab_url"
        )
    raw_components = project_path[len(prefix) :].split("/")
    if len(raw_components) < 2:
        raise SourceManagerError(
            "project_url must identify a GitLab project top page"
        )
    decoded_components: list[str] = []
    for raw_component in raw_components:
        try:
            component = unquote(raw_component, errors="strict")
        except UnicodeError as exc:
            raise SourceManagerError(
                "project_url path is invalid"
            ) from exc
        if (
            not component
            or component in {".", ".."}
            or "/" in component
            or "\\" in component
            or any(ord(character) < 0x20 for character in component)
        ):
            raise SourceManagerError(
                "project_url must identify a GitLab project top page"
            )
        decoded_components.append(component)
    relative_path = "/".join(decoded_components)
    if (
        relative_path.casefold().endswith(".git")
        or "/-/" in f"/{relative_path}/"
    ):
        raise SourceManagerError(
            "project_url must identify a GitLab project top page"
        )
    encoded_relative = "/".join(
        quote(component, safe="-._~")
        for component in relative_path.split("/")
    )
    normalized_path = (
        f"{mount_path}/{encoded_relative}"
        if mount_path
        else f"/{encoded_relative}"
    )
    normalized_project_url = urlunsplit(
        (
            project_split.scheme.casefold(),
            gitlab_split.netloc,
            normalized_path,
            "",
            "",
        )
    )
    return GitLabProject(
        gitlab_url=gitlab_text,
        api_base_url=f"{gitlab_text}/api/v4",
        project_url=normalized_project_url,
        project_path=relative_path,
    )


def _validate_gitlab_api_project_identity(
    payload: Mapping[str, Any],
    expected_project_path: str,
    *,
    error_message: str,
    stage: str | None = None,
) -> None:
    """Validate one authenticated project response without trusting its origin."""

    _canonical_web_root(
        payload.get("web_url"),
        field="GitLab project web_url",
    )
    response_path = payload.get("path_with_namespace")
    if not isinstance(response_path, str):
        raise SourceManagerError(error_message, stage=stage)
    normalized_path = response_path.strip()
    if (
        not normalized_path
        or normalized_path.casefold() != expected_project_path.casefold()
    ):
        raise SourceManagerError(error_message, stage=stage)


def gitlab_issues_updated_after(
    updated_within_days: Any,
    state: Mapping[str, Any] | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> str | None:
    """Return one resume-stable GitLab `updated_after` timestamp."""

    if updated_within_days is None:
        return None
    if (
        isinstance(updated_within_days, bool)
        or not str(updated_within_days).isdigit()
        or not 1 <= int(updated_within_days) <= 3650
    ):
        raise SourceManagerError(
            "updated_within_days must be null or between 1 and 3650"
        )
    payload = state if isinstance(state, Mapping) else {}
    saved = payload.get(GITLAB_ISSUES_CUTOFF_STATE_KEY)
    if saved is not None:
        text = str(saved)
        parsed = _parse_timestamp(text, field="GitLab cutoff state")
        return _format_timestamp(parsed)
    anchor = _state_start_time(payload)
    if anchor is None:
        anchor = (clock or _utc_now)()
    if not isinstance(anchor, datetime):
        raise SourceManagerError("GitLab clock must return a datetime")
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return _format_timestamp(
        anchor.astimezone(timezone.utc)
        - timedelta(days=int(updated_within_days))
    )


def generated_gitlab_issues_link(
    project_url: Any,
    gitlab_url: Any,
) -> dict[str, Any]:
    project = parse_gitlab_project(project_url, gitlab_url)
    return {
        "enabled": True,
        "strategy": "regex-template",
        "settings": {
            "path_pattern": _AUTO_ISSUE_PATTERN,
            "url_template": project.issue_link_template,
        },
    }


def repair_generated_gitlab_issues_link(
    project_url: Any,
    gitlab_url: Any,
    link: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Refresh only the canonical Link shape generated for GitLab Issues."""

    if not isinstance(link, Mapping):
        return None
    value = copy.deepcopy(dict(link))
    if (
        value.get("enabled") is not True
        or value.get("strategy") != "regex-template"
        or set(value) != {"enabled", "strategy", "settings"}
        or not isinstance(value.get("settings"), Mapping)
    ):
        return value
    settings = dict(value["settings"])
    template = str(settings.get("url_template") or "")
    suffix = "/-/issues/{issue_iid}"
    if (
        set(settings) != {"path_pattern", "url_template"}
        or settings.get("path_pattern") != _AUTO_ISSUE_PATTERN
        or not template.endswith(suffix)
    ):
        return value
    try:
        parse_gitlab_project(template[: -len(suffix)], gitlab_url)
    except SourceManagerError:
        return value
    return generated_gitlab_issues_link(project_url, gitlab_url)


def fetch_gitlab_issues(
    settings: Mapping[str, Any],
    work: Path,
    request: HttpRequest,
    environment: Mapping[str, str],
    *,
    item_callback: ItemCallback | None,
    batch_callback: ItemCallback | None,
    resume_count: int,
    stable_issue_ids: list[int] | None,
    stable_project_id: int | None,
    inventory_snapshot_callback: InventorySnapshotCallback | None,
    updated_after: str | None,
    progress_callback: ProgressCallback | None,
    no_change_callback: ItemCallback | None = None,
) -> dict[str, Any]:
    """Fetch Issue details and discussions serially, five Issues per ADD batch."""

    project = parse_gitlab_project(
        settings.get("project_url"),
        settings.get("gitlab_url"),
    )
    expected_token_env = gitlab_token_env(project.gitlab_url)
    if str(settings.get("token_env") or "").strip() != expected_token_env:
        raise SourceManagerError(
            "GitLab token environment does not match the GitLab URL",
            stage="fetch.gitlab_issues",
        )
    token = str(environment.get(expected_token_env) or "").strip()
    if not token:
        raise SourceManagerError(
            "GitLab access token environment is unavailable",
            stage="fetch.gitlab_issues",
        )
    headers = {
        "PRIVATE-TOKEN": token,
        "Accept": "application/json",
    }
    project = _fetch_project_identity(project, request, headers)
    if stable_project_id is not None:
        expected_project_id = _positive_integer(
            stable_project_id,
            field="GitLab resume project ID",
        )
        if project.project_id != expected_project_id:
            raise SourceManagerError(
                "GitLab resume project identity does not match",
                stage="fetch.gitlab_issues",
            )
    elif stable_issue_ids is not None:
        raise SourceManagerError(
            "GitLab resume project identity is missing",
            stage="fetch.gitlab_issues",
        )

    issues_directory = Path(work) / "issues"
    issues_directory.mkdir(parents=True, exist_ok=True)
    if issues_directory.is_symlink() or not issues_directory.is_dir():
        raise SourceManagerError(
            "GitLab Issue work directory is unsafe",
            stage="fetch.gitlab_issues",
        )

    inventory_count: int | None = None
    if stable_issue_ids is None:
        inventory = _fetch_inventory(
            project,
            request,
            headers,
            updated_after=updated_after,
            progress_callback=progress_callback,
        )
        inventory_count = len(inventory)
        changed = _changed_issue_iids(
            inventory,
            issues_directory,
            updated_after=updated_after,
        )
        # Freeze the update queue in one state-file compare-and-swap.
        if inventory_snapshot_callback is not None:
            inventory_snapshot_callback(
                _positive_integer(
                    project.project_id,
                    field="GitLab project ID",
                ),
                list(changed),
            )
    else:
        changed = _validated_issue_ids(
            stable_issue_ids,
            field="GitLab resume Issue list",
        )
    if resume_count < 0 or resume_count > len(changed):
        raise SourceManagerError(
            "GitLab resume checkpoint is invalid",
            stage="fetch.gitlab_issues",
        )

    written = 0
    unavailable = 0
    unreflected_writes = 0
    for position, issue_iid in enumerate(changed, start=1):
        if position <= resume_count:
            continue
        try:
            issue = _fetch_json_object(
                request,
                project.issue_api_url(issue_iid),
                headers,
                identity_field="iid",
                expected_identity=issue_iid,
                expected_project_id=project.project_id,
            )
        except SourceManagerError as exc:
            diagnostic = getattr(exc, "diagnostic", None)
            if (
                isinstance(diagnostic, Mapping)
                and int(diagnostic.get("status") or 0) == 404
            ):
                # Keep any previously indexed Markdown. Source-side deletion
                # or a temporary loss of visibility is not propagated into
                # the RAG database.
                unavailable += 1
                issue = None
            else:
                raise
        if issue is not None:
            # A discussions failure must leave the previous complete Markdown
            # byte-for-byte intact. A 404 from the Issue detail also preserves
            # any historical Markdown already collected for that Issue.
            discussions = _fetch_discussions(
                project,
                issue_iid,
                request,
                headers,
                progress_callback=progress_callback,
            )
            _atomic_write_text(
                issues_directory / f"{issue_iid}.md",
                gitlab_issue_markdown(project, issue, discussions),
            )
            written += 1
            unreflected_writes += 1
        if item_callback is not None:
            item_callback(position, issue_iid)
        if (
            issue is None
            and unreflected_writes == 0
            and no_change_callback is not None
        ):
            no_change_callback(position, issue_iid)
        if (
            batch_callback is not None
            and unreflected_writes >= GITLAB_ISSUES_BATCH_SIZE
        ):
            batch_callback(position, issue_iid)
            unreflected_writes = 0
    if batch_callback is not None and unreflected_writes > 0:
        batch_callback(len(changed), changed[-1])

    return {
        "status": "ok",
        "documents": len(changed),
        "inventory_documents": inventory_count,
        "local_documents": len(tuple(issues_directory.glob("*.md"))),
        "fetched_this_run": written,
        "unavailable_this_run": unavailable,
        "project_url": project.project_url,
        "last_completed_item": changed[-1] if changed else None,
    }


def gitlab_issue_markdown(
    project: GitLabProject,
    issue: Mapping[str, Any],
    discussions: list[Mapping[str, Any]],
) -> str:
    iid = _positive_integer(issue.get("iid"), field="GitLab Issue IID")
    issue_id = _positive_integer(issue.get("id"), field="GitLab Issue ID")
    title = str(issue.get("title") or "").strip()
    description = str(issue.get("description") or "").strip()
    updated_at = _format_timestamp(
        _parse_timestamp(issue.get("updated_at"), field="GitLab updated_at")
    )
    notes_count = _non_negative_integer(
        issue.get("user_notes_count") or 0,
        field="GitLab user_notes_count",
    )
    marker = json.dumps(
        {
            "iid": iid,
            "issue_id": issue_id,
            "updated_at": updated_at,
            "user_notes_count": notes_count,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    state = str(issue.get("state") or "")
    author = _user_label(issue.get("author"))
    assignees = ", ".join(
        _user_label(value)
        for value in issue.get("assignees") or []
        if isinstance(value, Mapping)
    )
    labels = ", ".join(str(value) for value in issue.get("labels") or [])
    web_url = project.issue_link_template.format(issue_iid=iid)
    lines = [
        f"# GitLab Issue #{iid}: {title}",
        "",
        f"<!-- local-rag-gitlab-issue: {marker} -->",
        "",
        f"- 状態: {state or '不明'}",
        f"- 作成者: {author or '不明'}",
        f"- 担当者: {assignees or 'なし'}",
        f"- ラベル: {labels or 'なし'}",
        f"- 作成日時: {str(issue.get('created_at') or '不明')}",
        f"- 更新日時: {updated_at}",
        f"- URL: {web_url}",
        "",
        "## 本文",
        "",
        description or "（本文なし）",
        "",
        "## コメント・履歴",
        "",
    ]
    notes = _flatten_notes(discussions)
    if not notes:
        lines.append("（コメント・履歴なし）")
    else:
        for note in notes:
            created_at = str(note.get("created_at") or "日時不明")
            author_label = _user_label(note.get("author")) or "不明"
            system = "／システム履歴" if bool(note.get("system")) else ""
            lines.extend(
                [
                    f"### {created_at} — {author_label}{system}",
                    "",
                    str(note.get("body") or "").strip() or "（本文なし）",
                    "",
                ]
            )
            attachment = note.get("attachment")
            if isinstance(attachment, Mapping):
                filename = str(
                    attachment.get("filename")
                    or attachment.get("name")
                    or "添付"
                )
                url = str(attachment.get("url") or "").strip()
                lines.append(
                    f"- 添付: [{filename}]({url})" if url else f"- 添付: {filename}"
                )
                lines.append("")
            elif isinstance(attachment, str) and attachment.strip():
                lines.append(f"- 添付: {attachment.strip()}")
                lines.append("")
    structured = json.dumps(
        {
            "issue": _without_project_id(issue),
            "discussions": _without_project_id(discussions),
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    lines.extend(
        [
            "## Structured GitLab metadata",
            "",
            "```json",
            structured,
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _fetch_inventory(
    project: GitLabProject,
    request: HttpRequest,
    headers: Mapping[str, str],
    *,
    updated_after: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> list[GitLabIssueInventoryItem]:
    page = 1
    expected_total: int | None = None
    values: list[GitLabIssueInventoryItem] = []
    seen: set[int] = set()
    while page <= _MAX_PAGES:
        query_values: dict[str, Any] = {
            "scope": "all",
            "state": "all",
            "order_by": "created_at",
            "sort": "asc",
            "per_page": 100,
            "page": page,
        }
        if updated_after is not None:
            query_values["updated_after"] = updated_after
        query = urlencode(query_values)
        status, body, response_headers = request(
            f"{project.issues_api_url}?{query}",
            headers,
        )
        if status != 200:
            raise _gitlab_http_status_error(
                "GitLab Issue inventory request failed",
                status,
            )
        payload = _decode_json(body, field="GitLab Issue inventory")
        if not isinstance(payload, list):
            raise SourceManagerError(
                "GitLab Issue inventory response must be an array",
                stage="fetch.gitlab_issues",
            )
        page_values: list[GitLabIssueInventoryItem] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                raise SourceManagerError(
                    "GitLab Issue inventory contains an invalid item",
                    stage="fetch.gitlab_issues",
                )
            iid = _positive_integer(raw.get("iid"), field="GitLab Issue IID")
            if (
                _positive_integer(
                    raw.get("project_id"),
                    field="GitLab Issue project_id",
                )
                != project.project_id
            ):
                raise SourceManagerError(
                    "GitLab Issue inventory has the wrong project identity",
                    stage="fetch.gitlab_issues",
                )
            if iid in seen:
                raise SourceManagerError(
                    "gitlab_inventory_changed",
                    stage="fetch.gitlab_issues",
                )
            seen.add(iid)
            timestamp = _parse_timestamp(
                raw.get("updated_at"),
                field="GitLab Issue updated_at",
            )
            page_values.append(
                GitLabIssueInventoryItem(
                    iid=iid,
                    issue_id=_positive_integer(
                        raw.get("id"),
                        field="GitLab Issue ID",
                    ),
                    updated_at=timestamp,
                    updated_at_text=_format_timestamp(timestamp),
                    user_notes_count=_non_negative_integer(
                        raw.get("user_notes_count") or 0,
                        field="GitLab user_notes_count",
                    ),
                )
            )
        values.extend(page_values)
        total_text = _header_value(response_headers, "X-Total")
        if total_text:
            if not total_text.isdigit():
                raise SourceManagerError(
                    "GitLab pagination total is invalid",
                    stage="fetch.gitlab_issues",
                )
            total = int(total_text)
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise SourceManagerError(
                    "gitlab_inventory_changed",
                    stage="fetch.gitlab_issues",
                )
        _emit_progress(
            progress_callback,
            {
                "event": "provider.page",
                "provider": "gitlab_issues",
                "phase": "gitlab_issues.inventory",
                "label_ja": "GitLab Issue一覧取得",
                "completed": len(values),
                "total": expected_total,
                "unit": "件",
                "total_kind": "exact" if expected_total is not None else "unknown",
                "current_item": f"page={page}",
                "status": "running",
            },
        )
        next_page = _header_value(response_headers, "X-Next-Page")
        if next_page:
            if (
                not next_page.isdigit()
                or int(next_page) <= page
                or int(next_page) > _MAX_PAGES
            ):
                raise SourceManagerError(
                    "GitLab pagination next page is invalid",
                    stage="fetch.gitlab_issues",
                )
            page = int(next_page)
            continue
        if expected_total is not None:
            if len(values) < expected_total:
                page += 1
                continue
            if len(values) != expected_total:
                raise SourceManagerError(
                    "gitlab_inventory_changed",
                    stage="fetch.gitlab_issues",
                )
            break
        if len(payload) < 100:
            break
        page += 1
    else:
        raise SourceManagerError(
            "GitLab pagination exceeded the safety limit",
            stage="fetch.gitlab_issues",
        )
    values.sort(key=lambda item: (item.updated_at, item.iid))
    return values


def _fetch_project_identity(
    project: GitLabProject,
    request: HttpRequest,
    headers: Mapping[str, str],
) -> GitLabProject:
    status, body, _response_headers = request(project.project_api_url, headers)
    if status != 200:
        raise _gitlab_http_status_error(
            "GitLab project request failed",
            status,
        )
    payload = _decode_json(body, field="GitLab project")
    if not isinstance(payload, Mapping):
        raise SourceManagerError(
            "GitLab project response must be an object",
            stage="fetch.gitlab_issues",
        )
    project_id = _positive_integer(
        payload.get("id"),
        field="GitLab project ID",
    )
    if (
        project.project_id is not None
        and project_id != project.project_id
    ):
        raise SourceManagerError(
            "GitLab project response has the wrong identity",
            stage="fetch.gitlab_issues",
        )
    _validate_gitlab_api_project_identity(
        payload,
        project.project_path,
        error_message=(
            "GitLab project response has the wrong path identity"
        ),
        stage="fetch.gitlab_issues",
    )
    return GitLabProject(
        gitlab_url=project.gitlab_url,
        api_base_url=project.api_base_url,
        project_url=project.project_url,
        project_path=project.project_path,
        project_id=project_id,
    )


def _fetch_json_object(
    request: HttpRequest,
    url: str,
    headers: Mapping[str, str],
    *,
    identity_field: str,
    expected_identity: int,
    expected_project_id: int,
) -> dict[str, Any]:
    status, body, _response_headers = request(url, headers)
    if status != 200:
        raise _gitlab_http_status_error(
            "GitLab Issue detail request failed",
            status,
        )
    payload = _decode_json(body, field="GitLab Issue detail")
    if not isinstance(payload, dict):
        raise SourceManagerError(
            "GitLab Issue detail response must be an object",
            stage="fetch.gitlab_issues",
        )
    identity = _positive_integer(
        payload.get(identity_field),
        field=f"GitLab Issue {identity_field}",
    )
    if identity != int(expected_identity):
        raise SourceManagerError(
            "GitLab Issue detail response has the wrong identity",
            stage="fetch.gitlab_issues",
        )
    project_id = _positive_integer(
        payload.get("project_id"),
        field="GitLab Issue project_id",
    )
    if project_id != int(expected_project_id):
        raise SourceManagerError(
            "GitLab Issue detail response has the wrong project identity",
            stage="fetch.gitlab_issues",
        )
    return payload


def _gitlab_http_status_error(
    message: str,
    status: Any,
) -> SourceManagerError:
    error = SourceManagerError(
        message,
        stage="fetch.gitlab_issues",
    )
    error.diagnostic = {
        "event": "gitlab_issues.http_response",
        "status": int(status),
        "retry": False,
    }
    return error


def _fetch_discussions(
    project: GitLabProject,
    issue_iid: int,
    request: HttpRequest,
    headers: Mapping[str, str],
    *,
    progress_callback: ProgressCallback | None,
) -> list[Mapping[str, Any]]:
    page = 1
    values: list[Mapping[str, Any]] = []
    seen_discussions: set[str] = set()
    expected_total: int | None = None
    while page <= _MAX_PAGES:
        query = urlencode({"per_page": 100, "page": page})
        status, body, response_headers = request(
            f"{project.discussions_api_url(issue_iid)}?{query}",
            headers,
        )
        if status != 200:
            raise _gitlab_http_status_error(
                "GitLab Issue discussions request failed",
                status,
            )
        payload = _decode_json(body, field="GitLab Issue discussions")
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
                or discussion_id in seen_discussions
                or not isinstance(notes, list)
                or any(not isinstance(note, Mapping) for note in notes)
            ):
                raise SourceManagerError(
                    "GitLab Issue discussion schema is invalid",
                    stage="fetch.gitlab_issues",
                )
            seen_discussions.add(discussion_id)
            values.append(dict(raw))
        total_text = _header_value(response_headers, "X-Total")
        if total_text:
            if not total_text.isdigit():
                raise SourceManagerError(
                    "GitLab discussion pagination total is invalid",
                    stage="fetch.gitlab_issues",
                )
            total = int(total_text)
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise SourceManagerError(
                    "gitlab_discussions_changed",
                    stage="fetch.gitlab_issues",
                )
        _emit_progress(
            progress_callback,
            {
                "event": "provider.page",
                "provider": "gitlab_issues",
                "phase": "gitlab_issues.discussions",
                "label_ja": "GitLab Issueコメント取得",
                "completed": len(values),
                "total": expected_total,
                "unit": "スレッド",
                "total_kind": "exact" if expected_total is not None else "unknown",
                "current_item": f"Issue #{issue_iid} page={page}",
                "status": "running",
            },
        )
        next_page = _header_value(response_headers, "X-Next-Page")
        if next_page:
            if (
                not next_page.isdigit()
                or int(next_page) <= page
                or int(next_page) > _MAX_PAGES
            ):
                raise SourceManagerError(
                    "GitLab discussion pagination next page is invalid",
                    stage="fetch.gitlab_issues",
                )
            page = int(next_page)
            continue
        if expected_total is not None:
            if len(values) < expected_total:
                page += 1
                continue
            if len(values) != expected_total:
                raise SourceManagerError(
                    "gitlab_discussions_changed",
                    stage="fetch.gitlab_issues",
                )
            break
        if len(payload) < 100:
            break
        page += 1
    else:
        raise SourceManagerError(
            "GitLab discussion pagination exceeded the safety limit",
            stage="fetch.gitlab_issues",
        )
    return values


def _changed_issue_iids(
    inventory: list[GitLabIssueInventoryItem],
    issues_directory: Path,
    *,
    updated_after: str | None,
) -> list[int]:
    cutoff = (
        _parse_timestamp(updated_after, field="GitLab updated_after")
        if updated_after is not None
        else None
    )
    changed: list[int] = []
    for item in inventory:
        path = issues_directory / f"{item.iid}.md"
        local = _local_issue_metadata(path)
        if local is None:
            if cutoff is None or item.updated_at >= cutoff:
                changed.append(item.iid)
            continue
        if (
            int(local.get("iid") or 0) != item.iid
            or int(local.get("issue_id") or 0) != item.issue_id
        ):
            changed.append(item.iid)
            continue
        if cutoff is not None and item.updated_at < cutoff:
            continue
        if (
            str(local.get("updated_at") or "") != item.updated_at_text
            or int(local.get("user_notes_count") or 0)
            != item.user_notes_count
        ):
            changed.append(item.iid)
    return changed


def _local_issue_metadata(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _LOCAL_METADATA.search(text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
        if not isinstance(payload, dict):
            return None
        _positive_integer(payload.get("iid"), field="local GitLab Issue IID")
        payload["issue_id"] = _positive_integer(
            payload.get("issue_id"),
            field="local GitLab Issue ID",
        )
        payload["updated_at"] = _format_timestamp(
            _parse_timestamp(
                payload.get("updated_at"),
                field="local GitLab Issue updated_at",
            )
        )
        payload["user_notes_count"] = _non_negative_integer(
            payload.get("user_notes_count") or 0,
            field="local GitLab user_notes_count",
        )
        return payload
    except (json.JSONDecodeError, SourceManagerError):
        return None


def _validated_issue_ids(values: list[int], *, field: str) -> list[int]:
    result = [_positive_integer(value, field=field) for value in values]
    if len(set(result)) != len(result):
        raise SourceManagerError(f"{field} contains duplicates")
    return result


def _flatten_notes(
    discussions: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    notes: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for discussion in discussions:
        for raw in discussion.get("notes") or []:
            if not isinstance(raw, Mapping):
                continue
            note_id = raw.get("id")
            if not isinstance(note_id, int) or isinstance(note_id, bool):
                continue
            if note_id in seen:
                continue
            seen.add(note_id)
            notes.append(dict(raw))
    notes.sort(
        key=lambda value: (
            str(value.get("created_at") or ""),
            int(value.get("id") or 0),
        )
    )
    return notes


def _without_project_id(value: Any) -> Any:
    """Return JSON-compatible GitLab metadata without numeric project IDs."""

    if isinstance(value, Mapping):
        return {
            str(key): _without_project_id(item)
            for key, item in value.items()
            if str(key) != "project_id"
        }
    if isinstance(value, list):
        return [_without_project_id(item) for item in value]
    return value


def _user_label(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    name = str(value.get("name") or "").strip()
    username = str(value.get("username") or "").strip()
    if name and username and name != username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else "")


def _canonical_web_root(value: Any, *, field: str) -> str:
    text = validate_web_url(value, field=field)
    split = urlsplit(text)
    if split.query or split.fragment:
        raise SourceManagerError(f"{field} cannot contain query or fragment")
    try:
        _ = split.port
    except ValueError as exc:
        raise SourceManagerError(f"{field} port is invalid") from exc
    path = split.path.rstrip("/")
    for raw_component in path.split("/"):
        if not raw_component:
            continue
        try:
            component = unquote(raw_component, errors="strict")
        except UnicodeError as exc:
            raise SourceManagerError(f"{field} path is invalid") from exc
        if (
            component in {".", ".."}
            or "/" in component
            or "\\" in component
            or any(ord(character) < 0x20 for character in component)
        ):
            raise SourceManagerError(f"{field} path is invalid")
    return urlunsplit(
        (
            split.scheme.casefold(),
            split.netloc.casefold(),
            path,
            "",
            "",
        )
    ).rstrip("/")


def _state_start_time(state: Mapping[str, Any]) -> datetime | None:
    value = state.get("started_at")
    if not isinstance(value, str) or not value:
        return None
    return _parse_timestamp(value, field="GitLab run start time")


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise SourceManagerError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceManagerError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise SourceManagerError(f"{field} has no timezone")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _positive_integer(value: Any, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not str(value).isdigit()
        or not 1 <= int(value) <= _MAX_PROJECT_ID
    ):
        raise SourceManagerError(f"{field} must be a positive integer")
    return int(value)


def _non_negative_integer(value: Any, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not str(value).isdigit()
        or not 0 <= int(value) <= _MAX_PROJECT_ID
    ):
        raise SourceManagerError(f"{field} must be a non-negative integer")
    return int(value)


def _decode_json(body: bytes, *, field: str) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        error = SourceManagerError(
            f"{field} response is invalid JSON",
            stage="fetch.gitlab_issues",
        )
        error.diagnostic = {
            "event": "gitlab_issues.response_invalid",
            "error_kind": "response_parse_failed",
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        raise error from exc


def _atomic_write_text(path: Path, text: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SourceManagerError(
            "GitLab Issue file is unsafe",
            stage="fetch.gitlab_issues",
        )
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_windows_retry(Path(temporary), path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _replace_with_windows_retry(source: Path, target: Path) -> None:
    deadline = (
        time.monotonic() + _WINDOWS_FILE_RETRY_SECONDS
        if os.name == "nt"
        else 0.0
    )
    delay = 0.01
    while True:
        if target.is_symlink() or (
            target.exists() and not target.is_file()
        ):
            raise SourceManagerError(
                "GitLab Issue file is unsafe",
                stage="fetch.gitlab_issues",
            )
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _should_retry_windows_file_error(exc, deadline):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _should_retry_windows_file_error(
    exc: OSError,
    deadline: float,
) -> bool:
    return (
        os.name == "nt"
        and getattr(exc, "winerror", None)
        in _WINDOWS_TRANSIENT_FILE_ERRORS
        and time.monotonic() < deadline
    )


def _header_value(headers: Mapping[str, Any], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value or "").strip()
    return ""


def _emit_progress(
    callback: ProgressCallback | None,
    event: Mapping[str, Any],
) -> None:
    if callback is None:
        return
    try:
        callback(dict(event))
    except Exception:
        return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
