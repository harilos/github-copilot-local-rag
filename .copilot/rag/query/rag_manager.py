from __future__ import annotations

import collections
import ctypes
import json
import multiprocessing
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RAG_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _RAG_ROOT / "gen_db" / "software_rag_tool"
if str(_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOL_ROOT))

DEFAULT_MAX_QUEUED_REQUESTS = 16
DEFAULT_MAX_PENDING_PER_CLIENT = 4
DEFAULT_MAX_CLIENTS = 4
DEFAULT_MAX_REQUESTS_PER_WORKER = 500
WORKER_START_TIMEOUT_SECONDS = 5.0
WORKER_SHUTDOWN_TIMEOUT_SECONDS = 2.0


@dataclass
class _PendingRequest:
    request_id: str
    client_id: str
    db_name: str
    payload: dict[str, Any]
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    cancelled: bool = False


def worker_process_entry(
    connection: Any,
    status_connection: Any,
    *,
    rag_root: str,
    dbs_root: str,
    worker_generation: str,
) -> None:
    """Spawn-safe entry point. Heavy retrieval imports happen in the child."""
    try:
        from rag_worker import worker_main

        worker_main(
            connection,
            status_connection,
            rag_root=rag_root,
            dbs_root=dbs_root,
            worker_generation=worker_generation,
        )
    finally:
        try:
            connection.close()
        except (OSError, ValueError):
            pass
        try:
            status_connection.close()
        except (OSError, ValueError):
            pass


