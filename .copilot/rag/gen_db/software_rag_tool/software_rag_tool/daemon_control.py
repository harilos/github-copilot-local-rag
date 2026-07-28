from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .db_maintenance import (
    acquire_maintenance_lease,
    database_integrity_ok,
    finish_maintenance,
)


_LOCAL_HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)


def release_db_before_mutation(
    db_name: str,
    *,
    lease_id: str | None = None,
    operation: str = "maintenance",
    timeout_seconds: float = 10.0,
    rag_root: Path | None = None,
) -> dict[str, Any]:
    release_lease_id = str(lease_id or uuid.uuid4().hex)
    root = (
        rag_root.resolve()
        if rag_root is not None
        else Path(__file__).resolve().parents[3]
    )
    state_path = root / "query" / "run" / "ragd.json"
    try:
        state = json.loads(
            state_path.read_text(encoding="utf-8", errors="replace")
        )
    except FileNotFoundError:
        return {"status": "no_daemon", "db": db_name}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Cannot release {db_name}: invalid daemon state: {exc}"
        ) from exc
    if not state.get("token") or not state.get("generation"):
        return {"status": "no_daemon", "db": db_name}
    if not _pid_is_alive(int(state.get("pid") or 0)):
        return {"status": "no_daemon", "db": db_name}
    transport = str(state.get("transport") or "tcp")
    if transport == "file":
        response = _file_control_request(
            state,
            {
                "op": "release_db",
                "db": db_name,
                "lease_id": release_lease_id,
                "operation": operation,
            },
            timeout_seconds=timeout_seconds,
        )
    elif transport == "unix":
        response = _unix_control_request(
            state,
            {
                "op": "release_db",
                "db": db_name,
                "lease_id": release_lease_id,
                "operation": operation,
            },
            timeout_seconds=timeout_seconds,
        )
    else:
        response = _tcp_control_request(
            state,
            "release-db",
            {
                "db": db_name,
                "lease_id": release_lease_id,
                "operation": operation,
            },
            timeout_seconds=timeout_seconds,
        )
    if not response:
        raise RuntimeError(
            f"Cannot release {db_name}: daemon did not acknowledge release"
        )
    if response.get("status") != "db_released":
        raise RuntimeError(
            f"Cannot release {db_name}: "
            f"{response.get('error') or response.get('status')}"
        )
    return response


