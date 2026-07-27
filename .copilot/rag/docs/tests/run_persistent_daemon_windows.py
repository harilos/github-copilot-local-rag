from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import importlib.util
import json
import math
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable


SCHEMA = "local-rag.persistent-daemon-test.v1"
SUCCESS_STATUSES = {"ok", "partial", "no_hit"}
EXPECTED_OVERLOAD_ERRORS = {
    "daemon_overloaded",
    "queue_deadline_expired",
}
PHASE_GATE_NAMES = {
    "structured-contract": ("structured_request_equivalence",),
    "lifecycle-20": ("lifecycle_20",),
    "clients-100": ("clients_100",),
    "concurrency": ("cold_concurrency_4", "warm_concurrency_2_4"),
    "db-release": ("db_release_all",),
    "crash": (
        "client_crash_recovery",
        "worker_exit_recovery",
        "worker_hang_recovery",
        "manager_job_recovery",
    ),
    "soak-200-c4": (
        "soak_200_c4",
        "resource_manager_handles",
        "resource_worker_handles",
        "resource_manager_threads",
        "resource_worker_threads",
        "resource_manager_rss",
        "resource_worker_rss",
    ),
    "overload-c8": ("overload_8_safety",),
    "exact-30": ("exact_30",),
    "broad-18": ("broad_search_18",),
}
DEFAULT_DATABASES = (
    "ac-rag",
    "incident-rag",
    "rfc-full-20k-rag",
)
QUESTIONS = {
    "ac-rag": {
        "H": "ポーランドの空調について、資料で確認できる範囲を教えて。",
        "L": "A2Lについて資料に書かれていることを教えて。",
        "V": "空調の効率改善と低GWP冷媒について教えて。",
    },
    "incident-rag": {
        "H": "ntsb_aviation_report_67438の事故概要と関連資料を教えて。",
        "L": "ntsb_aviation_report_67438の事故概要を教えて。",
        "V": "航空事故における操縦と気象の関連を教えて。",
    },
    "rfc-full-20k-rag": {
        "H": "RFC 10026の目的と関連する仕様を教えて。",
        "L": "RFC 10026の目的を教えて。",
        "V": "インターネットプロトコルの相互運用性について教えて。",
    },
}
PROFILE_MODE = {
    "H": "hybrid",
    "L": "lexical",
    "V": "dense",
}
_LOCAL_HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(
            len(ordered) - 1,
            math.ceil((percentage / 100.0) * len(ordered)) - 1,
        ),
    )
    return ordered[index]


def median_absolute_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def is_monotonic_increase(values: list[int | float | None]) -> bool:
    present = [float(value) for value in values if value is not None]
    return (
        len(present) >= 4
        and all(later > earlier for earlier, later in zip(present, present[1:]))
    )


def parse_one_json(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"stdout_not_utf8:{exc}"
    decoder = json.JSONDecoder()
    try:
        value, offset = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        return None, f"stdout_not_json:{exc}"
    if text[offset:].strip():
        return None, "stdout_contains_trailing_data"
    if not isinstance(value, dict):
        return None, "stdout_json_not_object"
    return value, None


def sanitize_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    return {
        key: value
        for key, value in state.items()
        if key not in {"token"}
    }


@dataclass(frozen=True)
class ClientCase:
    case_id: str
    db: str
    profile: str
    question: str
    expected_identifiers: tuple[str, ...] = ()


class ArtifactWriter:
    def __init__(self, output_dir: Path, run_id: str) -> None:
        self.output_dir = output_dir
        self.run_id = run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = (
            output_dir / f"persistent-daemon-results-{run_id}.jsonl"
        )
        self.resources_path = (
            output_dir / f"persistent-daemon-resources-{run_id}.jsonl"
        )
        self.events_path = (
            output_dir / f"persistent-daemon-events-{run_id}.jsonl"
        )
        self.summary_path = (
            output_dir / f"persistent-daemon-summary-{run_id}.json"
        )
        self.report_path = (
            output_dir / f"persistent-daemon-full-report-{run_id}.md"
        )
        self._lock = threading.Lock()

    def append(self, kind: str, value: dict[str, Any]) -> None:
        paths = {
            "case": self.results_path,
            "resource_sample": self.resources_path,
            "event": self.events_path,
        }
        path = paths[kind]
        row = {
            "schema": SCHEMA,
            "record_type": kind,
            "run_id": self.run_id,
            **value,
        }
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(row, ensure_ascii=False, sort_keys=True)
                    + "\n"
                )

    def write_summary(self, summary: dict[str, Any]) -> None:
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        lines = [
            "# Persistent daemon full test report",
            "",
            f"- run_id: `{self.run_id}`",
            f"- platform: `{summary['platform']}`",
            f"- started_at: `{summary['started_at']}`",
            f"- finished_at: `{summary['finished_at']}`",
            f"- overall: **{summary['overall']}**",
            "",
            "## Gates",
            "",
            "| Gate | Result | Detail |",
            "|---|---|---|",
        ]
        for name, gate in summary["gates"].items():
            detail = str(gate.get("detail") or "").replace("|", "\\|")
            lines.append(
                f"| `{name}` | **{gate['result']}** | {detail} |"
            )
        lines.extend(
            [
                "",
                "## Counts",
                "",
                f"- cases: {summary['case_count']}",
                f"- failures: {summary['failure_count']}",
                f"- JSON parse errors: {summary['json_parse_errors']}",
                f"- fallbacks: {summary['fallback_count']}",
                f"- response mismatches: {summary['identity_mismatches']}",
                "",
                "The frozen Semantic accuracy gate is independent of this "
                "runtime report.",
            ]
        )
        self.report_path.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )


class PersistentDaemonWindowsRunner:
    def __init__(
        self,
        *,
        installed_rag: Path,
        output_dir: Path,
        run_id: str,
        databases: tuple[str, ...],
        deadline_seconds: float,
    ) -> None:
        self.rag_root = installed_rag.resolve()
        self.output_dir = output_dir.resolve()
        if self.output_dir == self.rag_root or self.rag_root in self.output_dir.parents:
            raise ValueError("output-dir must be outside the installed RAG tree")
        self.query_root = self.rag_root / "query"
        expected_python = (
            self.query_root / ".venv" / "Scripts" / "python.exe"
            if os.name == "nt"
            else self.query_root / ".venv" / "bin" / "python"
        )
        self.python = expected_python.resolve()
        if not self.python.is_file():
            raise FileNotFoundError(f"missing installed venv Python: {self.python}")
        if Path(sys.executable).resolve() != self.python:
            raise RuntimeError(
                "run this harness with the installed RAG venv Python: "
                f"{self.python}"
            )
        self.search = self.query_root / "search.py"
        self.state_file = self.query_root / "run" / "ragd.json"
        self.lock_file = self.query_root / "run" / "ragd.lock"
        self.dbs_root = self.rag_root / "dbs"
        self.databases = databases
        self.deadline_seconds = deadline_seconds
        self.artifacts = ArtifactWriter(self.output_dir, run_id)
        self.run_id = run_id
        self.started_at = utc_now()
        self.rows: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.gates: dict[str, dict[str, Any]] = {}
        self.required_gates: set[str] = set()
        self.safety_stop = False
        self._row_lock = threading.Lock()

    def gate(self, name: str, result: str, detail: str = "") -> None:
        if result not in {"PASS", "FAIL", "NOT_RUN"}:
            raise ValueError(result)
        current = self.gates.get(name)
        if current and current["result"] == "FAIL" and result != "FAIL":
            return
        self.gates[name] = {"result": result, "detail": detail}
        self.artifacts.append(
            "event",
            {
                "phase": name,
                "event": "gate",
                "result": result,
                "detail": detail,
                "at": utc_now(),
            },
        )

    def read_state(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    def control(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        active = state or self.read_state()
        if not active or active.get("transport", "tcp") != "tcp":
            return None
        url = f"http://{active['host']}:{active['port']}/{path.lstrip('/')}"
        data = (
            json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-RAGD-Token": str(active["token"]),
            },
            method="POST" if payload is not None else "GET",
        )
        try:
            with _LOCAL_HTTP_OPENER.open(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ):
            return None
        return value if isinstance(value, dict) else None

    def health(
        self,
        *,
        timeout: float = 2.0,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self.control("health", None, timeout=timeout, state=state)

    def validated_daemon_pid(
        self,
        health: dict[str, Any] | None,
        role: str,
    ) -> int:
        if role not in {"manager", "worker"} or not health:
            return 0
        state = self.read_state()
        fresh = self.health(state=state) if state else None
        pid_key = f"{role}_pid"
        pid = int(health.get(pid_key) or 0)
        if (
            not fresh
            or pid <= 0
            or pid == os.getpid()
            or int(fresh.get(pid_key) or 0) != pid
            or int(state.get("pid") or 0)
            != int(fresh.get("manager_pid") or 0)
            or str(fresh.get("manager_generation") or "")
            != str(health.get("manager_generation") or "")
            or str(fresh.get("worker_generation") or "")
            != str(health.get("worker_generation") or "")
        ):
            return 0
        executable = process_executable(pid)
        if executable is None:
            return 0
        allowed_executables = {
            os.path.normcase(str(self.python)),
            os.path.normcase(str(Path(sys.executable).resolve())),
            os.path.normcase(
                str(
                    Path(
                        getattr(
                            sys,
                            "_base_executable",
                            sys.executable,
                        )
                    ).resolve()
                )
            ),
        }
        if os.path.normcase(str(Path(executable).resolve())) not in allowed_executables:
            return 0
        return pid

    def client_command(self, case: ClientCase) -> list[str]:
        command = [
            str(self.python),
            str(self.search),
            "--db",
            case.db,
            "--compact-json",
            "--require-daemon",
            "--daemon-idle-timeout",
            "3600",
            "--timeout",
            str(self.deadline_seconds),
        ]
        mode = PROFILE_MODE[case.profile]
        if mode != "hybrid":
            command.extend(["--retrieval-mode", mode])
        command.append(case.question)
        return command

    def spawn_client(
        self,
        case: ClientCase,
        *,
        extra_args: Iterable[str] = (),
        stdin_data: bytes | None = None,
        omit_question: bool = False,
    ) -> subprocess.Popen[bytes]:
        command = self.client_command(case)
        if omit_question:
            command.pop()
        if extra_args:
            insertion = len(command) if omit_question else len(command) - 1
            command[insertion:insertion] = list(extra_args)
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            cwd=str(self.query_root),
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )

    def run_client(
        self,
        case: ClientCase,
        *,
        phase: str,
        barrier: threading.Barrier | None = None,
        extra_args: Iterable[str] = (),
        stdin_data: bytes | None = None,
        omit_question: bool = False,
        record: bool = True,
    ) -> dict[str, Any]:
        if barrier is not None:
            barrier.wait(timeout=30)
        started_at = utc_now()
        started = time.monotonic()
        process = self.spawn_client(
            case,
            extra_args=extra_args,
            stdin_data=stdin_data,
            omit_question=omit_question,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(
                input=stdin_data,
                timeout=self.deadline_seconds + 3.0
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        elapsed = time.monotonic() - started
        payload, parse_error = parse_one_json(stdout)
        daemon = (payload or {}).get("daemon_state") or {}
        execution = (payload or {}).get("execution_metadata") or {}
        attempts = execution.get("attempts") or []
        daemon_attempt = next(
            (
                item
                for item in attempts
                if isinstance(item, dict) and item.get("route") == "daemon"
            ),
            {},
        )
        fallback_used = bool(execution.get("fallback_used"))
        evidence = [
            item
            for item in (payload or {}).get("evidence") or []
            if isinstance(item, dict)
        ]
        background = [
            item
            for item in (payload or {}).get("background_context") or []
            if isinstance(item, dict)
        ]
        identity_match = bool(
            payload
            and daemon.get("request_id")
            and daemon.get("client_id")
            and str(daemon.get("db") or "") == case.db
            and str((payload or {}).get("selected_db") or (payload or {}).get("db") or "")
            == case.db
            and str(daemon.get("manager_generation") or "")
            == str(daemon_attempt.get("daemon_generation") or "")
        )
        status = str((payload or {}).get("status") or "")
        error_kind = str(
            (payload or {}).get("error_kind")
            or (payload or {}).get("error")
            or ""
        )
        success = bool(
            not timed_out
            and process.returncode == 0
            and parse_error is None
            and status in SUCCESS_STATUSES
            and identity_match
            and not fallback_used
            and elapsed <= self.deadline_seconds
        )
        raw_verified_identifiers = [
            identifier
            for identifier in case.expected_identifiers
            if any(
                raw_identifier_occurs(
                    identifier,
                    str(item.get("text") or ""),
                    str((item.get("source") or {}).get("path") or ""),
                )
                for item in evidence
            )
        ]
        row = {
            "phase": phase,
            "case_id": case.case_id,
            "db": case.db,
            "profile": case.profile,
            "query_kind": PROFILE_MODE[case.profile],
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(elapsed, 6),
            "deadline_seconds": self.deadline_seconds,
            "exit_code": process.returncode,
            "client_process_pid": process.pid,
            "client_process_reaped": not process_is_alive(process.pid),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "stdout_json_valid": parse_error is None,
            "json_error": parse_error,
            "response_status": status,
            "error_kind": error_kind or None,
            "fallback_used": fallback_used,
            "response_identity_match": identity_match,
            "request_id": daemon.get("request_id"),
            "client_id": daemon.get("client_id"),
            "manager_pid": daemon.get("manager_pid"),
            "worker_pid": daemon.get("worker_pid"),
            "manager_generation": daemon.get("manager_generation"),
            "worker_generation": daemon.get("worker_generation"),
            "model_load_count": daemon.get("model_load_count"),
            "open_database_count": daemon.get("open_database_count"),
            "handled_request_count": daemon.get("handled_request_count"),
            "worker_rss_bytes": daemon.get("worker_rss_bytes"),
            "worker_handle_count": daemon.get("worker_handle_count"),
            "worker_thread_count": daemon.get("worker_thread_count"),
            "document_result_count": len(
                (payload or {}).get("document_results") or []
            ),
            "evidence_count": len(evidence),
            "evidence_exact_signal_count": sum(
                "exact" in set(item.get("signals") or [])
                for item in evidence
            ),
            "evidence_neighbor_exact_signal_count": sum(
                item.get("support_kind") == "anchored_neighbor"
                and "exact" in set(item.get("signals") or [])
                for item in evidence
            ),
            "background_exact_signal_count": sum(
                "exact" in set(item.get("signals") or [])
                for item in background
            ),
            "background_neighbor_exact_signal_count": sum(
                item.get("support_kind") == "anchored_neighbor"
                and "exact" in set(item.get("signals") or [])
                for item in background
            ),
            "evidence_paths": sorted(
                {
                    str((item.get("source") or {}).get("path") or "")
                    for item in evidence
                    if (item.get("source") or {}).get("path")
                }
            ),
            "duplicate_document_paths": duplicate_document_paths(payload or {}),
            "exact_candidate_count": (payload or {}).get("exact_candidate_count"),
            "unmatched_identifiers": (payload or {}).get("unmatched_identifiers")
            or [],
            "raw_verified_identifiers": raw_verified_identifiers,
            "warnings": (payload or {}).get("warnings") or [],
            "dense_used": (payload or {}).get("dense_used"),
            "dense_skipped_reason": (payload or {}).get("dense_skipped_reason"),
            "timed_out": timed_out,
            "result": "PASS" if success else "FAIL",
            "stderr_tail": stderr.decode("utf-8", errors="replace")[-500:],
            "_payload": payload,
        }
        public_row = {
            key: value
            for key, value in row.items()
            if not key.startswith("_")
        }
        if record:
            with self._row_lock:
                self.rows.append(public_row)
            self.artifacts.append("case", public_row)
        return row

    def run_concurrent(
        self,
        cases: list[ClientCase],
        *,
        phase: str,
    ) -> list[dict[str, Any]]:
        barrier = threading.Barrier(len(cases))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(cases)
        ) as executor:
            futures = [
                executor.submit(
                    self.run_client,
                    case,
                    phase=phase,
                    barrier=barrier,
                )
                for case in cases
            ]
            return [future.result() for future in futures]

    def shutdown_and_verify(self, phase: str) -> dict[str, Any]:
        state = self.read_state()
        health = self.health(state=state) if state else None
        manager_pid = int(
            (health or {}).get("manager_pid")
            or (state or {}).get("pid")
            or 0
        )
        worker_pid = int((health or {}).get("worker_pid") or 0)
        port = int((state or {}).get("port") or 0)
        acknowledged = bool(
            state
            and self.control(
                "shutdown",
                {},
                timeout=3.0,
                state=state,
            )
        )
        manager_gone = wait_process_gone(manager_pid, 10.0)
        worker_gone = wait_process_gone(worker_pid, 10.0)
        state_gone = wait_path_gone(self.state_file, 10.0)
        port_closed = wait_port_closed(port, 5.0)
        lock_acquirable = exclusive_lock_probe(self.lock_file)
        result = (
            manager_gone
            and worker_gone
            and state_gone
            and port_closed
            and lock_acquirable
        )
        event = {
            "phase": phase,
            "event": "shutdown_verify",
            "at": utc_now(),
            "acknowledged": acknowledged,
            "manager_pid": manager_pid or None,
            "worker_pid": worker_pid or None,
            "manager_gone": manager_gone,
            "worker_gone": worker_gone,
            "state_gone": state_gone,
            "port_closed": port_closed,
            "lock_acquirable": lock_acquirable,
            "result": "PASS" if result else "FAIL",
        }
        self.artifacts.append("event", event)
        return event

    def sample_resources(
        self,
        *,
        phase: str,
        sample_index: int,
        requests_completed: int,
    ) -> dict[str, Any]:
        state = self.read_state()
        health = self.health(state=state)
        manager_pid = int((health or {}).get("manager_pid") or 0)
        worker_pid = int((health or {}).get("worker_pid") or 0)
        manager_external = process_metrics(manager_pid)
        worker_external = process_metrics(worker_pid)
        port = int((state or {}).get("port") or 0)
        sample = {
            "phase": phase,
            "sample_index": sample_index,
            "requests_completed": requests_completed,
            "at": utc_now(),
            "manager_pid": manager_pid or None,
            "worker_pid": worker_pid or None,
            "manager_generation": (health or {}).get("manager_generation"),
            "worker_generation": (health or {}).get("worker_generation"),
            "model_load_count": (health or {}).get("model_load_count"),
            "open_database_count": (health or {}).get("open_database_count"),
            "handled_request_count": (health or {}).get("handled_request_count"),
            "manager_rss_bytes": manager_external.get("rss_bytes"),
            "worker_rss_bytes": worker_external.get("rss_bytes"),
            "manager_handle_count": manager_external.get("handle_count"),
            "worker_handle_count": worker_external.get("handle_count"),
            "manager_thread_count": manager_external.get("thread_count"),
            "worker_thread_count": worker_external.get("thread_count"),
            "manager_alive": process_is_alive(manager_pid),
            "worker_alive": process_is_alive(worker_pid),
            "established_local_tcp": established_tcp_count(
                manager_pid,
                worker_pid,
                port,
            ),
            "worker_job_object_active": (
                health or {}
            ).get("worker_job_object_active"),
        }
        self.resources.append(sample)
        self.artifacts.append("resource_sample", sample)
        return sample

    def warm_generation(
        self,
        phase: str,
        *,
        db_name: str | None = None,
    ) -> dict[str, Any]:
        row = self.run_client(
            make_case(
                "warmup",
                db_name or self.databases[0],
                "H",
                0,
            ),
            phase=phase,
        )
        deadline = time.monotonic() + 30.0
        health = self.health()
        while (
            health
            and health.get("dense_warmup_state") == "starting"
            and time.monotonic() < deadline
        ):
            time.sleep(0.2)
            health = self.health()
        return {"row": row, "health": health}

    def phase_structured_contract(self) -> None:
        question = (
            '社内資料で Mix-ID_2/Path.v1\\仕様 "引用" の根拠と'
            "関連資料を日本語で教えて。"
        )
        literal_identifiers = ["A2W", "Mix-ID_2/Path.v1"]
        request = {
            "schema_version": "rag-search-request-v1",
            "original_question": question,
            "answer_goal": "evidence",
            "literal_identifiers": literal_identifiers,
            "entities": ['製品 "Alpha"\\仕様', "Mix-ID_2/Path.v1"],
            "facets": [
                {
                    "kind": "literal",
                    "query": "A2W",
                    "purpose": (
                        "Find literal occurrences and identifier evidence."
                    ),
                },
                {
                    "kind": "literal",
                    "query": "Mix-ID_2/Path.v1",
                    "purpose": (
                        "Find literal occurrences and identifier evidence."
                    ),
                },
                {
                    "kind": "semantic",
                    "query": "空調の関連方式と社内資料",
                    "purpose": "Find related local documents.",
                },
                {
                    "kind": "semantic",
                    "query": '引用 "Alpha" と Path.v1\\仕様',
                    "purpose": "Find related local documents.",
                },
            ],
            "inferred_concepts": [
                {
                    "term": "A2L",
                    "confidence": "medium",
                    "semantic_only": True,
                }
            ],
            "coverage": {},
        }
        try:
            module = load_installed_search_request_module(self.rag_root)
            normalized_json, normalized_argv = normalize_structured_pair(
                module,
                request,
            )
        except Exception as exc:
            self.gate(
                "structured_request_equivalence",
                "FAIL",
                f"normalization_error={type(exc).__name__}:{exc}",
            )
            return
        normalized_equal = normalized_json == normalized_argv
        expected_normalized = bool(
            normalized_equal
            and normalized_json.get("original_question") == question
            and normalized_json.get("answer_goal") == "evidence"
            and normalized_json.get("literal_identifiers")
            == literal_identifiers
            and normalized_json.get("entities") == request["entities"]
            and normalized_json.get("facets")
            == normalized_argv.get("facets")
            and all(
                concept.get("semantic_only") is True
                for concept in normalized_json.get("inferred_concepts") or []
            )
            and (normalized_json.get("coverage") or {}).get("policy")
            == "wide"
        )
        self.warm_generation(
            "structured-contract-warm",
            db_name=self.databases[0],
        )
        case = ClientCase(
            case_id="STRUCTURED-ARGV-001",
            db=self.databases[0],
            profile="H",
            question=question,
            expected_identifiers=tuple(literal_identifiers),
        )
        argv_row = self.run_client(
            case,
            phase="structured-contract",
            extra_args=structured_request_arguments(request),
        )
        stdin_bytes = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        json_row = self.run_client(
            ClientCase(
                case_id="STRUCTURED-JSON-002",
                db=self.databases[0],
                profile="H",
                question=question,
                expected_identifiers=tuple(literal_identifiers),
            ),
            phase="structured-contract",
            extra_args=("--request-json", "--stdin"),
            stdin_data=stdin_bytes,
            omit_question=True,
        )
        rows = [argv_row, json_row]
        behavior_argv = retrieval_contract_projection(
            argv_row.get("_payload") or {}
        )
        behavior_json = retrieval_contract_projection(
            json_row.get("_payload") or {}
        )
        behavior_equal = behavior_argv == behavior_json
        generations_equal = bool(
            argv_row.get("manager_generation")
            and argv_row.get("manager_generation")
            == json_row.get("manager_generation")
            and argv_row.get("worker_generation")
            and argv_row.get("worker_generation")
            == json_row.get("worker_generation")
        )
        question_unchanged = all(
            str((row.get("_payload") or {}).get("query") or "") == question
            for row in rows
        )
        db_equal = all(
            str((row.get("_payload") or {}).get("selected_db") or "")
            == self.databases[0]
            for row in rows
        )
        exact_safe = all(
            int(row.get("exact_candidate_count") or 0) == 0
            and int(row.get("evidence_exact_signal_count") or 0) == 0
            and int(row.get("background_exact_signal_count") or 0) == 0
            and set(literal_identifiers)
            <= set(row.get("unmatched_identifiers") or [])
            for row in rows
        )
        json_pure = all(
            row.get("stdout_json_valid")
            and int(row.get("stdout_bytes") or 0) <= 16_384
            for row in rows
        )
        runtime_ok = all(row.get("result") == "PASS" for row in rows)
        passed = bool(
            expected_normalized
            and behavior_equal
            and generations_equal
            and question_unchanged
            and db_equal
            and exact_safe
            and json_pure
            and runtime_ok
        )
        detail = (
            f"contract_search_calls=2, warmup_search_calls=1, "
            f"normalized_equal={normalized_equal}, "
            f"normalized_complete={expected_normalized}, "
            f"behavior_equal={behavior_equal}, "
            f"same_generation={generations_equal}, "
            f"question_unchanged={question_unchanged}, db_equal={db_equal}, "
            f"exact_safe={exact_safe}, json_pure={json_pure}, "
            f"runtime={runtime_ok}"
        )
        self.artifacts.append(
            "event",
            {
                "phase": "structured-contract",
                "event": "equivalence",
                "normalized_argv": normalized_argv,
                "normalized_json": normalized_json,
                "behavior_argv": behavior_argv,
                "behavior_json": behavior_json,
                "result": "PASS" if passed else "FAIL",
            },
        )
        self.gate(
            "structured_request_equivalence",
            "PASS" if passed else "FAIL",
            detail,
        )

    def phase_lifecycle(self, cycles: int = 20) -> None:
        failures = 0
        for index in range(cycles):
            self.shutdown_and_verify(f"lifecycle-pre-{index + 1}")
            row = self.run_client(
                make_case("lifecycle", self.databases[0], "H", index),
                phase="lifecycle-20",
            )
            shutdown = self.shutdown_and_verify(
                f"lifecycle-post-{index + 1}"
            )
            if row["result"] != "PASS" or shutdown["result"] != "PASS":
                failures += 1
                self.safety_stop = True
                break
        completed = index + 1 if cycles else 0
        self.gate(
            "lifecycle_20",
            "PASS"
            if cycles == 20 and completed == 20 and failures == 0
            else "FAIL",
            f"completed={completed}/20, requested={cycles}, failures={failures}",
        )

    def phase_clients(self, count: int = 100) -> None:
        self.shutdown_and_verify("clients-100-pre")
        warm = self.warm_generation("clients-100-warmup")
        baseline = self.sample_resources(
            phase="clients-100",
            sample_index=0,
            requests_completed=0,
        )
        rows = []
        client_pids: list[int] = []
        for index in range(count):
            db = self.databases[index % len(self.databases)]
            profile = ("H", "L", "V")[index % 3]
            row = self.run_client(
                make_case("client100", db, profile, index),
                phase="clients-100",
            )
            rows.append(row)
            client_pids.append(int(row["client_process_pid"]))
            if (index + 1) % 20 == 0:
                self.sample_resources(
                    phase="clients-100",
                    sample_index=(index + 1) // 20,
                    requests_completed=index + 1,
                )
            if row["result"] != "PASS":
                self.safety_stop = True
                break
        final = self.sample_resources(
            phase="clients-100",
            sample_index=99,
            requests_completed=len(rows),
        )
        identity = stable_identity(rows)
        clients_gone = all(not process_is_alive(pid) for pid in client_pids)
        passed = (
            count == 100
            and len(rows) == 100
            and all(row["result"] == "PASS" for row in rows)
            and identity
            and clients_gone
            and final.get("manager_pid") == baseline.get("manager_pid")
            and final.get("worker_pid") == baseline.get("worker_pid")
            and final.get("model_load_count") == 1
            and warm["row"]["result"] == "PASS"
        )
        self.gate(
            "clients_100",
            "PASS" if passed else "FAIL",
            f"completed={len(rows)}/100, requested={count}, stable_identity={identity}, "
            f"clients_gone={clients_gone}",
        )

    def phase_concurrency(self, per_client: int = 20) -> None:
        self.shutdown_and_verify("cold-c4-pre")
        cold_cases = [
            make_case("cold-c4", self.databases[i % len(self.databases)], "H", i)
            for i in range(4)
        ]
        cold = self.run_concurrent(cold_cases, phase="cold-c4")
        cold_identity = stable_identity(cold)
        cold_pass = all(row["result"] == "PASS" for row in cold)
        health = self.health()
        cold_pass = bool(
            cold_pass
            and cold_identity
            and health
            and health.get("model_load_count") == 1
        )
        self.gate(
            "cold_concurrency_4",
            "PASS" if cold_pass else "FAIL",
            f"success={sum(row['result'] == 'PASS' for row in cold)}/4, "
            f"stable_identity={cold_identity}",
        )
        if not cold_pass:
            self.safety_stop = True
            return
        all_rows: list[dict[str, Any]] = []
        for concurrency in (2, 4):
            for iteration in range(per_client):
                cases = [
                    make_case(
                        f"warm-c{concurrency}",
                        self.databases[(iteration + client) % len(self.databases)],
                        ("H", "L", "V")[(iteration + client) % 3],
                        iteration * concurrency + client,
                    )
                    for client in range(concurrency)
                ]
                rows = self.run_concurrent(
                    cases,
                    phase=f"warm-c{concurrency}",
                )
                all_rows.extend(rows)
                if any(row["result"] != "PASS" for row in rows):
                    self.safety_stop = True
                    break
            if self.safety_stop:
                break
        expected = per_client * 2 + per_client * 4
        passed = (
            per_client == 20
            and len(all_rows) == 120
            and all(row["result"] == "PASS" for row in all_rows)
            and stable_identity(cold + all_rows)
        )
        self.gate(
            "warm_concurrency_2_4",
            "PASS" if passed else "FAIL",
            f"completed={len(all_rows)}/120, requested={expected}, "
            f"stable_identity={stable_identity(cold + all_rows)}",
        )

    def phase_db_release(self) -> None:
        failures: list[str] = []
        for index, db_name in enumerate(self.databases):
            self.shutdown_and_verify(f"db-release-{db_name}-pre")
            warm = self.warm_generation(
                f"db-release-{db_name}-warm",
                db_name=db_name,
            )
            state = self.read_state()
            health = self.health(state=state)
            old_worker = int((health or {}).get("worker_pid") or 0)
            lease_id = uuid.uuid4().hex
            release = self.control(
                "release-db",
                {"db": db_name, "lease_id": lease_id},
                timeout=25.0,
                state=state,
            )
            worker_gone = wait_process_gone(old_worker, 5.0)
            release_ready = bool(
                warm["row"]["result"] == "PASS"
                and release
                and release.get("status") == "db_released"
                and worker_gone
            )
            dbs_root = self.dbs_root.resolve(strict=True)
            db_candidate = self.dbs_root / db_name
            if Path(db_name).name != db_name or db_candidate.is_symlink():
                failures.append(f"{db_name}:unsafe_db_name_or_symlink")
                self.safety_stop = True
                break
            db_root = db_candidate.resolve(strict=True)
            if db_root.parent != dbs_root or db_root.name != db_name:
                failures.append(f"{db_name}:unsafe_db_root:{db_root}")
                self.safety_stop = True
                break
            catalog = db_root / "catalog.sqlite"
            index_dir = db_root / "index"
            suffix = f".codex-fulltest-{uuid.uuid4().hex}-{index}"
            moved: list[tuple[Path, Path]] = []
            rename_ok = release_ready
            try:
                if not release_ready:
                    raise RuntimeError(
                        "release_db was not acknowledged or worker is alive"
                    )
                for original in (catalog, index_dir):
                    if (
                        db_root not in original.parents
                        or not original.exists()
                        or ".codex-fulltest-" in original.name
                    ):
                        raise RuntimeError(f"unsafe DB test path: {original}")
                    temporary = original.with_name(original.name + suffix)
                    if temporary.exists():
                        raise RuntimeError(f"temporary path exists: {temporary}")
                    original.replace(temporary)
                    moved.append((original, temporary))
            except Exception as exc:
                rename_ok = False
                failures.append(f"{db_name}:rename:{type(exc).__name__}:{exc}")
            finally:
                for original, temporary in reversed(moved):
                    try:
                        temporary.replace(original)
                    except Exception as exc:
                        failures.append(
                            f"{db_name}:restore:{type(exc).__name__}:{exc}"
                        )
                        self.safety_stop = True
            restored = catalog.exists() and index_dir.exists()
            resume = (
                self.control(
                    "resume-db",
                    {"db": db_name, "lease_id": lease_id},
                    timeout=10.0,
                    state=state,
                )
                if restored
                else None
            )
            old_manager = int((health or {}).get("manager_pid") or 0)
            manager_gone = (
                wait_process_gone(old_manager, 12.0) if restored else False
            )
            next_row = (
                self.run_client(
                    make_case("db-release-next", db_name, "H", index),
                    phase="db-release",
                )
                if restored
                else {"result": "NOT_RUN"}
            )
            case_pass = bool(
                warm["row"]["result"] == "PASS"
                and release
                and release.get("status") == "db_released"
                and worker_gone
                and rename_ok
                and restored
                and resume
                and resume.get("status") == "db_resumed"
                and manager_gone
                and next_row["result"] == "PASS"
            )
            if not case_pass:
                failures.append(
                    f"{db_name}:release={release},worker_gone={worker_gone},"
                    f"rename={rename_ok},restored={restored},resume={resume},"
                    f"manager_gone={manager_gone},next={next_row['result']}"
                )
            self.artifacts.append(
                "event",
                {
                    "phase": "db-release",
                    "event": "release_rename_restore",
                    "db": db_name,
                    "release_status": (release or {}).get("status"),
                    "worker_gone_after_ack": worker_gone,
                    "rename_ok": rename_ok,
                    "restored": restored,
                    "resume_status": (resume or {}).get("status"),
                    "manager_gone": manager_gone,
                    "next_search": next_row["result"],
                    "result": "PASS" if case_pass else "FAIL",
                },
            )
            if self.safety_stop:
                break
        self.gate(
            "db_release_all",
            "PASS" if not failures and not self.safety_stop else "FAIL",
            "; ".join(failures)[:2000],
        )

    def phase_crash_recovery(self) -> None:
        failures: list[str] = []
        self.shutdown_and_verify("crash-pre")
        self.warm_generation("crash-warm")

        # Terminate clients shortly after direct launch. The manager must stay
        # alive and a subsequent normal client must succeed.
        for index, delay in enumerate((0.0, 0.05, 0.25, 0.75)):
            process = self.spawn_client(
                make_case("client-kill", self.databases[0], "V", index)
            )
            time.sleep(delay)
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
            healthy = self.run_client(
                make_case("client-kill-health", self.databases[1], "L", index),
                phase="client-crash",
            )
            if healthy["result"] != "PASS":
                failures.append(f"client_kill_{index}")
        self.gate(
            "client_crash_recovery",
            "PASS" if not failures else "FAIL",
            ",".join(failures),
        )

        # Kill the worker. One in-flight request may receive worker_crashed;
        # the following request must create exactly one replacement.
        before = self.health()
        old_worker = self.validated_daemon_pid(before, "worker")
        old_manager = self.validated_daemon_pid(before, "manager")
        worker_killed = bool(
            before
            and old_worker > 0
            and old_manager > 0
            and terminate_pid(old_worker)
        )
        wait_process_gone(old_worker, 5)
        probe = self.run_client(
            make_case("worker-kill-probe", self.databases[0], "H", 0),
            phase="worker-crash",
        )
        if probe["result"] != "PASS":
            probe = self.run_client(
                make_case("worker-kill-retry", self.databases[0], "H", 1),
                phase="worker-crash",
            )
        after = self.health()
        worker_recovered = bool(
            worker_killed
            and probe["result"] == "PASS"
            and after
            and int(after.get("manager_pid") or 0) == old_manager
            and int(after.get("worker_pid") or 0) not in {0, old_worker}
            and after.get("model_load_count") in {0, 1}
        )
        self.gate(
            "worker_exit_recovery",
            "PASS" if worker_recovered else "FAIL",
            f"old={old_worker}, new={(after or {}).get('worker_pid')}, "
            f"probe={probe['result']}",
        )

        # Suspend the worker to force a real execution timeout and manager-side
        # reap. This remains a QA-only OS action; no product fault hook exists.
        before_hang = self.health()
        hung_worker = self.validated_daemon_pid(before_hang, "worker")
        hung_manager = self.validated_daemon_pid(before_hang, "manager")
        suspended = bool(before_hang and hung_worker > 0 and suspend_pid(hung_worker))
        hang_row = (
            self.run_client(
                make_case("worker-hang", self.databases[0], "H", 0),
                phase="worker-hang",
            )
            if suspended
            else {"elapsed_seconds": 0.0, "response_status": "", "error_kind": ""}
        )
        if suspended and process_is_alive(hung_worker):
            resume_pid(hung_worker)
            terminate_pid(hung_worker)
        hung_reaped = suspended and wait_process_gone(hung_worker, 8)
        next_row = (
            self.run_client(
                make_case("worker-hang-health", self.databases[0], "L", 0),
                phase="worker-hang",
            )
            if suspended
            else {"result": "NOT_RUN", "manager_pid": None, "worker_pid": None}
        )
        hang_pass = bool(
            suspended
            and hang_row["elapsed_seconds"] <= self.deadline_seconds
            and hung_reaped
            and next_row["result"] == "PASS"
            and int(next_row.get("manager_pid") or 0) == hung_manager
            and int(next_row.get("worker_pid") or 0) not in {0, hung_worker}
        )
        self.gate(
            "worker_hang_recovery",
            "PASS" if hang_pass else "FAIL",
            f"suspended={suspended}, timeout_row={hang_row['response_status']}/"
            f"{hang_row['error_kind']}, reaped={hung_reaped}, "
            f"next={next_row['result']}",
        )

        # Manager crash proves Job Object ownership by observing the known
        # worker PID disappear without terminating it directly.
        before_manager = self.health()
        manager_pid = self.validated_daemon_pid(before_manager, "manager")
        worker_pid = self.validated_daemon_pid(before_manager, "worker")
        job_active = bool(
            (before_manager or {}).get("worker_job_object_active")
        )
        manager_killed = bool(
            before_manager
            and job_active
            and manager_pid > 0
            and worker_pid > 0
            and terminate_pid(manager_pid)
        )
        manager_gone = manager_killed and wait_process_gone(manager_pid, 8)
        worker_gone = manager_killed and wait_process_gone(worker_pid, 8)
        next_generation = (
            self.run_client(
                make_case("manager-kill-health", self.databases[0], "H", 0),
                phase="manager-crash",
            )
            if manager_killed and worker_gone
            else {"result": "NOT_RUN", "manager_pid": None, "worker_pid": None}
        )
        manager_pass = bool(
            job_active
            and manager_killed
            and manager_gone
            and worker_gone
            and next_generation["result"] == "PASS"
            and int(next_generation.get("manager_pid") or 0) != manager_pid
            and int(next_generation.get("worker_pid") or 0) != worker_pid
        )
        self.gate(
            "manager_job_recovery",
            "PASS" if manager_pass else "FAIL",
            f"job={job_active}, manager_gone={manager_gone}, "
            f"worker_gone={worker_gone}, next={next_generation['result']}",
        )
        if any(
            self.gates[name]["result"] == "FAIL"
            for name in (
                "client_crash_recovery",
                "worker_exit_recovery",
                "worker_hang_recovery",
                "manager_job_recovery",
            )
        ):
            self.safety_stop = True

    def phase_soak(self, total: int = 200, concurrency: int = 4) -> None:
        if (
            total != 200
            or concurrency != 4
            or total % 20
            or 20 % concurrency
        ):
            self.gate(
                "soak_200_c4",
                "FAIL",
                "release gate requires total=200 and concurrency=4",
            )
            return
        self.shutdown_and_verify("soak-pre")
        warm = self.warm_generation("soak-warmup")
        # Establish the steady-state three-DB cache before recording the
        # resource baseline. Opening Chroma/SQLite for DBs two and three can
        # legitimately create native handles and threads; counting that
        # one-time capacity as a leak would be a false failure.
        cache_warm_rows = [
            self.run_client(
                make_case("soak-cache-warm", db_name, "H", index),
                phase="soak-cache-warm",
            )
            for index, db_name in enumerate(self.databases)
        ]
        baseline_samples = [
            self.sample_resources(
                phase="soak-200-c4",
                sample_index=index,
                requests_completed=0,
            )
            for index in range(3)
        ]
        baseline_health = warm["health"] or self.health() or {}
        manager_generation = baseline_health.get("manager_generation")
        worker_generation = baseline_health.get("worker_generation")
        rows: list[dict[str, Any]] = []
        bucket_size = 20
        bucket_count = total // bucket_size
        for bucket in range(bucket_count):
            rounds = bucket_size // concurrency
            for round_index in range(rounds):
                offset = bucket * bucket_size + round_index * concurrency
                cases = [
                    make_case(
                        "soak",
                        self.databases[(offset + client) % len(self.databases)],
                        ("H", "L", "V")[(offset + client) % 3],
                        offset + client,
                    )
                    for client in range(concurrency)
                ]
                cohort = self.run_concurrent(
                    cases,
                    phase="soak-200-c4",
                )
                rows.extend(cohort)
                if any(row["result"] != "PASS" for row in cohort):
                    self.safety_stop = True
                    break
            self.sample_resources(
                phase="soak-200-c4",
                sample_index=bucket + 3,
                requests_completed=len(rows),
            )
            if self.safety_stop:
                break
        final = self.sample_resources(
            phase="soak-200-c4",
            sample_index=99,
            requests_completed=len(rows),
        )
        stable = bool(
            rows
            and all(
                row.get("manager_generation") == manager_generation
                and row.get("worker_generation") == worker_generation
                for row in rows
            )
        )
        passed = (
            len(rows) == total
            and all(row["result"] == "PASS" for row in cache_warm_rows)
            and all(row["result"] == "PASS" for row in rows)
            and stable
            and final.get("model_load_count") == 1
        )
        self.gate(
            "soak_200_c4",
            "PASS" if passed else "FAIL",
            f"completed={len(rows)}/{total}, stable_generation={stable}",
        )
        self.evaluate_resource_gates(baseline_samples, self.resources[-11:])

    def evaluate_resource_gates(
        self,
        baseline_samples: list[dict[str, Any]],
        cohort_samples: list[dict[str, Any]],
    ) -> None:
        def baseline(field: str) -> float | None:
            values = [
                float(item[field])
                for item in baseline_samples
                if item.get(field) is not None
            ]
            return statistics.median(values) if values else None

        def values(field: str) -> list[float]:
            return [
                float(item[field])
                for item in cohort_samples
                if item.get(field) is not None
            ]

        for owner, allowance in (("manager", 16), ("worker", 32)):
            field = f"{owner}_handle_count"
            base = baseline(field)
            series = values(field)
            if base is None or not series:
                self.gate(f"resource_{owner}_handles", "NOT_RUN", "missing measurement")
                continue
            limit = base + allowance
            passed = series[-1] <= limit and not is_monotonic_increase(series)
            self.gate(
                f"resource_{owner}_handles",
                "PASS" if passed else "FAIL",
                f"baseline={base}, final={series[-1]}, limit={limit}",
            )
        for owner in ("manager", "worker"):
            field = f"{owner}_thread_count"
            base = baseline(field)
            series = values(field)
            if base is None or not series:
                self.gate(f"resource_{owner}_threads", "NOT_RUN", "missing measurement")
                continue
            limit = base + 4
            passed = series[-1] <= limit and not is_monotonic_increase(series)
            self.gate(
                f"resource_{owner}_threads",
                "PASS" if passed else "FAIL",
                f"baseline={base}, final={series[-1]}, limit={limit}",
            )
        for owner in ("manager", "worker"):
            field = f"{owner}_rss_bytes"
            base_values = values_from(baseline_samples, field)
            series = values(field)
            if not base_values or not series:
                self.gate(f"resource_{owner}_rss", "NOT_RUN", "missing measurement")
                continue
            base = statistics.median(base_values)
            material = (
                series[-1] > base * 1.20
                and series[-1] > base + 200 * 1024 * 1024
            )
            passed = not material and not is_monotonic_increase(series)
            self.gate(
                f"resource_{owner}_rss",
                "PASS" if passed else "FAIL",
                f"baseline={int(base)}, final={int(series[-1])}, "
                f"monotonic={is_monotonic_increase(series)}",
            )

    def phase_overload(self) -> None:
        self.warm_generation("overload-warm")
        cases = [
            make_case(
                "overload-c8",
                self.databases[index % len(self.databases)],
                ("H", "V", "L")[index % 3],
                index,
            )
            for index in range(8)
        ]
        rows = self.run_concurrent(cases, phase="overload-c8")
        acceptable = all(
            row["result"] == "PASS"
            or row["error_kind"] in EXPECTED_OVERLOAD_ERRORS
            for row in rows
        )
        within_deadline = all(
            row["elapsed_seconds"] <= self.deadline_seconds
            for row in rows
        )
        no_mismatch = all(
            row["response_identity_match"]
            or row["error_kind"] in EXPECTED_OVERLOAD_ERRORS
            for row in rows
        )
        healthy = self.run_client(
            make_case("overload-health", self.databases[0], "L", 0),
            phase="overload-c8",
        )
        health = self.health()
        passed = bool(
            acceptable
            and within_deadline
            and no_mismatch
            and all(not row["fallback_used"] for row in rows)
            and healthy["result"] == "PASS"
            and health
            and health.get("model_load_count") == 1
        )
        self.gate(
            "overload_8_safety",
            "PASS" if passed else "FAIL",
            f"success={sum(row['result'] == 'PASS' for row in rows)}/8, "
            f"bounded={sum(row['error_kind'] in EXPECTED_OVERLOAD_ERRORS for row in rows)}, "
            f"healthy={healthy['result']}",
        )

    def phase_exact(self) -> None:
        case_path = (
            self.rag_root
            / "docs"
            / "tests"
            / "data"
            / "exact-cases-v1.jsonl"
        )
        if not case_path.is_file():
            self.gate("exact_30", "NOT_RUN", "missing exact-cases-v1.jsonl")
            return
        failures: list[str] = []
        count = 0
        for raw in case_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            item = json.loads(raw)
            case = ClientCase(
                case_id=str(item["id"]),
                db=str(item["db"]),
                profile="L",
                question=str(item["question"]),
                expected_identifiers=tuple(
                    str(value)
                    for value in item.get("expected_matched_identifiers") or []
                ),
            )
            row = self.run_client(case, phase="exact-30")
            count += 1
            # Compact rows intentionally retain the required diagnostics.
            positive = bool(item.get("expect_exact_positive"))
            negative = bool(item.get("expect_exact_negative"))
            if row["result"] != "PASS":
                failures.append(f"{item['id']}:runtime")
            if positive and not (
                int(row.get("exact_candidate_count") or 0) > 0
                and int(row.get("evidence_count") or 0) > 0
                and int(row.get("document_result_count") or 0) > 0
                and int(
                    row.get("evidence_neighbor_exact_signal_count") or 0
                )
                == 0
                and int(
                    row.get("background_neighbor_exact_signal_count") or 0
                )
                == 0
                and not row.get("unmatched_identifiers")
                and set(item.get("expected_matched_identifiers") or [])
                <= set(row.get("raw_verified_identifiers") or [])
            ):
                failures.append(f"{item['id']}:positive")
            if negative and not (
                int(row.get("exact_candidate_count") or 0) == 0
                and int(row.get("evidence_exact_signal_count") or 0) == 0
                and int(row.get("background_exact_signal_count") or 0) == 0
                and int(row.get("evidence_count") or 0) == 0
                and set(item.get("expected_unmatched_identifiers") or [])
                <= set(row.get("unmatched_identifiers") or [])
            ):
                failures.append(f"{item['id']}:negative")
            if row["duplicate_document_paths"]:
                failures.append(f"{item['id']}:duplicate_path")
            if row["stdout_bytes"] > 16_384:
                failures.append(f"{item['id']}:compact_size")
        self.gate(
            "exact_30",
            "PASS" if count == 30 and not failures else "FAIL",
            f"completed={count}/30, failures={','.join(failures)[:1000]}",
        )

    def phase_broad(self) -> None:
        case_path = (
            self.rag_root
            / "docs"
            / "tests"
            / "data"
            / "broad-search-cases-v1.jsonl"
        )
        if not case_path.is_file():
            self.gate(
                "broad_search_18",
                "NOT_RUN",
                "missing broad-search-cases-v1.jsonl",
            )
            return
        failures: list[str] = []
        completed = 0
        useful_counts: list[int] = []
        distinct_counts: list[int] = []
        for raw in case_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            item = json.loads(raw)
            if item.get("applicability") != "APPLICABLE":
                failures.append(f"{item.get('id')}:not_applicable")
                continue
            request_payload = dict(item["request"])
            extras = structured_request_arguments(request_payload)
            expectation = item.get("identifier_expectation") or {}
            case = ClientCase(
                case_id=str(item["id"]),
                db=str(item["db"]),
                profile="H",
                question=str(item["question"]),
                expected_identifiers=tuple(
                    str(value)
                    for value in expectation.get("identifiers") or []
                ),
            )
            row = self.run_client(
                case,
                phase="broad-18",
                extra_args=extras,
            )
            payload = row.get("_payload") or {}
            cards = [
                card
                for card in payload.get("document_results") or []
                if isinstance(card, dict)
            ][:8]
            reviewed = {
                normalize_document_path(value["path"]): value
                for value in item.get("reviewed_documents") or []
            }
            returned = [
                normalize_document_path(card.get("path"))
                for card in cards
                if card.get("path")
            ]
            distinct = len(set(returned))
            useful = sum(path in reviewed for path in returned)
            noise = sum(path not in reviewed for path in returned)
            useful_counts.append(useful)
            distinct_counts.append(distinct)
            coverage = payload.get("coverage") or {}
            facets_requested = int(coverage.get("facets_requested") or 0)
            facets_covered = int(coverage.get("facets_covered") or 0)
            aspect_coverage = (
                min(1.0, facets_covered / facets_requested)
                if facets_requested > 0
                else 0.0
            )
            top3_has_direct = any(
                int((reviewed.get(path) or {}).get("grade") or 0) >= 2
                for path in returned[:3]
            )
            calibration_ok = True
            for card, path in zip(cards, returned):
                grade = int((reviewed.get(path) or {}).get("grade") or 0)
                support = str(card.get("support_level") or "")
                authoritative = bool(card.get("authoritative"))
                if not support_level_is_calibrated(
                    grade=grade,
                    support=support,
                    authoritative=authoritative,
                    is_evidence=path
                    in {
                        normalize_document_path(value)
                        for value in row.get("evidence_paths") or []
                    },
                ):
                    calibration_ok = False
            identifier_ok = True
            expected_exact = expectation.get("expect_verified_exact")
            if expected_exact is True:
                identifier_ok = bool(
                    int(row.get("exact_candidate_count") or 0) > 0
                    and int(row.get("evidence_count") or 0) > 0
                    and int(row.get("document_result_count") or 0) > 0
                    and int(
                        row.get("evidence_neighbor_exact_signal_count") or 0
                    )
                    == 0
                    and int(
                        row.get("background_neighbor_exact_signal_count") or 0
                    )
                    == 0
                    and not row.get("unmatched_identifiers")
                    and set(expectation.get("identifiers") or [])
                    <= set(row.get("raw_verified_identifiers") or [])
                )
            elif expected_exact is False:
                identifier_ok = bool(
                    int(row.get("exact_candidate_count") or 0) == 0
                    and int(row.get("evidence_exact_signal_count") or 0) == 0
                    and int(row.get("background_exact_signal_count") or 0)
                    == 0
                    and int(row.get("evidence_count") or 0) == 0
                    and set(
                        expectation.get("expected_unmatched_identifiers")
                        or []
                    )
                    <= set(row.get("unmatched_identifiers") or [])
                )
            case_ok = bool(
                row["result"] == "PASS"
                and row["stdout_bytes"] <= 16_384
                and not row["duplicate_document_paths"]
                and calibration_ok
                and identifier_ok
            )
            if item.get("has_six_useful"):
                case_ok = bool(
                    case_ok
                    and distinct >= 6
                    and useful >= 5
                    and noise <= 2
                    and top3_has_direct
                    and aspect_coverage >= 0.60
                )
            if item.get("one_document_control"):
                case_ok = bool(
                    case_ok
                    and useful == 1
                    and noise == 0
                    and "insufficient_distinct_related_documents"
                    in set(row.get("warnings") or [])
                )
            if not case_ok:
                failures.append(
                    f"{item['id']}:runtime={row['result']},distinct={distinct},"
                    f"useful={useful},noise={noise},aspect={aspect_coverage:.2f},"
                    f"calibration={calibration_ok},identifier={identifier_ok},"
                    f"bytes={row['stdout_bytes']}"
                )
            self.artifacts.append(
                "event",
                {
                    "phase": "broad-18",
                    "event": "quality_metrics",
                    "case_id": item["id"],
                    "distinct_doc_at_8": distinct,
                    "useful_doc_at_8": useful,
                    "noise_doc_at_8": noise,
                    "aspect_coverage_at_8": round(aspect_coverage, 6),
                    "top3_has_direct_or_strong": top3_has_direct,
                    "support_calibration": calibration_ok,
                    "identifier_contract": identifier_ok,
                    "result": "PASS" if case_ok else "FAIL",
                },
            )
            completed += 1
        self.gate(
            "broad_search_18",
            "PASS" if completed == 18 and not failures else "FAIL",
            f"completed={completed}/18, median_distinct="
            f"{statistics.median(distinct_counts) if distinct_counts else 'NA'},"
            f" median_useful="
            f"{statistics.median(useful_counts) if useful_counts else 'NA'}, "
            f"failures={';'.join(failures)[:2000]}",
        )

    def finalize(self) -> dict[str, Any]:
        required = sorted(self.required_gates or set(self.gates))
        for name in required:
            if name not in self.gates:
                self.gate(name, "NOT_RUN", "required gate was not executed")
        overall = (
            "FAIL"
            if any(self.gates[name]["result"] == "FAIL" for name in required)
            else "NOT_RUN"
            if any(self.gates[name]["result"] == "NOT_RUN" for name in required)
            else "PASS"
            if required
            else "NOT_RUN"
        )
        summary = {
            "schema": "local-rag.persistent-daemon-summary.v1",
            "run_id": self.run_id,
            "platform": sys.platform,
            "python": str(self.python),
            "installed_rag": str(self.rag_root),
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "overall": overall,
            "gates": self.gates,
            "case_count": len(self.rows),
            "failure_count": sum(row["result"] == "FAIL" for row in self.rows),
            "json_parse_errors": sum(
                not row["stdout_json_valid"] for row in self.rows
            ),
            "fallback_count": sum(row["fallback_used"] for row in self.rows),
            "identity_mismatches": sum(
                not row["response_identity_match"]
                and row["error_kind"] not in EXPECTED_OVERLOAD_ERRORS
                for row in self.rows
            ),
            "max_elapsed_seconds": max(
                (row["elapsed_seconds"] for row in self.rows),
                default=None,
            ),
            "max_stdout_bytes": max(
                (row["stdout_bytes"] for row in self.rows),
                default=None,
            ),
            "semantic_gate_note": (
                "The historical frozen Semantic v2 gate remains FAIL. "
                "A new unseen holdout is still required for a stable release."
            ),
        }
        self.artifacts.write_summary(summary)
        return summary


def make_case(prefix: str, db: str, profile: str, index: int) -> ClientCase:
    return ClientCase(
        case_id=f"{prefix}-{index + 1:04d}",
        db=db,
        profile=profile,
        question=QUESTIONS[db][profile],
    )


def structured_request_arguments(request: dict[str, Any]) -> list[str]:
    arguments = [
        "--answer-goal",
        str(request.get("answer_goal") or "evidence"),
    ]
    for value in request.get("literal_identifiers") or []:
        arguments.extend(["--literal-identifier", str(value)])
    for value in request.get("entities") or []:
        arguments.extend(["--entity", str(value)])
    for facet in request.get("facets") or []:
        query = (
            facet.get("query")
            if isinstance(facet, dict)
            else facet
        )
        if query:
            arguments.extend(["--facet", str(query)])
    for concept in request.get("inferred_concepts") or []:
        term = (
            concept.get("term")
            if isinstance(concept, dict)
            else concept
        )
        if term:
            arguments.extend(["--semantic-hypothesis", str(term)])
    return arguments


def load_installed_search_request_module(rag_root: Path) -> ModuleType:
    module_path = (
        rag_root
        / "gen_db"
        / "software_rag_tool"
        / "software_rag_tool"
        / "search_request.py"
    )
    if not module_path.is_file():
        raise FileNotFoundError(
            f"missing installed structured request module: {module_path}"
        )
    name = f"_rag_search_request_contract_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load structured request module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_structured_pair(
    module: ModuleType,
    request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    empty = {
        "literal_facet": [],
        "semantic_facet": [],
        "coverage_policy": None,
        "coverage_target": None,
        "coverage_minimum": None,
        "coverage_maximum": None,
        "coverage_max_chunks": None,
        "coverage_allow_weak": None,
    }
    json_args = SimpleNamespace(
        request_json=True,
        stdin=True,
        answer_goal=None,
        literal_identifier=[],
        entity=[],
        facet=[],
        semantic_hypothesis=[],
        **empty,
    )
    normalized_json = module.request_from_cli(
        json_args,
        positional_question="",
        stdin_text=json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    argv_args = SimpleNamespace(
        request_json=False,
        stdin=False,
        answer_goal=str(request.get("answer_goal") or "evidence"),
        literal_identifier=list(request.get("literal_identifiers") or []),
        entity=list(request.get("entities") or []),
        facet=[
            str(
                facet.get("query")
                if isinstance(facet, dict)
                else facet
            )
            for facet in request.get("facets") or []
        ],
        semantic_hypothesis=[
            str(
                concept.get("term")
                if isinstance(concept, dict)
                else concept
            )
            for concept in request.get("inferred_concepts") or []
        ],
        **empty,
    )
    normalized_argv = module.request_from_cli(
        argv_args,
        positional_question=str(request["original_question"]),
    )
    return normalized_json, normalized_argv


def retrieval_contract_projection(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = [
        item
        for item in payload.get("evidence") or []
        if isinstance(item, dict)
    ]
    documents = [
        item
        for item in payload.get("document_results") or []
        if isinstance(item, dict)
    ]
    coverage = payload.get("coverage") or {}
    return {
        "status": payload.get("status"),
        "answerability": payload.get("answerability"),
        "selected_db": payload.get("selected_db") or payload.get("db"),
        "unmatched_identifiers": list(
            payload.get("unmatched_identifiers") or []
        ),
        "exact_candidate_count": int(
            payload.get("exact_candidate_count") or 0
        ),
        "evidence": [
            {
                "id": item.get("id"),
                "path": (item.get("source") or {}).get("path"),
                "signals": list(item.get("signals") or []),
            }
            for item in evidence
        ],
        "documents": [
            {
                "path": item.get("path"),
                "support_level": item.get("support_level"),
            }
            for item in documents
        ],
        "coverage": {
            "policy": coverage.get("policy"),
            "returned_distinct_documents": coverage.get(
                "returned_distinct_documents"
            ),
        },
        "dense_used": payload.get("dense_used"),
        "dense_skipped_reason": payload.get("dense_skipped_reason"),
    }


def normalize_document_path(value: object) -> str:
    return str(value or "").replace("\\", "/").casefold()


def duplicate_document_paths(payload: dict[str, Any]) -> list[str]:
    paths = [
        str(item.get("path") or "")
        .replace("\\", "/")
        .casefold()
        for item in payload.get("document_results") or []
        if isinstance(item, dict) and item.get("path")
    ]
    return sorted({path for path in paths if paths.count(path) > 1})


def raw_identifier_occurs(
    identifier: str,
    evidence_text: str,
    source_path: str,
) -> bool:
    if not identifier:
        return False
    # Exact verification must reject lossy prefixes such as RFC10002 in
    # RFC10002X. Punctuation remains part of the literal; only adjacent
    # identifier characters invalidate the occurrence.
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(identifier)}"
        rf"(?![A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )
    return bool(
        pattern.search(evidence_text)
        or pattern.search(source_path.replace("\\", "/"))
    )


def support_level_is_calibrated(
    *,
    grade: int,
    support: str,
    authoritative: bool,
    is_evidence: bool,
) -> bool:
    if support == "direct" and grade != 3:
        return False
    if grade <= 1 and support in {"direct", "strong"}:
        return False
    if authoritative and (support != "direct" or not is_evidence):
        return False
    return True


def stable_identity(rows: list[dict[str, Any]]) -> bool:
    usable = [
        row
        for row in rows
        if row.get("result") == "PASS"
    ]
    if not usable:
        return False
    identity_fields = (
        "manager_pid",
        "worker_pid",
        "manager_generation",
        "worker_generation",
    )
    if any(not row.get(field) for row in usable for field in identity_fields):
        return False
    return (
        len({row.get("manager_pid") for row in usable}) == 1
        and len({row.get("worker_pid") for row in usable}) == 1
        and len({row.get("manager_generation") for row in usable}) == 1
        and len({row.get("worker_generation") for row in usable}) == 1
        and all(row.get("model_load_count") in {0, 1} for row in usable)
    )


def values_from(
    rows: list[dict[str, Any]],
    field: str,
) -> list[float]:
    return [
        float(row[field])
        for row in rows
        if row.get(field) is not None
    ]


def wait_path_gone(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return not path.exists()


def wait_process_gone(pid: int, timeout: float) -> bool:
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    while process_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not process_is_alive(pid)


def wait_port_closed(port: int, timeout: float) -> bool:
    if port <= 0:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.1)
            if client.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.05)
    return False


def exclusive_lock_probe(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, b"full-test-probe\n")
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)
    return True


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(
            handle,
            ctypes.byref(exit_code),
        ):
            return False
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def process_executable(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name != "nt":
        if pid == os.getpid():
            return sys.executable
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(capacity),
        ):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def terminate_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 9)
            return True
        except OSError:
            return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        return not process_is_alive(pid)
    try:
        return bool(kernel32.TerminateProcess(handle, 137))
    finally:
        kernel32.CloseHandle(handle)