class PersistentWorkerManager:
    """Lightweight manager-side queue and one persistent spawned worker."""

    def __init__(
        self,
        *,
        rag_root: Path,
        dbs_root: Path,
        manager_generation: str,
        max_queued_requests: int = DEFAULT_MAX_QUEUED_REQUESTS,
        max_pending_per_client: int = DEFAULT_MAX_PENDING_PER_CLIENT,
        max_clients: int = DEFAULT_MAX_CLIENTS,
        max_requests_per_worker: int = DEFAULT_MAX_REQUESTS_PER_WORKER,
    ) -> None:
        self.rag_root = rag_root.resolve()
        self.dbs_root = dbs_root.resolve()
        self.manager_generation = manager_generation
        self.max_queued_requests = max_queued_requests
        self.max_pending_per_client = max_pending_per_client
        self.max_clients = max_clients
        self.max_requests_per_worker = max_requests_per_worker
        self.started_monotonic = time.monotonic()

        self._condition = threading.Condition()
        self._queues: dict[str, collections.deque[_PendingRequest]] = {}
        self._client_order: collections.deque[str] = collections.deque()
        self._pending_total = 0
        self._active: _PendingRequest | None = None
        self._closed = False
        self._request_sequence = 0
        self._handled_request_count = 0

        self._process: multiprocessing.Process | None = None
        self._connection: Any | None = None
        self._status_connection: Any | None = None
        self._worker_generation = ""
        self._worker_pid = 0
        self._worker_state: dict[str, Any] = {}
        self._worker_state_revision = -1
        self._job: _WindowsJobObject | None = None
        self._last_worker_start_error: str | None = None
        self._worker_lifecycle_lock = threading.RLock()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="ragd-worker-dispatch",
            daemon=True,
        )
        self._dispatcher.start()

    def submit_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.monotonic()
        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        client_id = str(payload.get("client_id") or request_id)
        db_name = str(payload.get("db") or "")
        remaining_ms = _bounded_remaining_ms(
            payload.get("remaining_deadline_ms"),
            default=14_500,
        )
        item = _PendingRequest(
            request_id=request_id,
            client_id=client_id,
            db_name=db_name,
            payload={
                **payload,
                "request_id": request_id,
                "client_id": client_id,
                "db": db_name,
            },
            deadline=now + (remaining_ms / 1000.0),
        )
        with self._condition:
            if self._closed:
                return self._manager_error(item, "daemon_draining")
            client_queue = self._queues.get(client_id)
            client_pending = len(client_queue or ())
            if self._active and self._active.client_id == client_id:
                client_pending += 1
            active_clients = set(self._queues)
            if self._active:
                active_clients.add(self._active.client_id)
            if (
                client_id not in active_clients
                and len(active_clients) >= self.max_clients
            ):
                return self._manager_error(item, "daemon_overloaded")
            if (
                self._pending_total >= self.max_queued_requests
                or client_pending >= self.max_pending_per_client
            ):
                return self._manager_error(item, "daemon_overloaded")
            if client_queue is None:
                client_queue = collections.deque()
                self._queues[client_id] = client_queue
                self._client_order.append(client_id)
            client_queue.append(item)
            self._pending_total += 1
            self._condition.notify_all()

        remaining = max(0.0, item.deadline - time.monotonic())
        if not item.event.wait(remaining):
            item.cancelled = True
            return self._manager_error(item, "queue_deadline_expired")
        return item.result or self._manager_error(
            item,
            "worker_response_missing",
        )

    def cancel_request(
        self,
        request_id: str,
        *,
        client_id: str = "",
    ) -> dict[str, Any]:
        cancelled_item: _PendingRequest | None = None
        state = "not_found"
        with self._condition:
            for queued_client, requests in list(self._queues.items()):
                kept = collections.deque()
                while requests:
                    item = requests.popleft()
                    matches = item.request_id == request_id and (
                        not client_id or item.client_id == client_id
                    )
                    if matches and cancelled_item is None:
                        item.cancelled = True
                        cancelled_item = item
                        self._pending_total -= 1
                        state = "queued_cancelled"
                    else:
                        kept.append(item)
                if kept:
                    self._queues[queued_client] = kept
                else:
                    self._queues.pop(queued_client, None)
                    self._remove_client_order(queued_client)
            active = self._active
            if (
                cancelled_item is None
                and active is not None
                and active.request_id == request_id
                and (not client_id or active.client_id == client_id)
            ):
                active.cancelled = True
                cancelled_item = active
                state = "active_response_discarded"
            self._condition.notify_all()
        if cancelled_item is not None:
            cancelled_item.result = self._manager_error(
                cancelled_item,
                "client_cancelled",
            )
            cancelled_item.event.set()
        return {
            "schema": "local-rag.ragd.cancel.v1",
            "status": state,
            "request_id": request_id,
            "client_id": client_id,
            "manager_pid": os.getpid(),
            "manager_generation": self.manager_generation,
        }

    def _accept_worker_state_locked(
        self,
        worker_state: dict[str, Any],
        *,
        worker_generation: str,
    ) -> bool:
        if (
            not worker_state
            or worker_generation != self._worker_generation
            or str(worker_state.get("worker_generation") or "")
            != self._worker_generation
        ):
            return False
        revision = int(worker_state.get("state_revision") or 0)
        if revision <= getattr(self, "_worker_state_revision", -1):
            return False
        self._worker_state = dict(worker_state)
        self._worker_state_revision = revision
        return True

    def _drain_worker_status_locked(self) -> None:
        status_connection = getattr(self, "_status_connection", None)
        if status_connection is None:
            return
        try:
            while status_connection.poll():
                message = status_connection.recv()
                if (
                    isinstance(message, dict)
                    and message.get("op") == "worker_status"
                ):
                    worker_state = dict(
                        message.get("worker_state") or {}
                    )
                    if int(message.get("state_revision") or 0) != int(
                        worker_state.get("state_revision") or 0
                    ):
                        continue
                    self._accept_worker_state_locked(
                        worker_state,
                        worker_generation=str(
                            message.get("worker_generation") or ""
                        ),
                    )
        except (EOFError, OSError, ValueError):
            try:
                status_connection.close()
            except (OSError, ValueError):
                pass
            if status_connection is self._status_connection:
                self._status_connection = None

    def health(self) -> dict[str, Any]:
        # Worker retirement can spend several seconds in bounded
        # join/terminate/kill calls while holding the lifecycle lock. Health
        # is a manager control operation and must stay responsive during that
        # transition; otherwise a client can misclassify the live manager as
        # dead and start a competing generation. Use a short non-blocking
        # snapshot and report STARTING until the transition completes.
        lifecycle_snapshot_acquired = self._worker_lifecycle_lock.acquire(
            timeout=0.01
        )
        try:
            if lifecycle_snapshot_acquired:
                self._drain_worker_status_locked()
            process = self._process
            connection = self._connection
            state = dict(self._worker_state)
            worker_pid = self._worker_pid
            worker_generation = self._worker_generation
            worker_job_object_active = self._job is not None
            if lifecycle_snapshot_acquired:
                try:
                    worker_alive = bool(
                        process is not None and process.is_alive()
                    )
                except (AssertionError, ValueError):
                    worker_alive = False
            else:
                worker_alive = False
        finally:
            if lifecycle_snapshot_acquired:
                self._worker_lifecycle_lock.release()
        connection_usable = bool(
            connection is not None
            and not bool(getattr(connection, "closed", False))
        )
        worker_usable = bool(
            worker_alive
            and connection_usable
            and (os.name != "nt" or worker_job_object_active)
        )
        with self._condition:
            active = self._active is not None
            queue_depth = self._pending_total
            closed = self._closed
        lifecycle = (
            "DRAINING"
            if closed
            else "BUSY"
            if active or queue_depth
            else "READY"
            if worker_usable
            else "STARTING"
        )
        manager_metrics = _current_process_metrics()
        return {
            "manager_pid": os.getpid(),
            "manager_generation": self.manager_generation,
            "worker_pid": worker_pid or None,
            "worker_generation": worker_generation or None,
            "worker_alive": worker_alive,
            "worker_usable": worker_usable,
            "worker_job_object_active": worker_job_object_active,
            "last_worker_start_error": self._last_worker_start_error,
            "worker_transitioning": not lifecycle_snapshot_acquired,
            "active_requests": 1 if active else 0,
            "queue_depth": queue_depth,
            "lifecycle_state": lifecycle,
            "ready": lifecycle == "READY",
            "handled_request_count": self._handled_request_count,
            "model_load_count": int(state.get("model_load_count") or 0),
            "dense_warmup_state": str(
                state.get("dense_warmup_state") or "not_started"
            ),
            "open_database_count": int(
                state.get("open_database_count") or 0
            ),
            "manager_rss_bytes": manager_metrics["rss_bytes"],
            "worker_rss_bytes": state.get("rss_bytes"),
            "manager_handle_count": manager_metrics["handle_count"],
            "worker_handle_count": state.get("handle_count"),
            "manager_thread_count": manager_metrics["thread_count"],
            "worker_thread_count": state.get("thread_count"),
        }

    def shutdown(self, *, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        cancelled: list[_PendingRequest] = []
        with self._condition:
            self._closed = True
            for requests in self._queues.values():
                cancelled.extend(requests)
            self._queues.clear()
            self._client_order.clear()
            self._pending_total = 0
            while self._active is not None and time.monotonic() < deadline:
                self._condition.wait(
                    timeout=min(0.1, deadline - time.monotonic())
                )
            self._condition.notify_all()
        for item in cancelled:
            item.result = self._manager_error(item, "daemon_draining")
            item.event.set()
        worker_reaped = self._stop_worker(graceful=self._active is None)
        self._dispatcher.join(
            timeout=max(0.0, deadline - time.monotonic())
        )
        return bool(worker_reaped and not self._dispatcher.is_alive())

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                item = self._next_request_locked()
                if item is None:
                    if self._closed:
                        return
                    continue
                self._active = item
            try:
                if item.cancelled or time.monotonic() >= item.deadline:
                    result = self._manager_error(
                        item,
                        "queue_deadline_expired",
                    )
                else:
                    result = self._execute_item(item)
                if not item.cancelled:
                    item.result = result
                    item.event.set()
            finally:
                with self._condition:
                    self._active = None
                    self._handled_request_count += 1
                    self._condition.notify_all()
            if (
                self._handled_request_count
                and self._handled_request_count
                % self.max_requests_per_worker
                == 0
            ):
                with self._condition:
                    queue_empty = self._pending_total == 0
                if queue_empty:
                    self._stop_worker(graceful=True)

    def _next_request_locked(self) -> _PendingRequest | None:
        while True:
            if self._closed and self._pending_total == 0:
                return None
            if self._pending_total == 0:
                self._condition.wait(timeout=0.2)
                continue
            while self._client_order:
                client_id = self._client_order.popleft()
                requests = self._queues.get(client_id)
                if not requests:
                    self._queues.pop(client_id, None)
                    continue
                item = requests.popleft()
                self._pending_total -= 1
                if requests:
                    self._client_order.append(client_id)
                else:
                    self._queues.pop(client_id, None)
                return item

    def _execute_item(self, item: _PendingRequest) -> dict[str, Any]:
        remaining = item.deadline - time.monotonic()
        if remaining <= 0:
            return self._manager_error(item, "queue_deadline_expired")
        if not self._ensure_worker(
            timeout_seconds=min(WORKER_START_TIMEOUT_SECONDS, remaining)
        ):
            return self._manager_error(item, "worker_start_failed")
        with self._worker_lifecycle_lock:
            connection = self._connection
        if connection is None:
            return self._manager_error(item, "worker_unavailable")
        remaining_ms = max(
            0,
            int((item.deadline - time.monotonic()) * 1000),
        )
        worker_payload = {
            **item.payload,
            "remaining_deadline_ms": remaining_ms,
        }
        message = {
            "op": "search",
            "request_id": item.request_id,
            "client_id": item.client_id,
            "db": item.db_name,
            "remaining_deadline_ms": remaining_ms,
            "deadline_monotonic": item.deadline,
            "payload": worker_payload,
        }
        try:
            connection.send(message)
            while time.monotonic() < item.deadline:
                if connection.poll(
                    min(0.1, item.deadline - time.monotonic())
                ):
                    response = connection.recv()
                    if (
                        response.get("request_id") != item.request_id
                        or response.get("client_id") != item.client_id
                        or response.get("db") != item.db_name
                    ):
                        self._stop_worker(graceful=False)
                        return self._manager_error(
                            item,
                            "worker_response_mismatch",
                        )
                    with self._worker_lifecycle_lock:
                        self._drain_worker_status_locked()
                        response_state = dict(
                            response.get("worker_state") or {}
                        )
                        self._accept_worker_state_locked(
                            response_state,
                            worker_generation=str(
                                response_state.get(
                                    "worker_generation"
                                )
                                or ""
                            ),
                        )
                    result = dict(response.get("result") or {})
                    result["daemon_state"] = self._request_metadata(
                        item,
                        queue_depth=self._pending_total,
                    )
                    return result
        except (EOFError, BrokenPipeError, OSError, ValueError):
            self._stop_worker(graceful=False)
            return self._manager_error(item, "worker_crashed")
        self._stop_worker(graceful=False)
        return self._manager_error(item, "worker_execution_timeout")

    def _ensure_worker(self, *, timeout_seconds: float) -> bool:
        with self._worker_lifecycle_lock:
            return self._ensure_worker_locked(timeout_seconds=timeout_seconds)

    def _ensure_worker_locked(self, *, timeout_seconds: float) -> bool:
        process = self._process
        connection = self._connection
        worker_usable = bool(
            process is not None
            and process.is_alive()
            and connection is not None
            and not bool(getattr(connection, "closed", False))
            and (os.name != "nt" or self._job is not None)
        )
        if worker_usable:
            return True
        if not self._stop_worker_locked(graceful=False):
            self._last_worker_start_error = "previous_worker_not_reaped"
            return False
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        status_receive, status_send = context.Pipe(duplex=False)
        generation = uuid.uuid4().hex
        process = context.Process(
            target=worker_process_entry,
            kwargs={
                "connection": child_connection,
                "status_connection": status_send,
                "rag_root": str(self.rag_root),
                "dbs_root": str(self.dbs_root),
                "worker_generation": generation,
            },
            name="local-rag-search-worker",
            daemon=False,
        )
        try:
            process.start()
        except BaseException:
            try:
                if process.pid and process.is_alive():
                    process.terminate()
                    process.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
                    if process.is_alive():
                        try:
                            process.kill()
                        except AttributeError:
                            process.terminate()
                        process.join(
                            timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS
                        )
            except (AssertionError, OSError, ValueError):
                pass
            for endpoint in (
                parent_connection,
                child_connection,
                status_receive,
                status_send,
            ):
                try:
                    endpoint.close()
                except (OSError, ValueError):
                    pass
            try:
                process.close()
            except (OSError, ValueError):
                pass
            raise
        child_connection.close()
        status_send.close()
        self._process = process
        self._connection = parent_connection
        self._status_connection = status_receive
        self._worker_generation = generation
        self._worker_state_revision = -1
        self._worker_pid = int(process.pid or 0)
        self._job = _WindowsJobObject.assign(process)
        if os.name == "nt" and self._job is None:
            self._last_worker_start_error = (
                "job_assignment_failed:"
                f"{_WindowsJobObject.last_error_code}"
            )
            self._stop_worker_locked(graceful=False)
            return False
        self._last_worker_start_error = None
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        try:
            while time.monotonic() < deadline:
                if not process.is_alive():
                    self._last_worker_start_error = (
                        f"worker_exited:{process.exitcode}"
                    )
                    break
                if parent_connection.poll(
                    min(0.1, deadline - time.monotonic())
                ):
                    ready = parent_connection.recv()
                    if (
                        ready.get("op") == "ready"
                        and ready.get("worker_generation") == generation
                    ):
                        ready_state = dict(
                            ready.get("worker_state") or {}
                        )
                        self._accept_worker_state_locked(
                            ready_state,
                            worker_generation=generation,
                        )
                        self._worker_pid = int(
                            self._worker_state.get("worker_pid")
                            or process.pid
                            or 0
                        )
                        self._last_worker_start_error = None
                        return True
                    self._last_worker_start_error = "invalid_ready_response"
        except (EOFError, OSError, ValueError) as exc:
            self._last_worker_start_error = (
                f"worker_ready_error:{type(exc).__name__}"
            )
        if self._last_worker_start_error is None:
            self._last_worker_start_error = "worker_ready_timeout"
        self._stop_worker_locked(graceful=False)
        return False

    def _stop_worker(self, *, graceful: bool) -> bool:
        with self._worker_lifecycle_lock:
            return self._stop_worker_locked(graceful=graceful)

    def _stop_worker_locked(self, *, graceful: bool) -> bool:
        process = self._process
        connection = self._connection
        status_connection = self._status_connection
        job = self._job
        if process is None:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, ValueError):
                    pass
            if status_connection is not None:
                try:
                    status_connection.close()
                except (OSError, ValueError):
                    pass
            if job is not None:
                job.close()
            self._clear_worker_references()
            return True
        if graceful and process.is_alive() and connection is not None:
            try:
                connection.send({"op": "shutdown"})
            except (BrokenPipeError, EOFError, OSError, ValueError):
                pass
        process.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        if process.is_alive():
            try:
                process.kill()
            except AttributeError:
                process.terminate()
            process.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        if process.is_alive() and job is not None:
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates the worker and any
            # native descendants if normal Process termination was incomplete.
            job.close()
            job = None
            process.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        worker_reaped = not process.is_alive()
        if connection is not None:
            try:
                connection.close()
            except (OSError, ValueError):
                pass
        if status_connection is not None:
            try:
                status_connection.close()
            except (OSError, ValueError):
                pass
        if job is not None:
            job.close()
        if worker_reaped:
            try:
                process.close()
            except (ValueError, OSError):
                pass
            self._clear_worker_references()
        return worker_reaped

    def _clear_worker_references(self) -> None:
        self._process = None
        self._connection = None
        self._status_connection = None
        self._job = None
        self._worker_pid = 0
        self._worker_generation = ""
        self._worker_state = {}
        self._worker_state_revision = -1

    def _request_metadata(
        self,
        item: _PendingRequest,
        *,
        queue_depth: int,
    ) -> dict[str, Any]:
        self._request_sequence += 1
        with self._worker_lifecycle_lock:
            self._drain_worker_status_locked()
            worker_pid = self._worker_pid
            worker_generation = self._worker_generation
            state = dict(self._worker_state)
        return {
            "manager_pid": os.getpid(),
            "worker_pid": worker_pid,
            "manager_generation": self.manager_generation,
            "worker_generation": worker_generation,
            "request_id": item.request_id,
            "client_id": item.client_id,
            "db": item.db_name,
            "request_sequence": self._request_sequence,
            "queue_depth": queue_depth,
            "model_load_count": int(
                state.get("model_load_count") or 0
            ),
            "dense_warmup_state": str(
                state.get("dense_warmup_state") or "not_started"
            ),
            "open_database_count": int(
                state.get("open_database_count") or 0
            ),
            "handled_request_count": int(
                state.get("handled_request_count") or 0
            ),
            "worker_rss_bytes": state.get("rss_bytes"),
            "worker_handle_count": state.get(
                "handle_count"
            ),
            "worker_thread_count": state.get(
                "thread_count"
            ),
        }

    def _manager_error(
        self,
        item: _PendingRequest,
        error_kind: str,
    ) -> dict[str, Any]:
        return {
            "schema": "local-rag.search.v1",
            "status": "error",
            "error": error_kind,
            "error_kind": error_kind,
            "db": item.db_name,
            "selected_db": item.db_name,
            "query": str(item.payload.get("question") or ""),
            "evidence": [],
            "background_context": [],
            "related_context": [],
            "document_results": [],
            "warnings": [],
            "daemon_state": self._request_metadata(
                item,
                queue_depth=self._pending_total,
            ),
        }

    def _remove_client_order(self, client_id: str) -> None:
        self._client_order = collections.deque(
            value
            for value in self._client_order
            if value != client_id
        )


