from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

from .errors import SourceManagerError


_SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_token",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "oauth",
    "password",
    "passwd",
    "private_key",
    "proxy",
    "secret",
    "signature",
    "token",
)
_SENSITIVE_QUERY_PARTS = (
    "access_token",
    "auth",
    "authorization",
    "code",
    "cookie",
    "credential",
    "key",
    "oauth",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_UNC = re.compile(r"^(?:\\\\|//)[^\\/]+[\\/][^\\/]+")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?:password|passwd|secret|token|authorization|cookie)\s*[:=]"
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


def validate_persistable(value: Any, *, field: str = "payload") -> None:
    """Reject credentials and host-absolute paths from persisted data."""
    _validate_persistable(value, field=field, depth=0)


def validate_web_url(value: Any, *, field: str) -> str:
    text = _bounded_text(value, field=field, limit=4096)
    try:
        split = urlsplit(text)
        hostname = split.hostname
        port = split.port
    except ValueError as exc:
        raise SourceManagerError(f"{field} contains an invalid URL") from exc
    if split.scheme.casefold() not in {"http", "https"} or not split.netloc:
        raise SourceManagerError(f"{field} must be an HTTP or HTTPS URL")
    if not hostname:
        raise SourceManagerError(f"{field} must include a host")
    host_port = split.netloc.rsplit("@", 1)[-1]
    if host_port.endswith(":") or (
        port is not None and not 1 <= port <= 65535
    ):
        raise SourceManagerError(f"{field} contains an invalid port")
    if split.username is not None or split.password is not None:
        raise SourceManagerError(f"{field} must not contain credentials")
    for name, _query_value in parse_qsl(split.query, keep_blank_values=True):
        lowered = name.casefold().replace("-", "_")
        if any(part in lowered for part in _SENSITIVE_QUERY_PARTS):
            raise SourceManagerError(f"{field} must not contain credentials")
    if _CREDENTIAL_ASSIGNMENT.search(text):
        raise SourceManagerError(f"{field} must not contain credentials")
    return text


def validate_svn_fetch_url(value: Any, *, field: str) -> str:
    """Validate a credential-free URL that is safe to pass to svn checkout."""
    text = _bounded_text(value, field=field, limit=4096)
    try:
        split = urlsplit(text)
    except ValueError as exc:
        raise SourceManagerError(f"{field} contains an invalid URL") from exc
    if split.scheme.casefold() not in {"http", "https", "svn"}:
        raise SourceManagerError(
            f"{field} must use HTTP, HTTPS, or SVN"
        )
    if not split.netloc:
        raise SourceManagerError(f"{field} must include a host")
    try:
        hostname = split.hostname
        port = split.port
    except ValueError as exc:
        raise SourceManagerError(
            f"{field} contains an invalid host or port"
        ) from exc
    if not hostname:
        raise SourceManagerError(f"{field} must include a host")
    host_port = split.netloc.rsplit("@", 1)[-1]
    if host_port.endswith(":"):
        raise SourceManagerError(f"{field} contains an invalid port")
    if port is not None and not 1 <= port <= 65535:
        raise SourceManagerError(f"{field} contains an invalid port")
    if split.username is not None or split.password is not None:
        raise SourceManagerError(f"{field} must not contain credentials")
    if not split.path or not split.path.strip("/"):
        raise SourceManagerError(f"{field} must include a repository path")
    if "?" in text or "#" in text:
        raise SourceManagerError(
            f"{field} cannot contain query or fragment"
        )
    if _CREDENTIAL_ASSIGNMENT.search(text):
        raise SourceManagerError(f"{field} must not contain credentials")
    return text


def validate_environment_name(value: Any, *, field: str) -> str:
    name = _bounded_text(value, field=field, limit=128)
    if not _ENVIRONMENT_NAME.fullmatch(name):
        raise SourceManagerError(
            f"{field} must be an uppercase environment variable name"
        )
    return name


