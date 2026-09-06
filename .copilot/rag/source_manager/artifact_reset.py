from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError
from .operation_lock import database_operation_lock
from . import providers


_CATALOG_FILES = (
    "catalog.sqlite",
    "catalog.sqlite-wal",
    "catalog.sqlite-shm",
    "catalog.sqlite-journal",
)
_ACTIVE_TOP_LEVEL = ("data", "index", "logs", "rag-wrapper.json")
_RETIRED_ROOT = ".retired-data"
_PROTECTED_ROOT = ".protected-originals"


def _tool_root() -> Path:
    return Path(__file__).resolve().parents[1] / "gen_db" / "software_rag_tool"


def _runtime() -> tuple[Any, Any, Any]:
    tool_root = _tool_root()
    if str(tool_root) not in sys.path:
        sys.path.insert(0, str(tool_root))
    from software_rag_tool.atomic_io import atomic_write_json, retry_windows_sharing
    from software_rag_tool.data_lifecycle import canonical_digest

    return atomic_write_json, retry_windows_sharing, canonical_digest


def plan_data_reset(db_root: Path) -> dict[str, Any]:
    root = Path(db_root).resolve(strict=True)
    _validate_db_root(root)
    _atomic_write_json, _retry, canonical_digest = _runtime()
    sources: list[dict[str, Any]] = []
    exceptions: list[dict[str, str]] = []
    source_root = root / "sources"
    _safe_entry(root, source_root)
    if source_root.is_dir():
        for directory in sorted(source_root.iterdir(), key=lambda item: item.name):
            _safe_entry(root, directory)
            config_path = directory / "source.json"
            _safe_entry(root, config_path)
            if not directory.is_dir() or not config_path.is_file():
                continue
            try:
                payload = json.loads(
                    config_path.read_text(encoding="utf-8", errors="strict")
                )
                if not isinstance(payload, dict):
                    raise ValueError("source config is not an object")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                exceptions.append(
                    {
                        "source_key": directory.name,
                        "reason": "source_configuration_unreadable",
                        "detail": type(exc).__name__,
                    }
                )
                continue
            source_type = str(payload.get("source_type") or "").strip().lower()
            sources.append(
                {
                    "source_key": directory.name,
                    "display_name": str(payload.get("display_name") or directory.name),
                    "source_type": source_type,
                    "configuration": _semantic_source_configuration(payload),
                    "work_present": (directory / "work").exists(),
                    "work_disposition": (
                        "protect_original" if source_type == "other" else "retire"
                    ),
                }
            )
            if source_type == "other":
                exceptions.append(
                    {
                        "source_key": directory.name,
                        "reason": "one_shot_original_requires_reimport",
                        "detail": "protected original is retained outside normal read paths",
                    }
                )
            elif source_type not in providers.SUPPORTED_PROVIDERS:
                exceptions.append(
                    {
                        "source_key": directory.name,
                        "reason": "source_type_requires_attention",
                        "detail": "Source type is not supported by the current runtime",
                    }
                )
    digest = canonical_digest(
        item["configuration"]
        for item in sorted(sources, key=lambda item: item["source_key"])
    )
    if not sources:
        exceptions.append(
            {
                "source_key": "",
                "reason": "no_registered_sources",
                "detail": "register at least one Source before refreshing the database",
            }
        )
    return {
        "db": root.name,
        "db_root": root,
        "source_count": len(sources),
        "source_config_digest": digest,
        "sources": sources,
        "exceptions": exceptions,
        "preserved": [
            "db.json",
            "VERSION.json",
            "DB_PROFILE.md",
            "source-links.json",
            "source-links.json.bak",
            "sources/*/source.json",
            "external source originals",
            "credentials and machine settings",
        ],
        "detached": [
            *_CATALOG_FILES,
            *_ACTIVE_TOP_LEVEL,
            "sources/*/state.json",
            "sources/*/events.jsonl",
            "sources/*/work",
        ],
    }


