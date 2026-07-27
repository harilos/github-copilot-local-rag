from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .catalog import connect_readonly
from .source_paths import (
    SourcePathError,
    canonical_stored_path,
    observed_root_from_paths,
)
from .source_links import SourceLinksLoad, load_source_links


@dataclass(frozen=True)
class InventoryDiagnostic:
    code: str
    message: str = ""


@dataclass(frozen=True)
class ObservedRootSummary:
    root: str
    document_count: int


@dataclass(frozen=True)
class DocumentSample:
    path: str
    title: str
    updated_at: str | None = None


@dataclass(frozen=True)
class SourceLinkSetting:
    display_name: str
    provider: str
    strategy: str
    enabled: bool
    status: str

    @property
    def configured(self) -> bool:
        return bool(self.provider)


@dataclass(frozen=True)
class SupplementalSourceState:
    state_file_count: int = 0
    indexed_files: int = 0
    error_files: int = 0
    record_count: int = 0
    scan_subdirectories: tuple[str, ...] = ()
    progress_status: str = ""
    progress_phase: str = ""
    ingestion_scopes: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SourceSummary:
    source_id: str
    display_name: str
    document_count: int
    chunk_count: int
    observed_roots: tuple[ObservedRootSummary, ...]
    last_updated_at: str | None
    sample_documents: tuple[DocumentSample, ...]
    document_paths: tuple[str, ...]
    link_setting: SourceLinkSetting | None
    supplemental_state: SupplementalSourceState
    diagnostics: tuple[InventoryDiagnostic, ...] = ()

    @property
    def observed_stored_roots(self) -> tuple[str, ...]:
        return tuple(item.root for item in self.observed_roots)

    @property
    def observed_root_status(self) -> str:
        if not self.observed_roots:
            return "no_observed_root"
        if len(self.observed_roots) > 1:
            return "multiple_observed_roots"
        return "ready"

    @property
    def indexed_file_count(self) -> int | None:
        return self.supplemental_state.indexed_files or None

    @property
    def error_file_count(self) -> int | None:
        return self.supplemental_state.error_files or None

    @property
    def link_mapping_count(self) -> int:
        return (
            1
            if self.link_setting is not None
            and self.link_setting.configured
            else 0
        )

    @property
    def link_status(self) -> str:
        if self.link_setting is None or not self.link_setting.configured:
            return (
                self.link_setting.status
                if self.link_setting is not None
                else "not_configured"
            )
        if self.link_setting.status not in {"", "configured"}:
            return self.link_setting.status
        return (
            "configured"
            if self.link_setting.enabled
            else "disabled"
        )

    @property
    def link_providers(self) -> tuple[str, ...]:
        if self.link_setting is None:
            return ()
        return (
            (self.link_setting.provider,)
            if self.link_setting.provider
            else ()
        )

    @property
    def ingestion_scopes(self) -> tuple[dict[str, Any], ...]:
        return self.supplemental_state.ingestion_scopes

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "observed_stored_roots": list(self.observed_stored_roots),
            "observed_root_status": self.observed_root_status,
            "observed_roots": [
                asdict(value) for value in self.observed_roots
            ],
            "last_updated_at": self.last_updated_at,
            "indexed_file_count": self.indexed_file_count,
            "error_file_count": self.error_file_count,
            "link_mapping_count": self.link_mapping_count,
            "link_status": self.link_status,
            "link_providers": list(self.link_providers),
            "sample_documents": [
                asdict(value) for value in self.sample_documents
            ],
            "ingestion_scopes": list(self.ingestion_scopes),
            "source_link_setting": (
                {
                    "display_name": self.link_setting.display_name,
                    "provider": self.link_setting.provider,
                    "strategy": self.link_setting.strategy,
                    "enabled": self.link_setting.enabled,
                    "configuration": (
                        "configured"
                        if self.link_setting.configured
                        else "not_configured"
                    ),
                    "status": self.link_status,
                }
                if self.link_setting
                else None
            ),
            "diagnostics": [
                asdict(value) for value in self.diagnostics
            ],
        }


@dataclass(frozen=True)
class UnmatchedSourceLinkSetting:
    source_id: str
    provider: str
    enabled: bool

    @property
    def link_mapping_count(self) -> int:
        return 1 if self.provider else 0


