from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
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
from software_rag_tool.search_api import (
    compact_search_contract,
    normalize_search_contract,
    payload_to_text,
)

RUN_DIR = Path(__file__).resolve().parent / "run"
STATE_FILE = RUN_DIR / "ragd.json"
LOCK_FILE = RUN_DIR / "ragd.lock"
LOG_FILE = RUN_DIR / "ragd.log"
SOCKET_FILE = RUN_DIR / "ragd.sock"
FILE_TRANSPORT_DIR = RUN_DIR / "file-transport"
DEFAULT_OUTER_TIMEOUT_SECONDS = float(os.getenv("RAG_QUERY_TIMEOUT_SECONDS", "15"))
DEFAULT_DAEMON_ATTEMPT_TIMEOUT_SECONDS = float(os.getenv("RAGD_QUERY_TIMEOUT_SECONDS", "5"))
DEADLINE_OUTPUT_RESERVE_SECONDS = 2.0
COMPACT_JSON_OUTPUT_RESERVE_SECONDS = 0.5
COMPACT_JSON_DEFAULT_BUDGET_TOKENS = 1200
SYNC_TIMEOUT_CLEANUP_SECONDS = 0.25
WINDOWS_TASKKILL_TIMEOUT_SECONDS = 0.15
WINDOWS_FALLBACK_DENSE_MIN_SECONDS = 10.0
DEADLINE_FALLBACK_WARNING = (
    "Dense retrieval was skipped during daemon timeout recovery to meet "
    "the outer deadline."
)


