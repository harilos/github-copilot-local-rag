from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import string
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit


SCHEMA_VERSION = "rag-source-links-v1"
SIDECAR_NAME = "source-links.json"
BACKUP_NAME = "source-links.json.bak"
MAX_SIDECAR_BYTES = 1_048_576
MAX_SOURCES = 500
MAX_MAPPINGS = 2_000
MAX_URL_LENGTH = 4_096
MAX_PATH_LENGTH = 2_048
MAX_PATTERN_LENGTH = 300
LOCK_WAIT_SECONDS = 2.0
LOCK_STALE_SECONDS = 30.0

_ALLOWED_PROVIDERS = {"sharepoint", "github", "redmine", "other"}
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "cookie",
    "password",
    "proxy",
    "oauth",
    "private_key",
    "secret",
)
_SENSITIVE_QUERY_PARTS = (
    "access_token",
    "auth",
    "code",
    "cookie",
    "credential",
    "key",
    "oauth",
    "password",
    "private_key",
    "proxy",
    "secret",
    "sig",
    "signature",
    "token",
)
_CACHE_LOCK = threading.Lock()
_CACHE: dict[
    tuple[Path, str],
    tuple[tuple[int, int, str], "SourceLinksLoad"],
] = {}


class SourceLinkError(ValueError):
    pass


@dataclass(frozen=True)
class SourceLinksLoad:
    status: str
    payload: dict[str, Any] | None
    error_kind: str | None = None