def reset_data(db_root: Path, *, daemon_status: str) -> dict[str, Any]:
    root = Path(db_root).resolve(strict=True)
    if str(daemon_status or "").strip() not in {"stopped", "not_running"}:
        raise SourceManagerError(
            "search daemon stop was not confirmed", stage="data_reset.daemon_stop"
        )
    plan = plan_data_reset(root)
    tool_root = _tool_root()
    if str(tool_root) not in sys.path:
        sys.path.insert(0, str(tool_root))
    from software_rag_tool.data_lifecycle import (
        ATTENTION_REQUIRED,
        REFETCH_REQUIRED,
        new_reset_lifecycle,
        read_lifecycle,
        transition_lifecycle,
        write_lifecycle,
    )
    from software_rag_tool.writer_runtime import (
        bind_database_runtime,
        database_writer_lock,
    )

    with database_operation_lock(root):
        with bind_database_runtime(root.parent, root.name) as target:
            with database_writer_lock(target):
                lifecycle = read_lifecycle(root)
                if lifecycle is None or lifecycle.status == "ready":
                    lifecycle = new_reset_lifecycle(
                        root, source_config_digest=plan["source_config_digest"]
                    )
                    write_lifecycle(root, lifecycle)
                elif lifecycle.source_config_digest != plan["source_config_digest"]:
                    if lifecycle.status == "resetting":
                        raise SourceManagerError(
                            "Source configuration changed during an unfinished reset",
                            stage="data_reset.configuration_changed",
                        )
                    lifecycle = new_reset_lifecycle(
                        root, source_config_digest=plan["source_config_digest"]
                    )
                    write_lifecycle(root, lifecycle)
                elif lifecycle.status != "resetting" and _active_generated_data_exists(
                    root, plan
                ):
                    # A completed reset may be followed by a partial refresh.
                    # A later reset must isolate those new partial artifacts in
                    # a fresh destination, rather than collide with the prior
                    # completed run.
                    lifecycle = new_reset_lifecycle(
                        root, source_config_digest=plan["source_config_digest"]
                    )
                    write_lifecycle(root, lifecycle)
                result = _execute_plan(root, plan, lifecycle.reset_run_id)
                final_status = (
                    ATTENTION_REQUIRED if plan["exceptions"] else REFETCH_REQUIRED
                )
                lifecycle = transition_lifecycle(root, lifecycle, status=final_status)
                return {
                    "status": final_status,
                    "epoch": lifecycle.epoch,
                    "reset_run_id": lifecycle.reset_run_id,
                    "detached": result["detached"],
                    "protected_originals": result["protected_originals"],
                    "exceptions": plan["exceptions"],
                }


def reset_derived_artifacts(db_root: Path, *, daemon_status: str) -> dict[str, Any]:
    """Backward-compatible name; semantics are now the full data-side reset."""
    return reset_data(db_root, daemon_status=daemon_status)