def main() -> None:
    _configure_standard_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="*")
    parser.add_argument("--db", help="Target DB name, e.g. project-rag")
    parser.add_argument("--auto", action="store_true", help="Allow natural-language RAG trigger when DB name is omitted")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--budget-tokens", type=int, default=0)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_OUTER_TIMEOUT_SECONDS,
        help="Outer user-visible deadline in seconds. Use 0 to disable the deadline.",
    )
    parser.add_argument("--stdin", action="store_true", help="Read the question from stdin")
    parser.add_argument("--explain", action="store_true", help="Include retriever ranks and RRF debug information")
    parser.add_argument("--format", choices=["json", "prompt"], default="prompt")
    parser.add_argument(
        "--compact-json",
        action="store_true",
        help="For JSON output, omit duplicate legacy result arrays and return the evidence-first contract.",
    )
    parser.add_argument("--include-db-hint", action="store_true")
    parser.add_argument(
        "--retrieval-mode",
        choices=["hybrid", "lexical", "dense"],
        default="hybrid",
        help="Optional evaluation mode. Default hybrid preserves normal behavior.",
    )
    parser.add_argument(
        "--disable-identifier-diagnostics",
        action="store_true",
        help="Skip identifier diagnostics for pure retrieval benchmarking.",
    )
    parser.add_argument("--no-daemon", action="store_true", help="Run synchronously without ragd")
    parser.add_argument(
        "--daemon-idle-timeout",
        type=int,
        default=int(os.getenv("RAGD_IDLE_TIMEOUT_SECONDS", str(DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS))),
        help="Seconds before an idle ragd exits",
    )
    parser.add_argument(
        "--daemon-attempt-timeout",
        type=float,
        default=DEFAULT_DAEMON_ATTEMPT_TIMEOUT_SECONDS,
        help="Maximum seconds for the first daemon attempt before one synchronous fallback.",
    )
    parser.add_argument(
        "--daemon-fallback",
        choices=["on", "off"],
        default=os.getenv("RAGD_FALLBACK", "on").lower(),
        help="Allow one read-only synchronous retry after a daemon transport failure.",
    )
    parser.add_argument(
        "--require-daemon",
        action="store_true",
        help="Require an actual daemon response; disables cold fast path and synchronous fallback.",
    )
    args = parser.parse_args()
    if args.daemon_fallback not in {"on", "off"}:
        parser.error("--daemon-fallback must be on or off")

    question = sys.stdin.read().strip() if args.stdin else " ".join(args.question).strip()
    if not question:
        parser.error("question is required unless --stdin provides input")
    request_started = time.monotonic()
    outer_deadline = _deadline_from_timeout(args.timeout, started=request_started)
    output_reserve = _output_reserve_seconds(
        output_format=args.format,
        compact_json=args.compact_json,
    )
    work_deadline = outer_deadline - output_reserve if outer_deadline is not None else None
    resolution = resolve_db_name(question, args.db, DBS_ROOT, args.auto)
    if not resolution.triggered:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": resolution.reason,
                    "message": "DB名（例: xxx-rag）を明示するか、list_dbs.pyで候補DBを確認してから --db を指定してください。",
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
    if not (venv_python.exists() and marker.exists()):
        _print_setup_required(args.format, resolution.db_name, question)
        raise SystemExit(2)
    python = str(venv_python) if venv_python.exists() and marker.exists() else sys.executable
    env = os.environ.copy()
    env.setdefault("RAG_DBS_ROOT", str(DBS_ROOT))
    env.setdefault("PYTHONIOENCODING", "utf-8")

    request = {
        "request_id": uuid.uuid4().hex,
        "db": resolution.db_name,
        "question": question,
        "top_k": args.top_k,
        "source": "any",
        "max_chars": args.max_chars,
        "budget_tokens": _effective_budget_tokens(args),
        "explain": args.explain,
        "include_db_hint": args.include_db_hint,
        "retrieval_mode": args.retrieval_mode,
        "identifier_diagnostics": not args.disable_identifier_diagnostics,
    }

    daemon_enabled = not args.no_daemon and os.getenv("RAG_DISABLE_DAEMON", "").lower() not in {"1", "true", "yes"}
    if daemon_enabled:
        desired_transport = _desired_transport()
        configured_transport = os.getenv("RAGD_TRANSPORT", "auto").lower()
        published_state = _read_state()
        if published_state and not _daemon_code_is_current(published_state):
            retirement_timeout = _bounded_timeout(work_deadline, 1.0)
            identity = _daemon_health_payload(
                published_state,
                timeout=min(0.5, retirement_timeout or 0.0),
            )
            if _daemon_identity_matches(published_state, identity):
                _retire_daemon(
                    published_state,
                    request_id=str(request["request_id"]),
                    failure_kind="code_changed",
                    shutdown_timeout=retirement_timeout or 0.0,
                    record_failure=False,
                )
            else:
                _discard_published_state(published_state)
        health_timeout = _bounded_timeout(work_deadline, 0.5)
        daemon_started_this_request = False
        daemon_lifecycle, observed_state, _health = (
            _inspect_daemon_state(
                timeout=health_timeout,
                desired_transport=None if configured_transport == "auto" else desired_transport,
            )
            if health_timeout is None or health_timeout > 0
            else ("DEADLINE_EXHAUSTED", None, None)
        )
        daemon_route = _select_daemon_route(
            daemon_lifecycle,
            require_daemon=args.require_daemon,
        )
        state = observed_state if daemon_route == "daemon_ready" else None
        if daemon_route == "cold_local":
            _run_sync_script(
                python=python,
                env=env,
                args=args,
                db_name=resolution.db_name,
                question=question,
                timeout_override=_remaining_seconds(work_deadline),
                execution_metadata={
                    "request_id": request["request_id"],
                    "requested_execution": "daemon",
                    "actual_execution": "no-daemon",
                    "fallback_used": False,
                    "daemon_lifecycle_state": daemon_lifecycle,
                    "route_selection": "cold_local_adaptive",
                    "outer_timeout_seconds": args.timeout or None,
                    "attempts": [],
                },
                request_started=request_started,
            )
        if daemon_route == "daemon_required" and state is None:
            state = (
                observed_state
                if daemon_lifecycle in {"STARTING", "BUSY"}
                else None
            )
        if state is None:
            startup_timeout = _bounded_timeout(work_deadline, 10.0)
            if startup_timeout is None or startup_timeout > 0:
                state = _start_daemon(
                    python=python,
                    env=env,
                    idle_timeout=args.daemon_idle_timeout,
                    startup_timeout=startup_timeout if startup_timeout is not None else 10.0,
                    transport=desired_transport,
                )
                daemon_started_this_request = state is not None
            if (
                not state
                and desired_transport == "tcp"
                and os.getenv("RAGD_TRANSPORT", "auto").lower() == "auto"
            ):
                retry_timeout = _bounded_timeout(work_deadline, 10.0)
                if retry_timeout is None or retry_timeout > 0:
                    state = _start_daemon(
                        python=python,
                        env=env,
                        idle_timeout=args.daemon_idle_timeout,
                        startup_timeout=retry_timeout if retry_timeout is not None else 10.0,
                        transport="file",
                    )
                    daemon_started_this_request = state is not None
        if state:
            daemon_timeout = _daemon_query_timeout(
                attempt_timeout=args.daemon_attempt_timeout,
                deadline=work_deadline,
                cold_start=daemon_started_this_request,
                require_daemon=args.require_daemon,
            )
            if daemon_timeout <= 0:
                _print_deadline_failure(
                    args=args,
                    db_name=resolution.db_name,
                    question=question,
                    request_id=str(request["request_id"]),
                    request_started=request_started,
                    attempts=[],
                    restart=None,
                )
                raise SystemExit(124)
            attempt_started = time.monotonic()
            payload = _query_daemon(state, request, timeout=daemon_timeout)
            attempt_elapsed = time.monotonic() - attempt_started
            if payload:
                success = payload.get("status") != "error"
                payload["execution_metadata"] = {
                    "request_id": request["request_id"],
                    "requested_execution": "daemon",
                    "actual_execution": "daemon",
                    "first_attempt_success": success,
                    "final_user_visible_success": success,
                    "fallback_used": False,
                    "outer_timeout_seconds": args.timeout or None,
                    "total_latency_seconds": round(time.monotonic() - request_started, 6),
                    "attempts": [
                        {
                            "route": "daemon",
                            "success": success,
                            "latency_seconds": round(attempt_elapsed, 6),
                            "cold_start": daemon_started_this_request,
                            **daemon_state_snapshot(state),
                        }
                    ],
                }
                _print_search_payload(payload, args=args)
                raise SystemExit(1 if payload.get("status") == "error" else 0)
            failure_kind = "timeout" if attempt_elapsed >= daemon_timeout * 0.95 else "transport_error"
            first_attempt = {
                "route": "daemon",
                "success": False,
                "failure_kind": failure_kind,
                "latency_seconds": round(attempt_elapsed, 6),
                "cold_start": daemon_started_this_request,
                **daemon_state_snapshot(state),
            }
            retire_timeout = _bounded_timeout(work_deadline, 1.0)
            restart = _retire_daemon(
                state,
                request_id=str(request["request_id"]),
                failure_kind=failure_kind,
                shutdown_timeout=retire_timeout or 0.0,
            )
            first_attempt = _record_retirement_outcome(first_attempt, restart)
            if args.require_daemon or args.daemon_fallback == "off":
                _print_daemon_failure(
                    args=args,
                    db_name=resolution.db_name,
                    question=question,
                    request_id=str(request["request_id"]),
                    first_attempt=first_attempt,
                    restart=restart,
                    request_started=request_started,
                )
                raise SystemExit(124 if failure_kind == "timeout" else 1)
            if not restart.get("process_exited"):
                _print_daemon_failure(
                    args=args,
                    db_name=resolution.db_name,
                    question=question,
                    request_id=str(request["request_id"]),
                    first_attempt=first_attempt,
                    restart=restart,
                    request_started=request_started,
                )
                raise SystemExit(124 if failure_kind == "timeout" else 1)
            remaining_timeout = _remaining_seconds(work_deadline)
            if remaining_timeout is not None and remaining_timeout <= 0:
                _print_deadline_failure(
                    args=args,
                    db_name=resolution.db_name,
                    question=question,
                    request_id=str(request["request_id"]),
                    request_started=request_started,
                    attempts=[first_attempt],
                    restart=restart,
                )
                raise SystemExit(124)
            execution_metadata = {
                "request_id": request["request_id"],
                "requested_execution": "daemon",
                "actual_execution": "no-daemon",
                "first_attempt_success": False,
                "final_user_visible_success": False,
                "fallback_used": True,
                "attempts": [first_attempt],
                "daemon_restart": restart,
                "outer_timeout_seconds": args.timeout or None,
            }
            fallback_retrieval_mode = _fallback_retrieval_mode(
                args.retrieval_mode,
                remaining_seconds=remaining_timeout,
            )
            if fallback_retrieval_mode != args.retrieval_mode:
                execution_metadata["fallback_retrieval_mode"] = (
                    fallback_retrieval_mode
                )
                execution_metadata["fallback_dense_skipped"] = True
                execution_metadata["fallback_dense_skipped_reason"] = (
                    "deadline_bounded_windows_fallback"
                )
            _run_sync_script(
                python=python,
                env=env,
                args=args,
                question=question,
                db_name=resolution.db_name,
                timeout_override=remaining_timeout,
                execution_metadata=execution_metadata,
                retrieval_mode_override=fallback_retrieval_mode,
                request_started=request_started,
            )
        first_attempt = {
            "route": "daemon",
            "success": False,
            "failure_kind": "startup_failed",
            "latency_seconds": round(time.monotonic() - request_started, 6),
        }
        if args.require_daemon or args.daemon_fallback == "off":
            _print_daemon_failure(
                args=args,
                db_name=resolution.db_name,
                question=question,
                request_id=str(request["request_id"]),
                first_attempt=first_attempt,
                restart=None,
                request_started=request_started,
            )
            raise SystemExit(1)
        remaining_timeout = _remaining_seconds(work_deadline)
        if remaining_timeout is not None and remaining_timeout <= 0:
            _print_deadline_failure(
                args=args,
                db_name=resolution.db_name,
                question=question,
                request_id=str(request["request_id"]),
                request_started=request_started,
                attempts=[first_attempt],
                restart=None,
            )
            raise SystemExit(124)
        _run_sync_script(
            python=python,
            env=env,
            args=args,
            question=question,
            db_name=resolution.db_name,
            timeout_override=remaining_timeout,
            execution_metadata={
                "request_id": request["request_id"],
                "requested_execution": "daemon",
                "actual_execution": "no-daemon",
                "first_attempt_success": False,
                "final_user_visible_success": False,
                "fallback_used": True,
                "attempts": [first_attempt],
                "outer_timeout_seconds": args.timeout or None,
            },
            request_started=request_started,
        )

    remaining_timeout = _remaining_seconds(work_deadline)
    if remaining_timeout is not None and remaining_timeout <= 0:
        _print_deadline_failure(
            args=args,
            db_name=resolution.db_name,
            question=question,
            request_id=str(request["request_id"]),
            request_started=request_started,
            attempts=[],
            restart=None,
        )
        raise SystemExit(124)
    _run_sync_script(
        python=python,
        env=env,
        args=args,
        question=question,
        db_name=resolution.db_name,
        timeout_override=remaining_timeout,
        request_started=request_started,
    )


