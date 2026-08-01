from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .errors import SourceManagerError


CommandRunner = Callable[[list[str]], Any]
ProgressCallback = Callable[[Mapping[str, Any]], None]
_REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_ISSUE_PATH = re.compile(r"^issues/(?P<number>[1-9][0-9]*)\.md$")
_WIKI_PATH = re.compile(r"^(?P<page>[^/]+)\.md$")


@dataclass(frozen=True)
class GitHubRepository:
    host: str
    owner: str
    name: str
    repository_url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def wiki_clone_url(self) -> str:
        return f"{self.repository_url}.wiki.git"


def parse_github_repository_url(value: Any) -> GitHubRepository:
    text = str(value or "").strip()
    split = urlsplit(text)
    if (
        split.scheme.casefold() != "https"
        or not split.hostname
        or split.username is not None
        or split.password is not None
        or split.port is not None
        or split.query
        or split.fragment
    ):
        raise SourceManagerError(
            "repository_url must be an absolute HTTPS GitHub repository URL"
        )
    components = [item for item in split.path.rstrip("/").split("/") if item]
    if len(components) != 2:
        raise SourceManagerError(
            "repository_url must identify a GitHub repository top page"
        )
    decoded: list[str] = []
    for raw in components:
        try:
            component = unquote(raw, errors="strict")
        except UnicodeError as exc:
            raise SourceManagerError("repository_url path is invalid") from exc
        if not _REPOSITORY_COMPONENT.fullmatch(component):
            raise SourceManagerError("repository_url path is invalid")
        decoded.append(component)
    owner, name = decoded
    if name.casefold().endswith(".git"):
        name = name[:-4]
    if not owner or not name or name in {".", ".."}:
        raise SourceManagerError(
            "repository_url must identify a GitHub repository top page"
        )
    encoded_path = f"/{quote(owner, safe='-._~')}/{quote(name, safe='-._~')}"
    host = str(split.hostname).casefold()
    repository_url = urlunsplit(("https", split.netloc, encoded_path, "", ""))
    return GitHubRepository(host, owner, name, repository_url)


def generated_github_issues_link(repository_url: Any) -> dict[str, Any]:
    repository = parse_github_repository_url(repository_url)
    return {
        "enabled": True,
        "strategy": "regex-template",
        "settings": {
            "path_pattern": _ISSUE_PATH.pattern,
            "url_template": f"{repository.repository_url}/issues/{{number}}",
        },
    }


def generated_github_wiki_link(repository_url: Any) -> dict[str, Any]:
    repository = parse_github_repository_url(repository_url)
    return {
        "enabled": True,
        "strategy": "regex-template",
        "settings": {
            "path_pattern": _WIKI_PATH.pattern,
            "url_template": f"{repository.repository_url}/wiki/{{page}}",
        },
    }


