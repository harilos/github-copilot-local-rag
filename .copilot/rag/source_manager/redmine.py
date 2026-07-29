from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import SourceManagerError
from .security import validate_web_url


REDMINE_CUTOFF_STATE_KEY = "redmine_updated_on_cutoff"
_AUTO_ISSUE_PATTERN = r"^issues/(?P<issue_id>[0-9]+)\.md$"


@dataclass(frozen=True)
class RedmineProject:
    project_url: str
    api_root: str
    project_id: str

    @property
    def issues_api_url(self) -> str:
        return f"{self.api_root}/issues.json"

    def issue_api_url(self, issue_id: int) -> str:
        return f"{self.api_root}/issues/{int(issue_id)}.json"

    @property
    def issue_link_template(self) -> str:
        return f"{self.api_root}/issues/{{issue_id}}"


def parse_redmine_project_url(value: Any) -> RedmineProject:
    """Parse one canonical Redmine project URL.

    Redmine can be mounted below an origin path.  The returned API root keeps
    that path, the explicit port, and the original HTTP(S) scheme.
    """
    project_url = validate_web_url(value, field="project_url")
    split = urlsplit(project_url)
    if split.username is not None or split.password is not None:
        raise SourceManagerError("project_url cannot contain user information")
    if split.query or split.fragment:
        raise SourceManagerError("project_url cannot contain query or fragment")
    try:
        _ = split.port
    except ValueError as exc:
        raise SourceManagerError("project_url port is invalid") from exc
    if "\\" in split.path:
        raise SourceManagerError("project_url path is invalid")
    path = split.path.rstrip("/")
    components = path.split("/")
    if (
        len(components) < 3
        or components[0] != ""
        or components[-2].casefold() != "projects"
        or not components[-1]
        or any(component in {"", ".", ".."} for component in components[1:])
    ):
        raise SourceManagerError(
            "project_url must end with /projects/<project-id>"
        )
    decoded_project_id = unquote(components[-1])
    if (
        not decoded_project_id
        or decoded_project_id in {".", ".."}
        or "/" in decoded_project_id
        or "\\" in decoded_project_id
        or any(ord(character) < 0x20 for character in decoded_project_id)
    ):
        raise SourceManagerError("project_url project ID is invalid")
    root_components = components[1:-2]
    root_path = f"/{'/'.join(root_components)}" if root_components else ""
    normalized_project_path = (
        f"{root_path}/projects/{components[-1]}"
    )
    normalized_project_url = urlunsplit(
        (
            split.scheme,
            split.netloc,
            normalized_project_path,
            "",
            "",
        )
    )
    api_root = urlunsplit(
        (split.scheme, split.netloc, root_path, "", "")
    ).rstrip("/")
    return RedmineProject(
        project_url=normalized_project_url,
        api_root=api_root,
        project_id=decoded_project_id,
    )


def redmine_updated_on_cutoff(
    updated_within_days: Any,
    state: Mapping[str, Any] | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> str | None:
    """Return a stable absolute UTC date for one update run."""
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
    saved = payload.get(REDMINE_CUTOFF_STATE_KEY)
    if saved is not None:
        text = str(saved)
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            raise SourceManagerError("Redmine cutoff state is invalid") from exc
        if parsed.isoformat() != text:
            raise SourceManagerError("Redmine cutoff state is invalid")
        return text
    anchor = _state_start_time(payload)
    if anchor is None:
        anchor = (clock or _utc_now)()
    if not isinstance(anchor, datetime):
        raise SourceManagerError("Redmine clock must return a datetime")
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    anchor = anchor.astimezone(timezone.utc)
    return (anchor.date() - timedelta(days=int(updated_within_days))).isoformat()


def generated_redmine_link(project_url: Any) -> dict[str, Any]:
    project = parse_redmine_project_url(project_url)
    return {
        "enabled": True,
        "strategy": "regex-template",
        "settings": {
            "path_pattern": _AUTO_ISSUE_PATTERN,
            "url_template": project.issue_link_template,
        },
    }


def repair_generated_redmine_link(
    project_url: Any,
    link: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Repair only the exact Link shape emitted by the legacy Manager.

    Human-authored regexes, disabled links, additional settings, and alternate
    URL templates are deliberately left untouched.
    """
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
    if (
        set(settings) != {"path_pattern", "url_template"}
        or settings.get("path_pattern") != _AUTO_ISSUE_PATTERN
    ):
        return value
    project = parse_redmine_project_url(project_url)
    split = urlsplit(project.project_url)
    legacy_template = urlunsplit(
        (split.scheme, split.netloc, "", "", "")
    ).rstrip("/") + "/issues/{issue_id}"
    if settings.get("url_template") not in {
        legacy_template,
        project.issue_link_template,
    }:
        return value
    return generated_redmine_link(project.project_url)


def _state_start_time(state: Mapping[str, Any]) -> datetime | None:
    value = state.get("started_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceManagerError("Redmine run start time is invalid") from exc
    return parsed


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