def _run_sync_script(
    *,
    python: str,
    env: dict[str, str],
    args: argparse.Namespace,
    question: str,
    db_name: str,
    timeout_override: float | None = None,
    execution_metadata: dict | None = None,
    retrieval_mode_override: str | None = None,
    request_started: float | None = None,
) -> None:
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
    effective_budget_tokens = _effective_budget_tokens(args)
    if effective_budget_tokens:
        cmd.extend(["--budget-tokens", str(effective_budget_tokens)])
    retrieval_mode = retrieval_mode_override or args.retrieval_mode
    if retrieval_mode != "hybrid":
        cmd.extend(["--retrieval-mode", retrieval_mode])
    else:
        cmd.append("--adaptive-hybrid")
    if args.stdin:
        cmd.append("--stdin")
    if args.explain:
        cmd.append("--explain")
    if args.include_db_hint:
        cmd.append("--include-db-hint")
    if args.disable_identifier_diagnostics:
        cmd.append("--disable-identifier-diagnostics")
    timeout = timeout_override if timeout_override is not None else (args.timeout or None)
    if timeout is not None and timeout <= 0:
        metadata = dict(execution_metadata or {})
        metadata.setdefault("request_id", uuid.uuid4().hex)
        metadata.setdefault("requested_execution", "no-daemon")
        metadata["actual_execution"] = "no-daemon"
        metadata.setdefault("first_attempt_success", False)
        metadata["final_user_visible_success"] = False
        metadata.setdefault("fallback_used", bool(execution_metadata))
        metadata.setdefault("attempts", [])
        metadata["deadline_exhausted"] = True
        metadata["outer_timeout_seconds"] = args.timeout or None
        if request_started is not None:
            metadata["total_latency_seconds"] = round(time.monotonic() - request_started, 6)
        _print_search_payload(
            {
                "schema": "local-rag.search.v1",
                "status": "error",
                "error": "outer search deadline exhausted before synchronous search could start",
                "db": db_name,
                "query": question,
                "execution_metadata": metadata,
            },
            args=args,
        )
        raise SystemExit(124)
    started = time.monotonic()
    try:
        completed = _run_sync_child(
            cmd,
            env=env,
            input_text=question if args.stdin else None,
            timeout=timeout,
        )
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        if args.format == "json":
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                metadata = dict(execution_metadata or {})
                attempt = {
                    "route": "no-daemon",
                    "success": False,
                    "failure_kind": "invalid_json",
                    "latency_seconds": round(time.monotonic() - started, 6),
                }
                metadata.setdefault("request_id", uuid.uuid4().hex)
                metadata.setdefault("requested_execution", "no-daemon")
                metadata["actual_execution"] = "no-daemon"
                metadata.setdefault("first_attempt_success", False)
                metadata["final_user_visible_success"] = False
                metadata.setdefault("fallback_used", bool(execution_metadata))
                metadata.setdefault("attempts", [])
                metadata["attempts"].append(attempt)
                metadata["outer_timeout_seconds"] = args.timeout or None
                if request_started is not None:
                    metadata["total_latency_seconds"] = round(time.monotonic() - request_started, 6)
                _print_search_payload(
                    {
                        "schema": "local-rag.search.v1",
                        "status": "error",
                        "error": "synchronous search returned invalid JSON",
                        "db": db_name,
                        "query": question,
                        "execution_metadata": metadata,
                    },
                    args=args,
                )
                raise SystemExit(completed.returncode or 1)
            else:
                payload = normalize_search_contract(payload)
                metadata = dict(execution_metadata or {})
                attempt = {
                    "route": "no-daemon",
                    "success": completed.returncode == 0 and payload.get("status") != "error",
                    "latency_seconds": round(time.monotonic() - started, 6),
                    "retrieval_mode": retrieval_mode,
                }
                metadata.setdefault("request_id", uuid.uuid4().hex)
                metadata.setdefault("requested_execution", "no-daemon")
                metadata["actual_execution"] = "no-daemon"
                metadata.setdefault("first_attempt_success", attempt["success"])
                metadata["final_user_visible_success"] = attempt["success"]
                metadata.setdefault("fallback_used", False)
                metadata.setdefault("attempts", [])
                metadata["attempts"].append(attempt)
                metadata["outer_timeout_seconds"] = args.timeout or None
                if request_started is not None:
                    metadata["total_latency_seconds"] = round(time.monotonic() - request_started, 6)
                if metadata.get("fallback_dense_skipped"):
                    warnings = list(payload.get("warnings") or [])
                    if DEADLINE_FALLBACK_WARNING not in warnings:
                        warnings.append(DEADLINE_FALLBACK_WARNING)
                    payload["warnings"] = warnings
                    payload["dense_used"] = False
                    payload["dense_skipped_reason"] = metadata.get(
                        "fallback_dense_skipped_reason"
                    )
                payload["execution_metadata"] = metadata
                _print_search_payload(payload, args=args)
        else:
            print(completed.stdout, end="")
        raise SystemExit(completed.returncode)
    except subprocess.TimeoutExpired:
        metadata = dict(execution_metadata or {})
        metadata.setdefault("request_id", uuid.uuid4().hex)
        metadata.setdefault("requested_execution", "no-daemon")
        metadata["actual_execution"] = "no-daemon"
        metadata.setdefault("first_attempt_success", False)
        metadata["final_user_visible_success"] = False
        metadata.setdefault("fallback_used", bool(execution_metadata))
        metadata.setdefault("attempts", [])
        metadata["attempts"].append(
            {
                "route": "no-daemon",
                "success": False,
                "failure_kind": "timeout",
                "latency_seconds": round(time.monotonic() - started, 6),
            }
        )
        metadata["deadline_exhausted"] = True
        metadata["outer_timeout_seconds"] = args.timeout or None
        if request_started is not None:
            metadata["total_latency_seconds"] = round(time.monotonic() - request_started, 6)
        _print_search_payload(
            {
                "schema": "local-rag.search.v1",
                "status": "error",
                "error": f"search timed out after {timeout} seconds",
                "db": db_name,
                "query": question,
                "execution_metadata": metadata,
            },
            args=args,
        )
        raise SystemExit(124)
    except OSError as exc:
        metadata = dict(execution_metadata or {})
        metadata.setdefault("request_id", uuid.uuid4().hex)
        metadata.setdefault("requested_execution", "no-daemon")
        metadata["actual_execution"] = "no-daemon"
        metadata.setdefault("first_attempt_success", False)
        metadata["final_user_visible_success"] = False
        metadata.setdefault("fallback_used", bool(execution_metadata))
        metadata.setdefault("attempts", [])
        metadata["attempts"].append(
            {
                "route": "no-daemon",
                "success": False,
                "failure_kind": "spawn_error",
                "latency_seconds": round(time.monotonic() - started, 6),
            }
        )
        metadata["outer_timeout_seconds"] = args.timeout or None
        if request_started is not None:
            metadata["total_latency_seconds"] = round(time.monotonic() - request_started, 6)
        _print_search_payload(
            {
                "schema": "local-rag.search.v1",
                "status": "error",
                "error": f"synchronous search could not start: {exc}",
                "db": db_name,
                "query": question,
                "execution_metadata": metadata,
            },
            args=args,
        )
        raise SystemExit(1)