def suspend_pid(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll.NtSuspendProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtSuspendProcess.restype = wintypes.LONG
    handle = kernel32.OpenProcess(0x0800, False, pid)
    if not handle:
        return False
    try:
        return ntdll.NtSuspendProcess(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def resume_pid(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = wintypes.LONG
    handle = kernel32.OpenProcess(0x0800, False, pid)
    if not handle:
        return False
    try:
        return ntdll.NtResumeProcess(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def process_metrics(pid: int) -> dict[str, int | None]:
    result: dict[str, int | None] = {
        "rss_bytes": None,
        "handle_count": None,
        "thread_count": None,
    }
    if os.name != "nt" or pid <= 0:
        return result

    class ProcessMemoryCounters(ctypes.Structure):
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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000 | 0x0400, False, pid)
    if handle:
        try:
            count = wintypes.DWORD()
            if kernel32.GetProcessHandleCount(
                handle,
                ctypes.byref(count),
            ):
                result["handle_count"] = int(count.value)
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(counters),
                counters.cb,
            ):
                result["rss_bytes"] = int(counters.WorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)
    result["thread_count"] = native_thread_count(pid)
    return result


def native_thread_count(pid: int) -> int | None:
    if os.name != "nt" or pid <= 0:
        return None

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    )
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    )
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    invalid = ctypes.c_void_p(-1).value
    if snapshot == invalid:
        return None
    count = 0
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Thread32First(
            snapshot,
            ctypes.byref(entry),
        )
        while more:
            if int(entry.th32OwnerProcessID) == pid:
                count += 1
            more = kernel32.Thread32Next(
                snapshot,
                ctypes.byref(entry),
            )
    finally:
        kernel32.CloseHandle(snapshot)
    return count


