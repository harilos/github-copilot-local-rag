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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

from .errors import SourceManagerError
from .redmine import parse_redmine_project_url


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
    secret = _validate_secret(api_key)
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


def resolve_redmine_api_key(
    rag_root: str | Path,
    *,
    project_url: Any,
    api_key_env: str | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    environment = os.environ if environ is None else environ
    requested_env = str(api_key_env or "").strip()
    if requested_env and requested_env != LEGACY_REDMINE_API_KEY_ENV:
        inherited = str(environment.get(requested_env) or "").strip()
        if inherited:
            return inherited

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
    }


def _default_secrets() -> dict[str, Any]:
    return {
        "schema_version": SECRET_SCHEMA_VERSION,
        "redmine": {},
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
    _atomic_json(connection_config_path(rag_root), payload)


def _save_secrets(rag_root: str | Path, value: Mapping[str, Any]) -> None:
    payload = copy.deepcopy(dict(value))
    payload["schema_version"] = SECRET_SCHEMA_VERSION
    payload.setdefault("redmine", {})
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


def _decrypt_secret(rag_root: str | Path, value: str) -> str:
    try:
        token = base64.urlsafe_b64decode(value.encode("ascii"))
        return _fernet(rag_root).decrypt(token).decode("utf-8")
    except (ValueError, UnicodeError, InvalidToken) as exc:
        raise SourceManagerError("stored Redmine API key cannot be decrypted") from exc


def _validate_secret(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > _MAX_SECRET_CHARS
        or any(ord(character) < 0x20 for character in text)
    ):
        raise SourceManagerError("Redmine API key is invalid")
    return text


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