def _execute_plan(root: Path, plan: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    atomic_write_json, retry_windows_sharing, _digest = _runtime()
    retired = root / _RETIRED_ROOT / run_id
    protected = root / _PROTECTED_ROOT
    _safe_entry(root, retired)
    _safe_entry(root, protected)
    retired.mkdir(parents=True, exist_ok=True)
    journal_path = retired / "reset-journal.json"
    detached: list[str] = []
    protected_originals: list[str] = []

    def publish_journal(state: str) -> None:
        atomic_write_json(
            journal_path,
            {
                "schema_version": "local-rag.data-reset-journal.v1",
                "db": root.name,
                "run_id": run_id,
                "state": state,
                "detached": detached,
                "protected_originals": protected_originals,
            },
        )

    publish_journal("running")
    for relative in (*_CATALOG_FILES, *_ACTIVE_TOP_LEVEL):
        _move_if_present(
            root,
            root / relative,
            retired / "active" / relative,
            detached,
            retry_windows_sharing,
        )
        publish_journal("running")
    for source in plan["sources"]:
        source_key = source["source_key"]
        source_dir = root / "sources" / source_key
        for relative in ("state.json", "events.jsonl"):
            _move_if_present(
                root,
                source_dir / relative,
                retired / "sources" / source_key / relative,
                detached,
                retry_windows_sharing,
            )
        work = source_dir / "work"
        if source["work_disposition"] == "protect_original":
            destination = protected / source_key / run_id / "content"
            if work.exists():
                _move_if_present(
                    root, work, destination, protected_originals, retry_windows_sharing
                )
            # A prior attempt may have completed the atomic move and then
            # failed while publishing the seal.  Rebuild the deterministic
            # seal from the already detached content so retry converges.
            if destination.exists():
                manifest = _protected_manifest(root, destination, source_key, run_id)
                atomic_write_json(destination.parent / "manifest.json", manifest)
                atomic_write_json(
                    destination.parent / "SEALED.json",
                    {
                        "schema_version": "local-rag.protected-original.v1",
                        "manifest_sha256": hashlib.sha256(
                            json.dumps(
                                manifest,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    },
                )
        else:
            _move_if_present(
                root,
                work,
                retired / "sources" / source_key / "work",
                detached,
                retry_windows_sharing,
            )
        publish_journal("running")
    publish_journal("complete")
    return {"detached": detached, "protected_originals": protected_originals}


def _move_if_present(
    root: Path,
    source: Path,
    destination: Path,
    recorded: list[str],
    retry: Any,
) -> None:
    _safe_entry(root, source)
    _safe_entry(root, destination)
    if not source.exists():
        return
    if destination.exists():
        raise SourceManagerError(
            "reset destination already exists while source is still active",
            stage="data_reset.destination_conflict",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    retry(lambda: os.replace(source, destination))
    recorded.append(source.relative_to(root).as_posix())


def _active_generated_data_exists(root: Path, plan: Mapping[str, Any]) -> bool:
    candidates = [root / item for item in (*_CATALOG_FILES, *_ACTIVE_TOP_LEVEL)]
    for source in plan["sources"]:
        directory = root / "sources" / str(source["source_key"])
        candidates.extend(
            (directory / "state.json", directory / "events.jsonl", directory / "work")
        )
    return any(path.exists() for path in candidates)


def _semantic_source_configuration(payload: Mapping[str, Any]) -> dict[str, Any]:
    fetch = dict(payload.get("fetch") or {})
    # These are generated Confluence discovery products, not operator scope.
    fetch.pop("page_urls", None)
    fetch.pop("resolved_page_urls", None)
    return {
        "local_source_key": str(payload.get("local_source_key") or ""),
        "source_type": str(payload.get("source_type") or "").strip().lower(),
        "display_name": str(payload.get("display_name") or ""),
        "fetch": fetch,
        "classification": payload.get("classification"),
        "description": payload.get("description"),
    }


def _protected_manifest(
    root: Path, content: Path, source_key: str, run_id: str
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    if content.is_dir():
        for path in sorted(content.rglob("*")):
            _safe_entry(root, path)
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                files.append(
                    {
                        "relative_path": path.relative_to(content).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": digest.hexdigest(),
                    }
                )
    return {
        "schema_version": "local-rag.protected-original-manifest.v1",
        "source_key": source_key,
        "run_id": run_id,
        "files": files,
    }


def _validate_db_root(root: Path) -> None:
    if not (root / "db.json").is_file():
        raise SourceManagerError("database root is unsafe", stage="data_reset.preflight")
    _safe_entry(root, root / "db.json")


def _safe_entry(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SourceManagerError("data reset target escaped database root") from exc
    current = path
    while current != root:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            current = current.parent
            continue
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)
            or (hasattr(current, "is_junction") and current.is_junction())
        ):
            raise SourceManagerError("data reset target contains a link")
        current = current.parent
