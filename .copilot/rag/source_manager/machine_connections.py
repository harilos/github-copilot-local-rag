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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.fernet import Fernet, InvalidToken

from .errors import SourceManagerError
from .gitlab_issues import (
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


GitLabHttpGet = Callable[
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
        returned_web_url = payload.get("web_url")
        returned = gitlab_project_location(
            location.gitlab_url,
            returned_web_url,
        )
        if returned.project_path != location.project_path:
            raise SourceManagerError(
                "GitLab project connection check returned a different project"
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
            location=returned,
            project_id=int(project_id),
            name=name.strip() or returned.project_path,
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


def _default_connections() -> dict[str, Any]:
    return {
        "schema_version": CONNECTION_SCHEMA_VERSION,
        "redmine": {},
        "gitlab": {},
    }


def _default_secrets() -> dict[str, Any]:
    return {
        "schema_version": SECRET_SCHEMA_VERSION,
        "redmine": {},
        "gitlab": {},
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
    _atomic_json(connection_config_path(rag_root), payload)


def _save_secrets(rag_root: str | Path, value: Mapping[str, Any]) -> None:
    payload = copy.deepcopy(dict(value))
    payload["schema_version"] = SECRET_SCHEMA_VERSION
    payload.setdefault("redmine", {})
    payload.setdefault("gitlab", {})
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