def established_tcp_count(
    manager_pid: int,
    worker_pid: int,
    port: int,
) -> int | None:
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            ["netstat.exe", "-ano", "-p", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = {str(pid) for pid in (manager_pid, worker_pid) if pid > 0}
    marker = f":{port}" if port > 0 else ""
    return sum(
        1
        for line in completed.stdout.splitlines()
        if "ESTABLISHED" in line.upper()
        and (
            line.split()[-1] in pids
            or (marker and marker in line)
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=(
            "all",
            "structured-contract",
            "lifecycle-20",
            "clients-100",
            "concurrency",
            "db-release",
            "crash",
            "soak-200-c4",
            "overload-c8",
            "exact-30",
            "broad-18",
        ),
        default="all",
    )
    parser.add_argument("--installed-rag", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", action="append", dest="databases")
    parser.add_argument("--deadline", type=float, default=15.0)
    parser.add_argument("--lifecycle-cycles", type=int, default=20)
    parser.add_argument("--client-count", type=int, default=100)
    parser.add_argument("--per-client", type=int, default=20)
    parser.add_argument("--soak-total", type=int, default=200)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    databases = tuple(args.databases or DEFAULT_DATABASES)
    unknown = [db for db in databases if db not in QUESTIONS]
    if unknown:
        raise SystemExit(f"unsupported test DBs: {', '.join(unknown)}")
    runner = PersistentDaemonWindowsRunner(
        installed_rag=Path(args.installed_rag),
        output_dir=Path(args.output_dir),
        run_id=args.run_id,
        databases=databases,
        deadline_seconds=args.deadline,
    )
    phases = (
        (
            "structured-contract",
            "lifecycle-20",
            "clients-100",
            "concurrency",
            "db-release",
            "crash",
            "soak-200-c4",
            "overload-c8",
            "exact-30",
            "broad-18",
        )
        if args.phase == "all"
        else (args.phase,)
    )
    runner.required_gates.update(
        name
        for phase in phases
        for name in PHASE_GATE_NAMES[phase]
    )
    for phase in phases:
        print(f"[{utc_now()}] phase={phase}", file=sys.stderr, flush=True)
        if runner.safety_stop:
            for name in PHASE_GATE_NAMES[phase]:
                runner.gate(name, "NOT_RUN", "prior_safety_stop")
            continue
        if phase == "structured-contract":
            runner.phase_structured_contract()
        elif phase == "lifecycle-20":
            runner.phase_lifecycle(args.lifecycle_cycles)
        elif phase == "clients-100":
            runner.phase_clients(args.client_count)
        elif phase == "concurrency":
            runner.phase_concurrency(args.per_client)
        elif phase == "db-release":
            runner.phase_db_release()
        elif phase == "crash":
            runner.phase_crash_recovery()
        elif phase == "soak-200-c4":
            runner.phase_soak(args.soak_total)
        elif phase == "overload-c8":
            runner.phase_overload()
        elif phase == "exact-30":
            runner.phase_exact()
        elif phase == "broad-18":
            runner.phase_broad()
    summary = runner.finalize()
    print(
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    return 0 if summary["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
