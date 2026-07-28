from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "local-rag.db-maintenance.v1"
SEARCH_SCHEMA_VERSION = "local-rag.search.v1"
MAX_STATE_BYTES = 65_536
WRITER_LOCK_WAIT_SECONDS = 25.0
SEARCH_LOCK_WAIT_SECONDS = 0.25
WINDOWS_REPLACE_RETRY_SECONDS = 2.0

_ACTIVE = "active"
_FAILED = "failed"
_REQUIRES_REPAIR = "requires_repair"
_VALID_STATES = {_ACTIVE, _FAILED, _REQUIRES_REPAIR}
_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class MaintenanceError(RuntimeError):
    """A structured, target-DB-only maintenance failure."""

    def __init__(
        self,
        error_kind: str,
        db_name: str,
        *,
        operation: str | None = None,
        search_payload: dict[str, Any] | None = None,
    ) -> None:
        self.error_kind = error_kind
        self.db_name = db_name
        self.operation = operation
        self.search_payload = search_payload
        super().__init__(error_kind)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "status": "error",
            "error": self.error_kind,
            "db": self.db_name,
        }
        if self.operation:
            payload["operation"] = self.operation
        return payload


@dataclass
class MaintenanceLease:
    db_name: str
    operation: str
    lease_id: str
    operation_token: str
    owner_pid: int
    owner_start_identity: str | None
    started_at: str
    state_path: Path
    lock_path: Path
    _descriptor: int | None = field(repr=False)
    stale_recovered: bool = False

    @property
    def active(self) -> bool:
        return self._descriptor is not None


def maintenance_paths(
    db_name: str,
    *,
    rag_root: Path | None = None,
) -> tuple[Path, Path]:
    """Return the hashed state and permanent lock paths for one database."""
    normalized = _validate_db_name(db_name)
    root = _resolve_rag_root(rag_root)
    directory = root / "query" / "run" / "db-maintenance"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json", directory / f"{digest}.lock"