def load_source_links(
    db_root: Path,
    db_name: str | None = None,
) -> SourceLinksLoad:
    """Read and validate the optional DB-local sidecar without writing."""
    root = Path(db_root).expanduser().resolve()
    path = root / SIDECAR_NAME
    cache_key = (path, str(db_name or ""))
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        fingerprint = (0, 0, "")
        loaded = SourceLinksLoad("unconfigured", None)
        with _CACHE_LOCK:
            _CACHE[cache_key] = (fingerprint, loaded)
        return loaded
    except OSError as exc:
        loaded = SourceLinksLoad(
            "invalid",
            None,
            type(exc).__name__,
        )
        with _CACHE_LOCK:
            _drop_cached_path(path)
        return loaded
    if len(raw) > MAX_SIDECAR_BYTES:
        loaded = SourceLinksLoad("invalid", None, "sidecar_too_large")
        with _CACHE_LOCK:
            _drop_cached_path(path)
        return loaded
    try:
        stat = path.stat()
    except OSError as exc:
        return SourceLinksLoad("invalid", None, type(exc).__name__)
    digest = hashlib.sha256(raw).hexdigest()
    fingerprint = (stat.st_mtime_ns, len(raw), digest)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] == fingerprint:
            return _clone_load(cached[1])
    try:
        payload = json.loads(raw.decode("utf-8"))
        payload = validate_source_links(
            payload,
            expected_database=db_name,
            allow_ambiguous=True,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceLinkError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        loaded = SourceLinksLoad("invalid", None, type(exc).__name__)
    else:
        loaded = SourceLinksLoad("configured", payload)
    with _CACHE_LOCK:
        # Invalid or removed settings replace any previously valid cache entry.
        _CACHE[cache_key] = (fingerprint, _clone_load(loaded))
    return _clone_load(loaded)


def validate_source_links(
    payload: Any,
    *,
    expected_database: str | None = None,
    existing_sources: Iterable[str] | None = None,
    observed_paths: dict[str, Iterable[str]] | None = None,
    allow_ambiguous: bool = False,
    allow_unmatched_sources: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SourceLinkError("source-links sidecar must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SourceLinkError("unsupported source-links schema")
    database = _bounded_text(payload.get("database"), "database", 200)
    if expected_database and database != expected_database:
        raise SourceLinkError("source-links database does not match the DB")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise SourceLinkError("source-links revision must be a positive integer")
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise SourceLinkError("source-links sources must be a bounded array")
    known_sources = (
        {str(value) for value in existing_sources}
        if existing_sources is not None
        else None
    )
    normalized_sources: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    mapping_count = 0
    for source in sources:
        if not isinstance(source, dict):
            raise SourceLinkError("each Source setting must be an object")
        source_id = _bounded_text(source.get("source_id"), "source_id", 200)
        if source_id in seen_sources:
            raise SourceLinkError("duplicate source_id in source-links")
        seen_sources.add(source_id)
        if (
            known_sources is not None
            and source_id not in known_sources
            and not allow_unmatched_sources
        ):
            raise SourceLinkError("mapping Source does not exist in the catalog")
        display_name = str(source.get("display_name") or "").strip()
        if len(display_name) > 300:
            raise SourceLinkError("display_name is too long")
        mappings = source.get("mappings")
        if not isinstance(mappings, list):
            raise SourceLinkError("Source mappings must be an array")
        normalized_mappings: list[dict[str, Any]] = []
        seen_mapping_ids: set[str] = set()
        seen_prefixes: set[str] = set()
        paths = [
            _normalize_stored_path(value)
            for value in (observed_paths or {}).get(source_id, [])
        ]
        for mapping in mappings:
            normalized = validate_mapping(mapping)
            mapping_id = normalized["mapping_id"]
            if mapping_id in seen_mapping_ids:
                raise SourceLinkError("duplicate mapping_id")
            seen_mapping_ids.add(mapping_id)
            prefix = normalized["path_prefix"]
            if prefix in seen_prefixes and not allow_ambiguous:
                raise SourceLinkError(
                    "duplicate path_prefix creates ambiguous priority"
                )
            seen_prefixes.add(prefix)
            if (
                observed_paths is not None
                and source_id in observed_paths
                and not any(
                _prefix_matches(path, prefix) for path in paths
                )
            ):
                raise SourceLinkError(
                    "path_prefix does not match an indexed document"
                )
            normalized_mappings.append(normalized)
            mapping_count += 1
            if mapping_count > MAX_MAPPINGS:
                raise SourceLinkError("too many Source-Link mappings")
        normalized_source: dict[str, Any] = {
            "source_id": source_id,
            "mappings": normalized_mappings,
        }
        if display_name:
            normalized_source["display_name"] = display_name
        normalized_sources.append(normalized_source)
    return {
        "schema_version": SCHEMA_VERSION,
        "database": database,
        "revision": revision,
        "sources": normalized_sources,
    }


def validate_mapping(mapping: Any) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        raise SourceLinkError("mapping must be an object")
    mapping_id = str(mapping.get("mapping_id") or uuid.uuid4())
    try:
        mapping_id = str(uuid.UUID(mapping_id))
    except ValueError as exc:
        raise SourceLinkError("mapping_id must be a UUID") from exc
    enabled = mapping.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SourceLinkError("mapping enabled must be boolean")
    path_prefix = _normalize_path_prefix(mapping.get("path_prefix"))
    provider = str(mapping.get("provider") or "").strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise SourceLinkError("unsupported Source-Link provider")
    strategy = str(mapping.get("strategy") or "").strip().lower()
    settings = mapping.get("settings")
    if not isinstance(settings, dict):
        raise SourceLinkError("mapping settings must be an object")
    _reject_sensitive_keys(settings)
    normalized_settings = _validate_provider_settings(
        provider,
        strategy,
        settings,
    )
    return {
        "mapping_id": mapping_id,
        "enabled": enabled,
        "path_prefix": path_prefix,
        "provider": provider,
        "strategy": strategy,
        "settings": normalized_settings,
    }


def save_source_links(
    db_root: Path,
    payload: dict[str, Any],
    *,
    db_name: str | None = None,
    existing_sources: Iterable[str] | None = None,
    observed_paths: dict[str, Iterable[str]] | None = None,
    allow_unmatched_sources: bool = False,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    root = Path(db_root).expanduser().resolve()
    if not root.is_dir():
        raise SourceLinkError("database directory does not exist")
    normalized = validate_source_links(
        payload,
        expected_database=db_name,
        existing_sources=existing_sources,
        observed_paths=observed_paths,
        allow_unmatched_sources=allow_unmatched_sources,
    )
    encoded = (
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_SIDECAR_BYTES:
        raise SourceLinkError("source-links sidecar is too large")
    current = root / SIDECAR_NAME
    backup = root / BACKUP_NAME
    lock = root / ".source-links.lock"
    descriptor = _acquire_lock(lock)
    temporary = root / f".{SIDECAR_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    backup_temporary = root / (
        f".{BACKUP_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        if expected_revision is not None:
            current_revision = _read_current_revision(
                current,
                db_name,
            )
            if current_revision != expected_revision:
                raise SourceLinkError(
                    "source-links changed since it was opened"
                )
        _write_bytes(temporary, encoded)
        if current.exists():
            previous = current.read_bytes()
            _write_bytes(backup_temporary, previous)
            os.replace(backup_temporary, backup)
        os.replace(temporary, current)
        _fsync_directory(root)
    finally:
        for candidate in (temporary, backup_temporary):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        try:
            os.close(descriptor)
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
    with _CACHE_LOCK:
        _drop_cached_path(current)
    return normalized


def resolve_mapping_preview(
    mapping: dict[str, Any],
    stored_paths: Iterable[str],
) -> list[dict[str, Any]]:
    normalized = validate_mapping(mapping)
    output: list[dict[str, Any]] = []
    prefix = normalized["path_prefix"]
    for stored_path in list(stored_paths)[:5]:
        path = _normalize_stored_path(stored_path)
        if not _prefix_matches(path, prefix):
            output.append({"path": path, "status": "prefix_not_matched"})
            continue
        relative = _strip_prefix(path, prefix)
        resolved = _generate_provider_urls(normalized, relative)
        output.append(
            {
                "path": path,
                "status": "resolved" if resolved else "unconfigured",
                **resolved,
            }
        )
    return output


def enrich_search_payload(
    payload: dict[str, Any],
    db_root: Path,
    db_name: str,
    *,
    explain: bool = False,
) -> dict[str, Any]:
    """Add optional links after retrieval without changing result semantics."""
    enriched = dict(payload)
    loaded = load_source_links(db_root, db_name)
    for key in (
        "evidence",
        "contexts",
        "background_context",
        "related_context",
        "document_results",
        "_result_detail_items",
    ):
        values = enriched.get(key)
        if not isinstance(values, list):
            continue
        updated: list[Any] = []
        for value in values:
            if not isinstance(value, dict):
                updated.append(value)
                continue
            item = dict(value)
            source = item.get("source")
            if isinstance(source, dict):
                source = dict(source)
                source_id = str(
                    item.pop("_source_id", "")
                    or source.pop("_source_id", "")
                )
                item["source"] = source
                path = str(source.get("path") or "")
            else:
                source_id = str(item.pop("_source_id", ""))
                path = str(item.get("path") or "")
            status = "unconfigured"
            resolved: dict[str, str] = {}
            if loaded.status == "configured" and loaded.payload is not None:
                try:
                    resolved, status = _resolve_from_payload(
                        loaded.payload,
                        source_id,
                        path,
                    )
                except (SourceLinkError, ValueError, KeyError):
                    status = "resolution_failed"
            elif loaded.status == "invalid":
                status = "resolution_failed"
            item.update(resolved)
            if explain:
                item["source_link_status"] = status
            updated.append(item)
        enriched[key] = updated
    return enriched


def preferred_source_link(item: dict[str, Any]) -> str:
    return str(
        item.get("source_permalink")
        or item.get("source_url")
        or ((item.get("source") or {}).get("path") if isinstance(item.get("source"), dict) else "")
        or item.get("path")
        or ""
    )


def _resolve_from_payload(
    payload: dict[str, Any],
    source_id: str,
    stored_path: str,
) -> tuple[dict[str, str], str]:
    if not source_id or not stored_path:
        return {}, "unconfigured"
    try:
        path = _normalize_stored_path(stored_path)
    except SourceLinkError:
        return {}, "resolution_failed"
    source = next(
        (
            item
            for item in payload.get("sources") or []
            if item.get("source_id") == source_id
        ),
        None,
    )
    if not isinstance(source, dict):
        return {}, "unconfigured"
    candidates = [
        mapping
        for mapping in source.get("mappings") or []
        if mapping.get("enabled") is True
        and _prefix_matches(path, str(mapping.get("path_prefix") or ""))
    ]
    if not candidates:
        return {}, "unconfigured"
    maximum = max(
        len(PurePosixPath(str(item["path_prefix"]).rstrip("/")).parts)
        if item.get("path_prefix")
        else 0
        for item in candidates
    )
    best = [
        item
        for item in candidates
        if (
            len(PurePosixPath(str(item["path_prefix"]).rstrip("/")).parts)
            if item.get("path_prefix")
            else 0
        )
        == maximum
    ]
    if len(best) != 1:
        return {}, "ambiguous"
    mapping = best[0]
    try:
        relative = _strip_prefix(path, str(mapping.get("path_prefix") or ""))
        resolved = _generate_provider_urls(mapping, relative)
    except (SourceLinkError, ValueError, KeyError):
        return {}, "resolution_failed"
    if not resolved:
        return {}, "unconfigured"
    return resolved, "resolved"


def _validate_provider_settings(
    provider: str,
    strategy: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    if provider == "sharepoint":
        if strategy not in {"home-only", "append-relative-path"}:
            raise SourceLinkError("unsupported SharePoint strategy")
        home = _optional_url(settings.get("source_home_url"))
        web_root = _optional_url(settings.get("source_web_root"))
        if web_root:
            web_root = _normalize_sharepoint_root(web_root)
            _required_root_url(web_root)
        if strategy == "home-only" and not home:
            raise SourceLinkError("SharePoint home-only requires source_home_url")
        if strategy == "append-relative-path" and not web_root:
            raise SourceLinkError(
                "SharePoint file links require source_web_root"
            )
        output: dict[str, Any] = {}
        if home:
            output["source_home_url"] = home
        if web_root:
            output["source_web_root"] = web_root
        return output
    if provider == "github":
        if strategy not in {"github-blob", "append-relative-path"}:
            raise SourceLinkError("unsupported GitHub strategy")
        repository_url = _required_url(settings.get("repository_url"))
        split = urlsplit(repository_url)
        if split.query or split.fragment:
            raise SourceLinkError("repository_url cannot contain query or fragment")
        path = split.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        repository_url = urlunsplit(
            (split.scheme, split.netloc, path, "", "")
        ).rstrip("/")
        ref = _bounded_text(settings.get("ref"), "ref", 300)
        prefix = _normalize_optional_relative_path(
            settings.get("repository_path_prefix")
        )
        commit = str(settings.get("commit") or "").strip()
        if len(commit) > 300:
            raise SourceLinkError("commit is too long")
        permalink_enabled = settings.get("permalink_enabled", False)
        if not isinstance(permalink_enabled, bool):
            raise SourceLinkError("permalink_enabled must be boolean")
        if permalink_enabled and not commit:
            raise SourceLinkError(
                "commit is required when permalinks are enabled"
            )
        output = {
            "repository_url": repository_url,
            "ref": ref,
            "permalink_enabled": permalink_enabled,
        }
        if prefix:
            output["repository_path_prefix"] = prefix
        if commit:
            output["commit"] = commit
        return output
    if strategy == "home-only":
        return {"source_home_url": _required_url(settings.get("source_home_url"))}
    if strategy == "append-relative-path":
        return {
            "source_web_root": _required_root_url(
                settings.get("source_web_root")
            )
        }
    if strategy != "regex-template":
        raise SourceLinkError("unsupported Web provider strategy")
    pattern = _bounded_text(settings.get("path_pattern"), "path_pattern", MAX_PATTERN_LENGTH)
    _validate_safe_pattern(pattern)
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise SourceLinkError("invalid path_pattern") from exc
    if not compiled.groupindex:
        raise SourceLinkError("path_pattern requires a named group")
    template = _bounded_text(settings.get("url_template"), "url_template", MAX_URL_LENGTH)
    try:
        parsed_fields = [
            (field_name, format_spec, conversion)
            for _literal, field_name, format_spec, conversion
            in string.Formatter().parse(template)
            if field_name
        ]
    except ValueError as exc:
        raise SourceLinkError("invalid url_template") from exc
    if any(
        format_spec or conversion
        for _field, format_spec, conversion in parsed_fields
    ):
        raise SourceLinkError(
            "url_template format specifications are not allowed"
        )
    fields = {field_name for field_name, _format, _conversion in parsed_fields}
    if not fields or not fields.issubset(compiled.groupindex):
        raise SourceLinkError(
            "url_template placeholders must be named regex groups"
        )
    preview_values = {name: "example" for name in compiled.groupindex}
    _required_url(template.format_map(preview_values))
    return {
        "path_pattern": pattern,
        "url_template": template,
    }


def _generate_provider_urls(
    mapping: dict[str, Any],
    relative_path: str,
) -> dict[str, str]:
    relative = _normalize_relative_result_path(relative_path)
    provider = str(mapping["provider"])
    strategy = str(mapping["strategy"])
    settings = dict(mapping.get("settings") or {})
    if strategy == "home-only":
        return {}
    output: dict[str, str] = {"source_provider": provider}
    if provider == "sharepoint":
        output["source_url"] = _append_encoded_path(
            str(settings["source_web_root"]),
            relative,
        )
    elif provider == "github":
        repository = str(settings["repository_url"]).rstrip("/")
        repo_prefix = str(
            settings.get("repository_path_prefix") or ""
        ).strip("/")
        file_path = "/".join(
            value for value in (repo_prefix, relative) if value
        )
        output["source_url"] = (
            f"{repository}/blob/{_encode_path(str(settings['ref']))}"
            + (f"/{_encode_path(file_path)}" if file_path else "")
        )
        if settings.get("permalink_enabled") and settings.get("commit"):
            output["source_permalink"] = (
                f"{repository}/blob/{_encode_path(str(settings['commit']))}"
                + (f"/{_encode_path(file_path)}" if file_path else "")
            )
    elif strategy == "append-relative-path":
        output["source_url"] = _append_encoded_path(
            str(settings["source_web_root"]),
            relative,
        )
    else:
        pattern = re.compile(str(settings["path_pattern"]))
        match = pattern.search(relative)
        if not match:
            return {}
        values = {
            key: quote(str(value), safe="")
            for key, value in match.groupdict().items()
            if value is not None
        }
        try:
            generated = str(settings["url_template"]).format_map(values)
        except KeyError as exc:
            raise SourceLinkError("template placeholder is unavailable") from exc
        output["source_url"] = _required_url(generated)
    for key in ("source_url", "source_permalink"):
        if key in output:
            output[key] = _required_url(output[key])
    return output


def _normalize_sharepoint_root(value: str) -> str:
    split = urlsplit(value)
    lowered = split.path.casefold()
    if "/forms/allitems.aspx" not in lowered:
        if "/:" in split.path:
            raise SourceLinkError("opaque sharing links cannot be path roots")
        return value.rstrip("/")
    query = parse_qs(split.query)
    selected = (query.get("id") or query.get("RootFolder") or [None])[0]
    if not selected:
        raise SourceLinkError("SharePoint folder browser URL has no folder")
    folder = unquote(str(selected))
    parsed_folder = urlsplit(folder)
    if parsed_folder.scheme or parsed_folder.netloc:
        if (
            parsed_folder.scheme.casefold() != split.scheme.casefold()
            or parsed_folder.netloc.casefold() != split.netloc.casefold()
        ):
            raise SourceLinkError("SharePoint folder URL changes origin")
        folder = parsed_folder.path
    if not folder.startswith("/"):
        raise SourceLinkError("SharePoint folder path must be server-relative")
    normalized_path = _encode_path(_normalize_absolute_web_path(folder))
    return urlunsplit(
        (split.scheme, split.netloc, "/" + normalized_path, "", "")
    ).rstrip("/")


def _required_url(value: Any) -> str:
    text = _bounded_text(value, "URL", MAX_URL_LENGTH)
    split = urlsplit(text)
    if (
        split.scheme.casefold() not in {"http", "https"}
        or not split.hostname
        or split.username is not None
        or split.password is not None
        or any(char.isspace() for char in text)
    ):
        raise SourceLinkError("URL must be HTTP(S) without credentials")
    for key in parse_qs(split.query, keep_blank_values=True):
        normalized = key.casefold()
        if any(part in normalized for part in _SENSITIVE_QUERY_PARTS):
            raise SourceLinkError(
                "URL query parameters must not contain credentials"
            )
    fragment = split.fragment.casefold()
    if any(part in fragment for part in _SENSITIVE_QUERY_PARTS):
        raise SourceLinkError("URL fragments must not contain credentials")
    return text


def _required_root_url(value: Any) -> str:
    text = _required_url(value)
    split = urlsplit(text)
    if split.query or split.fragment:
        raise SourceLinkError(
            "a per-document URL root cannot contain a query or fragment"
        )
    return text.rstrip("/")


def _optional_url(value: Any) -> str:
    text = str(value or "").strip()
    return _required_url(text) if text else ""


def _normalize_path_prefix(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    normalized = _normalize_relative_result_path(raw)
    return normalized.rstrip("/") + "/"


def _normalize_optional_relative_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _normalize_relative_result_path(raw)


def _normalize_stored_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > MAX_PATH_LENGTH:
        raise SourceLinkError("stored path is missing or too long")
    return _normalize_relative_result_path(raw)


def _normalize_relative_result_path(value: str) -> str:
    original = str(value).replace("\\", "/")
    if (
        PurePosixPath(original).is_absolute()
        or PureWindowsPath(original).is_absolute()
        or PureWindowsPath(original).drive
    ):
        raise SourceLinkError("path must be a relative stored path")
    raw = original.strip("/")
    if (
        not raw
    ):
        raise SourceLinkError("path must be a relative stored path")
    parts = PurePosixPath(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SourceLinkError("path traversal is not allowed")
    return PurePosixPath(*parts).as_posix()


def _normalize_absolute_web_path(value: str) -> str:
    raw = value.replace("\\", "/")
    parts = [part for part in PurePosixPath(raw).parts if part != "/"]
    if any(part in {"", ".", ".."} for part in parts):
        raise SourceLinkError("web path traversal is not allowed")
    return PurePosixPath(*parts).as_posix()


def _prefix_matches(path: str, prefix: str) -> bool:
    if not prefix:
        return True
    base = prefix.rstrip("/")
    return path == base or path.startswith(prefix)


def _strip_prefix(path: str, prefix: str) -> str:
    if not prefix:
        return path
    base = prefix.rstrip("/")
    if path == base:
        return ""
    if not path.startswith(prefix):
        raise SourceLinkError("stored path does not match path_prefix")
    relative = path[len(prefix) :]
    return _normalize_relative_result_path(relative)


def _encode_path(value: str) -> str:
    return "/".join(quote(segment, safe="") for segment in value.split("/"))


def _append_encoded_path(root: str, relative: str) -> str:
    base = _required_root_url(root)
    return base + (f"/{_encode_path(relative)}" if relative else "")


def _reject_sensitive_keys(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise SourceLinkError("source-link settings are nested too deeply")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise SourceLinkError(
                    "credentials and secrets are not allowed in source-links"
                )
            _reject_sensitive_keys(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_keys(child, depth=depth + 1)


def _validate_safe_pattern(pattern: str) -> None:
    unsafe = (
        r"\\[1-9]",
        "(?=",
        "(?!",
        "(?<=",
        "(?<!",
        "(?>",
        "(?(",
    )
    if any(value in pattern for value in unsafe):
        raise SourceLinkError("path_pattern uses an unsafe regex feature")
    groups = list(
        re.finditer(
            r"\(\?P<([A-Za-z_][A-Za-z0-9_]*)>([^()]*)\)",
            pattern,
        )
    )
    if len(groups) != 1 or pattern.count("(?P<") != 1:
        raise SourceLinkError(
            "path_pattern requires exactly one simple named group"
        )
    body = groups[0].group(2)
    body_match = re.fullmatch(
        r"(?:\[(?:\\.|[^\\\]])+\]|\\.|[A-Za-z0-9 _:/-])+"
        r"(?P<quantifier>[+*?]|\{(?P<minimum>\d+)"
        r"(?:,(?P<maximum>\d*))?\})?",
        body,
    )
    if body_match is None:
        raise SourceLinkError(
            "named-group pattern must use a simple linear expression"
        )
    minimum = body_match.group("minimum")
    maximum = body_match.group("maximum")
    if minimum is not None:
        upper = int(maximum) if maximum not in (None, "") else int(minimum)
        if int(minimum) > 2_048 or upper > 2_048:
            raise SourceLinkError("path_pattern repetition is too large")
    outside = pattern[: groups[0].start()] + pattern[groups[0].end() :]
    if re.fullmatch(
        r"(?:\\.|[A-Za-z0-9 _:/#%=&.,-]|\^|\$)*",
        outside,
    ) is None:
        raise SourceLinkError(
            "path_pattern outside the named group must be literal"
        )


def _clone_load(value: SourceLinksLoad) -> SourceLinksLoad:
    return SourceLinksLoad(
        value.status,
        copy.deepcopy(value.payload),
        value.error_kind,
    )


def _drop_cached_path(path: Path) -> None:
    for key in [
        value
        for value in _CACHE
        if value[0] == path
    ]:
        _CACHE.pop(key, None)


def _read_current_revision(
    path: Path,
    db_name: str | None,
) -> int:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 0
    if len(raw) > MAX_SIDECAR_BYTES:
        raise SourceLinkError("existing source-links sidecar is invalid")
    try:
        payload = validate_source_links(
            json.loads(raw.decode("utf-8")),
            expected_database=db_name,
            allow_ambiguous=True,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceLinkError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise SourceLinkError(
            "existing source-links sidecar is invalid"
        ) from exc
    return int(payload["revision"])


def _bounded_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise SourceLinkError(f"{field} is missing, invalid, or too long")
    return text


def _write_bytes(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _acquire_lock(path: Path) -> int:
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            return descriptor
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if stale:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise SourceLinkError("source-links update is busy")
            time.sleep(0.05)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