def _configure_standard_streams() -> None:
    """Keep the CLI UTF-8 contract independent of the Windows code page."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _fallback_retrieval_mode(
    requested_mode: str,
    *,
    remaining_seconds: float | None,
) -> str:
    if (
        sys.platform.startswith("win")
        and requested_mode == "hybrid"
        and remaining_seconds is not None
        and remaining_seconds < WINDOWS_FALLBACK_DENSE_MIN_SECONDS
    ):
        return "lexical"
    return requested_mode


def _run_sync_child(
    cmd: list[str],
    *,
    env: dict[str, str],
    input_text: str | None,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    popen_kwargs: dict = {
        "env": env,
        "stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform.startswith("win"):
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        cleanup_deadline = time.monotonic() + SYNC_TIMEOUT_CLEANUP_SECONDS
        _terminate_process_tree(
            process,
            timeout=min(
                WINDOWS_TASKKILL_TIMEOUT_SECONDS,
                max(0.0, cleanup_deadline - time.monotonic()),
            ),
        )
        cleanup_remaining = max(0.0, cleanup_deadline - time.monotonic())
        try:
            if cleanup_remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, cleanup_remaining)
            stdout, stderr = process.communicate(timeout=cleanup_remaining)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=stdout or exc.output,
            stderr=stderr or exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    timeout: float = WINDOWS_TASKKILL_TIMEOUT_SECONDS,
) -> None:
    if process.poll() is not None:
        return
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.01, timeout),
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _print_setup_required(output_format: str, db_name: str, question: str) -> None:
    payload = normalize_search_contract(
        {
            "schema": "local-rag.search.v1",
            "status": "setup_required",
            "db": db_name,
            "query": question,
            "message": "RAG runtime is not initialized. Run the initial setup, then retry the search.",
            "required_action": "initial_setup",
        }
    )
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print("## RAG setup required")
    print("")
    print("RAG runtime is not initialized. Run the initial setup, then retry the search.")


def daemon_state_snapshot(state: dict) -> dict:
    snapshot = {
        "daemon_pid": state.get("pid"),
        "daemon_generation": state.get("generation"),
        "daemon_transport": state.get("transport", "tcp"),
        "daemon_started_at": state.get("started_at"),
        "daemon_code_fingerprint": state.get("code_fingerprint"),
    }
    if state.get("transport") == "file" and state.get("heartbeat_file"):
        try:
            heartbeat = json.loads(Path(str(state["heartbeat_file"])).read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            heartbeat = {}
        snapshot["daemon_active_requests"] = heartbeat.get("active_requests")
        snapshot["daemon_heartbeat_at"] = heartbeat.get("updated_at")
    return snapshot


def _retire_daemon(
    state: dict,
    *,
    request_id: str,
    failure_kind: str,
    shutdown_timeout: float = 0.5,
    record_failure: bool = True,
) -> dict:
    snapshot = {
        "schema": "local-rag.daemon-failure.v1",
        "recorded_at": datetime_now(),
        "request_id": request_id,
        "failure_kind": failure_kind,
        **daemon_state_snapshot(state),
    }
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if record_failure:
        try:
            with (RUN_DIR / "daemon-failures.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass
    started = time.monotonic()
    ack_timeout = min(0.25, max(0.0, shutdown_timeout))
    acknowledged = (
        _request_daemon_shutdown(state, timeout=ack_timeout)
        if ack_timeout > 0
        else False
    )
    current = _read_state()
    state_owned = bool(
        current
        and current.get("generation") == state.get("generation")
        and current.get("pid") == state.get("pid")
        and current.get("token") == state.get("token")
    )
    if state_owned and not acknowledged:
        try:
            STATE_FILE.unlink()
        except FileNotFoundError:
            pass
    pid = _state_pid(state)
    remaining = max(0.0, shutdown_timeout - (time.monotonic() - started))
    process_exited = not _process_is_alive(pid)
    if not process_exited and remaining > 0:
        process_exited = _wait_for_process_exit(pid, timeout=min(0.25, remaining))
    force_attempted = False
    if not process_exited and (acknowledged or state_owned):
        force_attempted = True
        remaining = max(0.0, shutdown_timeout - (time.monotonic() - started))
        process_exited = _terminate_daemon_process(state, timeout=remaining)
    current = _read_state()
    if (
        current
        and current.get("generation") == state.get("generation")
        and current.get("pid") == state.get("pid")
        and (process_exited or state_owned)
    ):
        try:
            STATE_FILE.unlink()
        except FileNotFoundError:
            pass
    return {
        "attempted": True,
        "shutdown_acknowledged": acknowledged,
        "force_attempted": force_attempted,
        "force_terminated": force_attempted and process_exited,
        "process_exited": process_exited,
        "state_identity_verified": state_owned,
        "old_generation": state.get("generation"),
        "next_start_required": True,
    }


def _request_daemon_shutdown(state: dict, *, timeout: float) -> bool:
    if state.get("transport") == "file":
        payload = _file_request(state, {"op": "shutdown"}, timeout=timeout)
        return bool(payload and payload.get("status") == "ok")
    if state.get("transport") == "unix":
        payload = _unix_request(state, {"op": "shutdown"}, timeout=timeout)
        return bool(payload and payload.get("status") == "ok")
    try:
        request = urllib.request.Request(
            f"http://{state['host']}:{state['port']}/shutdown",
            data=b"{}",
            headers={"X-RAGD-Token": str(state["token"])},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def _print_daemon_failure(
    *,
    args: argparse.Namespace,
    db_name: str,
    question: str,
    request_id: str,
    first_attempt: dict,
    restart: dict | None,
    request_started: float | None = None,
) -> None:
    payload = {
        "schema": "local-rag.search.v1",
        "status": "error",
        "error": f"required daemon attempt failed: {first_attempt.get('failure_kind')}",
        "db": db_name,
        "query": question,
        "execution_metadata": {
            "request_id": request_id,
            "requested_execution": "daemon",
            "actual_execution": "daemon",
            "first_attempt_success": False,
            "final_user_visible_success": False,
            "fallback_used": False,
            "attempts": [first_attempt],
            "daemon_restart": restart,
            "outer_timeout_seconds": args.timeout or None,
            "total_latency_seconds": (
                round(time.monotonic() - request_started, 6)
                if request_started is not None
                else None
            ),
        },
    }
    _print_search_payload(payload, args=args)


def _print_deadline_failure(
    *,
    args: argparse.Namespace,
    db_name: str,
    question: str,
    request_id: str,
    request_started: float,
    attempts: list[dict],
    restart: dict | None,
) -> None:
    payload = {
        "schema": "local-rag.search.v1",
        "status": "error",
        "error": f"outer search deadline exhausted after {args.timeout} seconds",
        "db": db_name,
        "query": question,
        "execution_metadata": {
            "request_id": request_id,
            "requested_execution": "daemon" if attempts else "no-daemon",
            "actual_execution": attempts[-1]["route"] if attempts else "no-daemon",
            "first_attempt_success": False,
            "final_user_visible_success": False,
            "fallback_used": any(attempt.get("route") == "no-daemon" for attempt in attempts),
            "attempts": attempts,
            "daemon_restart": restart,
            "deadline_exhausted": True,
            "outer_timeout_seconds": args.timeout or None,
            "total_latency_seconds": round(time.monotonic() - request_started, 6),
        },
    }
    _print_search_payload(payload, args=args)


def _print_search_payload(payload: dict, *, args: argparse.Namespace) -> None:
    if args.format == "json" and getattr(args, "compact_json", False):
        print(
            json.dumps(
                compact_search_contract(payload, explain=args.explain),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(payload_to_text(payload, args.format, explain=args.explain))


def _deadline_from_timeout(timeout: float | int | None, *, started: float | None = None) -> float | None:
    value = float(timeout or 0)
    if value <= 0:
        return None
    return (time.monotonic() if started is None else started) + value


def _output_reserve_seconds(*, output_format: str, compact_json: bool) -> float:
    if output_format == "json" and compact_json:
        return COMPACT_JSON_OUTPUT_RESERVE_SECONDS
    return DEADLINE_OUTPUT_RESERVE_SECONDS


def _effective_budget_tokens(args: argparse.Namespace) -> int | None:
    configured = int(getattr(args, "budget_tokens", 0) or 0)
    if configured > 0:
        return configured
    if (
        getattr(args, "format", "prompt") == "json"
        and getattr(args, "compact_json", False)
    ):
        return COMPACT_JSON_DEFAULT_BUDGET_TOKENS
    return None


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _bounded_timeout(deadline: float | None, cap: float) -> float | None:
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        return cap
    return min(max(0.0, float(cap)), remaining)


def _daemon_query_timeout(
    *,
    attempt_timeout: float,
    deadline: float | None,
    cold_start: bool,
    require_daemon: bool,
) -> float:
    soft_timeout = max(0.1, float(attempt_timeout))
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        return soft_timeout
    if cold_start and require_daemon:
        return remaining
    return min(soft_timeout, remaining)


def _state_pid(state: dict) -> int:
    try:
        return int(state.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _record_retirement_outcome(first_attempt: dict, restart: dict | None) -> dict:
    outcome = dict(first_attempt)
    if restart and not restart.get("process_exited"):
        outcome["retirement_failed"] = True
    return outcome


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        return _windows_process_is_alive(pid)
    if not sys.platform.startswith("win") and hasattr(os, "waitpid"):
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        except OSError:
            pass
        else:
            if reaped_pid == pid:
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


def _windows_process_is_alive(pid: int) -> bool:
    """Query a Windows process without using os.kill(pid, 0).

    Python implements non-console os.kill signals on Windows with
    TerminateProcess, so signal 0 is not a safe liveness probe there.
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            # Access denied means the PID exists but cannot be inspected.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        # A failed non-mutating query must not be replaced with os.kill.
        return False


