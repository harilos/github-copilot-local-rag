from __future__ import annotations

import argparse
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
from software_rag_tool.search_api import run_search_payload

_UnixStreamServerBase = getattr(socketserver, "ThreadingUnixStreamServer", socketserver.ThreadingTCPServer)


class RagDaemonServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], token: str, idle_timeout: int, state_file: Path) -> None:
        super().__init__(server_address, RagDaemonHandler)
        self.token = token
        self.idle_timeout = idle_timeout
        self.state_file = state_file
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.last_used_at = time.monotonic()
        self.active_requests = 0
        self.state_lock = threading.Lock()


class RagUnixDaemonServer(_UnixStreamServerBase):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_file: str, token: str, idle_timeout: int, state_file: Path) -> None:
        self.socket_file = socket_file
        try:
            Path(socket_file).unlink()
        except FileNotFoundError:
            pass
        super().__init__(socket_file, RagUnixDaemonHandler)
        self.token = token
        self.idle_timeout = idle_timeout
        self.state_file = state_file
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.last_used_at = time.monotonic()
        self.active_requests = 0
        self.state_lock = threading.Lock()

    def server_close(self) -> None:
        super().server_close()
        try:
            Path(self.socket_file).unlink()
        except FileNotFoundError:
            pass


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
        self._send_json(
            {
                "schema": "local-rag.ragd.health.v1",
                "status": "ok",
                "pid": os.getpid(),
                "started_at": self.server.started_at,
                "idle_timeout_seconds": self.server.idle_timeout,
            }
        )

    def do_POST(self) -> None:
        if self.path == "/shutdown":
            if not self._authorized():
                self._send_json({"status": "forbidden"}, status=403)
                return
            self._send_json({"schema": "local-rag.ragd.shutdown.v1", "status": "ok"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
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

        with self.server.state_lock:
            self.server.active_requests += 1
        try:
            payload = run_search_payload(
                db_name=str(request["db"]),
                question=str(request["question"]),
                top_k=int(request.get("top_k") or 8),
                source=str(request.get("source") or "any"),
                max_chars=int(request.get("max_chars") or 900),
                budget_tokens=int(request["budget_tokens"]) if request.get("budget_tokens") else None,
                explain=bool(request.get("explain")),
                include_db_hint=bool(request.get("include_db_hint")),
                use_dense=True,
            )
        except Exception as exc:
            payload = {
                "schema": "local-rag.search.v1",
                "status": "error",
                "db": request.get("db") if isinstance(request, dict) else "",
                "query": request.get("question") if isinstance(request, dict) else "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            with self.server.state_lock:
                self.server.active_requests -= 1
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
        op = request.get("op")
        if op == "health":
            self._send_json(
                {
                    "schema": "local-rag.ragd.health.v1",
                    "status": "ok",
                    "pid": os.getpid(),
                    "started_at": self.server.started_at,
                    "idle_timeout_seconds": self.server.idle_timeout,
                }
            )
            return
        if op == "shutdown":
            self._send_json({"schema": "local-rag.ragd.shutdown.v1", "status": "ok"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if op != "search":
            self._send_json({"status": "not_found"})
            return

        payload = request.get("payload") or {}
        with self.server.state_lock:
            self.server.active_requests += 1
        try:
            result = run_search_payload(
                db_name=str(payload["db"]),
                question=str(payload["question"]),
                top_k=int(payload.get("top_k") or 8),
                source=str(payload.get("source") or "any"),
                max_chars=int(payload.get("max_chars") or 900),
                budget_tokens=int(payload["budget_tokens"]) if payload.get("budget_tokens") else None,
                explain=bool(payload.get("explain")),
                include_db_hint=bool(payload.get("include_db_hint")),
                use_dense=True,
            )
        except Exception as exc:
            result = {
                "schema": "local-rag.search.v1",
                "status": "error",
                "db": payload.get("db") if isinstance(payload, dict) else "",
                "query": payload.get("question") if isinstance(payload, dict) else "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            with self.server.state_lock:
                self.server.active_requests -= 1
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
    if not args.socket_file and not args.file_dir and not args.port:
        parser.error("--port, --socket-file, or --file-dir is required")

    os.environ.setdefault("RAG_DBS_ROOT", str(DBS_ROOT))
    state_file = Path(args.state_file).expanduser().resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if args.file_dir:
        _run_file_daemon(
            file_dir=Path(args.file_dir).expanduser().resolve(),
            token=args.token,
            idle_timeout=args.idle_timeout,
            state_file=state_file,
        )
        return
    if args.socket_file:
        server = RagUnixDaemonServer(args.socket_file, args.token, args.idle_timeout, state_file)
    else:
        server = RagDaemonServer((args.host, int(args.port)), args.token, args.idle_timeout, state_file)
    _write_state(server)
    monitor = threading.Thread(target=_idle_monitor, args=(server,), daemon=True)
    monitor.start()
    try:
        server.serve_forever(poll_interval=1.0)
    finally:
        server.server_close()
        _unlink_state(state_file)


def _run_file_daemon(*, file_dir: Path, token: str, idle_timeout: int, state_file: Path) -> None:
    file_dir.mkdir(parents=True, exist_ok=True)
    requests_dir = file_dir / "requests"
    responses_dir = file_dir / "responses"
    requests_dir.mkdir(exist_ok=True)
    responses_dir.mkdir(exist_ok=True)
    heartbeat_file = file_dir / "heartbeat.json"
    state = {
        "schema": "local-rag.ragd.v1",
        "transport": "file",
        "pid": os.getpid(),
        "token": token,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "idle_timeout_seconds": idle_timeout,
        "file_dir": str(file_dir),
        "heartbeat_file": str(heartbeat_file),
    }
    _write_json_atomic(state_file, state)
    last_used_at = time.monotonic()
    active_requests = 0

    try:
        while True:
            _write_json_atomic(
                heartbeat_file,
                {
                    "schema": "local-rag.ragd.heartbeat.v1",
                    "pid": os.getpid(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "active_requests": active_requests,
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
            try:
                request = json.loads(processing_file.read_text(encoding="utf-8", errors="replace"))
                response_name = str(request.get("response") or f"{uuid.uuid4().hex}.response.json")
                response_file = responses_dir / Path(response_name).name
                if request.get("token") != token:
                    result = {"status": "forbidden"}
                elif request.get("op") == "health":
                    result = {
                        "schema": "local-rag.ragd.health.v1",
                        "status": "ok",
                        "pid": os.getpid(),
                        "started_at": state["started_at"],
                        "idle_timeout_seconds": idle_timeout,
                    }
                elif request.get("op") == "search":
                    payload = request.get("payload") or {}
                    result = _run_search_request(payload)
                elif request.get("op") == "shutdown":
                    result = {"schema": "local-rag.ragd.shutdown.v1", "status": "ok"}
                    _write_json_atomic(response_file, result)
                    return
                else:
                    result = {"status": "not_found"}
                _write_json_atomic(response_file, result)
            except Exception as exc:
                fallback = responses_dir / f"{uuid.uuid4().hex}.response.json"
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
        _unlink_state(state_file)


def _run_search_request(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return run_search_payload(
            db_name=str(payload["db"]),
            question=str(payload["question"]),
            top_k=int(payload.get("top_k") or 8),
            source=str(payload.get("source") or "any"),
            max_chars=int(payload.get("max_chars") or 900),
            budget_tokens=int(payload["budget_tokens"]) if payload.get("budget_tokens") else None,
            explain=bool(payload.get("explain")),
            include_db_hint=bool(payload.get("include_db_hint")),
            use_dense=True,
        )
    except Exception as exc:
        return {
            "schema": "local-rag.search.v1",
            "status": "error",
            "db": payload.get("db") if isinstance(payload, dict) else "",
            "query": payload.get("question") if isinstance(payload, dict) else "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _write_state(server: RagDaemonServer) -> None:
    payload = {
        "schema": "local-rag.ragd.v1",
        "pid": os.getpid(),
        "token": server.token,
        "started_at": server.started_at,
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _unlink_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _idle_monitor(server: RagDaemonServer) -> None:
    while True:
        time.sleep(30)
        with server.state_lock:
            idle_for = time.monotonic() - server.last_used_at
            active = server.active_requests
        if active == 0 and idle_for >= server.idle_timeout:
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


if __name__ == "__main__":
    main()