def validate_relative_path(
    value: Any,
    *,
    field: str,
    allow_empty: bool = True,
) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.strip("/")
    if not text:
        if allow_empty:
            return ""
        raise SourceManagerError(f"{field} is required")
    if _looks_absolute_path(str(value)) or any(
        component in {"", ".", ".."}
        for component in text.split("/")
    ):
        raise SourceManagerError(
            f"{field} must be a safe relative path"
        )
    if len(text) > 2048:
        raise SourceManagerError(f"{field} is too long")
    return text


def redact_runtime_path(
    value: str | Path,
    *,
    roots: Iterable[tuple[str | Path, str]] | None = None,
) -> str:
    """Replace a runtime-only absolute root with a non-sensitive marker."""
    text = str(value)
    candidates = list(roots or ())
    if not candidates:
        candidates.append((tempfile.gettempdir(), "<TEMP_ROOT>"))
    normalized_value = _portable_path(text)
    for root, marker in candidates:
        normalized_root = _portable_path(str(root)).rstrip("/")
        if not normalized_root:
            continue
        folded_value = normalized_value.casefold()
        folded_root = normalized_root.casefold()
        if folded_value == folded_root:
            return marker
        if folded_value.startswith(folded_root + "/"):
            suffix = normalized_value[len(normalized_root) + 1 :]
            return f"{marker}/{suffix}" if suffix else marker
    if _looks_absolute_path(text):
        return "<ABSOLUTE_PATH>"
    return text.replace("\\", "/")


def redact_runtime_paths(
    value: Any,
    *,
    roots: Iterable[tuple[str | Path, str]] | None = None,
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): redact_runtime_paths(item, roots=roots)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_runtime_paths(item, roots=roots)
            for item in value
        ]
    if isinstance(value, Path):
        return redact_runtime_path(value, roots=roots)
    if isinstance(value, str) and _looks_absolute_path(value):
        return redact_runtime_path(value, roots=roots)
    return value


def _validate_persistable(value: Any, *, field: str, depth: int) -> None:
    if depth > 20:
        raise SourceManagerError(f"{field} is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, Path):
        raise SourceManagerError(f"{field} must not store host paths")
    if isinstance(value, str):
        if len(value) > 16_384:
            raise SourceManagerError(f"{field} is too long")
        if _looks_absolute_path(value):
            raise SourceManagerError(
                f"{field} must not store an absolute host path"
            )
        if _CREDENTIAL_ASSIGNMENT.search(value):
            raise SourceManagerError(f"{field} must not store credentials")
        try:
            split = urlsplit(value)
        except ValueError as exc:
            raise SourceManagerError(f"{field} contains an invalid URL") from exc
        if split.scheme.casefold() in {"http", "https"}:
            validate_web_url(value, field=field)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.casefold().replace("-", "_")
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                if not (
                    lowered.endswith("_env")
                    and isinstance(item, str)
                    and _ENVIRONMENT_NAME.fullmatch(item)
                ):
                    raise SourceManagerError(
                        f"{field} must not store credentials"
                    )
            _validate_persistable(
                item,
                field=f"{field}.{key_text}",
                depth=depth + 1,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_persistable(
                item,
                field=f"{field}[{index}]",
                depth=depth + 1,
            )
        return
    raise SourceManagerError(f"{field} contains an unsupported value")


def _looks_absolute_path(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    try:
        split = urlsplit(text)
    except ValueError:
        return False
    if split.scheme.casefold() in {"http", "https"} and split.netloc:
        return False
    return (
        text.startswith("/")
        or bool(_WINDOWS_DRIVE.match(text))
        or bool(_WINDOWS_UNC.match(text))
    )


def _portable_path(value: str) -> str:
    text = str(value).replace("\\", "/")
    while "//" in text and not text.startswith("//"):
        text = text.replace("//", "/")
    return text.rstrip("/")


def _bounded_text(value: Any, *, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise SourceManagerError(f"{field} is required")
    if len(text) > limit:
        raise SourceManagerError(f"{field} is too long")
    if any(ord(character) < 32 for character in text):
        raise SourceManagerError(f"{field} contains control characters")
    return text