def fetch_github_issues(
    settings: Mapping[str, Any],
    work_directory: Path,
    command_runner: CommandRunner,
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    repository = parse_github_repository_url(settings.get("repository_url"))
    state = str(settings.get("state") or "all")
    include_comments = settings.get("include_comments", True)
    endpoint = f"repos/{repository.slug}/issues?state={state}&per_page=100"
    issues = _gh_api_pages(repository, endpoint, command_runner)
    issues = [item for item in issues if "pull_request" not in item]
    numbers: set[int] = set()
    stage = Path(
        tempfile.mkdtemp(
            prefix=".github-issues-stage-",
            dir=str(work_directory.parent),
        )
    )
    try:
        issue_root = stage / "issues"
        issue_root.mkdir()
        total = len(issues)
        for index, issue in enumerate(issues, 1):
            number = _positive_integer(issue.get("number"), "issue number")
            if number in numbers:
                raise SourceManagerError("GitHub Issues response contains duplicates")
            numbers.add(number)
            comments: list[Mapping[str, Any]] = []
            if include_comments and int(issue.get("comments") or 0) > 0:
                comments = _gh_api_pages(
                    repository,
                    f"repos/{repository.slug}/issues/{number}/comments?per_page=100",
                    command_runner,
                )
            text = _issue_markdown(repository, issue, comments)
            (issue_root / f"{number}.md").write_text(text, encoding="utf-8")
            _emit_progress(progress_callback, index, total, number)
        _publish_tree(stage, Path(work_directory))
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return {
        "status": "ok",
        "documents": len(issues),
        "repository_url": repository.repository_url,
    }


def _gh_api_pages(
    repository: GitHubRepository,
    endpoint: str,
    command_runner: CommandRunner,
) -> list[Mapping[str, Any]]:
    arguments = [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "-H",
        "Accept: application/vnd.github+json",
    ]
    if repository.host != "github.com":
        arguments.extend(["--hostname", repository.host])
    arguments.append(endpoint)
    try:
        result = command_runner(arguments)
    except OSError as exc:
        raise SourceManagerError(
            "GitHub Issues require an installed and authenticated gh CLI"
        ) from exc
    if int(getattr(result, "returncode", 1)) != 0:
        detail = str(getattr(result, "stderr", "") or "").strip()[:500]
        raise SourceManagerError(
            "GitHub API request failed" + (f": {detail}" if detail else "")
        )
    try:
        payload = json.loads(str(getattr(result, "stdout", "") or ""))
    except json.JSONDecodeError as exc:
        raise SourceManagerError("GitHub API returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise SourceManagerError("GitHub API response must be an array")
    output: list[Mapping[str, Any]] = []
    for page in payload:
        if not isinstance(page, list):
            raise SourceManagerError("GitHub API page must be an array")
        for item in page:
            if not isinstance(item, Mapping):
                raise SourceManagerError("GitHub API item must be an object")
            output.append(item)
    return output


def _issue_markdown(
    repository: GitHubRepository,
    issue: Mapping[str, Any],
    comments: list[Mapping[str, Any]],
) -> str:
    number = _positive_integer(issue.get("number"), "issue number")
    title = str(issue.get("title") or f"Issue #{number}").strip()
    metadata = json.dumps(
        {
            "schema_version": "local-rag-github-issue-v1",
            "repository": repository.slug,
            "number": number,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    labels = [
        str(value.get("name") or "").strip()
        for value in (issue.get("labels") or [])
        if isinstance(value, Mapping) and str(value.get("name") or "").strip()
    ]
    lines = [
        f"# GitHub Issue #{number}: {title}",
        "",
        f"<!-- local-rag-github-issue: {metadata} -->",
        "",
        f"- URL: {repository.repository_url}/issues/{number}",
        f"- State: {str(issue.get('state') or '')}",
        f"- Author: {_login(issue.get('user'))}",
        f"- Created: {str(issue.get('created_at') or '')}",
        f"- Updated: {str(issue.get('updated_at') or '')}",
    ]
    if labels:
        lines.append(f"- Labels: {', '.join(labels)}")
    lines.extend(["", str(issue.get("body") or "").rstrip(), ""])
    if comments:
        lines.extend(["## Comments", ""])
        for comment in comments:
            lines.extend(
                [
                    f"### {_login(comment.get('user'))} — {str(comment.get('created_at') or '')}",
                    "",
                    str(comment.get("body") or "").rstrip(),
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _login(value: Any) -> str:
    return str(value.get("login") or "unknown") if isinstance(value, Mapping) else "unknown"


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise SourceManagerError(f"GitHub {field} is invalid")
    return int(value)


def _publish_tree(stage: Path, work: Path) -> None:
    if not work.is_dir() or work.is_symlink() or stage.is_symlink():
        raise SourceManagerError("GitHub Issues work directory is unsafe")
    backup = work.parent / f".{work.name}.github-issues-backup-{uuid.uuid4().hex}"
    published = False
    try:
        os.replace(work, backup)
        try:
            os.replace(stage, work)
            published = True
        except Exception:
            os.replace(backup, work)
            raise
    finally:
        if published and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _emit_progress(
    callback: ProgressCallback | None,
    completed: int,
    total: int,
    number: int,
) -> None:
    if callback is None:
        return
    callback(
        {
            "event": "provider.item",
            "provider": "github_issues",
            "phase": "github_issues.items",
            "completed": completed,
            "total": total,
            "item": number,
            "status": "running",
        }
    )
