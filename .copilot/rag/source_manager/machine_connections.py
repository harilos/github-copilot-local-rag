from __future__ import annotations

import base64
import copy
import functools
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.fernet import Fernet, InvalidToken

from .errors import SourceManagerError
from .gitlab_issues import (
    _validate_gitlab_api_project_identity,
    gitlab_connection_id,
    gitlab_token_env,
    parse_gitlab_project,
)
from .networking import reject_http_redirects
from .redmine import parse_redmine_project_url
from .security import validate_web_url


CONNECTION_SCHEMA_VERSION = "local-rag.source-connections.v1"
SECRET_SCHEMA_VERSION = "local-rag.source-connection-secrets.v1"
CONNECTION_FILE_NAME = "source-connections.json"
SECRET_FILE_NAME = "source-connections.secrets.json"
SECRET_KEY_FILE_NAME = ".source-connections.key"
SHAREPOINT_ROOT_ENV = "LOCAL_RAG_SHAREPOINT_ROOT"
LEGACY_REDMINE_API_KEY_ENV = "LOCAL_RAG_REDMINE_API_KEY"
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_SECRET_CHARS = 16 * 1024
_REDMINE_ENV_PREFIX = "LOCAL_RAG_REDMINE_API_KEY_"
_SAFE_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_RUNTIME_PATCH_MARKER = "_local_rag_machine_connections_installed"
_MAX_PROJECT_ID = 9_223_372_036_854_775_807
_MAX_CONFLUENCE_IDENTITY_BYTES = 1024 * 1024
_CONFLUENCE_CREDENTIAL_SCHEMA = "local-rag.confluence-credential.v1"
_CONFLUENCE_SECURITY_IDENTITY_SCHEMA = (
    "local-rag.confluence-security-identity.v1"
)
_CONFLUENCE_DEPLOYMENTS = frozenset({"cloud", "data_center"})
_CONFLUENCE_CLOUD_TOKEN_KINDS = frozenset({"unscoped", "scoped"})
_CONFLUENCE_PUBLIC_FIELDS = frozenset(
    {
        "display_name",
        "deployment",
        "base_url",
        "token_kind",
        "cloud_id",
        "api_root",
        "updated_at",
    }
)
_CONFLUENCE_TOMBSTONE_FIELDS = frozenset(
    {"security_identity", "updated_at"}
)


@dataclass(frozen=True)
class SharePointRootStatus:
    configured: bool
    root: str | None
    source: str


@dataclass(frozen=True)
class RedmineRegistration:
    connection_id: str
    api_root: str
    api_key_env: str
    registered: bool


@dataclass(frozen=True)
class GitLabRegistration:
    connection_id: str
    gitlab_url: str
    token_env: str
    registered: bool


@dataclass(frozen=True)
class GitLabProjectLocation:
    gitlab_url: str
    project_url: str
    project_path: str
    encoded_project_path: str

    @property
    def api_base_url(self) -> str:
        return f"{self.gitlab_url}/api/v4"

    @property
    def project_api_url(self) -> str:
        return f"{self.api_base_url}/projects/{self.encoded_project_path}"

    @property
    def issue_link(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "strategy": "regex-template",
            "settings": {
                "path_pattern": (
                    r"^issues/(?P<issue_iid>[0-9]+)\.md$"
                ),
                "url_template": (
                    f"{self.project_url}/-/issues/{{issue_iid}}"
                ),
            },
        }


@dataclass(frozen=True)
class GitLabProjectCheck:
    location: GitLabProjectLocation
    project_id: int
    name: str


@dataclass(frozen=True)
class ConfluenceRegistration:
    connection_id: str
    display_name: str
    deployment: str
    base_url: str
    token_kind: str
    cloud_id: str | None
    api_root: str
    registered: bool


@dataclass(frozen=True)
class ConfluenceCredentialConfirmation:
    """One verified credential bundle that is safe to pass only in memory."""

    deployment: str
    base_url: str
    token_kind: str
    cloud_id: str | None
    api_root: str
    account_email: str = dataclass_field(repr=False)
    token: str = dataclass_field(repr=False)
    principal: str = dataclass_field(repr=False)
    security_identity: str = dataclass_field(repr=False)


@dataclass(frozen=True)
class ResolvedConfluenceCredentials:
    """Machine-local Confluence credentials with a deliberately safe repr."""

    connection_id: str
    deployment: str
    base_url: str
    token_kind: str
    cloud_id: str | None
    api_root: str
    auth_type: str
    account_email: str = dataclass_field(repr=False)
    token: str = dataclass_field(repr=False)
    principal: str = dataclass_field(repr=False)
    security_identity: str = dataclass_field(repr=False)
    email: str = dataclass_field(repr=False)
    api_token: str = dataclass_field(repr=False)
    password: str = dataclass_field(repr=False)


GitLabHttpGet = Callable[
    [str, Mapping[str, str], float],
    tuple[int, bytes, Mapping[str, str]],
]
ConfluenceHttpGet = Callable[
    [str, Mapping[str, str], float],
    tuple[int, bytes, Mapping[str, str]],
]


def connection_config_path(rag_root: str | Path) -> Path:
    return Path(rag_root) / "config" / CONNECTION_FILE_NAME


def connection_secret_path(rag_root: str | Path) -> Path:
    return Path(rag_root) / "config" / SECRET_FILE_NAME


def connection_secret_key_path(rag_root: str | Path) -> Path:
    return Path(rag_root) / "config" / SECRET_KEY_FILE_NAME


def redmine_connection_id(project_url: Any) -> str:
    project = parse_redmine_project_url(project_url)
    digest = hashlib.sha256(project.api_root.casefold().encode("utf-8")).hexdigest()
    return f"redmine-{digest[:20]}"


def redmine_api_key_env(project_url: Any) -> str:
    connection_id = redmine_connection_id(project_url)
    suffix = connection_id.removeprefix("redmine-").upper()
    return f"{_REDMINE_ENV_PREFIX}{suffix}"


def gitlab_project_location(
    gitlab_url: Any,
    project_url: Any,
) -> GitLabProjectLocation:
    """Resolve one GitLab web project below an explicit instance root.

    GitLab Self-Managed can live below a path such as ``/gitlab``.  The
    instance URL therefore remains explicit, while only the relative
    ``group/subgroup/project`` identity is percent-encoded for the REST path.
    """

    project = parse_gitlab_project(project_url, gitlab_url)
    return GitLabProjectLocation(
        gitlab_url=project.gitlab_url,
        project_url=project.project_url,
        project_path=project.project_path,
        encoded_project_path=urllib.parse.quote(
            project.project_path,
            safe="",
        ),
    )


