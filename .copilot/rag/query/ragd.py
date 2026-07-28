from __future__ import annotations

import argparse
import hashlib
import json
import os
import socketserver
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
DBS_ROOT = Path(os.getenv("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))).expanduser().resolve()
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.config import DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS
from rag_manager import PersistentWorkerManager

_UnixStreamServerBase = getattr(socketserver, "ThreadingUnixStreamServer", socketserver.ThreadingTCPServer)


class RagDaemonServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], token: str, idle_timeout: int, state_file: Path, generation: str) -> None:
        super().__init__(server_address, RagDaemonHandler)
        self.token = token
        self.generation = generation
        self.idle_timeout = idle_timeout
        self.state_file = state_file
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_monotonic = time.monotonic()
        self.code_fingerprint = runtime_code_fingerprint()
        self.last_used_at = time.monotonic()
        self.active_requests = 0
        self.request_sequence = 0
        self.runtime_ready = False
        self.dense_ready = False
        self.state_lock = threading.Lock()
        self.shutdown_started = False
        self.worker_manager = PersistentWorkerManager(
            rag_root=RAG_ROOT,
            dbs_root=DBS_ROOT,
            manager_generation=generation,
        )


class RagUnixDaemonServer(_UnixStreamServerBase):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_file: str, token: str, idle_timeout: int, state_file: Path, generation: str) -> None:
        self.socket_file = socket_file
        try:
            Path(socket_file).unlink()
        except FileNotFoundError:
            pass
        super().__init__(socket_file, RagUnixDaemonHandler)
        self.token = token
        self.generation = generation
        self.idle_timeout = idle_timeout
        self.state_file = state_file
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_monotonic = time.monotonic()
        self.code_fingerprint = runtime_code_fingerprint()
        self.last_used_at = time.monotonic()
        self.active_requests = 0
        self.request_sequence = 0
        self.runtime_ready = False
        self.dense_ready = False
        self.state_lock = threading.Lock()
        self.shutdown_started = False
        self.worker_manager = PersistentWorkerManager(
            rag_root=RAG_ROOT,
            dbs_root=DBS_ROOT,
            manager_generation=generation,
        )

    def server_close(self) -> None:
        super().server_close()
        try:
            Path(self.socket_file).unlink()
        except FileNotFoundError:
            pass


def _server_health_payload(server: RagDaemonServer | RagUnixDaemonServer) -> dict[str, Any]:
    worker = server.worker_manager.health()
    lifecycle_state = str(worker["lifecycle_state"])
    return {
        "schema": "local-rag.ragd.health.v1",
        "status": "ok",
        "pid": os.getpid(),
        "generation": server.generation,
        "started_at": server.started_at,
        "uptime_seconds": round(time.monotonic() - server.started_monotonic, 6),
        "manager_pid": os.getpid(),
        "manager_generation": server.generation,
        "worker_pid": worker.get("worker_pid"),
        "worker_generation": worker.get("worker_generation"),
        "active_requests": worker["active_requests"],
        "queue_depth": worker["queue_depth"],
        "request_sequence": worker["handled_request_count"],
        "ready": lifecycle_state == "READY",
        "dense_ready": worker["model_load_count"] > 0,
        "model_load_count": worker["model_load_count"],
        "dense_warmup_state": worker.get("dense_warmup_state"),
        "open_database_count": worker["open_database_count"],
        "worker_job_object_active": worker[
            "worker_job_object_active"
        ],
        "worker_usable": worker.get("worker_usable"),
        "worker_transitioning": worker.get("worker_transitioning"),
        "last_worker_start_error": worker.get(
            "last_worker_start_error"
        ),
        "manager_rss_bytes": worker["manager_rss_bytes"],
        "worker_rss_bytes": worker["worker_rss_bytes"],
        "manager_handle_count": worker["manager_handle_count"],
        "worker_handle_count": worker["worker_handle_count"],
        "manager_thread_count": worker["manager_thread_count"],
        "worker_thread_count": worker["worker_thread_count"],
        "handled_request_count": worker["handled_request_count"],
        "lifecycle_state": lifecycle_state,
        "code_fingerprint": server.code_fingerprint,
        "idle_timeout_seconds": server.idle_timeout,
    }


