from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import platform
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import run_performance_eval as performance


RAG_ROOT = Path(__file__).resolve().parents[2]
QUERY_ROOT = RAG_ROOT / "query"
STATE_FILE = QUERY_ROOT / "run" / "ragd.json"
DATA_DIR = Path(__file__).resolve().parent / "data"
REPORT_DIR = Path(__file__).resolve().parent
CASES = (
    ("ac-rag", "冷房需要が増える背景を資料から説明して", True),
    ("incident-rag", "離陸後にエンジン故障が起きた事故の原因を探して", True),
    ("rfc-full-20k-rag", "DNSSEC Delegation Signer automation の仕様を教えて", False),
)


class HangingDaemonServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        hang_seconds: float,
        shutdown_ack: bool,
    ) -> None:
        self.token = token
        self.hang_seconds = hang_seconds
        self.shutdown_ack = shutdown_ack
        super().__init__(address, HangingDaemonHandler)


class HangingDaemonHandler(BaseHTTPRequestHandler):
    server: HangingDaemonServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authorized(self) -> bool:
        return self.headers.get("X-RAGD-Token") == self.server.token

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        if self.path != "/health" or not self._authorized():
            self._send_json(403, {"status": "error"})
            return
        self._send_json(200, {"status": "ok"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(403, {"status": "error"})
            return
        if self.path == "/shutdown":
            if self.server.shutdown_ack:
                self._send_json(200, {"status": "ok"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                time.sleep(self.server.hang_seconds)
            return
        if self.path != "/search":
            self._send_json(404, {"status": "error"})
            return
        time.sleep(self.server.hang_seconds)
        self._send_json(504, {"status": "error", "error": "injected timeout"})


def serve_hanging_daemon(
    token: str,
    hang_seconds: float,
    shutdown_ack: bool,
    port_queue: Any,
) -> None:
    server = HangingDaemonServer(
        ("127.0.0.1", 0),
        token,
        hang_seconds,
        shutdown_ack,
    )
    port_queue.put(server.server_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--outer-timeout", type=float, default=15.0)
    parser.add_argument("--daemon-attempt-timeout", type=float, default=5.0)
    parser.add_argument("--output-dir", help="Write raw JSONL and report outside the checkout for formal runs.")
    args = parser.parse_args()

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        results_path = output_dir / f"forced-fallback-results-{args.run_id}.jsonl"
        report_path = output_dir / f"forced-fallback-report-{args.run_id}.md"
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        results_path = DATA_DIR / f"forced-fallback-results-{args.run_id}.jsonl"
        report_path = REPORT_DIR / f"forced-fallback-report-{args.run_id}.md"
    if results_path.exists() or report_path.exists():
        raise FileExistsError(f"refusing to overwrite run {args.run_id}")

    provenance = collect_provenance()
    rows: list[dict[str, Any]] = []
    for db_name, question, shutdown_ack in CASES:
        performance.shutdown_daemon()
        try:
            STATE_FILE.unlink()
        except FileNotFoundError:
            pass
        row = run_case(
            db_name=db_name,
            question=question,
            outer_timeout=args.outer_timeout,
            daemon_attempt_timeout=args.daemon_attempt_timeout,
            shutdown_ack=shutdown_ack,
        )
        row.update(provenance)
        rows.append(row)
        append_jsonl(results_path, row)

    performance.shutdown_daemon()
    report_path.write_text(build_report(args.run_id, rows, args), encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "results": str(results_path),
                "report": str(report_path),
                "passed": all(row["contract_pass"] for row in rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(0 if all(row["contract_pass"] for row in rows) else 1)


def run_case(
    *,
    db_name: str,
    question: str,
    outer_timeout: float,
    daemon_attempt_timeout: float,
    shutdown_ack: bool,
) -> dict[str, Any]:
    token = uuid.uuid4().hex
    old_generation = f"injected-{uuid.uuid4().hex}"
    process_context = multiprocessing.get_context("spawn")
    port_queue = process_context.Queue()
    server_process = process_context.Process(
        target=serve_hanging_daemon,
        args=(token, daemon_attempt_timeout + 2.0, shutdown_ack, port_queue),
        daemon=False,
    )
    server_process.start()
    server_port = int(port_queue.get(timeout=5.0))
    port_queue.close()
    port_queue.join_thread()
    reaped = threading.Event()

    def reap_server() -> None:
        server_process.join()
        reaped.set()

    reaper = threading.Thread(target=reap_server, daemon=True)
    reaper.start()
    state = {
        "schema": "local-rag.ragd.v2",
        "pid": server_process.pid,
        "generation": old_generation,
        "transport": "tcp",
        "host": "127.0.0.1",
        "port": server_port,
        "token": token,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_fingerprint": "injected-timeout",
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    started = time.perf_counter()
    server_process_exited_before_cleanup = False
    try:
        completed = subprocess.run(
            search_command(
                db_name,
                question,
                outer_timeout=outer_timeout,
                daemon_attempt_timeout=daemon_attempt_timeout,
                require_daemon=False,
            ),
            cwd=str(RAG_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=outer_timeout + 1.0,
        )
    finally:
        server_process_exited_before_cleanup = reaped.wait(timeout=0.5)
        if not server_process_exited_before_cleanup and server_process.is_alive():
            server_process.terminate()
            server_process.join(timeout=0.5)
        if server_process.is_alive():
            server_process.kill()
            server_process.join(timeout=0.5)
    elapsed = time.perf_counter() - started
    payload = parse_json(completed.stdout)
    metadata = payload.get("execution_metadata") or {}
    attempts = metadata.get("attempts") or []
    daemon_attempts = [attempt for attempt in attempts if attempt.get("route") == "daemon"]
    fallback_attempts = [attempt for attempt in attempts if attempt.get("route") == "no-daemon"]
    restart = metadata.get("daemon_restart") or {}

    followup = subprocess.run(
        search_command(
            db_name,
            question,
            outer_timeout=outer_timeout,
            daemon_attempt_timeout=daemon_attempt_timeout,
            require_daemon=True,
        ),
        cwd=str(RAG_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=outer_timeout + 1.0,
    )
    followup_payload = parse_json(followup.stdout)
    followup_metadata = followup_payload.get("execution_metadata") or {}
    new_generation = next(
        (
            attempt.get("daemon_generation")
            for attempt in followup_metadata.get("attempts") or []
            if attempt.get("route") == "daemon"
        ),
        None,
    )

    checks = {
        "fallback_exit_zero": completed.returncode == 0,
        "stdout_json_pure": bool(payload),
        "first_attempt_failed": metadata.get("first_attempt_success") is False,
        "daemon_timeout_recorded": bool(
            daemon_attempts and daemon_attempts[0].get("failure_kind") == "timeout"
        ),
        "fallback_used": metadata.get("fallback_used") is True,
        "single_fallback": len(fallback_attempts) == 1,
        "fallback_succeeded": bool(fallback_attempts and fallback_attempts[0].get("success") is True),
        "final_user_visible_success": metadata.get("final_user_visible_success") is True,
        "outer_deadline_met": elapsed <= outer_timeout,
        "retirement_process_exited": restart.get("process_exited") is True,
        "retirement_mode": (
            restart.get("shutdown_acknowledged") is True
            if shutdown_ack
            else (
                restart.get("shutdown_acknowledged") is False
                and restart.get("force_attempted") is True
                and restart.get("force_terminated") is True
            )
        ),
        "server_process_exited_before_cleanup": server_process_exited_before_cleanup,
        "old_generation_retired": not STATE_FILE.exists()
        or read_generation() != old_generation,
        "followup_required_daemon_success": (
            followup.returncode == 0
            and followup_metadata.get("actual_execution") == "daemon"
            and followup_metadata.get("first_attempt_success") is True
        ),
        "new_generation": bool(new_generation and new_generation != old_generation),
    }
    return {
        "kind": "forced_fallback",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "db": db_name,
        "question": question,
        "outer_timeout_seconds": outer_timeout,
        "daemon_attempt_timeout_seconds": daemon_attempt_timeout,
        "shutdown_ack_expected": shutdown_ack,
        "latency_seconds": round(elapsed, 6),
        "exit_code": completed.returncode,
        "stderr": completed.stderr,
        "execution_metadata": metadata,
        "old_generation": old_generation,
        "new_generation": new_generation,
        "followup_exit_code": followup.returncode,
        "followup_execution_metadata": followup_metadata,
        "checks": checks,
        "contract_pass": all(checks.values()),
    }


def search_command(
    db_name: str,
    question: str,
    *,
    outer_timeout: float,
    daemon_attempt_timeout: float,
    require_daemon: bool,
) -> list[str]:
    command = [
        performance.query_python(),
        str(QUERY_ROOT / "search.py"),
        "--db",
        db_name,
        "--retrieval-mode",
        "hybrid",
        "--format",
        "json",
        "--timeout",
        str(outer_timeout),
        "--daemon-attempt-timeout",
        str(daemon_attempt_timeout),
        "--disable-identifier-diagnostics",
    ]
    if require_daemon:
        command.append("--require-daemon")
    command.append(question)
    return command


def collect_provenance() -> dict[str, Any]:
    source_paths = (
        QUERY_ROOT / "search.py",
        QUERY_ROOT / "ragd.py",
        RAG_ROOT / "gen_db" / "software_rag_tool" / "scripts" / "query.py",
        RAG_ROOT / "gen_db" / "software_rag_tool" / "software_rag_tool" / "search_api.py",
        Path(__file__).resolve(),
    )
    digest = hashlib.sha256()
    for path in source_paths:
        digest.update(str(path.relative_to(RAG_ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    case_fingerprint = hashlib.sha256(
        json.dumps(CASES, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    git_status = performance.git_value(
        ["status", "--short", "--untracked-files=all"],
        allow_empty=True,
    )
    return {
        "git_commit": performance.git_value(["rev-parse", "HEAD"]),
        "git_dirty": "unknown" if git_status == "unknown" else bool(git_status),
        "worktree_fingerprint": digest.hexdigest(),
        "daemon_code_fingerprint_expected": performance.expected_daemon_code_fingerprint(),
        "case_spec_fingerprint": case_fingerprint,
        "db_identities": {
            db_name: performance.read_db_identity(db_name)
            for db_name, _question, _shutdown_ack in CASES
        },
        "execution_os": platform.platform(),
        "python_version": platform.python_version(),
    }


def parse_json(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_generation() -> str | None:
    try:
        return str(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("generation") or "")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build_report(run_id: str, rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    first = rows[0] if rows else {}
    lines = [
        "# Forced daemon fallback smoke",
        "",
        f"- run_id: {run_id}",
        f"- git_commit: {first.get('git_commit', 'unknown')}",
        f"- git_dirty: {first.get('git_dirty', 'unknown')}",
        f"- worktree_fingerprint: {first.get('worktree_fingerprint', 'unknown')}",
        f"- daemon_code_fingerprint_expected: {first.get('daemon_code_fingerprint_expected', 'unknown')}",
        f"- case_spec_fingerprint: {first.get('case_spec_fingerprint', 'unknown')}",
        f"- outer timeout: {args.outer_timeout:.3f} sec",
        f"- daemon soft timeout: {args.daemon_attempt_timeout:.3f} sec",
        f"- result: {'PASS' if all(row['contract_pass'] for row in rows) else 'FAIL'}",
        "",
        "|DB|Result|Wall sec|Shutdown|Old PID exited|Fallback|Final success|New generation|",
        "|--|--|--:|--|--|--|--|--|",
    ]
    for row in rows:
        checks = row["checks"]
        lines.append(
            f"|{row['db']}|{'PASS' if row['contract_pass'] else 'FAIL'}|"
            f"{row['latency_seconds']:.3f}|"
            f"{'ACK' if row['shutdown_ack_expected'] else 'FORCE'}|"
            f"{'PASS' if checks['retirement_process_exited'] else 'FAIL'}|"
            f"{'PASS' if checks['fallback_succeeded'] else 'FAIL'}|"
            f"{'PASS' if checks['final_user_visible_success'] else 'FAIL'}|"
            f"{'PASS' if checks['new_generation'] else 'FAIL'}|"
        )
    lines.extend(["", "## Checks", ""])
    for row in rows:
        lines.append(f"### {row['db']}")
        lines.append("")
        for name, passed in row["checks"].items():
            lines.append(f"- {name}: {'PASS' if passed else 'FAIL'}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