def confluence_connection_id(value: Any) -> str:
    """Return the canonical form of one random Confluence connection UUID."""

    text = str(value or "").strip()
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise SourceManagerError("Confluence connection ID is invalid") from exc
    if parsed.version != 4:
        raise SourceManagerError("Confluence connection ID is invalid")
    return str(parsed)


def sharepoint_root_status(
    rag_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> SharePointRootStatus:
    config = _load_connections(rag_root)
    stored = str(config.get("sharepoint_root") or "").strip()
    if stored:
        normalized = _usable_sharepoint_root(stored)
        if normalized is not None:
            return SharePointRootStatus(True, str(normalized), "manager")
    environment = os.environ if environ is None else environ
    inherited = str(environment.get(SHAREPOINT_ROOT_ENV) or "").strip()
    if inherited:
        normalized = _usable_sharepoint_root(inherited)
        if normalized is not None:
            return SharePointRootStatus(True, str(normalized), "environment")
    return SharePointRootStatus(False, None, "missing")


def configured_sharepoint_root(
    rag_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    status = sharepoint_root_status(rag_root, environ=environ)
    return Path(status.root) if status.configured and status.root else None


def set_sharepoint_root(rag_root: str | Path, value: str | Path) -> Path:
    root = _required_sharepoint_root(value)
    config = _load_connections(rag_root)
    config["sharepoint_root"] = str(root)
    config["updated_at"] = _now()
    _save_connections(rag_root, config)
    return root


def clear_sharepoint_root(rag_root: str | Path) -> None:
    config = _load_connections(rag_root)
    config.pop("sharepoint_root", None)
    config["updated_at"] = _now()
    _save_connections(rag_root, config)


def check_confluence_credentials(
    *,
    deployment: Any,
    base_url: Any,
    token: Any,
    token_kind: Any = None,
    cloud_id: Any = None,
    account_email: Any = None,
    http_get: ConfluenceHttpGet | None = None,
) -> ConfluenceCredentialConfirmation:
    """Verify one Cloud or Data Center identity without persisting secrets."""

    kind = _normalize_confluence_deployment(deployment)
    root = _canonical_confluence_root(kind, base_url)
    secret = _validate_secret(token, label="Confluence credential")
    email = _normalize_confluence_email(account_email, deployment=kind)
    flavor = _normalize_confluence_token_kind(token_kind, deployment=kind)
    getter = http_get or _default_http_get
    resolved_cloud_id: str | None = None
    if kind == "cloud":
        if flavor == "scoped":
            manual_cloud_id = _normalize_confluence_cloud_id(
                cloud_id,
                required=False,
            )
            discovered_cloud_id = _discover_confluence_cloud_id(getter, root)
            if discovered_cloud_id is not None:
                if (
                    manual_cloud_id is not None
                    and manual_cloud_id != discovered_cloud_id
                ):
                    raise SourceManagerError(
                        "manual Confluence cloud ID does not match tenant info"
                    )
                resolved_cloud_id = discovered_cloud_id
            elif manual_cloud_id is not None:
                resolved_cloud_id = manual_cloud_id
            else:
                raise SourceManagerError(
                    "Confluence cloud ID could not be discovered; "
                    "manual cloud ID is required"
                )
            api_root = (
                "https://api.atlassian.com/ex/confluence/"
                f"{resolved_cloud_id}/wiki/api/v2"
            )
            endpoint = api_root.removesuffix("/api/v2") + "/rest/api/user/current"
            basic_material = f"{email}:{secret}"
            authorization = "Basic " + base64.b64encode(
                basic_material.encode("utf-8")
            ).decode("ascii")
        else:
            if cloud_id not in (None, ""):
                raise SourceManagerError(
                    "Confluence cloud ID is only valid for scoped tokens"
                )
            api_root = f"{root}/wiki/api/v2"
            endpoint = f"{root}/wiki/rest/api/user/current"
            basic_material = f"{email}:{secret}"
            authorization = "Basic " + base64.b64encode(
                basic_material.encode("utf-8")
            ).decode("ascii")
    else:
        if cloud_id not in (None, ""):
            raise SourceManagerError(
                "Confluence cloud ID is only valid for Cloud"
            )
        api_root = f"{root}/rest/api"
        endpoint = f"{root}/rest/api/user/current"
        basic_material = ""
        authorization = f"Bearer {secret}"
    headers = {
        "Authorization": authorization,
        "Accept": "application/json",
    }
    try:
        response = getter(endpoint, headers, 10.0)
        status = int(response[0])
        body = bytes(response[1])
        if status != 200:
            raise SourceManagerError(
                "Confluence credential check failed "
                f"(HTTP {status})"
            )
        if len(body) > _MAX_CONFLUENCE_IDENTITY_BYTES:
            raise SourceManagerError(
                "Confluence credential check returned an oversized response"
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceManagerError(
                "Confluence credential check returned invalid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SourceManagerError(
                "Confluence credential check returned an invalid user"
            )
        if kind == "cloud":
            principal = _normalize_confluence_principal(
                payload.get("accountId"),
                label="Confluence Cloud accountId",
            )
        else:
            principal = _normalize_confluence_principal(
                payload.get("userKey") or payload.get("accountId"),
                label="Confluence Data Center stable principal",
            )
        security_identity = _confluence_security_identity(
            deployment=kind,
            base_url=root,
            token_kind=flavor,
            cloud_id=resolved_cloud_id,
            api_root=api_root,
            principal=principal,
        )
        return ConfluenceCredentialConfirmation(
            deployment=kind,
            base_url=root,
            token_kind=flavor,
            cloud_id=resolved_cloud_id,
            api_root=api_root,
            account_email=email,
            token=secret,
            principal=principal,
            security_identity=security_identity,
        )
    finally:
        secret = ""
        basic_material = ""
        authorization = ""
        headers["Authorization"] = ""


def register_confluence_connection(
    rag_root: str | Path,
    *,
    display_name: Any,
    confirmation: ConfluenceCredentialConfirmation,
    expected_connection_id: Any = None,
) -> ConfluenceRegistration:
    """Atomically create, recover, or rotate one verified connection."""

    verified = _validated_confluence_confirmation(confirmation)
    name = _normalize_confluence_display_name(display_name)
    config = _load_connections(rag_root)
    secrets = _load_secrets(rag_root)
    public_entries = dict(config.get("confluence") or {})
    secret_entries = dict(secrets.get("confluence") or {})
    tombstones = _validated_confluence_tombstones(secrets)
    for raw_id, raw in public_entries.items():
        confluence_connection_id(raw_id)
        _validate_confluence_public_entry_fields(raw)
    if set(public_entries) & set(tombstones):
        raise SourceManagerError("Confluence connection registry is invalid")

    if expected_connection_id is None:
        connection_id = str(uuid.uuid4())
        while (
            connection_id in public_entries
            or connection_id in secret_entries
            or connection_id in tombstones
        ):
            connection_id = str(uuid.uuid4())
    else:
        connection_id = confluence_connection_id(expected_connection_id)
        public_present = connection_id in public_entries
        secret_present = connection_id in secret_entries
        if public_present != secret_present:
            raise SourceManagerError(
                "Confluence connection registration is incomplete"
            )
        if public_present:
            existing = _resolve_confluence_from_payloads(
                rag_root,
                connection_id,
                public_entries[connection_id],
                secret_entries[connection_id],
            )
            if existing.security_identity != verified.security_identity:
                raise SourceManagerError(
                    "Confluence security identity does not match"
                )
        else:
            tombstone = tombstones.get(connection_id)
            if (
                tombstone is not None
                and tombstone["security_identity"]
                != verified.security_identity
            ):
                raise SourceManagerError(
                    "Confluence security identity does not match"
                )

    updated_at = _now()
    public_entries[connection_id] = {
        "display_name": name,
        "deployment": verified.deployment,
        "base_url": verified.base_url,
        "token_kind": verified.token_kind,
        "cloud_id": verified.cloud_id,
        "api_root": verified.api_root,
        "updated_at": updated_at,
    }
    secret_entries[connection_id] = {
        "ciphertext": _encrypt_confluence_credentials(rag_root, verified),
        "updated_at": updated_at,
    }
    config["confluence"] = public_entries
    config["updated_at"] = updated_at
    secrets["confluence"] = secret_entries
    tombstones.pop(connection_id, None)
    secrets["confluence_tombstones"] = tombstones
    secrets["updated_at"] = updated_at
    _save_connection_pair(rag_root, config=config, secrets=secrets)
    return ConfluenceRegistration(
        connection_id=connection_id,
        display_name=name,
        deployment=verified.deployment,
        base_url=verified.base_url,
        token_kind=verified.token_kind,
        cloud_id=verified.cloud_id,
        api_root=verified.api_root,
        registered=True,
    )


def register_redmine_api_key(
    rag_root: str | Path,
    project_url: Any,
    api_key: Any,
) -> RedmineRegistration:
    project = parse_redmine_project_url(project_url)
    secret = _validate_secret(api_key, label="Redmine API key")
    connection_id = redmine_connection_id(project.project_url)
    api_key_env = redmine_api_key_env(project.project_url)

    secrets = _load_secrets(rag_root)
    redmine_secrets = dict(secrets.get("redmine") or {})
    redmine_secrets[connection_id] = {
        "ciphertext": _encrypt_secret(rag_root, secret),
        "updated_at": _now(),
    }
    secrets["redmine"] = redmine_secrets
    secrets["updated_at"] = _now()
    _save_secrets(rag_root, secrets)

    config = _load_connections(rag_root)
    redmine = dict(config.get("redmine") or {})
    redmine[connection_id] = {
        "api_root": project.api_root,
        "api_key_env": api_key_env,
        "updated_at": _now(),
    }
    config["redmine"] = redmine
    config["updated_at"] = _now()
    _save_connections(rag_root, config)
    return RedmineRegistration(
        connection_id=connection_id,
        api_root=project.api_root,
        api_key_env=api_key_env,
        registered=True,
    )


def register_gitlab_token(
    rag_root: str | Path,
    gitlab_url: Any,
    token: Any,
) -> GitLabRegistration:
    instance = _canonical_web_root(gitlab_url, field="GitLab URL")
    secret = _validate_secret(token, label="GitLab access token")
    connection_id = gitlab_connection_id(instance)
    token_env = gitlab_token_env(instance)

    secrets = _load_secrets(rag_root)
    gitlab_secrets = dict(secrets.get("gitlab") or {})
    gitlab_secrets[connection_id] = {
        "ciphertext": _encrypt_secret(rag_root, secret),
        "updated_at": _now(),
    }
    secrets["gitlab"] = gitlab_secrets
    secrets["updated_at"] = _now()
    _save_secrets(rag_root, secrets)

    config = _load_connections(rag_root)
    gitlab = dict(config.get("gitlab") or {})
    gitlab[connection_id] = {
        "gitlab_url": instance,
        "token_env": token_env,
        "updated_at": _now(),
    }
    config["gitlab"] = gitlab
    config["updated_at"] = _now()
    _save_connections(rag_root, config)
    return GitLabRegistration(
        connection_id=connection_id,
        gitlab_url=instance,
        token_env=token_env,
        registered=True,
    )


def has_stored_redmine_api_key(rag_root: str | Path, project_url: Any) -> bool:
    connection_id = redmine_connection_id(project_url)
    config = _load_connections(rag_root)
    configured = (config.get("redmine") or {}).get(connection_id)
    secrets = _load_secrets(rag_root)
    entry = (secrets.get("redmine") or {}).get(connection_id)
    return (
        isinstance(configured, Mapping)
        and isinstance(entry, Mapping)
        and bool(entry.get("ciphertext"))
    )


def has_stored_gitlab_token(
    rag_root: str | Path,
    gitlab_url: Any,
) -> bool:
    connection_id = gitlab_connection_id(gitlab_url)
    config = _load_connections(rag_root)
    configured = (config.get("gitlab") or {}).get(connection_id)
    secrets = _load_secrets(rag_root)
    entry = (secrets.get("gitlab") or {}).get(connection_id)
    return (
        isinstance(configured, Mapping)
        and isinstance(entry, Mapping)
        and bool(entry.get("ciphertext"))
    )


def has_stored_confluence_credentials(
    rag_root: str | Path,
    connection_id: Any,
) -> bool:
    identity = confluence_connection_id(connection_id)
    config = _load_connections(rag_root)
    configured = (config.get("confluence") or {}).get(identity)
    secrets = _load_secrets(rag_root)
    entry = (secrets.get("confluence") or {}).get(identity)
    return (
        isinstance(configured, Mapping)
        and isinstance(entry, Mapping)
        and bool(entry.get("ciphertext"))
    )


def list_redmine_registrations(
    rag_root: str | Path,
) -> tuple[RedmineRegistration, ...]:
    config = _load_connections(rag_root)
    secrets = _load_secrets(rag_root)
    secret_entries = secrets.get("redmine") or {}
    values: list[RedmineRegistration] = []
    for connection_id, raw in sorted(
        (config.get("redmine") or {}).items(),
        key=lambda item: str((item[1] or {}).get("api_root") or "").casefold(),
    ):
        if not isinstance(raw, Mapping):
            continue
        api_root = str(raw.get("api_root") or "").strip()
        api_key_env = str(raw.get("api_key_env") or "").strip()
        if not api_root or not _SAFE_ENV.fullmatch(api_key_env):
            continue
        secret = secret_entries.get(connection_id)
        values.append(
            RedmineRegistration(
                connection_id=str(connection_id),
                api_root=api_root,
                api_key_env=api_key_env,
                registered=isinstance(secret, Mapping)
                and bool(secret.get("ciphertext")),
            )
        )
    return tuple(values)


def list_gitlab_registrations(
    rag_root: str | Path,
) -> tuple[GitLabRegistration, ...]:
    config = _load_connections(rag_root)
    secrets = _load_secrets(rag_root)
    secret_entries = secrets.get("gitlab") or {}
    values: list[GitLabRegistration] = []
    for connection_id, raw in sorted(
        (config.get("gitlab") or {}).items(),
        key=lambda item: str(
            (item[1] or {}).get("gitlab_url") or ""
        ).casefold(),
    ):
        if not isinstance(raw, Mapping):
            continue
        gitlab_url = str(raw.get("gitlab_url") or "").strip()
        token_env = str(raw.get("token_env") or "").strip()
        if not gitlab_url or not _SAFE_ENV.fullmatch(token_env):
            continue
        secret = secret_entries.get(connection_id)
        values.append(
            GitLabRegistration(
                connection_id=str(connection_id),
                gitlab_url=gitlab_url,
                token_env=token_env,
                registered=isinstance(secret, Mapping)
                and bool(secret.get("ciphertext")),
            )
        )
    return tuple(values)


def list_confluence_registrations(
    rag_root: str | Path,
) -> tuple[ConfluenceRegistration, ...]:
    config = _load_connections(rag_root)
    secrets = _load_secrets(rag_root)
    public_entries = config.get("confluence") or {}
    secret_entries = secrets.get("confluence") or {}
    if not isinstance(public_entries, Mapping) or not isinstance(
        secret_entries, Mapping
    ):
        raise SourceManagerError("Confluence connection registry is invalid")
    tombstones = _validated_confluence_tombstones(secrets)
    if set(public_entries) & set(tombstones):
        raise SourceManagerError("Confluence connection registry is invalid")
    values: list[ConfluenceRegistration] = []
    for raw_id, raw in sorted(
        public_entries.items(),
        key=lambda item: str(
            (item[1] or {}).get("display_name")
            if isinstance(item[1], Mapping)
            else ""
        ).casefold(),
    ):
        if not isinstance(raw, Mapping):
            raise SourceManagerError("Confluence connection registry is invalid")
        _validate_confluence_public_entry_fields(raw)
        identity = confluence_connection_id(raw_id)
        name = _normalize_confluence_display_name(raw.get("display_name"))
        deployment = _normalize_confluence_deployment(raw.get("deployment"))
        base_url = _canonical_confluence_root(
            deployment,
            raw.get("base_url"),
        )
        token_kind = _normalize_confluence_token_kind(
            raw.get("token_kind"),
            deployment=deployment,
        )
        cloud_id = _normalize_confluence_cloud_id(
            raw.get("cloud_id"),
            required=deployment == "cloud" and token_kind == "scoped",
        )
        api_root = _confluence_api_root(
            deployment=deployment,
            base_url=base_url,
            token_kind=token_kind,
            cloud_id=cloud_id,
        )
        if raw.get("api_root") != api_root:
            raise SourceManagerError("Confluence connection registry is invalid")
        secret = secret_entries.get(identity)
        values.append(
            ConfluenceRegistration(
                connection_id=identity,
                display_name=name,
                deployment=deployment,
                base_url=base_url,
                token_kind=token_kind,
                cloud_id=cloud_id,
                api_root=api_root,
                registered=isinstance(secret, Mapping)
                and bool(secret.get("ciphertext")),
            )
        )
    return tuple(values)


def resolve_confluence_credentials(
    rag_root: str | Path,
    connection_id: Any,
) -> ResolvedConfluenceCredentials | None:
    identity = confluence_connection_id(connection_id)
    config = _load_connections(rag_root)
    secrets = _load_secrets(rag_root)
    public_entry = (config.get("confluence") or {}).get(identity)
    secret_entry = (secrets.get("confluence") or {}).get(identity)
    if public_entry is None and secret_entry is None:
        return None
    if not isinstance(public_entry, Mapping) or not isinstance(
        secret_entry, Mapping
    ):
        raise SourceManagerError(
            "Confluence connection registration is incomplete"
        )
    tombstones = _validated_confluence_tombstones(secrets)
    if identity in tombstones:
        raise SourceManagerError("Confluence connection registry is invalid")
    return _resolve_confluence_from_payloads(
        rag_root,
        identity,
        public_entry,
        secret_entry,
    )


def delete_confluence_connection(
    rag_root: str | Path,
    connection_id: Any,
) -> bool:
    identity = confluence_connection_id(connection_id)
    config = _load_connections(rag_root)
    secrets = _load_secrets(rag_root)
    public_entries = dict(config.get("confluence") or {})
    secret_entries = dict(secrets.get("confluence") or {})
    tombstones = _validated_confluence_tombstones(secrets)
    public_present = identity in public_entries
    secret_present = identity in secret_entries
    if not public_present and not secret_present:
        return False
    if public_present != secret_present:
        raise SourceManagerError(
            "Confluence connection registration is incomplete"
        )
    if identity in tombstones:
        raise SourceManagerError("Confluence connection registry is invalid")
    existing = _resolve_confluence_from_payloads(
        rag_root,
        identity,
        public_entries[identity],
        secret_entries[identity],
    )
    public_entries.pop(identity, None)
    secret_entries.pop(identity, None)
    updated_at = _now()
    tombstones[identity] = {
        "security_identity": existing.security_identity,
        "updated_at": updated_at,
    }
    config["confluence"] = public_entries
    config["updated_at"] = updated_at
    secrets["confluence"] = secret_entries
    secrets["confluence_tombstones"] = tombstones
    secrets["updated_at"] = updated_at
    _save_connection_pair(rag_root, config=config, secrets=secrets)
    return True


def resolve_redmine_api_key(
    rag_root: str | Path,
    *,
    project_url: Any,
    api_key_env: str | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    environment = os.environ if environ is None else environ
    requested_env = str(api_key_env or "").strip()

    # A value registered through the unified Manager screen is authoritative.
    # Environment variables remain a compatibility fallback for existing and
    # scripted installations that have not registered a machine-local key.
    connection_id = redmine_connection_id(project_url)
    secrets = _load_secrets(rag_root)
    entry = (secrets.get("redmine") or {}).get(connection_id)
    if isinstance(entry, Mapping) and entry.get("ciphertext"):
        return _decrypt_secret(rag_root, str(entry["ciphertext"]))

    if requested_env:
        inherited = str(environment.get(requested_env) or "").strip()
        if inherited:
            return inherited

    # Existing Sources may still point at the former shared environment name.
    legacy = str(environment.get(LEGACY_REDMINE_API_KEY_ENV) or "").strip()
    return legacy or None


def resolve_gitlab_token(
    rag_root: str | Path,
    *,
    gitlab_url: Any,
    token_env: str | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    environment = os.environ if environ is None else environ
    requested_env = str(token_env or "").strip()
    expected_env = gitlab_token_env(gitlab_url)
    if requested_env and requested_env != expected_env:
        raise SourceManagerError(
            "GitLab token environment does not match the GitLab URL"
        )
    connection_id = gitlab_connection_id(gitlab_url)
    secrets = _load_secrets(rag_root)
    entry = (secrets.get("gitlab") or {}).get(connection_id)
    if isinstance(entry, Mapping) and entry.get("ciphertext"):
        return _decrypt_secret(
            rag_root,
            str(entry["ciphertext"]),
            label="GitLab access token",
        )
    inherited = str(environment.get(expected_env) or "").strip()
    if inherited:
        return inherited
    return None


def check_gitlab_project(
    rag_root: str | Path,
    *,
    gitlab_url: Any,
    project_url: Any,
    token_env: str | None = None,
    environ: Mapping[str, str] | None = None,
    http_get: GitLabHttpGet | None = None,
) -> GitLabProjectCheck:
    """Resolve a project and verify that its Issues API is readable."""

    location = gitlab_project_location(gitlab_url, project_url)
    environment = os.environ if environ is None else environ
    environment_name = str(
        token_env or gitlab_token_env(location.gitlab_url)
    ).strip()
    token = resolve_gitlab_token(
        rag_root,
        gitlab_url=location.gitlab_url,
        token_env=environment_name,
        environ=environment,
    )
    if not token:
        raise SourceManagerError(
            "GitLab access token is not registered on this computer"
        )
    getter = http_get or _default_http_get
    headers = {
        "PRIVATE-TOKEN": token,
        "Accept": "application/json",
    }
    try:
        response = getter(
            location.project_api_url,
            headers,
            10.0,
        )
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
            or not 1 <= int(project_id) <= _MAX_PROJECT_ID
        ):
            raise SourceManagerError(
                "GitLab project connection check returned an invalid project ID"
            )
        _validate_gitlab_api_project_identity(
            payload,
            location.project_path,
            error_message=(
                "GitLab project connection check returned a different project"
            ),
        )
        issues_url = (
            f"{location.api_base_url}/projects/{int(project_id)}/issues"
            "?scope=all&state=all&per_page=1&page=1"
        )
        issues_response = getter(
            issues_url,
            headers,
            10.0,
        )
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
            payload.get("name_with_namespace") or payload.get("name") or ""
        )
        return GitLabProjectCheck(
            location=location,
            project_id=int(project_id),
            name=name.strip() or location.project_path,
        )
    finally:
        token = ""
        headers["PRIVATE-TOKEN"] = ""


def source_runtime_environment(
    rag_root: str | Path,
    source_payload: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    result = dict(os.environ if environ is None else environ)
    source_type = str(source_payload.get("source_type") or "").strip().lower()
    fetch = source_payload.get("fetch")
    settings = dict(fetch) if isinstance(fetch, Mapping) else {}

    if source_type == "sharepoint":
        name = str(settings.get("root_env") or SHAREPOINT_ROOT_ENV).strip()
        if name:
            root = configured_sharepoint_root(rag_root, environ=result)
            if root is not None:
                result[name] = str(root)
    elif source_type == "redmine":
        name = str(settings.get("api_key_env") or LEGACY_REDMINE_API_KEY_ENV).strip()
        project_url = settings.get("project_url")
        if name and project_url:
            secret = resolve_redmine_api_key(
                rag_root,
                project_url=project_url,
                api_key_env=name,
                environ=result,
            )
            if secret:
                result[name] = secret
    elif source_type == "gitlab_issues":
        gitlab_url = settings.get("gitlab_url")
        name = str(
            settings.get("token_env")
            or (
                gitlab_token_env(gitlab_url)
                if gitlab_url
                else ""
            )
        ).strip()
        if name and gitlab_url:
            secret = resolve_gitlab_token(
                rag_root,
                gitlab_url=gitlab_url,
                token_env=name,
                environ=result,
            )
            if secret:
                result[name] = secret
    return result


def install_machine_connection_runtime() -> None:
    from . import runner

    if bool(getattr(runner, _RUNTIME_PATCH_MARKER, False)):
        return
    original = runner.update_source

    @functools.wraps(original)
    def update_source(
        db_root: Path,
        local_source_key: str,
        *,
        executor: Any = None,
        python_executable: str | Path | None = None,
        rag_root: str | Path | None = None,
        command_runner: Any = None,
        http_get: Any = None,
        environment: Mapping[str, str] | None = None,
        metadata_publisher: Any = None,
        runtime_input: str | Path | None = None,
        clock: Any = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        effective_environment = environment
        if rag_root is not None:
            source = runner.SourceStore(Path(db_root)).read_source(local_source_key)
            if source.payload:
                effective_environment = source_runtime_environment(
                    Path(rag_root),
                    source.payload,
                    environ=environment,
                )
                source_type = str(source.payload.get("source_type") or "")
                fetch = source.payload.get("fetch")
                fetch = fetch if isinstance(fetch, Mapping) else {}
                if source_type == "redmine":
                    name = str(fetch.get("api_key_env") or "")
                    if not name or not str(effective_environment.get(name) or ""):
                        raise SourceManagerError(
                            "Redmine API key is not registered on this computer"
                        )
                if source_type == "gitlab_issues":
                    name = str(fetch.get("token_env") or "")
                    if not name or not str(effective_environment.get(name) or ""):
                        raise SourceManagerError(
                            "GitLab access token is not registered on this computer"
                        )
                if source_type == "sharepoint":
                    name = str(fetch.get("root_env") or SHAREPOINT_ROOT_ENV)
                    if not str(effective_environment.get(name) or ""):
                        raise SourceManagerError(
                            "SharePoint synchronization root is not registered on this computer"
                        )
        return original(
            db_root,
            local_source_key,
            executor=executor,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            http_get=http_get,
            environment=effective_environment,
            metadata_publisher=metadata_publisher,
            runtime_input=runtime_input,
            clock=clock,
            progress_callback=progress_callback,
        )

    runner.update_source = update_source
    setattr(runner, _RUNTIME_PATCH_MARKER, True)
    package = sys.modules.get(__package__)
    if package is not None:
        setattr(package, "update_source", update_source)


def _normalize_confluence_deployment(value: Any) -> str:
    deployment = str(value or "").strip().lower()
    if deployment not in _CONFLUENCE_DEPLOYMENTS:
        raise SourceManagerError(
            "Confluence deployment must be cloud or data_center"
        )
    return deployment


def _normalize_confluence_token_kind(
    value: Any,
    *,
    deployment: str,
) -> str:
    if deployment == "data_center":
        normalized = str(value or "pat").strip().lower()
        if normalized != "pat":
            raise SourceManagerError(
                "Confluence Data Center credential kind must be pat"
            )
        return normalized
    normalized = str(value or "").strip().lower()
    if normalized not in _CONFLUENCE_CLOUD_TOKEN_KINDS:
        raise SourceManagerError(
            "Confluence Cloud token kind must be unscoped or scoped"
        )
    return normalized


def _canonical_confluence_root(deployment: str, value: Any) -> str:
    root = _canonical_web_root(value, field="Confluence base URL")
    if deployment == "cloud":
        split = urllib.parse.urlsplit(root)
        path = split.path.rstrip("/")
        if path.casefold() == "/wiki":
            path = ""
        if path:
            raise SourceManagerError(
                "Confluence Cloud base URL must be the tenant root"
            )
        return urllib.parse.urlunsplit(
            (split.scheme, split.netloc, "", "", "")
        )
    return root


def _normalize_confluence_email(value: Any, *, deployment: str) -> str:
    email = str(value or "").strip()
    if deployment == "data_center":
        if email:
            raise SourceManagerError(
                "Confluence account email is only valid for Cloud"
            )
        return ""
    if (
        not email
        or len(email) > 320
        or any(ord(character) < 0x20 for character in email)
    ):
        raise SourceManagerError("Confluence Cloud account email is invalid")
    return email


def _normalize_confluence_principal(value: Any, *, label: str) -> str:
    principal = str(value or "").strip()
    if (
        not principal
        or len(principal) > 1024
        or any(ord(character) < 0x20 for character in principal)
    ):
        raise SourceManagerError(f"{label} is missing or invalid")
    return principal


def _normalize_confluence_display_name(value: Any) -> str:
    name = str(value or "").strip()
    if (
        not name
        or len(name) > 200
        or any(ord(character) < 0x20 for character in name)
    ):
        raise SourceManagerError("Confluence connection display name is invalid")
    return name


def _validate_confluence_public_entry_fields(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _CONFLUENCE_PUBLIC_FIELDS:
        raise SourceManagerError("Confluence public connection data is invalid")


def _validated_confluence_tombstones(
    secrets: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    raw_tombstones = secrets.get("confluence_tombstones") or {}
    if not isinstance(raw_tombstones, Mapping):
        raise SourceManagerError("Confluence connection tombstones are invalid")
    values: dict[str, dict[str, str]] = {}
    for raw_id, raw in raw_tombstones.items():
        identity = confluence_connection_id(raw_id)
        if identity != str(raw_id) or identity in values:
            raise SourceManagerError(
                "Confluence connection tombstones are invalid"
            )
        if not isinstance(raw, Mapping) or set(raw) != _CONFLUENCE_TOMBSTONE_FIELDS:
            raise SourceManagerError(
                "Confluence connection tombstones are invalid"
            )
        security_identity = str(raw.get("security_identity") or "")
        updated_at = str(raw.get("updated_at") or "").strip()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", security_identity)
            or not updated_at
            or any(ord(character) < 0x20 for character in updated_at)
        ):
            raise SourceManagerError(
                "Confluence connection tombstones are invalid"
            )
        values[identity] = {
            "security_identity": security_identity,
            "updated_at": updated_at,
        }
    return values


def _normalize_confluence_cloud_id(
    value: Any,
    *,
    required: bool,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise SourceManagerError("Confluence cloud ID is missing")
        return None
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise SourceManagerError("Confluence cloud ID is invalid") from exc


def _discover_confluence_cloud_id(
    getter: ConfluenceHttpGet,
    base_url: str,
) -> str | None:
    try:
        response = getter(
            f"{base_url}/_edge/tenant_info",
            {"Accept": "application/json"},
            10.0,
        )
        if int(response[0]) != 200:
            return None
        body = bytes(response[1])
        if len(body) > _MAX_CONFLUENCE_IDENTITY_BYTES:
            return None
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, Mapping):
            return None
        return _normalize_confluence_cloud_id(
            payload.get("cloudId"),
            required=True,
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None


def _confluence_api_root(
    *,
    deployment: str,
    base_url: str,
    token_kind: str,
    cloud_id: str | None,
) -> str:
    if deployment == "data_center":
        if token_kind != "pat" or cloud_id is not None:
            raise SourceManagerError("Confluence connection registry is invalid")
        return f"{base_url}/rest/api"
    if token_kind == "unscoped":
        if cloud_id is not None:
            raise SourceManagerError("Confluence connection registry is invalid")
        return f"{base_url}/wiki/api/v2"
    identity = _normalize_confluence_cloud_id(cloud_id, required=True)
    return (
        "https://api.atlassian.com/ex/confluence/"
        f"{identity}/wiki/api/v2"
    )


def _confluence_security_identity(
    *,
    deployment: str,
    base_url: str,
    token_kind: str,
    cloud_id: str | None,
    api_root: str,
    principal: str,
) -> str:
    body = "\0".join(
        (
            _CONFLUENCE_SECURITY_IDENTITY_SCHEMA,
            deployment,
            base_url,
            token_kind,
            cloud_id or "",
            api_root,
            principal,
        )
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validated_confluence_confirmation(
    value: Any,
) -> ConfluenceCredentialConfirmation:
    if not isinstance(value, ConfluenceCredentialConfirmation):
        raise SourceManagerError(
            "Confluence credentials must be confirmed before registration"
        )
    deployment = _normalize_confluence_deployment(value.deployment)
    base_url = _canonical_confluence_root(deployment, value.base_url)
    token_kind = _normalize_confluence_token_kind(
        value.token_kind,
        deployment=deployment,
    )
    cloud_id = _normalize_confluence_cloud_id(
        value.cloud_id,
        required=deployment == "cloud" and token_kind == "scoped",
    )
    api_root = _confluence_api_root(
        deployment=deployment,
        base_url=base_url,
        token_kind=token_kind,
        cloud_id=cloud_id,
    )
    if value.api_root != api_root:
        raise SourceManagerError("Confluence confirmed API root is invalid")
    email = _normalize_confluence_email(
        value.account_email,
        deployment=deployment,
    )
    token = _validate_secret(value.token, label="Confluence credential")
    principal = _normalize_confluence_principal(
        value.principal,
        label="Confluence stable principal",
    )
    identity = _confluence_security_identity(
        deployment=deployment,
        base_url=base_url,
        token_kind=token_kind,
        cloud_id=cloud_id,
        api_root=api_root,
        principal=principal,
    )
    if value.security_identity != identity:
        raise SourceManagerError(
            "Confluence confirmed security identity is invalid"
        )
    return ConfluenceCredentialConfirmation(
        deployment=deployment,
        base_url=base_url,
        token_kind=token_kind,
        cloud_id=cloud_id,
        api_root=api_root,
        account_email=email,
        token=token,
        principal=principal,
        security_identity=identity,
    )


def _encrypt_confluence_credentials(
    rag_root: str | Path,
    value: ConfluenceCredentialConfirmation,
) -> str:
    payload = {
        "schema_version": _CONFLUENCE_CREDENTIAL_SCHEMA,
        "deployment": value.deployment,
        "base_url": value.base_url,
        "token_kind": value.token_kind,
        "cloud_id": value.cloud_id,
        "api_root": value.api_root,
        "account_email": value.account_email,
        "token": value.token,
        "principal": value.principal,
        "security_identity": value.security_identity,
    }
    return _encrypt_secret(
        rag_root,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _resolve_confluence_from_payloads(
    rag_root: str | Path,
    connection_id: str,
    public_entry: Any,
    secret_entry: Any,
) -> ResolvedConfluenceCredentials:
    if not isinstance(public_entry, Mapping) or not isinstance(
        secret_entry, Mapping
    ):
        raise SourceManagerError(
            "Confluence connection registration is incomplete"
        )
    _validate_confluence_public_entry_fields(public_entry)
    ciphertext = str(secret_entry.get("ciphertext") or "").strip()
    if not ciphertext:
        raise SourceManagerError(
            "Confluence connection registration is incomplete"
        )
    try:
        payload = json.loads(
            _decrypt_secret(
                rag_root,
                ciphertext,
                label="Confluence credentials",
            )
        )
    except json.JSONDecodeError as exc:
        raise SourceManagerError(
            "stored Confluence credentials are invalid"
        ) from exc
    if not isinstance(payload, Mapping) or payload.get(
        "schema_version"
    ) != _CONFLUENCE_CREDENTIAL_SCHEMA:
        raise SourceManagerError("stored Confluence credentials are invalid")
    confirmation = _validated_confluence_confirmation(
        ConfluenceCredentialConfirmation(
            deployment=payload.get("deployment"),
            base_url=payload.get("base_url"),
            token_kind=payload.get("token_kind"),
            cloud_id=payload.get("cloud_id"),
            api_root=payload.get("api_root"),
            account_email=payload.get("account_email"),
            token=payload.get("token"),
            principal=payload.get("principal"),
            security_identity=payload.get("security_identity"),
        )
    )
    deployment = _normalize_confluence_deployment(public_entry.get("deployment"))
    base_url = _canonical_confluence_root(
        deployment,
        public_entry.get("base_url"),
    )
    token_kind = _normalize_confluence_token_kind(
        public_entry.get("token_kind"),
        deployment=deployment,
    )
    cloud_id = _normalize_confluence_cloud_id(
        public_entry.get("cloud_id"),
        required=deployment == "cloud" and token_kind == "scoped",
    )
    api_root = _confluence_api_root(
        deployment=deployment,
        base_url=base_url,
        token_kind=token_kind,
        cloud_id=cloud_id,
    )
    if public_entry.get("api_root") != api_root or any(
        (
            confirmation.deployment != deployment,
            confirmation.base_url != base_url,
            confirmation.token_kind != token_kind,
            confirmation.cloud_id != cloud_id,
            confirmation.api_root != api_root,
        )
    ):
        raise SourceManagerError("Confluence connection identity is inconsistent")
    return ResolvedConfluenceCredentials(
        connection_id=connection_id,
        deployment=deployment,
        base_url=base_url,
        token_kind=token_kind,
        cloud_id=cloud_id,
        api_root=api_root,
        auth_type="basic" if deployment == "cloud" else "bearer",
        account_email=confirmation.account_email,
        token=confirmation.token,
        principal=confirmation.principal,
        security_identity=confirmation.security_identity,
        email=confirmation.account_email if deployment == "cloud" else "",
        api_token=confirmation.token if deployment == "cloud" else "",
        password=confirmation.token if deployment == "data_center" else "",
    )


def _save_connection_pair(
    rag_root: str | Path,
    *,
    config: Mapping[str, Any],
    secrets: Mapping[str, Any],
) -> None:
    config_path = connection_config_path(rag_root)
    secret_path = connection_secret_path(rag_root)
    config_snapshot = _connection_file_snapshot(config_path)
    secret_snapshot = _connection_file_snapshot(secret_path)
    try:
        _save_secrets(rag_root, secrets)
        _save_connections(rag_root, config)
    except Exception:
        try:
            _restore_connection_file(secret_path, secret_snapshot)
            _restore_connection_file(config_path, config_snapshot)
        except Exception as rollback_error:
            raise SourceManagerError(
                "machine Source connection transaction rollback failed"
            ) from rollback_error
        raise


def _connection_file_snapshot(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SourceManagerError(
            f"machine Source connection settings are invalid: {path.name}"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SourceManagerError(
            f"machine Source connection settings are invalid: {path.name}"
        ) from exc


def _restore_connection_file(path: Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.rollback.",
        dir=str(path.parent),
    )
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
def _default_connections() -> dict[str, Any]:
    return {
        "schema_version": CONNECTION_SCHEMA_VERSION,
        "redmine": {},
        "gitlab": {},
        "confluence": {},
    }


def _default_secrets() -> dict[str, Any]:
    return {
        "schema_version": SECRET_SCHEMA_VERSION,
        "redmine": {},
        "gitlab": {},
        "confluence": {},
        "confluence_tombstones": {},
    }


def _load_connections(rag_root: str | Path) -> dict[str, Any]:
    return _load_json(
        connection_config_path(rag_root),
        schema=CONNECTION_SCHEMA_VERSION,
        default=_default_connections(),
    )


def _load_secrets(rag_root: str | Path) -> dict[str, Any]:
    return _load_json(
        connection_secret_path(rag_root),
        schema=SECRET_SCHEMA_VERSION,
        default=_default_secrets(),
    )


def _save_connections(rag_root: str | Path, value: Mapping[str, Any]) -> None:
    payload = copy.deepcopy(dict(value))
    payload["schema_version"] = CONNECTION_SCHEMA_VERSION
    payload.setdefault("redmine", {})
    payload.setdefault("gitlab", {})
    payload.setdefault("confluence", {})
    _atomic_json(connection_config_path(rag_root), payload)


def _save_secrets(rag_root: str | Path, value: Mapping[str, Any]) -> None:
    payload = copy.deepcopy(dict(value))
    payload["schema_version"] = SECRET_SCHEMA_VERSION
    payload.setdefault("redmine", {})
    payload.setdefault("gitlab", {})
    payload.setdefault("confluence", {})
    payload.setdefault("confluence_tombstones", {})
    _atomic_json(connection_secret_path(rag_root), payload)


def _load_json(
    path: Path,
    *,
    schema: str,
    default: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists():
        return copy.deepcopy(dict(default))
    try:
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.stat().st_size > _MAX_CONFIG_BYTES
        ):
            raise OSError("invalid settings file")
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceManagerError(
            f"machine Source connection settings are invalid: {candidate.name}"
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise SourceManagerError(
            f"machine Source connection settings use an unsupported schema: {candidate.name}"
        )
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _fernet(rag_root: str | Path) -> Fernet:
    path = connection_secret_key_path(rag_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("invalid key file")
            key = path.read_bytes().strip()
            return Fernet(key)
        except (OSError, ValueError) as exc:
            raise SourceManagerError("machine credential key is invalid") from exc
    key = Fernet.generate_key()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _fernet(rag_root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return Fernet(key)


def _encrypt_secret(rag_root: str | Path, value: str) -> str:
    token = _fernet(rag_root).encrypt(value.encode("utf-8"))
    return base64.urlsafe_b64encode(token).decode("ascii")


def _decrypt_secret(
    rag_root: str | Path,
    value: str,
    *,
    label: str = "Redmine API key",
) -> str:
    try:
        token = base64.urlsafe_b64decode(value.encode("ascii"))
        return _fernet(rag_root).decrypt(token).decode("utf-8")
    except (ValueError, UnicodeError, InvalidToken) as exc:
        raise SourceManagerError(f"stored {label} cannot be decrypted") from exc


def _validate_secret(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > _MAX_SECRET_CHARS
        or any(ord(character) < 0x20 for character in text)
    ):
        raise SourceManagerError(f"{label} is invalid")
    return text


def _canonical_web_root(value: Any, *, field: str) -> str:
    text = validate_web_url(value, field=field)
    split = urllib.parse.urlsplit(text)
    if split.query or split.fragment:
        raise SourceManagerError(
            f"{field} cannot contain a query or fragment"
        )
    try:
        _ = split.port
    except ValueError as exc:
        raise SourceManagerError(f"{field} port is invalid") from exc
    path = split.path.rstrip("/")
    if "\\" in path or any(
        urllib.parse.unquote(component) in {".", ".."}
        for component in path.split("/")
    ):
        raise SourceManagerError(f"{field} path is invalid")
    if path.casefold().endswith("/api/v4"):
        raise SourceManagerError(
            f"{field} must be the GitLab web root, not the API URL"
        )
    return urllib.parse.urlunsplit(
        (
            split.scheme.casefold(),
            split.netloc.casefold(),
            path,
            "",
            "",
        )
    ).rstrip("/")


def _default_http_get(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, bytes, Mapping[str, str]]:
    request = urllib.request.Request(
        url,
        headers=dict(headers),
        method="GET",
    )
    opener = reject_http_redirects(urllib.request.build_opener())
    try:
        with opener.open(request, timeout=timeout) as response:
            return (
                int(response.status),
                response.read(),
                dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(), dict(exc.headers or {})


def _usable_sharepoint_root(value: str | Path) -> Path | None:
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
            return None
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _required_sharepoint_root(value: str | Path) -> Path:
    candidate = _usable_sharepoint_root(value)
    if candidate is None:
        raise SourceManagerError(
            "SharePoint synchronization root must be an existing absolute directory"
        )
    return candidate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