class _WindowsJobObject:
    last_error_code = 0

    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def assign(
        cls,
        process: multiprocessing.Process,
    ) -> _WindowsJobObject | None:
        if os.name != "nt":
            return None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_job = kernel32.CreateJobObjectW
            create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            create_job.restype = ctypes.c_void_p
            handle = create_job(None, None)
            if not handle:
                return None

            class _BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class _ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimitInformation),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = 0x00002000
            set_info = kernel32.SetInformationJobObject
            set_info.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            set_info.restype = ctypes.c_int
            if not set_info(
                handle,
                9,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                cls.last_error_code = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                return None
            assign = kernel32.AssignProcessToJobObject
            assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            assign.restype = ctypes.c_int
            # multiprocessing exposes the Windows process HANDLE as the
            # sentinel. Process has no public/private ``_handle`` attribute.
            process_handle = int(process.sentinel)
            if not process_handle or not assign(handle, process_handle):
                cls.last_error_code = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                return None
            cls.last_error_code = 0
            return cls(int(handle))
        except (AttributeError, OSError, TypeError, ValueError):
            cls.last_error_code = ctypes.get_last_error()
            return None

    def close(self) -> None:
        if not self.handle:
            return
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                ctypes.c_void_p(self.handle)
            )
        finally:
            self.handle = 0


