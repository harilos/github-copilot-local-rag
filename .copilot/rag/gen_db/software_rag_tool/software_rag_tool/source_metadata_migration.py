from __future__ import annotations

import copy
import os
import stat
from pathlib import Path
from typing import Any

from .source_links import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SourceLinkError,
    load_source_links,
    save_source_links,
)
from .source_paths import read_visible_observed_roots


_SAFE_LEGACY_STATUSES = frozenset(
    {
        "legacy_migration_available",
        "legacy_v2_migration_available",
        "not_configured",
    }
)


def migrate_source_metadata(
    dbs_root: Path,
    db_name: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or atomically migrate one DB-local Source Metadata sidecar."""
    result: dict[str, Any] = {
        "db": db_name,
        "schema_version": "",
        "applied": False,
    }
    try:
        root = _safe_database_root(dbs_root, db_name)
    except (OSError, ValueError):
        return {
            **result,
            "status": "invalid",
            "error": "unsafe_database_root",
        }
    loaded = load_source_links(root, db_name)
    result["schema_version"] = str(loaded.loaded_schema_version or "")
    if loaded.status == "unconfigured":
        return {**result, "status": "unconfigured"}
    if loaded.status == "manual_required":
        return {
            **result,
            "status": "manual_required",
            "source_ids": sorted(
                source_id
                for source_id, status in loaded.source_statuses
                if status not in _SAFE_LEGACY_STATUSES
            ),
        }
    if loaded.status == "invalid" or loaded.payload is None:
        return {
            **result,
            "status": "invalid",
            "error": loaded.error_kind or "invalid_source_metadata",
        }
    if not loaded.migration_required:
        return {
            **result,
            "status": "already_current",
            "schema_version": SCHEMA_VERSION,
        }

    source_statuses = dict(loaded.source_statuses)
    manual = sorted(
        source_id
        for source_id, status in source_statuses.items()
        if status not in _SAFE_LEGACY_STATUSES
    )
    if manual:
        return {
            **result,
            "status": "manual_required",
            "source_ids": manual,
        }
    if not apply:
        return {
            **result,
            "status": "migration_available",
            "target_schema_version": SCHEMA_VERSION,
            "source_count": len(loaded.payload.get("sources") or []),
        }

    payload = copy.deepcopy(loaded.payload)
    payload["revision"] = int(loaded.revision) + 1
    try:
        existing_sources = (
            tuple(read_visible_observed_roots(root))
            if loaded.loaded_schema_version == LEGACY_SCHEMA_VERSION
            else ()
        )
        saved = save_source_links(
            root,
            payload,
            db_name=db_name,
            # Migration preserves every legacy v2 Source mechanically,
            # including disabled, unmatched, and currently unresolved Links.
            # Root eligibility is enforced for new Manager saves; changing
            # catalog shape must not discard an existing portable setting.
            existing_sources=existing_sources,
            allow_unmatched_sources=True,
            expected_revision=int(loaded.revision),
            expected_etag=str(loaded.etag),
        )
    except SourceLinkError as exc:
        if str(exc) == "source_link_configuration_changed":
            return {
                **result,
                "status": "conflict",
                "error": "source_link_configuration_changed",
            }
        return {
            **result,
            "status": "failed",
            "error": type(exc).__name__,
        }
    except Exception as exc:
        return {
            **result,
            "status": "failed",
            "error": type(exc).__name__,
        }
    return {
        **result,
        "status": "migrated",
        "applied": True,
        "schema_version": SCHEMA_VERSION,
        "revision": int(saved["revision"]),
        "source_count": len(saved.get("sources") or []),
    }


def _safe_database_root(dbs_root: Path, db_name: str) -> Path:
    """Resolve one mutation target without following a DB-dir link."""
    base = Path(dbs_root).expanduser().resolve(strict=True)
    candidate = Path(dbs_root).expanduser() / db_name
    if candidate.name != db_name or candidate.parent.resolve() != base:
        raise ValueError("database must be a direct child")
    metadata = os.lstat(candidate)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        or (
            hasattr(candidate, "is_junction")
            and candidate.is_junction()
        )
    ):
        raise ValueError("linked database roots are unsafe")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or resolved.parent != base:
        raise ValueError("database must be a direct child directory")
    return resolved
