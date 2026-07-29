from __future__ import annotations

import functools
from typing import Any, Iterable, Mapping


_RUNTIME_PATCH_MARKER = "_local_rag_provisional_source_merge_runtime_installed"
_CLASS_PATCH_MARKER = "_local_rag_provisional_source_merge_installed"
_PROVISIONAL_MARKER = "_provisional_catalog_identity"

# Catalog-derived fields remain authoritative when a provisional management
# record is joined to the partially indexed catalog Source.
_CATALOG_FIELDS = frozenset(
    {
        "source_id",
        "document_count",
        "chunk_count",
        "observed_stored_roots",
        "observed_root_status",
        "observed_roots",
        "last_updated_at",
        "indexed_file_count",
        "error_file_count",
        "link_mapping_count",
        "link_status",
        "link_providers",
        "sample_documents",
        "ingestion_scopes",
        "source_link_setting",
        "diagnostics",
        "_catalog_present",
    }
)


def install_provisional_source_merge_runtime() -> None:
    """Extend the Manager installer before a LocalRagManager is instantiated."""

    from . import manager_connections

    if bool(getattr(manager_connections, _RUNTIME_PATCH_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_merge(manager_class)

    manager_connections.install_manager_connection_ui = install_manager_connection_ui
    setattr(manager_connections, _RUNTIME_PATCH_MARKER, True)


def merge_provisional_source_records(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse one provisional management row with its partial catalog row.

    During first ingestion, ADD uses ``local_source_key`` as the indexed
    ``source_id`` before Source Metadata confirmation.  If ingestion is
    interrupted after a batch commit, the catalog therefore contains that key
    while ``source.json`` still has ``source_id = null``.  Those two rows are
    one logical Source and must be presented as one.
    """

    values = [dict(value) for value in records]
    catalog_ids = {
        str(value.get("source_id") or "").strip()
        for value in values
        if value.get("_catalog_present")
        and str(value.get("source_id") or "").strip()
    }
    provisional_by_key = {
        str(value.get("_local_source_key") or "").strip(): value
        for value in values
        if not value.get("_catalog_present")
        and not str(value.get("source_id") or "").strip()
        and str(value.get("_local_source_key") or "").strip()
    }
    matches = catalog_ids.intersection(provisional_by_key)
    if not matches:
        return values

    combined: list[dict[str, Any]] = []
    for value in values:
        source_id = str(value.get("source_id") or "").strip()
        local_key = str(value.get("_local_source_key") or "").strip()

        if value.get("_catalog_present") and source_id in matches:
            provisional = provisional_by_key[source_id]
            merged = dict(value)
            for key, item in provisional.items():
                if key not in _CATALOG_FIELDS:
                    merged[key] = item
            merged["source_id"] = source_id
            merged["_catalog_present"] = True
            merged[_PROVISIONAL_MARKER] = True
            combined.append(merged)
            continue

        if (
            not value.get("_catalog_present")
            and not source_id
            and local_key in matches
        ):
            continue

        combined.append(value)
    return combined


def _install_manager_merge(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _CLASS_PATCH_MARKER, False)):
        return

    original_combined = manager_class._combined_source_records
    original_status = manager_class._source_manager_status

    @functools.wraps(original_combined)
    def combined_source_records(
        self: Any,
        db_name: str,
        catalog_sources: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records = original_combined(self, db_name, catalog_sources)
        return merge_provisional_source_records(records)

    @functools.wraps(original_status)
    def source_manager_status(self: Any, source: dict[str, Any]) -> str:
        if bool(source.get(_PROVISIONAL_MARKER)):
            return "初回取得途中・再開可能"
        return original_status(self, source)

    manager_class._combined_source_records = combined_source_records
    manager_class._source_manager_status = source_manager_status
    setattr(manager_class, _CLASS_PATCH_MARKER, True)
