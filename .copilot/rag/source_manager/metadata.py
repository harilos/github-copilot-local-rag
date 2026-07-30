from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError
from .gitlab_issues import (
    repair_generated_gitlab_issues_link,
)
from .redmine import repair_generated_redmine_link


def publish_source_metadata(
    db_root: Path,
    source: Mapping[str, Any],
    rag_root: Path,
) -> None:
    """CAS-publish one indexed Source into the canonical DB sidecar."""
    source_id = str(source.get("source_id") or "").strip()
    if not source_id:
        raise SourceManagerError(
            "Source Metadata requires a trusted indexed source_id"
        )
    source_links = _source_links_module(Path(rag_root))
    loaded = source_links.load_source_links(
        Path(db_root),
        Path(db_root).name,
    )
    if loaded.status == "unconfigured":
        payload: dict[str, Any] = {
            "schema_version": source_links.SCHEMA_VERSION,
            "revision": 1,
            "sources": [],
        }
        expected_revision = 0
        expected_etag = "missing"
    elif loaded.status == "configured" and isinstance(loaded.payload, dict):
        payload = copy.deepcopy(loaded.payload)
        payload["schema_version"] = source_links.SCHEMA_VERSION
        payload["revision"] = int(loaded.revision) + 1
        expected_revision = int(loaded.revision)
        expected_etag = str(loaded.etag)
    else:
        raise SourceManagerError(
            "canonical Source Metadata is not writable"
        )

    current_source = next(
        (
            value
            for value in payload.get("sources") or []
            if isinstance(value, dict)
            and str(value.get("source_id") or "") == source_id
        ),
        None,
    )
    replacement = _canonical_source(source, current_source=current_source)
    existing = [
        value
        for value in payload.get("sources") or []
        if isinstance(value, dict)
        and str(value.get("source_id") or "") != source_id
    ]
    existing.append(replacement)
    payload["sources"] = sorted(
        existing,
        key=lambda value: str(value.get("source_id") or ""),
    )
    try:
        source_links.save_source_links(
            Path(db_root),
            payload,
            db_name=Path(db_root).name,
            expected_revision=expected_revision,
            expected_etag=expected_etag,
        )
    except Exception as exc:
        raise SourceManagerError(
            "Source Metadata synchronization failed"
        ) from exc


def remove_source_metadata(
    db_root: Path,
    source_id: str,
    rag_root: Path,
) -> bool:
    """CAS-remove one Source from the canonical sidecar."""
    value = str(source_id or "")
    if not value:
        return False
    source_links = _source_links_module(Path(rag_root))
    loaded = source_links.load_source_links(
        Path(db_root),
        Path(db_root).name,
    )
    if loaded.status == "unconfigured":
        return False
    if loaded.status != "configured" or not isinstance(loaded.payload, dict):
        raise SourceManagerError(
            "canonical Source Metadata is not writable"
        )
    payload = copy.deepcopy(loaded.payload)
    sources = payload.get("sources")
    sources = sources if isinstance(sources, list) else []
    remaining = [
        item
        for item in sources
        if not isinstance(item, dict)
        or str(item.get("source_id") or "") != value
    ]
    if len(remaining) == len(sources):
        return False
    payload["schema_version"] = source_links.SCHEMA_VERSION
    payload["revision"] = int(loaded.revision) + 1
    payload["sources"] = remaining
    try:
        source_links.save_source_links(
            Path(db_root),
            payload,
            db_name=Path(db_root).name,
            allow_unmatched_sources=True,
            expected_revision=int(loaded.revision),
            expected_etag=str(loaded.etag),
        )
    except Exception as exc:
        raise SourceManagerError(
            "Source Metadata removal failed"
        ) from exc
    return True


def _canonical_source(
    source: Mapping[str, Any],
    *,
    current_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "source_id": str(source["source_id"]),
        "display_name": str(source["display_name"]),
        "source_type": str(source["source_type"]),
    }
    pending = source.get("pending_metadata")
    selected_link: Mapping[str, Any] | None = None
    if isinstance(pending, Mapping):
        link = pending.get("link")
        if isinstance(link, Mapping):
            selected_link = link
    elif isinstance(current_source, Mapping):
        current_link = current_source.get("link")
        if isinstance(current_link, Mapping):
            selected_link = current_link
    if selected_link is not None:
        link_value = copy.deepcopy(dict(selected_link))
        if value["source_type"] == "redmine":
            fetch = source.get("fetch")
            project_url = (
                fetch.get("project_url")
                if isinstance(fetch, Mapping)
                else None
            )
            if project_url:
                repaired = repair_generated_redmine_link(
                    project_url,
                    link_value,
                )
                if repaired is not None:
                    link_value = repaired
        elif value["source_type"] == "gitlab_issues":
            fetch = source.get("fetch")
            project_url = (
                fetch.get("project_url")
                if isinstance(fetch, Mapping)
                else None
            )
            gitlab_url = (
                fetch.get("gitlab_url")
                if isinstance(fetch, Mapping)
                else None
            )
            if project_url and gitlab_url:
                repaired = repair_generated_gitlab_issues_link(
                    project_url,
                    gitlab_url,
                    link_value,
                )
                if repaired is not None:
                    link_value = repaired
        value["link"] = link_value
    return value


def _source_links_module(rag_root: Path):
    tool_root = (
        Path(rag_root)
        / "gen_db"
        / "software_rag_tool"
    )
    if not tool_root.is_dir():
        raise SourceManagerError(
            "Source Metadata runtime is unavailable"
        )
    value = str(tool_root)
    if value not in sys.path:
        sys.path.insert(0, value)
    try:
        from software_rag_tool import source_links
    except Exception as exc:
        raise SourceManagerError(
            "Source Metadata runtime is unavailable"
        ) from exc
    return source_links