class RagDaemonHandler(BaseHTTPRequestHandler):
    server: RagDaemonServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json({"status": "not_found"}, status=404)
            return
        if not self._authorized():
            self._send_json({"status": "forbidden"}, status=403)
            return
        self._send_json(_server_health_payload(self.server))

    def do_POST(self) -> None:
        if self.path == "/shutdown":
            if not self._authorized():
                self._send_json({"status": "forbidden"}, status=403)
                return
            self._send_json({"schema": "local-rag.ragd.shutdown.v1", "status": "ok"})
            _request_graceful_shutdown(self.server)
            return
        if self.path == "/release-db":
            if not self._authorized():
                self._send_json({"status": "forbidden"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(
                    self.rfile.read(length).decode("utf-8")
                )
                db_name = str(body["db"])
                lease_id = str(body.get("lease_id") or "")
                operation = str(body.get("operation") or "maintenance")
            except Exception as exc:
                self._send_json(
                    {
                        "status": "error",
                        "error": f"invalid release request: {exc}",
                    },
                    status=400,
                )
                return
            self._send_json(
                self.server.worker_manager.release_db(
                    db_name,
                    lease_id=lease_id or None,
                    operation=operation,
                )
            )
            return
        if self.path == "/resume-db":
            if not self._authorized():
                self._send_json({"status": "forbidden"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                db_name = str(body["db"])
                lease_id = str(body["lease_id"])
            except Exception as exc:
                self._send_json(
                    {"status": "error", "error": f"invalid resume request: {exc}"},
                    status=400,
                )
                return
            result = self.server.worker_manager.resume_db(
                db_name,
                lease_id=lease_id,
            )
            self._send_json(result)
            if result.get("manager_restart_required"):
                _request_graceful_shutdown(self.server)
            return
        if self.path == "/cancel":
            if not self._authorized():
                self._send_json({"status": "forbidden"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                request_id = str(body["request_id"])
                client_id = str(body.get("client_id") or "")
            except Exception as exc:
                self._send_json(
                    {"status": "error", "error": f"invalid cancel request: {exc}"},
                    status=400,
                )
                return
            self._send_json(
                self.server.worker_manager.cancel_request(
                    request_id,
                    client_id=client_id,
                )
            )
            return
        if self.path != "/search":
            self._send_json({"status": "not_found"}, status=404)
            return
        if not self._authorized():
            self._send_json({"status": "forbidden"}, status=403)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length)
            request = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send_json({"schema": "local-rag.search.v1", "status": "error", "error": f"invalid request: {exc}"})
            return

        payload = self.server.worker_manager.submit_search(request)
        self.server.last_used_at = time.monotonic()
        self._send_json(payload)

    def _authorized(self) -> bool:
        return self.headers.get("X-RAGD-Token") == self.server.token

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class RagUnixDaemonHandler(socketserver.StreamRequestHandler):
    server: RagUnixDaemonServer

    def handle(self) -> None:
        try:
            body = self.rfile.readline(10 * 1024 * 1024)
            request = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send_json({"schema": "local-rag.ragd.v1", "status": "error", "error": f"invalid request: {exc}"})
            return
        if request.get("token") != self.server.token:
            self._send_json({"status": "forbidden"})
            return
        if request.get("generation") != self.server.generation:
            self._send_json({"status": "forbidden"})
            return
        op = request.get("op")
        if op == "health":
            self._send_json(_server_health_payload(self.server))
            return
        if op == "shutdown":
            self._send_json({"schema": "local-rag.ragd.shutdown.v1", "status": "ok"})
            _request_graceful_shutdown(self.server)
            return
        if op == "release_db":
            result = self.server.worker_manager.release_db(
                str(request.get("db") or ""),
                lease_id=str(request.get("lease_id") or "") or None,
                operation=str(request.get("operation") or "maintenance"),
            )
            self._send_json(result)
            return
        if op == "resume_db":
            result = self.server.worker_manager.resume_db(
                str(request.get("db") or ""),
                lease_id=str(request.get("lease_id") or ""),
            )
            self._send_json(result)
            if result.get("manager_restart_required"):
                _request_graceful_shutdown(self.server)
            return
        if op == "cancel":
            result = self.server.worker_manager.cancel_request(
                str(request.get("request_id") or ""),
                client_id=str(request.get("client_id") or ""),
            )
            self._send_json(result)
            return
        if op != "search":
            self._send_json({"status": "not_found"})
            return

        payload = request.get("payload") or {}
        result = self.server.worker_manager.submit_search(payload)
        self.server.last_used_at = time.monotonic()
        self._send_json(result)

    def _send_json(self, payload: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--socket-file")
    parser.add_argument("--file-dir")
    parser.add_argument("--token", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--idle-timeout", type=int, default=DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if (
        not args.socket_file
        and not args.file_dir
        and args.port is None
    ):
        parser.error("--port, --socket-file, or --file-dir is required")

    os.environ.setdefault("RAG_DBS_ROOT", str(DBS_ROOT))
    state_file = Path(args.state_file).expanduser().resolve()
    _secure_runtime_directory(state_file.parent)
    if args.file_dir:
        _run_file_daemon(
            file_dir=Path(args.file_dir).expanduser().resolve(),
            token=args.token,
            generation=args.token,
            idle_timeout=args.idle_timeout,
            state_file=state_file,
        )
        return
    if args.socket_file:
        server = RagUnixDaemonServer(args.socket_file, args.token, args.idle_timeout, state_file, generation=args.token)
    else:
        server = RagDaemonServer((args.host, int(args.port)), args.token, args.idle_timeout, state_file, generation=args.token)
    _write_state(server)
    monitor = threading.Thread(target=_idle_monitor, args=(server,), daemon=True)
    monitor.start()
    try:
        server.serve_forever(poll_interval=1.0)
    finally:
        server.worker_manager.shutdown()
        server.server_close()
        _unlink_state(state_file)


def _run_file_daemon(*, file_dir: Path, token: str, generation: str, idle_timeout: int, state_file: Path) -> None:
    _secure_runtime_directory(file_dir)
    requests_dir = file_dir / "requests"
    responses_dir = file_dir / "responses"
    _secure_runtime_directory(requests_dir)
    _secure_runtime_directory(responses_dir)
    heartbeat_file = file_dir / "heartbeat.json"
    code_fingerprint = runtime_code_fingerprint()
    started_monotonic = time.monotonic()
    state = {
        "schema": "local-rag.ragd.v2",
        "transport": "file",
        "pid": os.getpid(),
        "generation": generation,
        "token": token,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_fingerprint": code_fingerprint,
        "lifecycle_state": "STARTING",
        "idle_timeout_seconds": idle_timeout,
        "file_dir": str(file_dir),
        "heartbeat_file": str(heartbeat_file),
    }
    _write_json_atomic(state_file, state)
    worker_manager = PersistentWorkerManager(
        rag_root=RAG_ROOT,
        dbs_root=DBS_ROOT,
        manager_generation=generation,
    )
    last_used_at = time.monotonic()
    last_cleanup_at = 0.0
    active_requests = 0
    request_sequence = 0
    runtime_ready = False
    dense_ready = False

    try:
        while True:
            if _state_is_superseded(state_file, generation=generation, pid=os.getpid()):
                return
            if time.monotonic() - last_cleanup_at >= 60:
                _cleanup_stale_transport_files(responses_dir, max_age_seconds=600)
                last_cleanup_at = time.monotonic()
            _write_json_atomic(
                heartbeat_file,
                {
                    "schema": "local-rag.ragd.heartbeat.v1",
                    "pid": os.getpid(),
                    "generation": generation,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "active_requests": active_requests,
                    "request_sequence": request_sequence,
                    "dense_ready": dense_ready,
                    "uptime_seconds": round(time.monotonic() - started_monotonic, 6),
                    "code_fingerprint": code_fingerprint,
                },
            )
            request_files = sorted(requests_dir.glob("*.request.json"))
            if not request_files:
                if active_requests == 0 and time.monotonic() - last_used_at >= idle_timeout:
                    return
                time.sleep(0.2)
                continue

            request_file = request_files[0]
            processing_file = request_file.with_suffix(".processing")
            try:
                request_file.replace(processing_file)
            except FileNotFoundError:
                continue
            active_requests += 1
            request_sequence += 1
            request_started = time.monotonic()
            response_file: Path | None = None
            try:
                request = json.loads(processing_file.read_text(encoding="utf-8", errors="replace"))
                response_name = str(request.get("response") or f"{uuid.uuid4().hex}.response.json")
                response_file = responses_dir / Path(response_name).name
                if request.get("token") != token:
                    result = {"status": "forbidden"}
                elif request.get("generation") != generation:
                    result = {"status": "forbidden"}
                elif request.get("op") == "health":
                    worker_health = worker_manager.health()
                    lifecycle_state = worker_health["lifecycle_state"]
                    result = {
                        "schema": "local-rag.ragd.health.v1",
                        "status": "ok",
                        "pid": os.getpid(),
                        "generation": generation,
                        "started_at": state["started_at"],
                        "uptime_seconds": round(time.monotonic() - started_monotonic, 6),
                        "manager_pid": os.getpid(),
                        "manager_generation": generation,
                        "worker_pid": worker_health.get("worker_pid"),
                        "worker_generation": worker_health.get(
                            "worker_generation"
                        ),
                        "active_requests": worker_health["active_requests"],
                        "queue_depth": worker_health["queue_depth"],
                        "request_sequence": worker_health[
                            "handled_request_count"
                        ],
                        "ready": lifecycle_state == "READY",
                        "dense_ready": (
                            worker_health["model_load_count"] > 0
                        ),
                        "model_load_count": worker_health[
                            "model_load_count"
                        ],
                        "open_database_count": worker_health[
                            "open_database_count"
                        ],
                        "lifecycle_state": lifecycle_state,
                        "code_fingerprint": code_fingerprint,
                        "idle_timeout_seconds": idle_timeout,
                    }
                elif request.get("op") == "search":
                    payload = request.get("payload") or {}
                    result = worker_manager.submit_search(payload)
                    runtime_ready = result.get("status") != "error"
                    dense_ready = (
                        worker_manager.health()["model_load_count"] > 0
                    )
                elif request.get("op") == "release_db":
                    result = worker_manager.release_db(
                        str(request.get("db") or ""),
                        lease_id=(
                            str(request.get("lease_id") or "") or None
                        ),
                        operation=str(
                            request.get("operation") or "maintenance"
                        ),
                    )
                elif request.get("op") == "resume_db":
                    result = worker_manager.resume_db(
                        str(request.get("db") or ""),
                        lease_id=str(request.get("lease_id") or ""),
                    )
                elif request.get("op") == "cancel":
                    result = worker_manager.cancel_request(
                        str(request.get("request_id") or ""),
                        client_id=str(request.get("client_id") or ""),
                    )
                elif request.get("op") == "shutdown":
                    result = {"schema": "local-rag.ragd.shutdown.v1", "status": "ok"}
                    _write_json_atomic(response_file, result)
                    return
                else:
                    result = {"status": "not_found"}
                _write_json_atomic(response_file, result)
                if result.get("manager_restart_required"):
                    return
            except Exception as exc:
                fallback = response_file or (responses_dir / f"{uuid.uuid4().hex}.response.json")
                _write_json_atomic(
                    fallback,
                    {"schema": "local-rag.ragd.v1", "status": "error", "error": f"{type(exc).__name__}: {exc}"},
                )
            finally:
                active_requests -= 1
                last_used_at = time.monotonic()
                try:
                    processing_file.unlink()
                except FileNotFoundError:
                    pass
    finally:
        worker_manager.shutdown()
        _unlink_state(state_file)


def _cleanup_stale_transport_files(directory: Path, *, max_age_seconds: float) -> None:
    cutoff = time.time() - max_age_seconds
    try:
        paths = list(directory.glob("*.response.json"))
    except OSError:
        return
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except (FileNotFoundError, OSError):
            pass


def runtime_code_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in runtime_fingerprint_paths():
        digest.update(str(path.relative_to(RAG_ROOT)).encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def runtime_fingerprint_paths() -> list[Path]:
    package_root = TOOL_ROOT / "software_rag_tool"
    paths = [
        Path(__file__).resolve(),
        Path(__file__).resolve().parent / "rag_manager.py",
        Path(__file__).resolve().parent / "rag_worker.py",
        TOOL_ROOT / "pyproject.toml",
        TOOL_ROOT / "requirements.txt",
        *package_root.rglob("*.py"),
    ]
    return sorted(set(paths), key=lambda path: str(path.relative_to(RAG_ROOT)))


def daemon_request_metadata(
    *,
    pid: int,
    generation: str,
    started_at: str,
    uptime_seconds: float,
    code_fingerprint: str,
    request_id: str,
    request_sequence: int,
    queue_depth: int,
    request_seconds: float,
    dense_ready: bool,
) -> dict[str, Any]:
    return {
        "pid": pid,
        "generation": generation,
        "started_at": started_at,
        "uptime_seconds": round(uptime_seconds, 6),
        "code_fingerprint": code_fingerprint,
        "request_id": request_id,
        "request_sequence": request_sequence,
        "queue_depth": queue_depth,
        "request_seconds": round(request_seconds, 6),
        "dense_ready": dense_ready,
    }


def _write_state(server: RagDaemonServer) -> None:
    payload = {
        "schema": "local-rag.ragd.v2",
        "pid": os.getpid(),
        "generation": server.generation,
        "token": server.token,
        "started_at": server.started_at,
        "code_fingerprint": server.code_fingerprint,
        "lifecycle_state": "STARTING",
        "idle_timeout_seconds": server.idle_timeout,
    }
    if isinstance(server, RagUnixDaemonServer):
        payload["transport"] = "unix"
        payload["socket_file"] = server.socket_file
    else:
        payload["transport"] = "tcp"
        payload["host"] = server.server_address[0]
        payload["port"] = server.server_address[1]
    tmp = server.state_file.with_suffix(server.state_file.suffix + ".tmp")
    _write_json_atomic(server.state_file, payload)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _secure_runtime_directory(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    tmp.replace(path)


def _secure_runtime_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _unlink_state(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if data.get("pid") not in {None, os.getpid()}:
            return
        path.unlink()
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _state_is_superseded(path: Path, *, generation: str, pid: int) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return True
    except json.JSONDecodeError:
        return False
    try:
        owner_pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return True
    return (
        str(data.get("generation") or "") != generation
        or owner_pid != pid
    )


def _request_graceful_shutdown(
    server: RagDaemonServer | RagUnixDaemonServer,
) -> None:
    with server.state_lock:
        if server.shutdown_started:
            return
        server.shutdown_started = True
    threading.Thread(
        target=_drain_worker_then_stop_listener,
        args=(server,),
        name="ragd-graceful-shutdown",
        daemon=True,
    ).start()


def _drain_worker_then_stop_listener(
    server: RagDaemonServer | RagUnixDaemonServer,
) -> None:
    # Keep the listener available while the manager marks itself DRAINING and
    # fully reaps the native-heavy worker. Health/control requests therefore
    # keep identifying this generation, and clients cannot mistake a live
    # shutdown transition for a dead daemon and start a competing manager.
    while not server.worker_manager.shutdown():
        # A Windows worker can remain observable briefly after terminate,
        # kill, or Job Object close. Keep this authenticated generation's
        # listener and state alive in DRAINING until both the worker and its
        # dispatcher are fully reaped. Closing the listener earlier would let
        # a client start a competing manager/worker generation.
        time.sleep(0.1)
    server.shutdown()


def _idle_monitor(server: RagDaemonServer | RagUnixDaemonServer) -> None:
    while True:
        time.sleep(1)
        if _state_is_superseded(
            server.state_file,
            generation=server.generation,
            pid=os.getpid(),
        ):
            _request_graceful_shutdown(server)
            return
        worker = server.worker_manager.health()
        idle_for = time.monotonic() - server.last_used_at
        if (
            worker["active_requests"] == 0
            and worker["queue_depth"] == 0
            and idle_for >= server.idle_timeout
        ):
            _request_graceful_shutdown(server)
            return


if __name__ == "__main__":
    main()