def _wait_for_process_exit(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            return True
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    return not _process_is_alive(pid)


def _terminate_daemon_process(state: dict, *, timeout: float) -> bool:
    pid = _state_pid(state)
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return not _process_is_alive(pid)
    grace = min(0.25, max(0.0, timeout))
    if _wait_for_process_exit(pid, timeout=grace):
        return True
    if not sys.platform.startswith("win") and hasattr(signal, "SIGKILL"):
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            return not _process_is_alive(pid)
        remaining = max(0.0, timeout - grace)
        return _wait_for_process_exit(pid, timeout=remaining)
    return not _process_is_alive(pid)


def datetime_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _desired_transport() -> str:
    transport = os.getenv("RAGD_TRANSPORT", "auto").lower()
    if transport in {"tcp", "file"}:
        return transport
    return "tcp"


def _runtime_code_fingerprint() -> str:
    digest = hashlib.sha256()
    package_root = TOOL_ROOT / "software_rag_tool"
    paths = [
        Path(__file__).resolve().parent / "ragd.py",
        TOOL_ROOT / "pyproject.toml",
        TOOL_ROOT / "requirements.txt",
        *package_root.rglob("*.py"),
    ]
    for path in sorted(set(paths), key=lambda item: str(item.relative_to(RAG_ROOT))):
        digest.update(str(path.relative_to(RAG_ROOT)).encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _daemon_code_is_current(state: dict) -> bool:
    published = str(state.get("code_fingerprint") or "")
    return bool(published) and published == _runtime_code_fingerprint()


def _active_daemon_state(*, timeout: int | float, desired_transport: str | None = None) -> dict | None:
    lifecycle, state, _health = _inspect_daemon_state(
        timeout=timeout,
        desired_transport=desired_transport,
    )
    return state if lifecycle == "READY" else None


def _inspect_daemon_state(
    *,
    timeout: int | float,
    desired_transport: str | None = None,
) -> tuple[str, dict | None, dict | None]:
    state = _read_state()
    if not state:
        return "MISSING", None, None
    if not _daemon_code_is_current(state):
        return "DEAD", state, None
    if desired_transport and state.get("transport", "tcp") != desired_transport:
        return "DEAD", state, None
    health = _daemon_health_payload(state, timeout=timeout)
    if not _daemon_identity_matches(state, health):
        return "DEAD", state, health
    lifecycle = str((health or {}).get("lifecycle_state") or "").upper()
    if lifecycle not in {"STARTING", "READY", "BUSY"}:
        if int((health or {}).get("active_requests") or 0) > 0:
            lifecycle = "BUSY"
        elif (health or {}).get("ready") is True:
            lifecycle = "READY"
        else:
            lifecycle = "STARTING"
    return lifecycle, state, health


def _select_daemon_route(lifecycle: str, *, require_daemon: bool) -> str:
    if lifecycle == "READY":
        return "daemon_ready"
    if require_daemon:
        return "daemon_required"
    return "cold_local"


def _daemon_health_payload(state: dict, *, timeout: int | float) -> dict | None:
    if timeout <= 0:
        return None
    if state.get("transport") == "file":
        return _file_request(state, {"op": "health"}, timeout=timeout)
    if state.get("transport") == "unix":
        return _unix_request(state, {"op": "health"}, timeout=timeout)
    try:
        request = urllib.request.Request(
            f"http://{state['host']}:{state['port']}/health",
            headers={"X-RAGD-Token": str(state["token"])},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _daemon_identity_matches(state: dict, identity: dict | None) -> bool:
    return bool(
        identity
        and identity.get("status") == "ok"
        and identity.get("pid") == state.get("pid")
        and identity.get("generation") == state.get("generation")
        and identity.get("code_fingerprint") == state.get("code_fingerprint")
    )


def _discard_published_state(state: dict) -> None:
    current = _read_state()
    if not current:
        return
    if (
        current.get("pid") != state.get("pid")
        or current.get("generation") != state.get("generation")
        or current.get("token") != state.get("token")
    ):
        return
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def _read_state() -> dict | None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8", errors="replace"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if data.get("schema") != "local-rag.ragd.v2":
        return None
    if not data.get("token"):
        return None
    if not data.get("generation"):
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
    payload = _daemon_health_payload(state, timeout=timeout)
    return _daemon_identity_matches(state, payload)


def _start_daemon(
    *,
    python: str,
    env: dict[str, str],
    idle_timeout: int,
    startup_timeout: int | float,
    transport: str,
) -> dict | None:
    if startup_timeout <= 0:
        return None
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + startup_timeout
    lock_fd = _acquire_start_lock()
    if lock_fd is None:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            state = _active_daemon_state(timeout=min(1.0, remaining), desired_transport=transport)
            if state:
                return state
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return None
    token = secrets.token_hex(24)
    process: subprocess.Popen[bytes] | None = None
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
            cmd.extend(["--file-dir", str(FILE_TRANSPORT_DIR / token)])
        popen_kwargs: dict = {}
        if sys.platform.startswith("win"):
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        with LOG_FILE.open("ab") as log:
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **popen_kwargs,
            )
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            state = _read_state()
            if (
                state
                and _spawned_daemon_state_matches(
                    state,
                    generation=token,
                    launcher_pid=process.pid,
                    transport=transport,
                )
                and _healthcheck(state, timeout=min(1.0, remaining))
            ):
                return state
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        _terminate_process_tree(process)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        state = _read_state()
        if (
            state
            and state.get("generation") == token
            and state.get("transport", "tcp") == transport
        ):
            try:
                STATE_FILE.unlink()
            except FileNotFoundError:
                pass
        return None
    except OSError:
        if process is not None:
            _terminate_process_tree(process)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        return None
    finally:
        _release_start_lock(lock_fd)


def _spawned_daemon_state_matches(
    state: dict,
    *,
    generation: str,
    launcher_pid: int,
    transport: str,
) -> bool:
    """Validate the state published by the daemon spawned for this attempt.

    A Windows virtual-environment launcher can keep its own PID while the
    actual interpreter publishes a child PID. The unguessable per-attempt
    generation is also the authenticated daemon token, so the child PID is
    accepted only on Windows and is still verified by the health response.
    """
    if (
        state.get("generation") != generation
        or state.get("token") != generation
        or state.get("transport", "tcp") != transport
    ):
        return False
    try:
        daemon_pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if daemon_pid <= 0:
        return False
    return sys.platform.startswith("win") or daemon_pid == launcher_pid


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
        payload["generation"] = str(state["generation"])
        payload["response"] = response_name
        request_file = requests_dir / f"{request_id}.request.json"
        tmp = request_file.with_name(f"{request_file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(request_file)
        request_timeout = 300.0 if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + request_timeout
        while time.monotonic() < deadline:
            if response_file.exists():
                data = json.loads(response_file.read_text(encoding="utf-8", errors="replace"))
                try:
                    response_file.unlink()
                except FileNotFoundError:
                    pass
                return data
            time.sleep(0.05)
        for path in (request_file, response_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    except (OSError, TimeoutError, json.JSONDecodeError):
        return None
    return None


def _unix_request(state: dict, payload: dict, *, timeout: int | float | None) -> dict | None:
    try:
        payload = dict(payload)
        payload["token"] = str(state["token"])
        payload["generation"] = str(state["generation"])
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