def resume_db_after_mutation(
    db_name: str,
    *,
    lease_id: str,
    timeout_seconds: float = 10.0,
    rag_root: Path | None = None,
) -> dict[str, Any]:
    root = (
        rag_root.resolve()
        if rag_root is not None
        else Path(__file__).resolve().parents[3]
    )
    state_path = root / "query" / "run" / "ragd.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "no_daemon", "db": db_name}
    transport = str(state.get("transport") or "tcp")
    payload = {"op": "resume_db", "db": db_name, "lease_id": lease_id}
    if transport == "file":
        response = _file_control_request(
            state,
            payload,
            timeout_seconds=timeout_seconds,
        )
    elif transport == "unix":
        response = _unix_control_request(
            state,
            payload,
            timeout_seconds=timeout_seconds,
        )
    else:
        response = _tcp_control_request(
            state,
            "resume-db",
            {"db": db_name, "lease_id": lease_id},
            timeout_seconds=timeout_seconds,
        )
    if not response or response.get("status") != "db_resumed":
        raise RuntimeError(
            f"Cannot resume {db_name}: "
            f"{(response or {}).get('error') or (response or {}).get('status') or 'no response'}"
        )
    if response.get("manager_restart_required"):
        deadline = time.monotonic() + timeout_seconds
        manager_pid = int(state.get("pid") or 0)
        while _pid_is_alive(manager_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_is_alive(manager_pid):
            raise RuntimeError(
                f"Cannot resume {db_name}: daemon manager did not exit"
            )
    return response


@contextmanager
def database_mutation_guard(
    db_name: str,
    *,
    operation: str = "maintenance",
    timeout_seconds: float = 25.0,
    rag_root: Path | None = None,
    dbs_root: Path | None = None,
) -> Any:
    lease = acquire_maintenance_lease(
        db_name,
        operation=operation,
        rag_root=rag_root,
        dbs_root=dbs_root,
        lock_timeout_seconds=timeout_seconds,
        recover_failed=(
            operation in {"build", "resume"}
            or operation.startswith("rebuild_")
        ),
    )
    release: dict[str, Any] | None = None
    mutation_started = False
    try:
        release = release_db_before_mutation(
            db_name,
            lease_id=lease.lease_id,
            operation=operation,
            timeout_seconds=timeout_seconds,
            rag_root=rag_root,
        )
        mutation_started = True
        yield release
    except BaseException as exc:
        integrity_ok = (
            database_integrity_ok(
                db_name,
                rag_root=rag_root,
                dbs_root=dbs_root,
            )
            if mutation_started
            else True
        )
        if (
            release
            and release.get("status") == "db_released"
            and release.get("lease_id")
        ):
            try:
                resume_db_after_mutation(
                    db_name,
                    lease_id=str(release["lease_id"]),
                    timeout_seconds=10.0,
                    rag_root=rag_root,
                )
            except BaseException:
                # Preserve the original mutation failure. The persistent
                # state below remains the authoritative target-DB gate.
                pass
        finish_maintenance(
            lease,
            operation_succeeded=False,
            integrity_ok=integrity_ok,
            error_kind=type(exc).__name__,
        )
        raise
    else:
        integrity_ok = (
            True
            if operation == "delete"
            else database_integrity_ok(
                db_name,
                rag_root=rag_root,
                dbs_root=dbs_root,
            )
        )
        resume_error: BaseException | None = None
        if (
            release
            and release.get("status") == "db_released"
            and release.get("lease_id")
        ):
            try:
                resume_db_after_mutation(
                    db_name,
                    lease_id=str(release["lease_id"]),
                    timeout_seconds=10.0,
                    rag_root=rag_root,
                )
            except BaseException as exc:
                resume_error = exc
        finish_maintenance(
            lease,
            operation_succeeded=True,
            integrity_ok=integrity_ok,
            error_kind=(
                None
                if integrity_ok
                else "post_mutation_integrity_failed"
            ),
        )
        if resume_error is not None:
            raise resume_error
        if not integrity_ok:
            raise RuntimeError(
                f"Database integrity verification failed for {db_name}"
            )


def _tcp_control_request(
    state: dict[str, Any],
    operation: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(
            f"http://{state['host']}:{state['port']}/{operation}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-RAGD-Token": str(state["token"]),
            },
            method="POST",
        )
        with _LOCAL_HTTP_OPENER.open(
            request,
            timeout=timeout_seconds,
        ) as response:
            return json.loads(
                response.read().decode("utf-8", errors="replace")
            )
    except (OSError, TimeoutError, json.JSONDecodeError):
        return None


def _unix_control_request(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    request = {
        **payload,
        "token": str(state["token"]),
        "generation": str(state["generation"]),
    }
    try:
        data = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(state["socket_file"]))
            client.sendall(data)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return json.loads(
            b"".join(chunks).decode("utf-8", errors="replace")
        )
    except (OSError, TimeoutError, json.JSONDecodeError):
        return None


def _file_control_request(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    file_dir = Path(str(state["file_dir"]))
    requests_dir = file_dir / "requests"
    responses_dir = file_dir / "responses"
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    response_name = f"{request_id}.response.json"
    response_file = responses_dir / response_name
    request = {
        **payload,
        "token": str(state["token"]),
        "generation": str(state["generation"]),
        "response": response_name,
    }
    request_file = requests_dir / f"{request_id}.request.json"
    temporary = request_file.with_name(
        f"{request_file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(request, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(request_file)
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if response_file.exists():
                response = json.loads(
                    response_file.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                )
                response_file.unlink(missing_ok=True)
                return response
            time.sleep(0.05)
    finally:
        request_file.unlink(missing_ok=True)
        response_file.unlink(missing_ok=True)
    return None


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
