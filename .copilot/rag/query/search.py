from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
DBS_ROOT = Path(os.getenv("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))).expanduser().resolve()
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.config import DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS
from software_rag_tool.dbs import resolve_db_name
from software_rag_tool.search_api import payload_to_text, try_cold_lexical_fast_path

RUN_DIR = Path(__file__).resolve().parent / "run"
STATE_FILE = RUN_DIR / "ragd.json"
LOCK_FILE = RUN_DIR / "ragd.lock"
LOG_FILE = RUN_DIR / "ragd.log"
SOCKET_FILE = RUN_DIR / "ragd.sock"
FILE_TRANSPORT_DIR = RUN_DIR / "file-transport"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="*")
    parser.add_argument("--db", help="Target DB name, e.g. project-rag")
    parser.add_argument("--auto", action="store_true", help="Allow natural-language RAG trigger when DB name is omitted")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--budget-tokens", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--stdin", action="store_true", help="Read the question from stdin")
    parser.add_argument("--explain", action="store_true", help="Include retriever ranks and RRF debug information")
    parser.add_argument("--format", choices=["json", "prompt"], default="prompt")
    parser.add_argument("--include-db-hint", action="store_true")
    parser.add_argument("--no-daemon", action="store_true", help="Run synchronously without ragd")
    parser.add_argument(
        "--daemon-idle-timeout",
        type=int,
        default=int(os.getenv("RAGD_IDLE_TIMEOUT_SECONDS", str(DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS))),
        help="Seconds before an idle ragd exits",
    )
    args = parser.parse_args()

    question = sys.stdin.read().strip() if args.stdin else " ".join(args.question).strip()
    if not question:
        parser.error("question is required unless --stdin provides input")
    resolution = resolve_db_name(question, args.db, DBS_ROOT, args.auto)
    if not resolution.triggered:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": resolution.reason,
                    "message": "DB名（例: xxx-rag）を明示するか、RAG検索が必要な自然言語指示で --auto を使ってください。",
                    "available_dbs": resolution.candidates,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not resolution.db_name:
        print(
            json.dumps(
                {
                    "status": "needs_db",
                    "reason": resolution.reason,
                    "available_dbs": resolution.candidates,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    venv_python = Path(__file__).resolve().parent / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    marker = Path(__file__).resolve().parent / ".venv" / ".rag-deps-installed"
    python = str(venv_python) if venv_python.exists() and marker.exists() else sys.executable
    env = os.environ.copy()
    env.setdefault("RAG_DBS_ROOT", str(DBS_ROOT))
    env.setdefault("PYTHONIOENCODING", "utf-8")

    request = {
        "db": resolution.db_name,
        "question": question,
        "top_k": args.top_k,
        "source": "any",
        "max_chars": args.max_chars,
        "budget_tokens": args.budget_tokens,
        "explain": args.explain,
        "include_db_hint": args.include_db_hint,
    }

    daemon_enabled = not args.no_daemon and os.getenv("RAG_DISABLE_DAEMON", "").lower() not in {"1", "true", "yes"}
    if daemon_enabled:
        desired_transport = _desired_transport()
        state = _active_daemon_state(timeout=min(args.timeout or 3, 3), desired_transport=desired_transport)
        if not state:
            fast_payload = _try_fast_path(
                db_name=resolution.db_name,
                question=question,
                args=args,
            )
            if fast_payload:
                print(payload_to_text(fast_payload, args.format, explain=args.explain))
                return
            state = _start_daemon(
                python=python,
                env=env,
                idle_timeout=args.daemon_idle_timeout,
                startup_timeout=min(args.timeout or 10, 10),
                transport=desired_transport,
            )
            if not state and desired_transport == "tcp" and os.getenv("RAGD_TRANSPORT", "auto").lower() == "auto":
                state = _start_daemon(
                    python=python,
                    env=env,
                    idle_timeout=args.daemon_idle_timeout,
                    startup_timeout=min(args.timeout or 10, 10),
                    transport="file",
                )
        if state:
            payload = _query_daemon(state, request, timeout=args.timeout or None)
            if payload:
                print(payload_to_text(payload, args.format, explain=args.explain))
                raise SystemExit(1 if payload.get("status") == "error" else 0)

    _run_sync_script(python=python, env=env, args=args, question=question, db_name=resolution.db_name)


def _run_sync_script(*, python: str, env: dict[str, str], args: argparse.Namespace, question: str, db_name: str) -> None:
    script = TOOL_ROOT / "scripts" / "query.py"
    cmd = [
        python,
        str(script),
    ]
    if not args.stdin:
        cmd.append(question)
    cmd.extend(
        [
            "--db",
            db_name,
            "--top-k",
            str(args.top_k),
            "--max-chars",
            str(args.max_chars),
            "--format",
            args.format,
        ]
    )
    if args.budget_tokens:
        cmd.extend(["--budget-tokens", str(args.budget_tokens)])
    if args.stdin:
        cmd.append("--stdin")
    if args.explain:
        cmd.append("--explain")
    if args.include_db_hint:
        cmd.append("--include-db-hint")
    try:
        if args.stdin:
            completed = subprocess.run(cmd, env=env, input=question, text=True, timeout=args.timeout or None)
        else:
            completed = subprocess.run(cmd, env=env, timeout=args.timeout or None)
        raise SystemExit(completed.returncode)
    except subprocess.TimeoutExpired:
        print(
            json.dumps(
                {
                    "schema": "local-rag.search.v1",
                    "status": "error",
                    "error": f"search timed out after {args.timeout} seconds",
                    "db": db_name,
                    "query": question,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(124)


def _try_fast_path(*, db_name: str, question: str, args: argparse.Namespace) -> dict | None:
    try:
        return try_cold_lexical_fast_path(
            db_name=db_name,
            question=question,
            top_k=args.top_k,
            max_chars=args.max_chars,
            budget_tokens=args.budget_tokens or None,
            explain=args.explain,
            include_db_hint=args.include_db_hint,
        )
    except Exception:
        return None


def _desired_transport() -> str:
    transport = os.getenv("RAGD_TRANSPORT", "auto").lower()
    if transport in {"tcp", "file"}:
        return transport
    return "tcp"


def _active_daemon_state(*, timeout: int | float, desired_transport: str | None = None) -> dict | None:
    state = _read_state()
    if not state:
        return None
    if desired_transport and state.get("transport", "tcp") != desired_transport:
        return None
    if _healthcheck(state, timeout=timeout):
        return state
    return None


def _read_state() -> dict | None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8", errors="replace"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not data.get("token"):
        return None
    if data.get("transport") == "unix":
        if not data.get("socket_file"):
            return None
    elif data.get("transport") == "file":
        if not data.get("file_dir") or not data.get("heartbeat_file"):
            return None
    elif not data.get("host") or not data.get("port"):
        return None
    return data


def _healthcheck(state: dict, *, timeout: int | float) -> bool:
    if state.get("transport") == "file":
        try:
            heartbeat = Path(str(state["heartbeat_file"]))
            return heartbeat.exists() and time.time() - heartbeat.stat().st_mtime <= max(float(timeout) + 2.0, 5.0)
        except OSError:
            return False
    if state.get("transport") == "unix":
        payload = _unix_request(state, {"op": "health"}, timeout=timeout)
        return bool(payload and payload.get("status") == "ok")
    try:
        request = urllib.request.Request(
            f"http://{state['host']}:{state['port']}/health",
            headers={"X-RAGD-Token": str(state["token"])},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def _start_daemon(
    *,
    python: str,
    env: dict[str, str],
    idle_timeout: int,
    startup_timeout: int | float,
    transport: str,
) -> dict | None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = _acquire_start_lock()
    if lock_fd is None:
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            state = _active_daemon_state(timeout=1, desired_transport=transport)
            if state:
                return state
            time.sleep(0.2)
        return None
    token = secrets.token_hex(24)
    try:
        cmd = [
            python,
            str(Path(__file__).resolve().parent / "ragd.py"),
            "--token",
            token,
            "--state-file",
            str(STATE_FILE),
            "--idle-timeout",
            str(idle_timeout),
        ]
        if transport == "tcp":
            port = _free_port()
            cmd.extend(["--host", "127.0.0.1", "--port", str(port)])
        else:
            cmd.extend(["--file-dir", str(FILE_TRANSPORT_DIR)])
        popen_kwargs: dict = {}
        if sys.platform.startswith("win"):
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        with LOG_FILE.open("ab") as log:
            subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **popen_kwargs)
    except OSError:
        return None
    finally:
        _release_start_lock(lock_fd)
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        state = _active_daemon_state(timeout=1, desired_transport=transport)
        if state:
            return state
        time.sleep(0.2)
    return None


def _acquire_start_lock() -> int | None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(LOCK_FILE, flags)
    except FileExistsError:
        if _lock_is_stale():
            try:
                LOCK_FILE.unlink()
                fd = os.open(LOCK_FILE, flags)
            except OSError:
                return None
        else:
            return None
    os.write(fd, f"{os.getpid()} {time.time()}\n".encode("utf-8"))
    return fd


def _release_start_lock(fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def _lock_is_stale(max_age_seconds: int = 30) -> bool:
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
    except FileNotFoundError:
        return True
    return age > max_age_seconds


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _query_daemon(state: dict, payload: dict, *, timeout: int | None) -> dict | None:
    if state.get("transport") == "file":
        return _file_request(state, {"op": "search", "payload": payload}, timeout=timeout)
    if state.get("transport") == "unix":
        return _unix_request(state, {"op": "search", "payload": payload}, timeout=timeout)
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://{state['host']}:{state['port']}/search",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8", "X-RAGD-Token": str(state["token"])},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _file_request(state: dict, payload: dict, *, timeout: int | float | None) -> dict | None:
    try:
        file_dir = Path(str(state["file_dir"]))
        requests_dir = file_dir / "requests"
        responses_dir = file_dir / "responses"
        requests_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        response_name = f"{request_id}.response.json"
        response_file = responses_dir / response_name
        payload = dict(payload)
        payload["token"] = str(state["token"])
        payload["response"] = response_name
        request_file = requests_dir / f"{request_id}.request.json"
        tmp = request_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(request_file)
        deadline = time.monotonic() + float(timeout or 300)
        while time.monotonic() < deadline:
            if response_file.exists():
                data = json.loads(response_file.read_text(encoding="utf-8", errors="replace"))
                try:
                    response_file.unlink()
                except FileNotFoundError:
                    pass
                return data
            time.sleep(0.05)
    except (OSError, TimeoutError, json.JSONDecodeError):
        return None
    return None


def _unix_request(state: dict, payload: dict, *, timeout: int | float | None) -> dict | None:
    try:
        payload = dict(payload)
        payload["token"] = str(state["token"])
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            if timeout:
                sock.settimeout(timeout)
            sock.connect(str(state["socket_file"]))
            sock.sendall(data)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        if not chunks:
            return None
        return json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except (OSError, TimeoutError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    main()