@dataclass(frozen=True)
class UnmatchedStateSource:
    source_id: str
    state_file_count: int


@dataclass(frozen=True)
class SourceInventory:
    db_name: str
    catalog_status: str
    source_links_status: str
    sources: tuple[SourceSummary, ...]
    document_count: int
    chunk_count: int
    missing_source_document_count: int
    missing_source_chunk_count: int
    unmatched_settings: tuple[UnmatchedSourceLinkSetting, ...]
    unmatched_state_sources: tuple[UnmatchedStateSource, ...]
    diagnostics: tuple[InventoryDiagnostic, ...]

    @property
    def documents_without_source_id(self) -> int:
        return self.missing_source_document_count

    @property
    def unmatched_source_link_settings(
        self,
    ) -> tuple[UnmatchedSourceLinkSetting, ...]:
        return self.unmatched_settings

    @property
    def sidecar_status(self) -> str:
        return self.source_links_status

    def source_ids(self) -> list[str]:
        return [source.source_id for source in self.sources]

    def observed_paths_by_source(self) -> dict[str, list[str]]:
        return {
            source.source_id: list(source.document_paths)
            for source in self.sources
        }

    def get_source(self, source_id: str) -> SourceSummary | None:
        return next(
            (
                source
                for source in self.sources
                if source.source_id == source_id
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_name": self.db_name,
            "catalog_status": self.catalog_status,
            "source_links_status": self.source_links_status,
            "sources": [source.to_dict() for source in self.sources],
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "documents_without_source_id": (
                self.missing_source_document_count
            ),
            "missing_source_chunk_count": self.missing_source_chunk_count,
            "unmatched_source_link_settings": [
                asdict(value) for value in self.unmatched_settings
            ],
            "unmatched_state_sources": [
                asdict(value) for value in self.unmatched_state_sources
            ],
            "diagnostics": [
                asdict(value) for value in self.diagnostics
            ],
        }


def build_source_inventory(
    db_root: Path,
    db_name: str | None = None,
) -> SourceInventory:
    """Build catalog-authoritative Source inventory without DB writes."""
    root = Path(db_root).expanduser().resolve()
    name = db_name or root.name
    diagnostics: list[InventoryDiagnostic] = []
    catalog_path = root / "catalog.sqlite"
    if not catalog_path.is_file():
        sidecar = load_source_links(root, name, observed_roots={})
        if sidecar.status == "invalid":
            diagnostics.append(
                InventoryDiagnostic(
                    "source_links_invalid",
                    "The optional Source-Link sidecar is invalid.",
                )
            )
        diagnostics.append(
            InventoryDiagnostic(
                "catalog_missing",
                "catalog.sqlite is not available.",
            )
        )
        return SourceInventory(
            db_name=name,
            catalog_status="missing",
            source_links_status=sidecar.status,
            sources=(),
            document_count=0,
            chunk_count=0,
            missing_source_document_count=0,
            missing_source_chunk_count=0,
            unmatched_settings=_unmatched_link_settings(sidecar, set()),
            unmatched_state_sources=(),
            diagnostics=tuple(diagnostics),
        )

    catalog = _read_catalog(catalog_path)
    observed_roots: dict[str, tuple[str, ...]] = {}
    for row in catalog["sources"]:
        try:
            roots = observed_root_from_paths(
                canonical_stored_path(document["path"])
                for document in row["documents"]
                if _valid_stored_path(str(document["path"]))
            )
        except SourcePathError:
            roots = ()
        observed_roots[str(row["source_id"])] = roots
    sidecar = load_source_links(
        root,
        name,
        observed_roots=observed_roots,
    )
    if sidecar.status == "invalid":
        diagnostics.append(
            InventoryDiagnostic(
                "source_links_invalid",
                "The optional Source-Link sidecar is invalid"
                + (
                    f" ({sidecar.error_kind})."
                    if sidecar.error_kind
                    else "."
                ),
            )
        )
    if catalog["missing_documents"]:
        diagnostics.append(
            InventoryDiagnostic(
                "catalog_documents_missing_source_id",
                "Some indexed documents have no usable source_id.",
            )
        )
    configured = _configured_sources(sidecar)
    source_link_statuses = dict(sidecar.source_statuses)
    state = _supplemental_state(root)
    diagnostics.extend(state["diagnostics"])
    if state["missing_source_entries"]:
        diagnostics.append(
            InventoryDiagnostic(
                "index_state_entries_missing_source_id",
                "Some supplemental state entries have no source_id.",
            )
        )
    catalog_source_ids = {
        str(row["source_id"]) for row in catalog["sources"]
    }
    unmatched_state = tuple(
        UnmatchedStateSource(source_id, int(values["state_file_count"]))
        for source_id, values in sorted(state["sources"].items())
        if source_id not in catalog_source_ids
    )
    if unmatched_state:
        diagnostics.append(
            InventoryDiagnostic(
                "supplemental_state_without_catalog_source",
                "Supplemental state references a Source absent from the catalog.",
            )
        )

    summaries: list[SourceSummary] = []
    for row in catalog["sources"]:
        source_id = str(row["source_id"])
        paths: list[str] = []
        prefixes: dict[str, int] = {}
        source_diagnostics: list[InventoryDiagnostic] = []
        samples: list[DocumentSample] = []
        for document in row["documents"]:
            try:
                path = canonical_stored_path(document["path"])
            except SourcePathError:
                source_diagnostics.append(
                    InventoryDiagnostic(
                        "invalid_stored_path",
                        "An indexed path is not a canonical stored path.",
                    )
                )
                continue
            paths.append(path)
            prefix = path.split("/", 1)[0] + "/"
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if len(samples) < 5:
                samples.append(
                    DocumentSample(
                        path=path,
                        title=str(document.get("title") or ""),
                        updated_at=(
                            str(document["updated_at"])
                            if document.get("updated_at")
                            else None
                        ),
                    )
                )
        overlay = configured.get(source_id)
        link_setting = _link_setting(
            overlay,
            source_link_statuses.get(source_id, "not_configured"),
        )
        supplemental = _source_state(
            state,
            source_id,
        )
        summaries.append(
            SourceSummary(
                source_id=source_id,
                display_name=str(
                    (overlay or {}).get("display_name") or source_id
                ),
                document_count=int(row["document_count"]),
                chunk_count=int(row["chunk_count"]),
                observed_roots=tuple(
                    ObservedRootSummary(prefix, count)
                    for prefix, count in sorted(prefixes.items())
                ),
                last_updated_at=(
                    str(row["last_updated_at"])
                    if row["last_updated_at"]
                    else None
                ),
                sample_documents=tuple(samples),
                document_paths=tuple(paths),
                link_setting=link_setting,
                supplemental_state=supplemental,
                diagnostics=tuple(
                    {
                        item.code: item
                        for item in source_diagnostics
                    }.values()
                ),
            )
        )
    return SourceInventory(
        db_name=name,
        catalog_status="ready",
        source_links_status=sidecar.status,
        sources=tuple(summaries),
        document_count=int(catalog["document_count"]),
        chunk_count=int(catalog["chunk_count"]),
        missing_source_document_count=int(catalog["missing_documents"]),
        missing_source_chunk_count=int(catalog["missing_chunks"]),
        unmatched_settings=_unmatched_link_settings(
            sidecar,
            catalog_source_ids,
        ),
        unmatched_state_sources=unmatched_state,
        diagnostics=tuple(diagnostics),
    )


def _read_catalog(path: Path) -> dict[str, Any]:
    with connect_readonly(path) as connection:
        document_columns = _table_columns(connection, "document")
        chunk_columns = _table_columns(connection, "chunk")
        if not document_columns or not chunk_columns:
            raise sqlite3.OperationalError(
                "catalog document/chunk tables are missing"
            )
        document_visible = (
            "d.visible_until IS NULL"
            if "visible_until" in document_columns
            else "1=1"
        )
        chunk_visible_join = (
            "AND c.visible_until IS NULL"
            if "visible_until" in chunk_columns
            else ""
        )
        updated_expression = (
            "MAX(d.updated_at)"
            if "updated_at" in document_columns
            else "NULL"
        )
        grouped = connection.execute(
            f"""
            SELECT
              d.source_id,
              COUNT(DISTINCT d.doc_pk) AS document_count,
              COUNT(DISTINCT c.chunk_pk) AS chunk_count,
              {updated_expression} AS last_updated_at
            FROM document AS d
            LEFT JOIN chunk AS c
              ON c.doc_pk = d.doc_pk
              {chunk_visible_join}
            WHERE {document_visible}
              AND d.source_id IS NOT NULL
              AND TRIM(d.source_id) <> ''
            GROUP BY d.source_id
            ORDER BY d.source_id
            """
        ).fetchall()
        title_expression = (
            "d.title" if "title" in document_columns else "''"
        )
        updated_column = (
            "d.updated_at" if "updated_at" in document_columns else "NULL"
        )
        documents = connection.execute(
            f"""
            SELECT
              d.source_id,
              d.path,
              {title_expression} AS title,
              {updated_column} AS updated_at
            FROM document AS d
            WHERE {document_visible}
              AND d.source_id IS NOT NULL
              AND TRIM(d.source_id) <> ''
            ORDER BY d.source_id, d.path
            """
        ).fetchall()
        total_documents = int(
            connection.execute(
                f"SELECT COUNT(*) FROM document AS d WHERE {document_visible}"
            ).fetchone()[0]
        )
        total_chunks = int(
            connection.execute(
                f"""
                SELECT COUNT(c.chunk_pk)
                FROM chunk AS c
                JOIN document AS d ON d.doc_pk = c.doc_pk
                WHERE {document_visible}
                  {"AND c.visible_until IS NULL" if "visible_until" in chunk_columns else ""}
                """
            ).fetchone()[0]
        )
        missing_documents = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM document AS d
                WHERE {document_visible}
                  AND (d.source_id IS NULL OR TRIM(d.source_id) = '')
                """
            ).fetchone()[0]
        )
        missing_chunks = int(
            connection.execute(
                f"""
                SELECT COUNT(c.chunk_pk)
                FROM chunk AS c
                JOIN document AS d ON d.doc_pk = c.doc_pk
                WHERE {document_visible}
                  {"AND c.visible_until IS NULL" if "visible_until" in chunk_columns else ""}
                  AND (d.source_id IS NULL OR TRIM(d.source_id) = '')
                """
            ).fetchone()[0]
        )
    documents_by_source: dict[str, list[dict[str, Any]]] = {}
    for row in documents:
        documents_by_source.setdefault(str(row["source_id"]), []).append(
            {
                "path": str(row["path"] or ""),
                "title": str(row["title"] or ""),
                "updated_at": row["updated_at"],
            }
        )
    return {
        "sources": [
            {
                "source_id": str(row["source_id"]),
                "document_count": int(row["document_count"] or 0),
                "chunk_count": int(row["chunk_count"] or 0),
                "last_updated_at": row["last_updated_at"],
                "documents": documents_by_source.get(
                    str(row["source_id"]),
                    [],
                ),
            }
            for row in grouped
        ],
        "document_count": total_documents,
        "chunk_count": total_chunks,
        "missing_documents": missing_documents,
        "missing_chunks": missing_chunks,
    }


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _configured_sources(
    loaded: SourceLinksLoad,
) -> dict[str, dict[str, Any]]:
    if loaded.status != "configured" or loaded.payload is None:
        return {}
    return {
        str(source.get("source_id") or ""): source
        for source in loaded.payload.get("sources") or []
        if isinstance(source, dict) and source.get("source_id")
    }


def _link_setting(
    source: dict[str, Any] | None,
    status: str,
) -> SourceLinkSetting | None:
    if source is None and status == "not_configured":
        return None
    value = source or {}
    return SourceLinkSetting(
        display_name=str(value.get("display_name") or ""),
        provider=str(value.get("provider") or ""),
        strategy=str(value.get("strategy") or ""),
        enabled=bool(value.get("enabled")),
        status=status,
    )


def _unmatched_link_settings(
    loaded: SourceLinksLoad,
    catalog_source_ids: set[str],
) -> tuple[UnmatchedSourceLinkSetting, ...]:
    configured = _configured_sources(loaded)
    return tuple(
        UnmatchedSourceLinkSetting(
            source_id=source_id,
            provider=str(source.get("provider") or ""),
            enabled=bool(source.get("enabled")),
        )
        for source_id, source in sorted(configured.items())
        if source_id not in catalog_source_ids
    )


def _supplemental_state(root: Path) -> dict[str, Any]:
    diagnostics: list[InventoryDiagnostic] = []
    state = _load_json(
        root / "logs" / "index_state.json",
        diagnostics,
        "index_state_invalid",
    )
    progress = _load_json(
        root / "logs" / "progress.json",
        diagnostics,
        "progress_state_invalid",
    )
    sources: dict[str, dict[str, Any]] = {}
    missing_source_entries = 0
    files = state.get("files") if isinstance(state, dict) else {}
    if isinstance(files, dict):
        for item in files.values():
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or "")
            if not source_id:
                missing_source_entries += 1
                continue
            values = sources.setdefault(
                source_id,
                {
                    "state_file_count": 0,
                    "indexed_files": 0,
                    "error_files": 0,
                    "record_count": 0,
                    "scan_subdirectories": set(),
                    "scopes": [],
                },
            )
            values["state_file_count"] += 1
            status = str(item.get("status") or "")
            if status == "indexed":
                values["indexed_files"] += 1
            elif status == "error":
                values["error_files"] += 1
            record_ids = item.get("record_ids")
            if isinstance(record_ids, list):
                values["record_count"] += len(record_ids)
            else:
                try:
                    record_count = int(item.get("record_count") or 0)
                    if record_count < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    diagnostics.append(
                        InventoryDiagnostic(
                            "index_state_record_count_invalid",
                            "A supplemental record count was ignored.",
                        )
                    )
                else:
                    values["record_count"] += record_count
            scan_subdir = str(item.get("scan_subdir") or "")
            if scan_subdir:
                values["scan_subdirectories"].add(scan_subdir)
            scope = {
                key: item.get(key)
                for key in (
                    "root",
                    "resolved_root",
                    "root_display_name",
                    "scan_subdir",
                    "scan_root",
                    "stored_path_prefix",
                    "operation",
                    "status",
                )
                if item.get(key) not in (None, "")
            }
            if scope and scope not in values["scopes"]:
                values["scopes"].append(scope)
    ingestion = state.get("ingestion") if isinstance(state, dict) else {}
    for candidate in (ingestion, progress):
        if not isinstance(candidate, dict):
            continue
        source_id = str(candidate.get("source_id") or "")
        if not source_id:
            continue
        values = sources.setdefault(
            source_id,
            {
                "state_file_count": 0,
                "indexed_files": 0,
                "error_files": 0,
                "record_count": 0,
                "scan_subdirectories": set(),
                "scopes": [],
            },
        )
        scan_subdir = str(candidate.get("scan_subdir") or ".")
        values["scan_subdirectories"].add(scan_subdir)
        scope = {
            key: candidate.get(key)
            for key in (
                "root",
                "root_display_name",
                "scan_subdir",
                "scan_root",
                "stored_path_prefix",
                "operation",
                "status",
            )
            if candidate.get(key) not in (None, "")
        }
        if scope and scope not in values["scopes"]:
            values["scopes"].append(scope)
    return {
        "sources": sources,
        "missing_source_entries": missing_source_entries,
        "progress": progress,
        "diagnostics": diagnostics,
    }


def _source_state(
    state: dict[str, Any],
    source_id: str,
) -> SupplementalSourceState:
    values = state["sources"].get(source_id) or {}
    progress = state.get("progress") or {}
    progress_applies = str(progress.get("source_id") or "") == source_id
    return SupplementalSourceState(
        state_file_count=int(values.get("state_file_count") or 0),
        indexed_files=int(values.get("indexed_files") or 0),
        error_files=int(values.get("error_files") or 0),
        record_count=int(values.get("record_count") or 0),
        scan_subdirectories=tuple(
            sorted(values.get("scan_subdirectories") or [])
        ),
        progress_status=(
            str(progress.get("status") or "")
            if progress_applies
            else ""
        ),
        progress_phase=(
            str(progress.get("phase") or "")
            if progress_applies
            else ""
        ),
        ingestion_scopes=tuple(values.get("scopes") or []),
    )


def _valid_stored_path(value: str) -> bool:
    try:
        canonical_stored_path(value)
    except SourcePathError:
        return False
    return True


def _load_json(
    path: Path,
    diagnostics: list[InventoryDiagnostic],
    diagnostic_code: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        diagnostics.append(
            InventoryDiagnostic(
                diagnostic_code,
                "Optional ingestion state could not be read.",
            )
        )
        return {}
    if not isinstance(payload, dict):
        diagnostics.append(
            InventoryDiagnostic(
                diagnostic_code,
                "Optional ingestion state is not a JSON object.",
            )
        )
        return {}
    return payload
