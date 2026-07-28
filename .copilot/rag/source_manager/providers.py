from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .errors import SourceManagerError
from .security import (
    validate_environment_name,
    validate_persistable,
    validate_relative_path,
    validate_web_url,
)


SUPPORTED_PROVIDERS = frozenset(
    {"github", "svn", "redmine", "sharepoint", "other"}
)
REDMINE_BATCH_SIZE = 5
REDMINE_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class FetchStep:
    step_id: str
    operation: str
    requires_network: bool
    destination: str
    parameters: dict[str, Any]
    checkpoint_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation,
            "requires_network": self.requires_network,
            "destination": self.destination,
            "parameters": dict(self.parameters),
            "checkpoint_kind": self.checkpoint_kind,
        }


@dataclass(frozen=True)
class FetchPlan:
    schema_version: str
    source_key: str
    provider: str
    logical_root: str
    work_path: str
    steps: tuple[FetchStep, ...]
    plan_etag: str

    @property
    def requires_network(self) -> bool:
        return any(step.requires_network for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_key": self.source_key,
            "provider": self.provider,
            "logical_root": self.logical_root,
            "work_path": self.work_path,
            "steps": [step.to_dict() for step in self.steps],
            "plan_etag": self.plan_etag,
        }


def validate_provider_config(
    provider: str,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    kind = str(provider or "").strip().lower()
    if kind not in SUPPORTED_PROVIDERS:
        raise SourceManagerError("unsupported Source Manager provider")
    if not isinstance(settings, Mapping):
        raise SourceManagerError("provider settings must be an object")
    supplied = dict(settings)
    validate_persistable(supplied, field="provider_settings")
    if kind == "github":
        return _validate_github(supplied)
    if kind == "svn":
        return _validate_svn(supplied)
    if kind == "redmine":
        return _validate_redmine(supplied)
    if kind == "sharepoint":
        return _validate_environment_source(
            supplied,
            environment_key="root_env",
        )
    _only_keys(supplied, {"one_shot"})
    if "one_shot" in supplied and supplied["one_shot"] is not True:
        raise SourceManagerError("Other one_shot must be true")
    return {}


def build_fetch_plan(
    *,
    source_key: str,
    provider: str,
    settings: Mapping[str, Any],
    logical_root: str,
    work_path: str,
) -> FetchPlan:
    normalized = validate_provider_config(provider, settings)
    normalized_root = validate_relative_path(
        logical_root,
        field="logical_root",
        allow_empty=False,
    )
    normalized_work = validate_relative_path(
        work_path,
        field="work_path",
        allow_empty=False,
    )
    if normalized_root != normalized_work:
        raise SourceManagerError(
            "logical_root and work_path must use the fixed Source work path"
        )
    kind = str(provider).strip().lower()
    destination = normalized_work
    if kind == "github":
        step = FetchStep(
            "repository",
            "git_fetch",
            True,
            destination,
            normalized,
            "repository_revision",
        )
    elif kind == "svn":
        step = FetchStep(
            "repository",
            "svn_checkout_or_update",
            True,
            destination,
            normalized,
            "repository_revision",
        )
    elif kind == "redmine":
        step = FetchStep(
            "issues",
            "redmine_fetch_issues",
            True,
            destination,
            {
                **normalized,
                "batch_size": REDMINE_BATCH_SIZE,
                "retry": {
                    "max_attempts": REDMINE_MAX_ATTEMPTS,
                    "retry_statuses": [429, 502, 503, 504],
                },
            },
            "redmine_issue_cursor",
        )
    else:
        step = FetchStep(
            "files",
            "copy_from_environment_root",
            False,
            destination,
            normalized,
            "file_manifest",
        )
    body = {
        "schema_version": "local-rag.fetch-plan.v1",
        "source_key": str(source_key),
        "provider": kind,
        "logical_root": normalized_root,
        "work_path": normalized_work,
        "steps": [step.to_dict()],
    }
    validate_persistable(body, field="fetch_plan")
    digest = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return FetchPlan(
        schema_version=body["schema_version"],
        source_key=str(source_key),
        provider=kind,
        logical_root=normalized_root,
        work_path=normalized_work,
        steps=(step,),
        plan_etag=digest,
    )


def resolve_environment_root(
    settings: Mapping[str, Any],
    *,
    provider: str,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a local runtime root without making it persistable."""
    normalized = validate_provider_config(provider, settings)
    if provider != "sharepoint":
        raise SourceManagerError(
            "only SharePoint uses an environment-backed root"
        )
    key = "root_env"
    name = str(normalized[key])
    effective_environment = os.environ if environment is None else environment
    value = effective_environment.get(name)
    if not value:
        raise SourceManagerError(f"required environment root is unavailable: {name}")
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise SourceManagerError("environment root must be absolute at runtime")
    relative = str(normalized.get("relative_path") or "")
    candidate = root.joinpath(*relative.split("/")) if relative else root
    return candidate


def _validate_github(settings: dict[str, Any]) -> dict[str, Any]:
    _only_keys(
        settings,
        {"repository_url"},
    )
    repository = _validate_git_fetch_url(settings.get("repository_url"))
    return {"repository_url": repository}


def _validate_svn(settings: dict[str, Any]) -> dict[str, Any]:
    _only_keys(
        settings,
        {"repository_url", "recursive"},
    )
    repository = validate_web_url(
        settings.get("repository_url"),
        field="repository_url",
    )
    split = urlsplit(repository)
    if split.query or split.fragment:
        raise SourceManagerError(
            "repository_url cannot contain query or fragment"
        )
    output: dict[str, Any] = {
        "repository_url": repository.rstrip("/"),
    }
    recursive = settings.get("recursive", True)
    if not isinstance(recursive, bool):
        raise SourceManagerError("recursive must be boolean")
    output["recursive"] = recursive
    return output


def _validate_redmine(settings: dict[str, Any]) -> dict[str, Any]:
    _only_keys(
        settings,
        {
            "project_url",
            "updated_within_days",
            "api_key_env",
            "base_url",
            "project_id",
        },
    )
    project_url = validate_web_url(
        settings.get("project_url"),
        field="project_url",
    )
    split = urlsplit(project_url)
    if split.query or split.fragment:
        raise SourceManagerError("project_url cannot contain query or fragment")
    components = [
        component for component in split.path.strip("/").split("/") if component
    ]
    if len(components) < 2 or components[-2].casefold() != "projects":
        raise SourceManagerError(
            "project_url must end with /projects/<project-id>"
        )
    days = settings.get("updated_within_days")
    if days is not None:
        if (
            isinstance(days, bool)
            or not str(days).isdigit()
            or not 1 <= int(days) <= 3650
        ):
            raise SourceManagerError(
                "updated_within_days must be null or between 1 and 3650"
            )
    output: dict[str, Any] = {
        "project_url": project_url.rstrip("/"),
        "base_url": urlunsplit(
            (split.scheme, split.netloc, "", "", "")
        ).rstrip("/"),
        "project_id": components[-1],
        "updated_within_days": int(days) if days is not None else None,
        "api_key_env": validate_environment_name(
            settings.get("api_key_env") or "RAG_REDMINE_API_KEY",
            field="api_key_env",
        ),
    }
    if (
        settings.get("base_url") is not None
        and str(settings["base_url"]).rstrip("/") != output["base_url"]
    ):
        raise SourceManagerError("Redmine base_url does not match project_url")
    if (
        settings.get("project_id") is not None
        and str(settings["project_id"]) != output["project_id"]
    ):
        raise SourceManagerError(
            "Redmine project_id does not match project_url"
        )
    return output


def _validate_environment_source(
    settings: dict[str, Any],
    *,
    environment_key: str,
) -> dict[str, Any]:
    _only_keys(settings, {environment_key, "relative_path"})
    output = {
        environment_key: validate_environment_name(
            settings.get(environment_key),
            field=environment_key,
        )
    }
    relative = validate_relative_path(
        settings.get("relative_path"),
        field="relative_path",
    )
    if relative:
        output["relative_path"] = relative
    return output


def _only_keys(settings: dict[str, Any], allowed: set[str]) -> None:
    if set(settings) - allowed:
        raise SourceManagerError("provider settings contain unsupported fields")


def _required_text(value: Any, *, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise SourceManagerError(f"{field} is required")
    if len(text) > limit or any(ord(character) < 32 for character in text):
        raise SourceManagerError(f"{field} is invalid")
    return text


def _validate_git_fetch_url(value: Any) -> str:
    text = _required_text(value, field="repository_url", limit=4096)
    if re.fullmatch(
        r"git@[A-Za-z0-9.-]+:[A-Za-z0-9._~!$&'()+,;=@%/-]+",
        text,
    ):
        return text
    split = urlsplit(text)
    if split.scheme.casefold() == "ssh":
        if split.password is not None or not split.hostname or not split.path:
            raise SourceManagerError("repository_url is unsafe")
        return text.rstrip("/")
    repository = validate_web_url(text, field="repository_url")
    split = urlsplit(repository)
    if split.query or split.fragment:
        raise SourceManagerError(
            "repository_url cannot contain query or fragment"
        )
    if any(
        marker in split.path.casefold()
        for marker in ("/blob/", "/tree/")
    ):
        raise SourceManagerError(
            "repository_url must identify a repository root"
        )
    return repository.rstrip("/")