def acquire_maintenance_lease(
    db_name: str,
    *,
    operation: str,
    rag_root: Path | None = None,
    dbs_root: Path | None = None,
    lock_timeout_seconds: float = WRITER_LOCK_WAIT_SECONDS,
    recover_failed: bool = False,
) -> MaintenanceLease:
    """Acquire the persistent per-DB writer lease.

    The returned object owns the kernel lock until
    :func:`finish_maintenance` succeeds. Callers must not drop it while a
    mutation is running.
    """
    normalized_db = _validate_db_name(db_name)
    normalized_operation = _validate_operation(
        operation,
        db_name=normalized_db,
    )
    state_path, lock_path = maintenance_paths(
        normalized_db,
        rag_root=rag_root,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_directory(state_path.parent)
    _reject_live_or_failed_state(
        normalized_db,
        state_path=state_path,
        recover_failed=recover_failed,
    )
    try:
        descriptor = _acquire_kernel_lock(
            lock_path,
            timeout_seconds=max(0.0, float(lock_timeout_seconds)),
        )
    except MaintenanceError as exc:
        raise MaintenanceError(
            exc.error_kind,
            normalized_db,
            operation=normalized_operation,
        ) from exc
    stale_recovered = False
    try:
        loaded = _load_state_file(state_path, expected_db=normalized_db)
        if loaded["status"] == "invalid":
            raise MaintenanceError(
                "db_maintenance_state_invalid",
                normalized_db,
            )
        state = loaded.get("state")
        if isinstance(state, dict):
            state_kind = str(state.get("state") or "")
            if state_kind == _ACTIVE and not _active_owner_is_live(state):
                if (
                    database_integrity_ok(
                        normalized_db,
                        rag_root=rag_root,
                        dbs_root=dbs_root,
                    )
                    or recover_failed
                ):
                    state = None
                    stale_recovered = True
                else:
                    failed = {
                        **state,
                        "state": _REQUIRES_REPAIR,
                        "finished_at": _utc_now(),
                        "owner_pid": None,
                        "owner_start_identity": None,
                        "error_kind": "stale_maintenance_integrity_failed",
                    }
                    _atomic_write_json(state_path, failed)
                    raise MaintenanceError(
                        "db_requires_repair",
                        normalized_db,
                        operation=str(
                            state.get("operation") or ""
                        )
                        or None,
                    )
            elif state_kind == _ACTIVE:
                raise MaintenanceError(
                    "db_writer_already_active",
                    normalized_db,
                    operation=str(state.get("operation") or "") or None,
                )
            elif (
                state_kind in {_FAILED, _REQUIRES_REPAIR}
                and recover_failed
            ):
                state = None
            elif state_kind in {_FAILED, _REQUIRES_REPAIR}:
                raise MaintenanceError(
                    (
                        "db_requires_repair"
                        if state_kind == _REQUIRES_REPAIR
                        else "db_maintenance_failed"
                    ),
                    normalized_db,
                    operation=str(state.get("operation") or "") or None,
                )

        owner_pid = os.getpid()
        lease = MaintenanceLease(
            db_name=normalized_db,
            operation=normalized_operation,
            lease_id=uuid.uuid4().hex,
            operation_token=uuid.uuid4().hex,
            owner_pid=owner_pid,
            owner_start_identity=_process_start_identity(owner_pid),
            started_at=_utc_now(),
            state_path=state_path,
            lock_path=lock_path,
            _descriptor=descriptor,
            stale_recovered=stale_recovered,
        )
        _atomic_write_json(state_path, _active_payload(lease))
        return lease
    except Exception:
        _release_kernel_lock(descriptor)
        raise


def finish_maintenance(
    lease: MaintenanceLease,
    *,
    lease_id: str | None = None,
    operation_succeeded: bool = True,
    integrity_ok: bool | None = True,
    error_kind: str | None = None,
) -> dict[str, Any]:
    """Finish one lease without ever exposing a possibly inconsistent DB.

    A successful operation, or a failed operation whose integrity check
    passed, removes the maintenance state. An integrity failure persists a
    fail-closed ``requires_repair`` state. An unknown integrity result after
    failure persists ``failed``.
    """
    if not lease.active:
        raise MaintenanceError(
            "db_maintenance_lease_inactive",
            lease.db_name,
            operation=lease.operation,
        )
    supplied_lease_id = str(lease_id or lease.lease_id)
    loaded = _load_state_file(
        lease.state_path,
        expected_db=lease.db_name,
    )
    state = loaded.get("state")
    if loaded["status"] == "invalid":
        raise MaintenanceError(
            "db_maintenance_state_invalid",
            lease.db_name,
            operation=lease.operation,
        )
    if (
        not isinstance(state, dict)
        or str(state.get("lease_id") or "") != supplied_lease_id
        or supplied_lease_id != lease.lease_id
        or str(state.get("operation_token") or "")
        != lease.operation_token
    ):
        raise MaintenanceError(
            "maintenance_lease_mismatch",
            lease.db_name,
            operation=lease.operation,
        )

    if integrity_ok is False:
        state_kind = _REQUIRES_REPAIR
        persisted = {
            **state,
            "state": state_kind,
            "finished_at": _utc_now(),
            "owner_pid": None,
            "owner_start_identity": None,
            "error_kind": str(error_kind or state_kind),
        }
        _atomic_write_json(lease.state_path, persisted)
        final_status = "db_requires_repair"
    elif operation_succeeded or integrity_ok is True:
        final_status = (
            "maintenance_complete"
            if operation_succeeded
            else "maintenance_failed_integrity_verified"
        )
        lease.state_path.unlink(missing_ok=True)
        _fsync_directory(lease.state_path.parent)
    else:
        state_kind = _FAILED
        persisted = {
            **state,
            "state": state_kind,
            "finished_at": _utc_now(),
            "owner_pid": None,
            "owner_start_identity": None,
            "error_kind": str(error_kind or state_kind),
        }
        _atomic_write_json(lease.state_path, persisted)
        final_status = "db_maintenance_failed"

    _release_kernel_lock(lease._descriptor)
    lease._descriptor = None
    return {
        "schema": SCHEMA_VERSION,
        "status": final_status,
        "db": lease.db_name,
        "operation": lease.operation,
        "lease_id": lease.lease_id,
    }


def maintenance_status(
    db_name: str,
    *,
    rag_root: Path | None = None,
    dbs_root: Path | None = None,
) -> dict[str, Any]:
    """Return the current target-DB state and recover dead active owners."""
    normalized_db = _validate_db_name(db_name)
    state_path, lock_path = maintenance_paths(
        normalized_db,
        rag_root=rag_root,
    )
    loaded = _load_state_file(state_path, expected_db=normalized_db)
    if loaded["status"] == "missing":
        return _available_status(normalized_db)
    if loaded["status"] == "invalid":
        return {
            "schema": SCHEMA_VERSION,
            "status": "invalid",
            "error": "db_maintenance_state_invalid",
            "db": normalized_db,
            "blocks_search": True,
        }
    state = loaded["state"]
    state_kind = str(state.get("state") or "")
    if state_kind == _ACTIVE and not _active_owner_is_live(state):
        recovered = _recover_stale_active(
            normalized_db,
            state_path=state_path,
            lock_path=lock_path,
            rag_root=rag_root,
            dbs_root=dbs_root,
        )
        if recovered:
            return _available_status(
                normalized_db,
                stale_recovered=True,
            )
        # Another process may have acquired the writer lock or replaced the
        # state during recovery. Re-read once and remain fail-closed.
        loaded = _load_state_file(state_path, expected_db=normalized_db)
        if loaded["status"] == "missing":
            return _available_status(
                normalized_db,
                stale_recovered=True,
            )
        if loaded["status"] == "invalid":
            return {
                "schema": SCHEMA_VERSION,
                "status": "invalid",
                "error": "db_maintenance_state_invalid",
                "db": normalized_db,
                "blocks_search": True,
            }
        state = loaded["state"]
        state_kind = str(state.get("state") or "")

    error = {
        _ACTIVE: "db_maintenance_in_progress",
        _FAILED: "db_maintenance_failed",
        _REQUIRES_REPAIR: "db_requires_repair",
    }[state_kind]
    return {
        "schema": SCHEMA_VERSION,
        "status": state_kind,
        "error": error,
        "db": normalized_db,
        "operation": str(state.get("operation") or ""),
        "lease_id": str(state.get("lease_id") or ""),
        "started_at": str(state.get("started_at") or ""),
        "blocks_search": True,
    }


def maintenance_search_payload(
    db_name: str,
    *,
    rag_root: Path | None = None,
    dbs_root: Path | None = None,
) -> dict[str, Any] | None:
    """Return a compact search response when this DB must be blocked."""
    status = maintenance_status(
        db_name,
        rag_root=rag_root,
        dbs_root=dbs_root,
    )
    return _search_payload_from_status(status)


@contextmanager
def database_search_guard(
    db_name: str,
    *,
    rag_root: Path | None = None,
    dbs_root: Path | None = None,
) -> Iterator[None]:
    """Hold the per-DB kernel lock for one explicit synchronous search.

    The daemon manager performs its own request admission while a persistent
    worker is active. This guard is for explicit ``--no-daemon`` execution:
    the lock remains held until the child search has exited, closing the
    check-then-start race with a database writer.
    """
    normalized_db = _validate_db_name(db_name)
    state_path, lock_path = maintenance_paths(
        normalized_db,
        rag_root=rag_root,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_directory(state_path.parent)
    try:
        descriptor = _acquire_kernel_lock(
            lock_path,
            timeout_seconds=SEARCH_LOCK_WAIT_SECONDS,
        )
    except MaintenanceError as exc:
        payload = maintenance_search_payload(
            normalized_db,
            rag_root=rag_root,
            dbs_root=dbs_root,
        )
        if payload is not None:
            raise MaintenanceError(
                str(payload["error"]),
                normalized_db,
                operation=str(payload.get("operation") or "") or None,
                search_payload=payload,
            ) from exc
        raise MaintenanceError(
            "db_search_guard_busy",
            normalized_db,
        ) from exc
    try:
        payload = _search_payload_while_lock_held(
            normalized_db,
            state_path=state_path,
            rag_root=rag_root,
            dbs_root=dbs_root,
        )
        if payload is not None:
            raise MaintenanceError(
                str(payload["error"]),
                normalized_db,
                operation=str(payload.get("operation") or "") or None,
                search_payload=payload,
            )
        yield
    finally:
        _release_kernel_lock(descriptor)


def _active_payload(lease: MaintenanceLease) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "state": _ACTIVE,
        "db": lease.db_name,
        "operation": lease.operation,
        "lease_id": lease.lease_id,
        "operation_token": lease.operation_token,
        "owner_pid": lease.owner_pid,
        "owner_start_identity": lease.owner_start_identity,
        "started_at": lease.started_at,
    }


def _reject_live_or_failed_state(
    db_name: str,
    *,
    state_path: Path,
    recover_failed: bool,
) -> None:
    """Fail fast for another writer while still allowing stale recovery."""
    loaded = _load_state_file(state_path, expected_db=db_name)
    if loaded["status"] == "missing":
        return
    if loaded["status"] == "invalid":
        raise MaintenanceError(
            "db_maintenance_state_invalid",
            db_name,
        )
    state = loaded["state"]
    state_kind = str(state.get("state") or "")
    operation = str(state.get("operation") or "") or None
    if state_kind == _ACTIVE and _active_owner_is_live(state):
        raise MaintenanceError(
            "db_writer_already_active",
            db_name,
            operation=operation,
        )
    if state_kind == _FAILED and not recover_failed:
        raise MaintenanceError(
            "db_maintenance_failed",
            db_name,
            operation=operation,
        )
    if state_kind == _REQUIRES_REPAIR and not recover_failed:
        raise MaintenanceError(
            "db_requires_repair",
            db_name,
            operation=operation,
        )


def _search_payload_while_lock_held(
    db_name: str,
    *,
    state_path: Path,
    rag_root: Path | None,
    dbs_root: Path | None,
) -> dict[str, Any] | None:
    loaded = _load_state_file(state_path, expected_db=db_name)
    if loaded["status"] == "missing":
        return None
    if loaded["status"] == "valid":
        state = loaded["state"]
        if (
            str(state.get("state") or "") == _ACTIVE
            and not _active_owner_is_live(state)
        ):
            if database_integrity_ok(
                db_name,
                rag_root=rag_root,
                dbs_root=dbs_root,
            ):
                state_path.unlink(missing_ok=True)
                _fsync_directory(state_path.parent)
                return None
            failed = {
                **state,
                "state": _REQUIRES_REPAIR,
                "finished_at": _utc_now(),
                "owner_pid": None,
                "owner_start_identity": None,
                "error_kind": "stale_maintenance_integrity_failed",
            }
            _atomic_write_json(state_path, failed)
            loaded = {"status": "valid", "state": failed}
    status = _status_from_loaded(db_name, loaded)
    return _search_payload_from_status(status)


def _available_status(
    db_name: str,
    *,
    stale_recovered: bool = False,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "status": "available",
        "db": db_name,
        "blocks_search": False,
        "stale_recovered": stale_recovered,
    }


def _status_from_loaded(
    db_name: str,
    loaded: dict[str, Any],
) -> dict[str, Any]:
    if loaded["status"] == "invalid":
        return {
            "schema": SCHEMA_VERSION,
            "status": "invalid",
            "error": "db_maintenance_state_invalid",
            "db": db_name,
            "blocks_search": True,
        }
    if loaded["status"] == "missing":
        return _available_status(db_name)
    state = loaded["state"]
    state_kind = str(state.get("state") or "")
    error = {
        _ACTIVE: "db_maintenance_in_progress",
        _FAILED: "db_maintenance_failed",
        _REQUIRES_REPAIR: "db_requires_repair",
    }[state_kind]
    return {
        "schema": SCHEMA_VERSION,
        "status": state_kind,
        "error": error,
        "db": db_name,
        "operation": str(state.get("operation") or ""),
        "lease_id": str(state.get("lease_id") or ""),
        "started_at": str(state.get("started_at") or ""),
        "blocks_search": True,
    }


def _search_payload_from_status(
    status: dict[str, Any],
) -> dict[str, Any] | None:
    if not status.get("blocks_search"):
        return None
    error = str(status.get("error") or "db_maintenance_state_invalid")
    operation = str(status.get("operation") or "")
    if error == "db_maintenance_in_progress":
        return {
            "schema": SEARCH_SCHEMA_VERSION,
            "status": "busy",
            "error": error,
            "db": status["db"],
            "operation": operation or "maintenance",
        }
    payload: dict[str, Any] = {
        "schema": SEARCH_SCHEMA_VERSION,
        "status": "error",
        "error": error,
        "error_kind": error,
        "db": status["db"],
        "selected_db": status["db"],
        "evidence": [],
        "background_context": [],
        "related_context": [],
        "document_results": [],
        "warnings": [],
    }
    if operation:
        payload["operation"] = operation
    return payload


def _recover_stale_active(
    db_name: str,
    *,
    state_path: Path,
    lock_path: Path,
    rag_root: Path | None,
    dbs_root: Path | None,
) -> bool:
    try:
        descriptor = _acquire_kernel_lock(
            lock_path,
            timeout_seconds=0.0,
        )
    except MaintenanceError:
        return False
    try:
        loaded = _load_state_file(state_path, expected_db=db_name)
        state = loaded.get("state")
        if (
            loaded["status"] == "valid"
            and isinstance(state, dict)
            and str(state.get("state") or "") == _ACTIVE
            and not _active_owner_is_live(state)
        ):
            if database_integrity_ok(
                db_name,
                rag_root=rag_root,
                dbs_root=dbs_root,
            ):
                state_path.unlink(missing_ok=True)
                _fsync_directory(state_path.parent)
                return True
            failed = {
                **state,
                "state": _REQUIRES_REPAIR,
                "finished_at": _utc_now(),
                "owner_pid": None,
                "owner_start_identity": None,
                "error_kind": "stale_maintenance_integrity_failed",
            }
            _atomic_write_json(state_path, failed)
            return False
        return loaded["status"] == "missing"
    finally:
        _release_kernel_lock(descriptor)


def database_integrity_ok(
    db_name: str,
    *,
    rag_root: Path | None = None,
    dbs_root: Path | None = None,
) -> bool:
    """Run a bounded, read-only consistency check after abnormal mutation."""
    try:
        normalized_db = _validate_db_name(db_name)
        root = _resolve_rag_root(rag_root)
        configured_dbs_root = _resolve_dbs_root(
            root,
            dbs_root=dbs_root,
        )
        db_root = (configured_dbs_root / normalized_db).resolve()
        db_root.relative_to(configured_dbs_root)
        config = _read_json_object(db_root / "db.json")
        version = _read_json_object(db_root / "VERSION.json")
        manifest = _read_json_object(
            db_root / "index" / "manifest.json"
        )
        if version.get("schema") != "local-rag.db-version.v1":
            return False
        collection = str(
            config.get("collection")
            or version.get("collection")
            or manifest.get("collection")
            or ""
        )
        if not collection:
            return False
        catalog_count = _sqlite_count(
            db_root / "catalog.sqlite",
            "SELECT COUNT(*) FROM chunk",
        )
        chroma_path = db_root / "index" / "chroma" / "chroma.sqlite3"
        chroma_uri = f"{chroma_path.resolve().as_uri()}?mode=ro"
        chroma = sqlite3.connect(chroma_uri, uri=True, timeout=2.0)
        try:
            if chroma.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            row = chroma.execute(
                "SELECT id FROM collections WHERE name = ?",
                (collection,),
            ).fetchone()
            if row is None:
                return False
            chroma_count = int(
                chroma.execute(
                    """
                    SELECT COUNT(*)
                    FROM embeddings AS e
                    JOIN segments AS s ON s.id = e.segment_id
                    WHERE s.collection = ?
                    """,
                    (row[0],),
                ).fetchone()[0]
            )
        finally:
            chroma.close()
        manifest_count = manifest.get("record_count")
        return bool(
            catalog_count == chroma_count
            and (
                manifest_count is None
                or int(manifest_count) == chroma_count
            )
        )
    except (
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        return False


def recover_maintenance_state(
    db_name: str,
    *,
    rag_root: Path | None = None,
    dbs_root: Path | None = None,
) -> dict[str, Any]:
    """Explicitly clear a stale/failed state only for a healthy database."""
    normalized_db = _validate_db_name(db_name)
    state_path, lock_path = maintenance_paths(
        normalized_db,
        rag_root=rag_root,
    )
    if not state_path.exists():
        return _available_status(normalized_db)
    descriptor = _acquire_kernel_lock(
        lock_path,
        timeout_seconds=LOCK_WAIT_SECONDS,
    )
    try:
        loaded = _load_state_file(
            state_path,
            expected_db=normalized_db,
        )
        state = loaded.get("state")
        if (
            loaded["status"] == "valid"
            and isinstance(state, dict)
            and str(state.get("state") or "") == _ACTIVE
            and _active_owner_is_live(state)
        ):
            raise MaintenanceError(
                "db_writer_already_active",
                normalized_db,
                operation=str(state.get("operation") or "") or None,
            )
        if not database_integrity_ok(
            normalized_db,
            rag_root=rag_root,
            dbs_root=dbs_root,
        ):
            raise MaintenanceError(
                "db_requires_repair",
                normalized_db,
            )
        state_path.unlink(missing_ok=True)
        _fsync_directory(state_path.parent)
        return {
            **_available_status(normalized_db),
            "recovered": True,
        }
    finally:
        _release_kernel_lock(descriptor)


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _sqlite_count(path: Path, query: str) -> int:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("SQLite quick_check failed")
        return int(connection.execute(query).fetchone()[0])
    finally:
        connection.close()


def _load_state_file(
    path: Path,
    *,
    expected_db: str,
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"status": "missing", "state": None}
    except OSError:
        return {"status": "invalid", "state": None}
    if not raw or len(raw) > MAX_STATE_BYTES:
        return {"status": "invalid", "state": None}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        return {"status": "invalid", "state": None}
    if not _valid_state_payload(payload, expected_db=expected_db):
        return {"status": "invalid", "state": None}
    return {"status": "valid", "state": payload}


def _valid_state_payload(payload: Any, *, expected_db: str) -> bool:
    if not isinstance(payload, dict):
        return False
    state = payload.get("state")
    if (
        payload.get("schema") != SCHEMA_VERSION
        or payload.get("db") != expected_db
        or state not in _VALID_STATES
        or not _OPERATION.fullmatch(str(payload.get("operation") or ""))
        or not _valid_token(payload.get("lease_id"))
        or not _valid_token(payload.get("operation_token"))
        or not isinstance(payload.get("started_at"), str)
    ):
        return False
    if state == _ACTIVE:
        owner_pid = payload.get("owner_pid")
        if (
            not isinstance(owner_pid, int)
            or isinstance(owner_pid, bool)
            or owner_pid <= 0
        ):
            return False
        identity = payload.get("owner_start_identity")
        if identity is not None and not isinstance(identity, str):
            return False
    return True


def _valid_token(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and 8 <= len(value) <= 128
        and value.isascii()
        and value.replace("-", "").replace("_", "").isalnum()
    )


def _active_owner_is_live(state: dict[str, Any]) -> bool:
    pid = int(state.get("owner_pid") or 0)
    if not _pid_is_alive(pid):
        return False
    expected_identity = str(state.get("owner_start_identity") or "")
    if not expected_identity:
        # Identity probes are best-effort. A live PID is fail-closed when no
        # stronger identity was available at acquisition time.
        return True
    current_identity = _process_start_identity(pid)
    if current_identity is None:
        return True
    return current_identity == expected_identity


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                kernel32.GetExitCodeProcess.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.DWORD),
                ]
                kernel32.GetExitCodeProcess.restype = wintypes.BOOL
                if not kernel32.GetExitCodeProcess(
                    handle,
                    ctypes.byref(exit_code),
                ):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_start_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_identity(pid)
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii")
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        if closing > 0 and len(fields) > 19:
            return f"proc:{fields[19]}"
    except (OSError, UnicodeError, ValueError):
        pass
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(completed.stdout.split())
    return f"ps:{value}" if completed.returncode == 0 and value else None


