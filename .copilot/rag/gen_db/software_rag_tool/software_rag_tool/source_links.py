from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import string
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    unquote,
    urlsplit,
    urlunsplit,
)

from .source_paths import (
    SourcePathError,
    canonical_stored_path,
    observed_root_from_paths,
    read_visible_observed_roots,
    source_relative_path,
)


SCHEMA_VERSION = "rag-source-metadata-v1"
LEGACY_V2_SCHEMA_VERSION = "rag-source-links-v2"
LEGACY_SCHEMA_VERSION = "rag-source-links-v1"
SIDECAR_NAME = "source-links.json"
BACKUP_NAME = "source-links.json.bak"
MAX_SIDECAR_BYTES = 1_048_576
MAX_SOURCES = 500
MAX_URL_LENGTH = 4_096
MAX_PATH_LENGTH = 2_048
MAX_PATTERN_LENGTH = 300
WINDOWS_REPLACE_RETRY_SECONDS = 2.0

_GIT_PROVIDER_STRATEGIES = {
    "github": "github-blob",
    "gitlab": "gitlab-blob",
    "azure_devops": "azure-devops-item",
}
_ALLOWED_PROVIDERS = {
    "sharepoint",
    *_GIT_PROVIDER_STRATEGIES,
    "svn",
    "redmine",
    "other",
}
ALLOWED_SOURCE_TYPES = frozenset(
    {
        "folder",
        "git",
        *_ALLOWED_PROVIDERS,
    }
)
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "bearer",
    "cookie",
    "credential",
    "jwt",
    "password",
    "passwd",
    "proxy",
    "oauth",
    "private_key",
    "secret",
    "token",
)
_SENSITIVE_QUERY_PARTS = (
    "access_token",
    "auth",
    "bearer",
    "code",
    "cookie",
    "credential",
    "jwt",
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
_LEGACY_MANUAL_STATUSES = frozenset(
    {
        "legacy_multiple_mappings",
        "legacy_root_mismatch",
        "multiple_observed_roots",
        "no_observed_root",
    }
)


class SourceLinkError(ValueError):
    pass


class LegacySourceManualRequired(SourceLinkError):
    pass


@dataclass(frozen=True)
class SourceLinksLoad:
    status: str
    payload: dict[str, Any] | None
    error_kind: str | None = None
    revision: int = 0
    etag: str = "missing"
    migration_required: bool = False
    source_statuses: tuple[tuple[str, str], ...] = ()
    loaded_schema_version: str = ""


def load_source_links(
    db_root: Path,
    db_name: str | None = None,
    *,
    observed_roots: dict[str, Iterable[str]] | None = None,
) -> SourceLinksLoad:
    """Read and validate the optional DB-local sidecar without writing."""
    unresolved_root = Path(db_root).expanduser()
    if unresolved_root.is_symlink():
        return SourceLinksLoad("invalid", None, "unsafe_database_symlink")
    root = unresolved_root.resolve()
    path = root / SIDECAR_NAME
    effective_db_name = str(db_name or root.name)
    cache_key = (path, effective_db_name)
    try:
        _validate_optional_regular_path(path)
        raw, file_stat = _read_regular_bytes(path)
    except FileNotFoundError:
        fingerprint = (0, 0, "")
        loaded = SourceLinksLoad("unconfigured", None)
        with _CACHE_LOCK:
            _CACHE[cache_key] = (fingerprint, loaded)
        return loaded
    except (OSError, SourceLinkError) as exc:
        loaded = SourceLinksLoad("invalid", None, type(exc).__name__)
        with _CACHE_LOCK:
            _drop_cached_path(path)
        return loaded
    if len(raw) > MAX_SIDECAR_BYTES:
        loaded = SourceLinksLoad("invalid", None, "sidecar_too_large")
        with _CACHE_LOCK:
            _drop_cached_path(path)
        return loaded
    digest = hashlib.sha256(raw).hexdigest()
    try:
        decoded = json.loads(raw.decode("utf-8"))
        roots = (
            {
                str(key): tuple(str(value) for value in values)
                for key, values in observed_roots.items()
            }
            if observed_roots is not None
            else read_visible_observed_roots(root)
        )
        payload, source_statuses, migration_required = _normalize_payload(
            decoded,
            observed_roots=roots,
            expected_database=effective_db_name,
            allow_legacy_provider_settings=True,
        )
        loaded_schema_version = str(decoded.get("schema_version") or "")
        manual_required = (
            loaded_schema_version == LEGACY_SCHEMA_VERSION
            and any(
                status in _LEGACY_MANUAL_STATUSES
                for status in source_statuses.values()
            )
        )
        roots_digest = hashlib.sha256(
            json.dumps(
                roots,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fingerprint = (
            file_stat.st_mtime_ns,
            len(raw),
            digest + roots_digest,
        )
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and cached[0] == fingerprint:
                return _clone_load(cached[1])
    except LegacySourceManualRequired:
        fingerprint = (file_stat.st_mtime_ns, len(raw), digest)
        loaded = SourceLinksLoad(
            "manual_required",
            None,
            "legacy_non_sharepoint_manual_required",
            revision=(
                int(decoded.get("revision") or 0)
                if isinstance(decoded, dict)
                else 0
            ),
            etag=digest,
            migration_required=True,
            loaded_schema_version=(
                str(decoded.get("schema_version") or "")
                if isinstance(decoded, dict)
                else ""
            ),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceLinkError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        fingerprint = (file_stat.st_mtime_ns, len(raw), digest)
        loaded = SourceLinksLoad(
            "invalid",
            None,
            type(exc).__name__,
            etag=digest,
        )
    else:
        if manual_required:
            loaded = SourceLinksLoad(
                "manual_required",
                None,
                revision=int(decoded["revision"]),
                etag=digest,
                migration_required=True,
                source_statuses=tuple(sorted(source_statuses.items())),
                loaded_schema_version=loaded_schema_version,
            )
        else:
            loaded = SourceLinksLoad(
                "configured",
                payload,
                revision=int(payload["revision"]),
                etag=digest,
                migration_required=migration_required,
                source_statuses=tuple(sorted(source_statuses.items())),
                loaded_schema_version=loaded_schema_version,
            )
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
    normalized, _statuses, _migration = _normalize_payload(
        payload,
        observed_roots=observed_paths or {},
        expected_database=expected_database,
        existing_sources=existing_sources,
        allow_unmatched_sources=allow_unmatched_sources,
    )
    return normalized


def _normalize_payload(
    payload: Any,
    *,
    observed_roots: dict[str, Iterable[str]],
    expected_database: str | None = None,
    existing_sources: Iterable[str] | None = None,
    allow_unmatched_sources: bool = True,
    allow_legacy_provider_settings: bool = False,
) -> tuple[dict[str, Any], dict[str, str], bool]:
    if not isinstance(payload, dict):
        raise SourceLinkError("Source Metadata sidecar must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version not in {
        SCHEMA_VERSION,
        LEGACY_V2_SCHEMA_VERSION,
        LEGACY_SCHEMA_VERSION,
    }:
        raise SourceLinkError("unsupported Source Metadata schema")
    if schema_version == LEGACY_SCHEMA_VERSION:
        declared_database = _bounded_text(
            payload.get("database"),
            "legacy database",
            200,
        )
        if (
            not expected_database
            or declared_database != str(expected_database)
        ):
            raise SourceLinkError(
                "legacy source-links database does not match its directory"
            )
    if schema_version in {SCHEMA_VERSION, LEGACY_V2_SCHEMA_VERSION}:
        unexpected = set(payload) - {
            "schema_version",
            "revision",
            "sources",
        }
        if unexpected:
            raise SourceLinkError(
                "Source Metadata contains unsupported top-level fields"
            )
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
    source_statuses: dict[str, str] = {}
    seen_sources: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise SourceLinkError("each Source setting must be an object")
        source_id = _bounded_text(
            source.get("source_id"),
            "source_id",
            200,
        )
        if source_id in seen_sources:
            raise SourceLinkError("duplicate source_id in source-links")
        seen_sources.add(source_id)
        legacy_status = ""
        if schema_version == LEGACY_SCHEMA_VERSION:
            original_source_id = source_id
            source, legacy_status = _upgrade_legacy_source(
                source,
                tuple(
                    str(value)
                    for value in observed_roots.get(original_source_id, ())
                ),
            )
            if original_source_id:
                source_statuses[original_source_id] = legacy_status
            if source is None:
                continue
        elif schema_version == LEGACY_V2_SCHEMA_VERSION:
            source = _upgrade_v2_source(source)
            legacy_status = "legacy_v2_migration_available"
            source_statuses[source_id] = legacy_status
        elif set(source) - {
            "source_id",
            "display_name",
            "source_type",
            "link",
        }:
            raise SourceLinkError(
                "Source Metadata contains unsupported Source fields"
            )
        if (
            known_sources is not None
            and source_id not in known_sources
            and not allow_unmatched_sources
        ):
            raise SourceLinkError("mapping Source does not exist in the catalog")
        normalized_source: dict[str, Any] = {
            "source_id": source_id,
        }
        display_name = str(source.get("display_name") or "").strip()
        if display_name:
            normalized_source["display_name"] = _bounded_text(
                display_name,
                "display_name",
                300,
            )
        source_type = str(source.get("source_type") or "").strip().lower()
        if source_type:
            if source_type not in ALLOWED_SOURCE_TYPES:
                raise SourceLinkError("unsupported source_type")
            normalized_source["source_type"] = source_type
        link_value = source.get("link")
        if link_value is not None and not isinstance(link_value, dict):
            raise SourceLinkError("Source link must be an object")
        if isinstance(link_value, dict):
            unexpected_link_fields = set(link_value) - {
                "enabled",
                "strategy",
                "settings",
            }
            if unexpected_link_fields:
                raise SourceLinkError(
                    "Source link contains unsupported fields"
                )
            if not source_type:
                raise SourceLinkError(
                    "Source link requires source_type"
                )
            if source_type not in _ALLOWED_PROVIDERS:
                raise SourceLinkError(
                    "source_type does not support a Source Link"
                )
            normalized_link = validate_source_link(
                {
                    "provider": source_type,
                    **link_value,
                },
                allow_legacy_provider_settings=(
                    allow_legacy_provider_settings
                    and schema_version != SCHEMA_VERSION
                ),
            )
            normalized_source["link"] = {
                "enabled": normalized_link["enabled"],
                "strategy": normalized_link["strategy"],
                "settings": normalized_link["settings"],
            }
            source_statuses.setdefault(source_id, "configured")
        elif any(
            field in source
            for field in ("provider", "enabled", "strategy", "settings")
        ):
            raise SourceLinkError(
                "legacy Source Link fields are not valid Source Metadata"
            )
        elif source_type:
            source_statuses.setdefault(source_id, "type_only")
        else:
            source_statuses.setdefault(source_id, "not_configured")
        normalized_sources.append(normalized_source)
    return ({
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "sources": normalized_sources,
    }, source_statuses, schema_version != SCHEMA_VERSION)


def _upgrade_v2_source(source: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "source_id",
        "display_name",
        "enabled",
        "provider",
        "strategy",
        "settings",
    }
    if set(source) - allowed:
        raise SourceLinkError(
            "legacy v2 Source settings contain unsupported fields"
        )
    upgraded: dict[str, Any] = {
        "source_id": source.get("source_id"),
    }
    display_name = str(source.get("display_name") or "").strip()
    if display_name:
        upgraded["display_name"] = display_name
    link_fields = ("provider", "enabled", "strategy", "settings")
    present = [field in source for field in link_fields]
    if any(present) and not all(present):
        raise SourceLinkError(
            "legacy v2 Source Link is incomplete"
        )
    if all(present):
        if str(source.get("provider") or "").strip().lower() != "sharepoint":
            raise LegacySourceManualRequired(
                "only legacy SharePoint settings are supported"
            )
        upgraded["source_type"] = source.get("provider")
        upgraded["link"] = {
            "enabled": source.get("enabled"),
            "strategy": source.get("strategy"),
            "settings": source.get("settings"),
        }
    return upgraded


def validate_source_link(
    link: Any,
    *,
    allow_legacy_provider_settings: bool = False,
) -> dict[str, Any]:
    if not isinstance(link, dict):
        raise SourceLinkError("Source Link must be an object")
    if isinstance(link.get("link"), dict):
        nested = link["link"]
        if set(nested) - {"enabled", "strategy", "settings"}:
            raise SourceLinkError(
                "Source link contains unsupported fields"
            )
        source_type = str(link.get("source_type") or "").strip().lower()
        if not source_type:
            raise SourceLinkError("Source link requires source_type")
        return validate_source_link(
            {
                "provider": source_type,
                **nested,
            },
            allow_legacy_provider_settings=allow_legacy_provider_settings,
        )
    enabled = link.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SourceLinkError("Source Link enabled must be boolean")
    provider = str(link.get("provider") or "").strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise SourceLinkError("unsupported Source-Link provider")
    settings = link.get("settings")
    if not isinstance(settings, dict):
        raise SourceLinkError("Source Link settings must be an object")
    _reject_sensitive_keys(settings)
    strategy = str(link.get("strategy") or "").strip().lower()
    if not strategy:
        raise SourceLinkError("Source Link strategy is required")
    if (
        allow_legacy_provider_settings
        and provider == "github"
        and strategy == "append-relative-path"
    ):
        strategy = "github-blob"
    normalized_settings = _validate_provider_settings(
        provider,
        strategy,
        settings,
        allow_legacy_provider_settings=allow_legacy_provider_settings,
    )
    return {
        "enabled": enabled,
        "provider": provider,
        "strategy": strategy,
        "settings": normalized_settings,
    }


def validate_mapping(mapping: Any) -> dict[str, Any]:
    """Compatibility alias that normalizes an old flat mapping."""
    return validate_source_link(mapping)


def _upgrade_legacy_source(
    source: dict[str, Any],
    observed_roots: tuple[str, ...],
) -> tuple[dict[str, Any] | None, str]:
    """Normalize one safe legacy mapping without writing the sidecar."""
    upgraded: dict[str, Any] = {"source_id": source.get("source_id")}
    display_name = str(source.get("display_name") or "").strip()
    if display_name:
        upgraded["display_name"] = display_name
    mappings = source.get("mappings")
    if mappings is None:
        return upgraded, "not_configured"
    if not isinstance(mappings, list):
        raise SourceLinkError("legacy Source mappings must be an array")
    if not mappings:
        return upgraded, "not_configured"
    if len(mappings) != 1:
        return (upgraded if display_name else None), "legacy_multiple_mappings"
    first = mappings[0]
    if not isinstance(first, dict):
        raise SourceLinkError("legacy Source mapping must be an object")
    if len(observed_roots) == 0:
        return (upgraded if display_name else None), "no_observed_root"
    if len(observed_roots) > 1:
        return (upgraded if display_name else None), "multiple_observed_roots"
    try:
        former_prefix = canonical_stored_path(
            str(first.get("path_prefix") or "").rstrip("/")
        )
        observed = canonical_stored_path(observed_roots[0].rstrip("/"))
    except SourcePathError:
        return (upgraded if display_name else None), "legacy_root_mismatch"
    if former_prefix != observed:
        return (upgraded if display_name else None), "legacy_root_mismatch"
    provider = str(first.get("provider") or "").strip().lower()
    if provider != "sharepoint":
        raise LegacySourceManualRequired(
            "only legacy SharePoint settings are supported"
        )
    upgraded["source_type"] = provider
    upgraded["link"] = {
        key: first[key]
        for key in ("enabled", "settings", "strategy")
        if key in first
    }
    return upgraded, "legacy_migration_available"


def save_source_links(
    db_root: Path,
    payload: dict[str, Any],
    *,
    db_name: str | None = None,
    existing_sources: Iterable[str] | None = None,
    observed_paths: dict[str, Iterable[str]] | None = None,
    allow_unmatched_sources: bool = False,
    expected_revision: int | None = None,
    expected_etag: str | None = None,
) -> dict[str, Any]:
    unresolved_root = Path(db_root).expanduser()
    if unresolved_root.is_symlink():
        raise SourceLinkError("database directory cannot be a symlink")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise SourceLinkError("database directory does not exist")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SourceLinkError(
            "legacy Source settings require explicit migration"
        )
    if expected_revision is None or expected_etag is None:
        raise SourceLinkError(
            "source-links save requires revision and content hash"
        )
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
    temporary = root / f".{SIDECAR_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    backup_temporary = root / (
        f".{BACKUP_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        _validate_optional_regular_path(current)
        _validate_optional_regular_path(backup)
        current_revision = _read_current_revision(
            current,
            db_name,
        )
        current_etag = _current_etag(current)
        if (
            current_revision != expected_revision
            or current_etag != expected_etag
        ):
            raise SourceLinkError(
                "source_link_configuration_changed"
            )
        try:
            authoritative_roots = read_visible_observed_roots(root)
        except Exception as exc:
            raise SourceLinkError(
                "Source roots could not be verified from the catalog"
            ) from exc
        current_load = load_source_links(
            root,
            db_name,
            observed_roots=authoritative_roots,
        )
        current_payload = (
            current_load.payload
            if current_load.status not in {"invalid", "unconfigured"}
            else None
        )
        _validate_observed_root_contract(
            normalized,
            authoritative_roots,
            existing_sources=existing_sources,
            current_payload=current_payload,
        )
        _write_bytes(temporary, encoded)
        if current.exists():
            previous, _current_stat = _read_regular_bytes(current)
            _write_bytes(backup_temporary, previous)
        if (
            _read_current_revision(current, db_name) != expected_revision
            or _current_etag(current) != expected_etag
        ):
            raise SourceLinkError("source_link_configuration_changed")
        try:
            roots_before_publish = read_visible_observed_roots(root)
        except Exception as exc:
            raise SourceLinkError(
                "Source roots could not be reverified from the catalog"
            ) from exc
        if roots_before_publish != authoritative_roots:
            raise SourceLinkError("source_link_catalog_roots_changed")
        _validate_optional_regular_path(current)
        _validate_optional_regular_path(backup)
        if backup_temporary.exists():
            _atomic_replace(backup_temporary, backup)
        _atomic_replace(temporary, current)
        _fsync_directory(root)
    finally:
        for candidate in (temporary, backup_temporary):
            try:
                candidate.unlink()
            except OSError:
                pass
    with _CACHE_LOCK:
        _drop_cached_path(current)
    return normalized


def _validate_observed_root_contract(
    payload: dict[str, Any],
    observed_roots: dict[str, Iterable[str]],
    *,
    existing_sources: Iterable[str] | None,
    current_payload: dict[str, Any] | None = None,
) -> None:
    known_sources = (
        {str(value) for value in existing_sources}
        if existing_sources is not None
        else None
    )
    current_sources = {
        str(source.get("source_id") or ""): source
        for source in (
            (current_payload or {}).get("sources") or []
        )
        if isinstance(source, dict)
    }
    for source in payload.get("sources") or []:
        source_id = str(source.get("source_id") or "")
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("link"), dict)
            or source["link"].get("strategy")
            in {"home-only", "svn-web-root"}
            or (
                known_sources is not None
                and source_id not in known_sources
            )
        ):
            continue
        roots = tuple(
            str(value)
            for value in observed_roots.get(
                source_id,
                (),
            )
        )
        current_source = current_sources.get(source_id)
        unchanged_legacy_link = (
            isinstance(current_source, dict)
            and source.get("source_type")
            == current_source.get("source_type")
            and source.get("link") == current_source.get("link")
        )
        if not roots:
            if unchanged_legacy_link:
                continue
            raise SourceLinkError(
                "per-file Source Link requires one observed root: "
                "no_observed_root"
            )
        if len(roots) != 1:
            if unchanged_legacy_link:
                continue
            raise SourceLinkError(
                "per-file Source Link requires one observed root: "
                "multiple_observed_roots"
            )


def resolve_mapping_preview(
    mapping: dict[str, Any],
    stored_paths: Iterable[str],
) -> list[dict[str, Any]]:
    normalized = _validated_link_from_source(mapping)
    root_independent = normalized["strategy"] == "svn-web-root"
    values = list(stored_paths)[:5]
    try:
        roots = observed_root_from_paths(values)
    except SourcePathError:
        roots = ()
    output: list[dict[str, Any]] = []
    for stored_path in values:
        path = _normalize_stored_path(stored_path)
        resolved: dict[str, str] = {}
        status = (
            "no_observed_root"
            if not roots
            else (
                "multiple_observed_roots"
                if len(roots) > 1
                else "unconfigured"
            )
        )
        if normalized["enabled"] and (len(roots) == 1 or root_independent):
            try:
                relative = (
                    path
                    if root_independent
                    else source_relative_path(path, roots[0])
                )
                resolved = _generate_provider_urls(normalized, relative)
            except (SourcePathError, SourceLinkError):
                status = "resolution_failed"
            else:
                status = "resolved" if resolved else "unconfigured"
        output.append(
            {
                "path": path,
                "status": status,
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
    try:
        observed_roots = read_visible_observed_roots(db_root)
    except (OSError, ValueError):
        observed_roots = {}
    loaded = load_source_links(
        db_root,
        db_name,
        observed_roots=observed_roots,
    )
    source_statuses = dict(loaded.source_statuses)
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
                        observed_roots,
                    )
                except (SourceLinkError, ValueError, KeyError):
                    status = "resolution_failed"
            elif loaded.status == "invalid":
                status = "resolution_failed"
            if status == "unconfigured":
                status = source_statuses.get(source_id, status)
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
    observed_roots: dict[str, Iterable[str]],
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
    if (
        not isinstance(source.get("link"), dict)
        or source["link"].get("enabled") is not True
        or not source.get("source_type")
        or not isinstance(source["link"].get("settings"), dict)
    ):
        return {}, "unconfigured"
    source_link = _validated_link_from_source(source)
    if source_link.get("strategy") == "svn-web-root":
        try:
            resolved = _generate_provider_urls(source_link, path)
        except (SourcePathError, SourceLinkError, ValueError, KeyError):
            return {}, "resolution_failed"
        return (
            (resolved, "resolved")
            if resolved
            else ({}, "unconfigured")
        )
    roots = tuple(str(value) for value in observed_roots.get(source_id, ()))
    if not roots:
        return {}, "no_observed_root"
    if len(roots) > 1:
        return {}, "multiple_observed_roots"
    try:
        relative = source_relative_path(path, roots[0])
        resolved = _generate_provider_urls(source_link, relative)
    except (SourcePathError, SourceLinkError, ValueError, KeyError):
        return {}, "resolution_failed"
    if not resolved:
        return {}, "unconfigured"
    return resolved, "resolved"


def _validated_link_from_source(
    source: dict[str, Any],
) -> dict[str, Any]:
    return validate_source_link(
        source,
        allow_legacy_provider_settings=True,
    )


def _validate_provider_settings(
    provider: str,
    strategy: str,
    settings: dict[str, Any],
    *,
    allow_legacy_provider_settings: bool = False,
) -> dict[str, Any]:
    if provider == "sharepoint":
        if strategy == "home-only":
            # Compatibility-only representation. The Manager never offers
            # this strategy for new SharePoint settings, and URL generation
            # deliberately returns path-only for every home-only Link. It
            # remains persistable so explicit v1/v2 metadata migration does
            # not discard an existing setting.
            return {
                "source_home_url": _required_url(
                    settings.get("source_home_url")
                )
            }
        if strategy != "append-relative-path":
            raise SourceLinkError("unsupported SharePoint strategy")
        # Legacy append settings may still contain a retired home URL. Validate
        # it for credential/security rules before deliberately omitting it.
        if str(settings.get("source_home_url") or "").strip():
            _optional_url(settings.get("source_home_url"))
        web_root = _optional_url(settings.get("source_web_root"))
        if web_root:
            web_root = _normalize_sharepoint_root(web_root)
            _required_root_url(web_root)
        if not web_root:
            raise SourceLinkError(
                "SharePoint file links require source_web_root"
            )
        # source_home_url was accepted by older sidecars. File links do not
        # use it, so canonical v2 saves deliberately omit it.
        return {"source_web_root": web_root}
    if provider in _GIT_PROVIDER_STRATEGIES:
        return _validate_git_repository_settings(
            provider,
            strategy,
            settings,
        )
    if provider == "svn":
        return _validate_svn_settings(strategy, settings)
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


def _validate_git_repository_settings(
    provider: str,
    strategy: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    expected_strategy = _GIT_PROVIDER_STRATEGIES[provider]
    if strategy != expected_strategy:
        raise SourceLinkError(
            f"unsupported {_git_provider_name(provider)} strategy"
        )
    repository_url = _normalize_repository_url(
        provider,
        settings.get("repository_url"),
    )
    ref = _validate_git_ref(
        _bounded_text(settings.get("ref"), "ref", 300)
    )
    prefix = _normalize_optional_relative_path(
        settings.get("repository_path_prefix")
    )
    commit = str(settings.get("commit") or "").strip()
    if commit and not re.fullmatch(r"[0-9A-Fa-f]{40,64}", commit):
        raise SourceLinkError(
            "commit must be a full 40-64 character hexadecimal ID"
        )
    commit = commit.lower()
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


def _validate_svn_settings(
    strategy: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    if strategy == "svn-web-root":
        # Product-specific browser URLs are intentionally opaque. Keep their
        # query, fragment, path, encoding, and trailing slash exactly as given.
        return {
            "repository_url": _required_preserved_web_url(
                settings.get("repository_url")
            )
        }
    if strategy != "svn-http":
        raise SourceLinkError("unsupported Subversion strategy")
    repository_url = _required_root_url(settings.get("repository_url"))
    prefix = _normalize_optional_relative_path(
        settings.get("repository_path_prefix")
    )
    permalink_enabled = settings.get("permalink_enabled", False)
    if not isinstance(permalink_enabled, bool):
        raise SourceLinkError("permalink_enabled must be boolean")
    revision = _normalize_svn_revision(settings.get("revision"))
    if permalink_enabled and revision is None:
        raise SourceLinkError(
            "revision is required when SVN permalinks are enabled"
        )
    output: dict[str, Any] = {
        "repository_url": repository_url,
        "permalink_enabled": permalink_enabled,
    }
    if prefix:
        output["repository_path_prefix"] = prefix
    if revision is not None:
        output["revision"] = revision
    return output


def _normalize_svn_revision(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SourceLinkError("SVN revision must be a positive integer")
    if isinstance(value, int):
        revision = value
    elif (
        isinstance(value, str)
        and len(value) <= 19
        and re.fullmatch(r"[1-9][0-9]*", value)
    ):
        revision = int(value)
    else:
        raise SourceLinkError("SVN revision must be a positive integer")
    if revision < 1 or revision > 9_223_372_036_854_775_807:
        raise SourceLinkError("SVN revision must be a positive integer")
    return revision


def _normalize_repository_url(provider: str, value: Any) -> str:
    repository_url = _required_url(value)
    split = urlsplit(repository_url)
    if split.query or split.fragment:
        raise SourceLinkError(
            "repository_url cannot contain query or fragment"
        )
    decoded_path = _fully_unquote(split.path)
    lowered_path = decoded_path.casefold()
    if provider == "github":
        # Keep the established GitHub/GHE compatibility boundary unchanged.
        invalid_markers = ("/blob/", "/tree/")
    elif provider == "gitlab":
        invalid_markers = (
            "/-/blob/",
            "/-/tree/",
            "/-/raw/",
            "/-/commit/",
        )
    else:
        invalid_markers = ()
    if any(marker in lowered_path for marker in invalid_markers):
        raise SourceLinkError(
            "repository_url must identify the repository root"
        )
    if provider == "gitlab" and (
        "/-/" in lowered_path or lowered_path.rstrip("/").endswith("/-")
    ):
        raise SourceLinkError(
            "repository_url must identify the repository root"
        )

    path = split.path.rstrip("/")
    if provider in {"github", "gitlab"} and path.casefold().endswith(".git"):
        path = path[:-4]
    if provider == "gitlab":
        project_components = [
            component
            for component in _fully_unquote(path).strip("/").split("/")
            if component
        ]
        if len(project_components) < 2:
            raise SourceLinkError(
                "GitLab repository_url must include a namespace and project"
            )
    if provider == "azure_devops":
        _validate_azure_devops_repository_root(split.hostname or "", path)
    return urlunsplit(
        (split.scheme, split.netloc, path, "", "")
    ).rstrip("/")


def _validate_azure_devops_repository_root(hostname: str, path: str) -> None:
    host = hostname.casefold()
    decoded_path = _fully_unquote(path)
    components = [
        component
        for component in decoded_path.strip("/").split("/")
        if component
    ]
    if host == "dev.azure.com":
        valid = (
            len(components) == 4
            and components[2].casefold() == "_git"
        )
    elif host.endswith(".visualstudio.com") and host != ".visualstudio.com":
        valid = (
            (
                len(components) == 3
                and components[1].casefold() == "_git"
            )
            or (
                len(components) == 4
                and components[0].casefold() == "defaultcollection"
                and components[2].casefold() == "_git"
            )
        )
    else:
        valid = False
    if not valid:
        raise SourceLinkError(
            "Azure DevOps repository_url must identify a repository root"
        )


def _git_provider_name(provider: str) -> str:
    return {
        "github": "GitHub",
        "gitlab": "GitLab",
        "azure_devops": "Azure DevOps",
    }.get(provider, "Git repository")


def _azure_devops_item_url(
    repository_url: str,
    file_path: str,
    *,
    version_kind: str,
    version: str,
) -> str:
    encoded_path = "/" + _encode_path(file_path)
    encoded_version = quote(f"{version_kind}{version}", safe="")
    return (
        f"{repository_url}?path={encoded_path}"
        f"&version={encoded_version}"
    )


def _generate_provider_urls(
    source_link: dict[str, Any],
    stored_path: str,
) -> dict[str, str]:
    path = _normalize_stored_path(stored_path)
    provider = str(source_link["provider"])
    settings = dict(source_link.get("settings") or {})
    strategy = str(source_link.get("strategy") or "")
    if strategy == "home-only":
        return {}
    output: dict[str, str] = {"source_provider": provider}
    if provider == "sharepoint":
        output["source_url"] = _append_encoded_path(
            str(settings["source_web_root"]),
            path,
        )
    elif provider in _GIT_PROVIDER_STRATEGIES:
        repository = str(settings["repository_url"]).rstrip("/")
        repo_prefix = str(
            settings.get("repository_path_prefix") or ""
        ).strip("/")
        file_path = "/".join(
            value for value in (repo_prefix, path) if value
        )
        ref = str(settings["ref"])
        if provider == "azure_devops":
            output["source_url"] = _azure_devops_item_url(
                repository,
                file_path,
                version_kind="GB",
                version=ref,
            )
        else:
            marker = "blob" if provider == "github" else "-/blob"
            output["source_url"] = (
                f"{repository}/{marker}/{_encode_path(ref)}"
                + (f"/{_encode_path(file_path)}" if file_path else "")
            )
        if settings.get("permalink_enabled") and settings.get("commit"):
            commit = str(settings["commit"])
            if provider == "azure_devops":
                output["source_permalink"] = _azure_devops_item_url(
                    repository,
                    file_path,
                    version_kind="GC",
                    version=commit,
                )
            else:
                marker = "blob" if provider == "github" else "-/blob"
                output["source_permalink"] = (
                    f"{repository}/{marker}/{_encode_path(commit)}"
                    + (f"/{_encode_path(file_path)}" if file_path else "")
                )
    elif provider == "svn":
        repository = str(settings["repository_url"])
        if strategy == "svn-web-root":
            output["source_url"] = repository
        else:
            repo_prefix = str(
                settings.get("repository_path_prefix") or ""
            ).strip("/")
            file_path = "/".join(
                value for value in (repo_prefix, path) if value
            )
            output["source_url"] = _append_encoded_path(
                repository,
                file_path,
            )
            if (
                settings.get("permalink_enabled")
                and settings.get("revision") is not None
            ):
                revision = int(settings["revision"])
                output["source_permalink"] = (
                    f"{output['source_url']}?p={revision}&r={revision}"
                )
    elif strategy == "append-relative-path":
        output["source_url"] = _append_encoded_path(
            str(settings["source_web_root"]),
            path,
        )
    else:
        pattern = re.compile(str(settings["path_pattern"]))
        match = pattern.search(path)
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
        or re.search(r"%(?![0-9A-Fa-f]{2})", text)
    ):
        raise SourceLinkError("URL must be HTTP(S) without credentials")
    decoded_text = _fully_unquote(text)
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in decoded_text
    ):
        raise SourceLinkError("URL must not contain control characters")
    _reject_sensitive_credential_expressions(decoded_text)
    _reject_sensitive_url_path(split.path)
    for key, query_value in parse_qsl(
        split.query,
        keep_blank_values=True,
    ):
        decoded_key = _fully_unquote(key)
        normalized = decoded_key.casefold()
        if (
            any(part in normalized for part in _SENSITIVE_QUERY_PARTS)
            or _is_sensitive_assignment_name(decoded_key)
        ):
            raise SourceLinkError(
                "URL query parameters must not contain credentials"
            )
        _reject_sensitive_credential_expressions(query_value)
    _reject_sensitive_credential_expressions(split.fragment)
    return text


def _reject_sensitive_url_path(path: str) -> None:
    decoded = _fully_unquote(path)
    if "\\" in decoded or any(
        component in {".", ".."}
        for component in decoded.split("/")
    ):
        raise SourceLinkError(
            "URL paths must not contain traversal or backslashes"
        )
    _reject_sensitive_credential_expressions(decoded)


def _reject_sensitive_credential_expressions(value: Any) -> None:
    decoded = _fully_unquote(value)
    if re.search(
        r"(?i)(?:[a-z][a-z0-9+.-]*:)?//[^\s/?#@]+@",
        decoded,
    ):
        raise SourceLinkError(
            "URL components must not contain embedded credentials"
        )
    if re.search(
        r"(?i)(?:^|[^a-z0-9])(?:bearer|basic)\s+\S",
        decoded,
    ):
        raise SourceLinkError(
            "URL components must not contain authentication values"
        )
    separators = "/?#&;,=:@"
    for index, character in enumerate(decoded):
        if character not in "=:":
            continue
        end = index
        while end > 0 and decoded[end - 1].isspace():
            end -= 1
        start = end
        while start > 0 and decoded[start - 1] not in separators:
            start -= 1
        candidate = decoded[start:end].strip()
        if _is_sensitive_assignment_name(candidate):
            raise SourceLinkError(
                "URL components must not contain credential assignments"
            )


def _is_sensitive_assignment_name(value: str) -> bool:
    camel_split = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        "_",
        value,
    )
    camel_split = re.sub(
        r"(?<=[A-Z])(?=[A-Z][a-z])",
        "_",
        camel_split,
    )
    components = [
        component
        for component in re.split(
            r"[^a-z0-9]+",
            camel_split.casefold(),
        )
        if component
    ]
    direct = {
        "auth",
        "authentication",
        "authorization",
        "bearer",
        "basic",
        "code",
        "jwt",
        "key",
        "keys",
        "oauth",
        "passphrase",
        "passphrases",
        "proxy",
        "pwd",
        "sas",
        "sig",
        "signature",
    }
    suffixes = (
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "credential",
        "credentials",
        "cookie",
        "cookies",
    )
    key_names = {
        "accesskey",
        "accesskeyid",
        "accesskeys",
        "apikey",
        "apikeys",
        "authcode",
        "authorizationcode",
        "awsaccesskeyid",
        "encryptionkey",
        "encryptionkeys",
        "oauthcode",
        "privatekey",
        "privatekeys",
        "secretkey",
        "secretkeys",
        "signingkey",
        "signingkeys",
        "sshkey",
        "sshkeys",
        "subscriptionkey",
        "subscriptionkeys",
    }
    key_suffixes = tuple(
        name
        for name in key_names
        if name not in {"authcode", "authorizationcode", "oauthcode"}
    )
    collapsed_auth_suffixes = (
        "auth",
        "authentication",
        "authorization",
        "jwt",
        "passphrase",
        "passphrases",
        "pwd",
        "sas",
        "sig",
        "signature",
        "signatures",
    )
    collapsed = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return any(
        component in direct
        or component in key_names
        or component.endswith(suffixes)
        for component in components
    ) or (
        collapsed in direct
        or collapsed in key_names
        or collapsed.endswith(suffixes)
        or collapsed.endswith(key_suffixes)
        or collapsed.endswith(collapsed_auth_suffixes)
    )


def _fully_unquote(value: Any) -> str:
    decoded = str(value or "")
    for _ in range(len(decoded) + 1):
        value = unquote(decoded)
        if value == decoded:
            return decoded
        decoded = value
    raise SourceLinkError("URL encoding nesting is too deep")


def _required_root_url(value: Any) -> str:
    text = _required_url(value)
    split = urlsplit(text)
    if split.query or split.fragment:
        raise SourceLinkError(
            "a per-document URL root cannot contain a query or fragment"
        )
    return text.rstrip("/")


def _required_preserved_web_url(value: Any) -> str:
    text = _required_url(value)
    return text


def _optional_url(value: Any) -> str:
    text = str(value or "").strip()
    return _required_url(text) if text else ""


def _normalize_optional_relative_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _normalize_relative_result_path(raw)


def _validate_git_ref(value: str) -> str:
    if (
        value == "@"
        or value.startswith("/")
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
    ):
        raise SourceLinkError("ref is not a safe Git ref")
    components = value.split("/")
    if any(
        component in {"", ".", ".."}
        or component.startswith(".")
        or component.casefold().endswith(".lock")
        for component in components
    ):
        raise SourceLinkError("ref is not a safe Git ref")
    return value


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
            normalized = _fully_unquote(key).casefold()
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
        value.revision,
        value.etag,
        value.migration_required,
        tuple(value.source_statuses),
        value.loaded_schema_version,
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
        raw, _file_stat = _read_regular_bytes(path)
    except FileNotFoundError:
        return 0
    if len(raw) > MAX_SIDECAR_BYTES:
        raise SourceLinkError("existing source-links sidecar is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise SourceLinkError("sidecar must be an object")
        if payload.get("schema_version") not in {
            SCHEMA_VERSION,
            LEGACY_V2_SCHEMA_VERSION,
            LEGACY_SCHEMA_VERSION,
        }:
            raise SourceLinkError("unsupported source-links schema")
        revision = payload.get("revision")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise SourceLinkError("invalid source-links revision")
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
    return int(revision)


def _current_etag(path: Path) -> str:
    try:
        raw, _file_stat = _read_regular_bytes(path)
        return hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        return "missing"


def _read_regular_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SourceLinkError(
                f"{path.name} must be a regular non-symlink file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(), file_stat
    finally:
        os.close(descriptor)


def _validate_optional_regular_path(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SourceLinkError(
            f"{path.name} must be a regular non-symlink file"
        )


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


def _atomic_replace(source: Path, target: Path) -> None:
    deadline = (
        time.monotonic() + WINDOWS_REPLACE_RETRY_SECONDS
        if _is_windows()
        else 0.0
    )
    delay = 0.01
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if (
                not _is_windows()
                or getattr(exc, "winerror", None) not in {5, 32}
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _is_windows() -> bool:
    return os.name == "nt"