def _bounded_remaining_ms(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 300_000))


def _current_process_metrics() -> dict[str, int | None]:
    metrics: dict[str, int | None] = {
        "rss_bytes": None,
        "handle_count": None,
        "thread_count": threading.active_count(),
    }
    if os.name != "nt":
        try:
            import resource

            value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            metrics["rss_bytes"] = value * (
                1 if sys.platform == "darwin" else 1024
            )
        except (ImportError, OSError, ValueError):
            pass
        return metrics
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = wintypes.HANDLE
        current = get_current_process()
        count = wintypes.DWORD()
        get_handle_count = kernel32.GetProcessHandleCount
        get_handle_count.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_handle_count.restype = wintypes.BOOL
        if get_handle_count(current, ctypes.byref(count)):
            metrics["handle_count"] = int(count.value)

        class _MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        get_memory = psapi.GetProcessMemoryInfo
        get_memory.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_MemoryCounters),
            wintypes.DWORD,
        ]
        get_memory.restype = wintypes.BOOL
        if get_memory(
            current,
            ctypes.byref(counters),
            counters.cb,
        ):
            metrics["rss_bytes"] = int(counters.WorkingSetSize)

        class _ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        snapshot_fn = kernel32.CreateToolhelp32Snapshot
        snapshot_fn.argtypes = [wintypes.DWORD, wintypes.DWORD]
        snapshot_fn.restype = wintypes.HANDLE
        first_thread = kernel32.Thread32First
        first_thread.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry),
        ]
        first_thread.restype = wintypes.BOOL
        next_thread = kernel32.Thread32Next
        next_thread.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry),
        ]
        next_thread.restype = wintypes.BOOL
        snapshot = snapshot_fn(0x00000004, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot and int(snapshot) != invalid_handle:
            try:
                entry = _ThreadEntry()
                entry.dwSize = ctypes.sizeof(entry)
                process_id = os.getpid()
                thread_count = 0
                has_entry = bool(first_thread(snapshot, ctypes.byref(entry)))
                while has_entry:
                    if int(entry.th32OwnerProcessID) == process_id:
                        thread_count += 1
                    has_entry = bool(
                        next_thread(snapshot, ctypes.byref(entry))
                    )
                metrics["thread_count"] = thread_count
            finally:
                kernel32.CloseHandle(snapshot)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return metrics