def _windows_process_start_identity(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"win:{value}"
        finally:
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def _resolve_rag_root(rag_root: Path | None) -> Path:
    if rag_root is not None:
        value = (
            rag_root
            if isinstance(rag_root, Path)
            else Path(rag_root)
        )
        return value.expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def _resolve_dbs_root(
    rag_root: Path,
    *,
    dbs_root: Path | None,
) -> Path:
    configured = dbs_root or os.getenv("RAG_DBS_ROOT")
    if configured:
        value = (
            configured
            if isinstance(configured, Path)
            else Path(configured)
        )
        return value.expanduser().resolve()
    return (rag_root / "dbs").resolve()


def _validate_db_name(db_name: str) -> str:
    normalized = str(db_name or "").strip()
    if (
        not _DB_NAME.fullmatch(normalized)
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
    ):
        raise MaintenanceError("invalid_db_name", normalized or "<empty>")
    return normalized


def _validate_operation(
    operation: str,
    *,
    db_name: str = "<unknown>",
) -> str:
    normalized = str(operation or "").strip().lower()
    if not _OPERATION.fullmatch(normalized):
        raise MaintenanceError(
            "invalid_maintenance_operation",
            db_name,
        )
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = None
        _atomic_replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)


def _atomic_replace(source: Path, target: Path) -> None:
    deadline = (
        time.monotonic() + WINDOWS_REPLACE_RETRY_SECONDS
        if os.name == "nt"
        else 0.0
    )
    delay = 0.01
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in {5, 32}
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _acquire_kernel_lock(
    path: Path,
    *,
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        descriptor: int | None = None
        try:
            if os.name == "nt" and path.is_symlink():
                raise MaintenanceError(
                    "db_maintenance_lock_invalid",
                    "<unknown>",
                )
            descriptor = os.open(
                path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.set_inheritable(descriptor, False)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise MaintenanceError(
                    "db_maintenance_lock_invalid",
                    "<unknown>",
                )
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            if _try_kernel_lock(descriptor):
                return descriptor
        except MaintenanceError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise MaintenanceError(
                "db_maintenance_lock_unavailable",
                "<unknown>",
            ) from exc
        if descriptor is not None:
            os.close(descriptor)
        if time.monotonic() >= deadline:
            raise MaintenanceError(
                "db_writer_already_active",
                "<unknown>",
            )
        time.sleep(0.025)


def _try_kernel_lock(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            }:
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except OSError as exc:
        if exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
        }:
            return False
        raise
    return True


def _release_kernel_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


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
