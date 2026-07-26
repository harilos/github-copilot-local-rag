from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RAG_ROOT = Path(__file__).resolve().parents[2]
QUERY_ROOT = RAG_ROOT / "query"
DATA_DIR = Path(__file__).resolve().parent / "data"
REPORT_DIR = Path(__file__).resolve().parent

DEFAULT_CASES = [
    {
        "id": "AC_EXACT_NEG_COLLISION_001",
        "db": "ac-rag",
        "class": "exact_negative",
        "question": "A2Wに関する情報を教えて",
        "expected_unmatched": ["A2W"],
    },
    {
        "id": "AC_EXACT_LOWDF_001",
        "db": "ac-rag",
        "class": "exact_positive",
        "question": "A2Lに関する情報を教えて",
        "expected_matched": ["A2L"],
    },
    {
        "id": "AC_SEM_001",
        "db": "ac-rag",
        "class": "semantic",
        "question": "冷房需要が増える背景を資料から説明して",
    },
    {
        "id": "AC_BROAD_001",
        "db": "ac-rag",
        "class": "broad",
        "question": "空調市場で効率化と冷媒転換が重要な理由を複数資料からまとめて",
    },
    {
        "id": "INC_EXACT_001",
        "db": "incident-rag",
        "class": "exact_positive",
        "question": "ntsb_aviation_report_67438 の事故情報を教えて",
    },
    {
        "id": "INC_SEM_001",
        "db": "incident-rag",
        "class": "semantic",
        "question": "離陸後にエンジン故障が起きた事故の原因に関する根拠を探して",
    },
    {
        "id": "INC_BROAD_001",
        "db": "incident-rag",
        "class": "broad",
        "question": "landing gear collapse に関する事故の傾向を資料から拾って",
    },
    {
        "id": "RFC_EXACT_001",
        "db": "rfc-full-20k-rag",
        "class": "exact_positive",
        "question": "RFC10026 の内容を教えて",
    },
    {
        "id": "RFC_SEM_001",
        "db": "rfc-full-20k-rag",
        "class": "semantic",
        "question": "DNSSEC Delegation Signer automation は何を自動化する仕様か",
    },
    {
        "id": "RFC_BROAD_001",
        "db": "rfc-full-20k-rag",
        "class": "broad",
        "question": "QUIC transport protocol の congestion control に関する根拠を探して",
    },
]

PROFILE_TO_MODE = {
    "H": "hybrid",
    "L": "lexical",
    "V": "dense",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--profiles", nargs="+", default=["H", "L", "V"], choices=sorted(PROFILE_TO_MODE))
    parser.add_argument("--daemon-repeats", type=int, default=3)
    parser.add_argument("--no-daemon-repeats", type=int, default=1)
    parser.add_argument("--list-repeats", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--budget-tokens", type=int, default=1200)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--restart-daemon", action="store_true")
    parser.add_argument("--db", action="append", help="Limit execution to one or more DB names.")
    parser.add_argument("--case-id", action="append", help="Limit execution to one or more case IDs.")
    parser.add_argument("--executions", nargs="+", default=["daemon", "no-daemon"], choices=["daemon", "no-daemon"])
    parser.add_argument("--report-only", nargs="+", help="Build a markdown report from existing performance-results JSONL file(s).")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id
    results_path = DATA_DIR / f"performance-results-{run_id}.jsonl"
    report_path = REPORT_DIR / f"performance-report-{run_id}.md"

    if args.report_only:
        sources = [Path(value) for value in args.report_only]
        rows = []
        for source in sources:
            rows.extend(json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip())
        report = build_report(run_id, rows, args)
        report_path.write_text(report, encoding="utf-8")
        print(json.dumps({"run_id": run_id, "results": [str(source) for source in sources], "report": str(report_path)}, ensure_ascii=False, indent=2))
        return

    if args.restart_daemon:
        shutdown_daemon()

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    python = query_python()

    rows: list[dict[str, Any]] = []
    list_rows = run_list_dbs(python, env, repeats=args.list_repeats, timeout=args.timeout)
    rows.extend(list_rows)
    for row in list_rows:
        append_jsonl(results_path, row)

    cases = [
        case
        for case in DEFAULT_CASES
        if (not args.db or case["db"] in set(args.db)) and (not args.case_id or case["id"] in set(args.case_id))
    ]

    if "daemon" in args.executions and args.daemon_repeats > 0 and cases:
        warmup_case = cases[0]
        run_search_case(
            python,
            env,
            warmup_case,
            profile="H",
            execution="daemon",
            repeat=-1,
            args=args,
            warmup=True,
        )

    for execution, repeats in [("daemon", args.daemon_repeats), ("no-daemon", args.no_daemon_repeats)]:
        if execution not in args.executions:
            continue
        for repeat in range(repeats):
            for case in cases:
                for profile in args.profiles:
                    row = run_search_case(
                        python,
                        env,
                        case,
                        profile=profile,
                        execution=execution,
                        repeat=repeat,
                        args=args,
                    )
                    rows.append(row)
                    append_jsonl(results_path, row)

    report = build_report(run_id, rows, args)
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps({"run_id": run_id, "results": str(results_path), "report": str(report_path)}, ensure_ascii=False, indent=2))


def query_python() -> str:
    candidate = QUERY_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    return str(candidate if candidate.exists() else Path(sys.executable))


def run_list_dbs(python: str, env: dict[str, str], *, repeats: int, timeout: int) -> list[dict[str, Any]]:
    rows = []
    cmd = [python, str(QUERY_ROOT / "list_dbs.py")]
    for repeat in range(repeats):
        started = time.perf_counter()
        completed = subprocess.run(cmd, cwd=str(RAG_ROOT), env=env, capture_output=True, text=True, timeout=timeout)
        elapsed = time.perf_counter() - started
        payload, json_ok, parse_error = parse_json(completed.stdout)
        rows.append(
            {
                "kind": "list_dbs",
                "run_at": now(),
                "repeat": repeat,
                "execution": "process",
                "exit_code": completed.returncode,
                "latency_seconds": round(elapsed, 6),
                "json_ok": json_ok,
                "parse_error": parse_error,
                "db_count": len((payload or {}).get("dbs") or []) if isinstance(payload, dict) else None,
                "dbs": [item.get("db") for item in (payload or {}).get("dbs") or []] if isinstance(payload, dict) else [],
                "stdout_prefix": completed.stdout[:80],
                "stderr_tail": completed.stderr[-500:],
            }
        )
    return rows


def run_search_case(
    python: str,
    env: dict[str, str],
    case: dict[str, Any],
    *,
    profile: str,
    execution: str,
    repeat: int,
    args: argparse.Namespace,
    warmup: bool = False,
) -> dict[str, Any]:
    mode = PROFILE_TO_MODE[profile]
    cmd = [
        python,
        str(QUERY_ROOT / "search.py"),
        "--db",
        case["db"],
        "--retrieval-mode",
        mode,
        "--format",
        "json",
        "--explain",
        "--budget-tokens",
        str(args.budget_tokens),
        "--max-chars",
        str(args.max_chars),
        "--top-k",
        str(args.top_k),
        "--timeout",
        str(args.timeout),
    ]
    if execution == "no-daemon":
        cmd.append("--no-daemon")
    cmd.append(case["question"])

    started = time.perf_counter()
    try:
        completed = subprocess.run(cmd, cwd=str(RAG_ROOT), env=env, capture_output=True, text=True, timeout=args.timeout + 5)
        elapsed = time.perf_counter() - started
        payload, json_ok, parse_error = parse_json(completed.stdout)
        if not isinstance(payload, dict):
            payload = {}
        return summarize_search(case, profile, execution, repeat, completed, payload, json_ok, parse_error, elapsed, warmup=warmup)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        return {
            "kind": "search",
            "run_at": now(),
            "warmup": warmup,
            "case_id": case["id"],
            "db": case["db"],
            "class": case["class"],
            "profile": profile,
            "retrieval_mode": mode,
            "execution": execution,
            "repeat": repeat,
            "exit_code": 124,
            "latency_seconds": round(elapsed, 6),
            "json_ok": False,
            "parse_error": f"TimeoutExpired: {exc}",
            "status": "timeout",
        }


def summarize_search(
    case: dict[str, Any],
    profile: str,
    execution: str,
    repeat: int,
    completed: subprocess.CompletedProcess[str],
    payload: dict[str, Any],
    json_ok: bool,
    parse_error: str | None,
    elapsed: float,
    *,
    warmup: bool,
) -> dict[str, Any]:
    evidence = payload.get("evidence") or []
    results = payload.get("results") or []
    top = evidence[0] if evidence else {}
    signals = sorted({signal for item in evidence for signal in item.get("signals", [])})
    exact_signal_count = sum(1 for item in evidence if "exact" in (item.get("signals") or []))
    neighbor_signal_count = sum(1 for item in evidence if "neighbor" in (item.get("signals") or []))
    unmatched = payload.get("unmatched_identifiers") or (payload.get("identifiers") or {}).get("unmatched_identifiers") or []
    exact_candidate_count = payload.get("exact_candidate_count")
    if exact_candidate_count is None:
        exact_candidate_count = (payload.get("identifiers") or {}).get("exact_candidate_count")
    row = {
        "kind": "search",
        "run_at": now(),
        "warmup": warmup,
        "case_id": case["id"],
        "db": case["db"],
        "class": case["class"],
        "question": case["question"],
        "profile": profile,
        "retrieval_mode": PROFILE_TO_MODE[profile],
        "execution": execution,
        "repeat": repeat,
        "exit_code": completed.returncode,
        "latency_seconds": round(elapsed, 6),
        "json_ok": json_ok,
        "parse_error": parse_error,
        "status": payload.get("status"),
        "evidence_count": len(evidence),
        "results_count": len(results),
        "signals": signals,
        "exact_signal_count": exact_signal_count,
        "neighbor_signal_count": neighbor_signal_count,
        "exact_candidate_count": exact_candidate_count,
        "unmatched_identifiers": unmatched,
        "warnings": payload.get("warnings") or [],
        "top1_path": ((top.get("source") or {}).get("path") if isinstance(top, dict) else None),
        "top1_section": ((top.get("location") or {}).get("section") if isinstance(top, dict) else None),
        "stdout_prefix": completed.stdout[:80],
        "stderr_tail": completed.stderr[-500:],
    }
    row.update(quality_flags(case, row))
    return row


def quality_flags(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    qclass = case.get("class")
    flags: dict[str, Any] = {
        "json_pass": row["exit_code"] == 0 and row["json_ok"],
        "stdout_json_pure": row["json_ok"] and row["stdout_prefix"].lstrip().startswith("{"),
    }
    if qclass == "exact_negative":
        flags["negative_exact_pass"] = (row.get("exact_candidate_count") or 0) == 0 and row.get("exact_signal_count") == 0
        flags["expected_unmatched_pass"] = sorted(row.get("unmatched_identifiers") or []) == sorted(case.get("expected_unmatched") or [])
        flags["false_exact"] = not flags["negative_exact_pass"]
    if qclass == "exact_positive" and row.get("profile") in {"H", "L"}:
        flags["positive_exact_pass"] = (row.get("exact_candidate_count") or 0) > 0 or row.get("exact_signal_count", 0) > 0
    return flags


def parse_json(stdout: str) -> tuple[Any | None, bool, str | None]:
    try:
        return json.loads(stdout), True, None
    except json.JSONDecodeError as exc:
        return None, False, str(exc)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def shutdown_daemon() -> None:
    state_path = QUERY_ROOT / "run" / "ragd.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if state.get("transport") != "file":
        return
    file_dir = Path(str(state.get("file_dir") or ""))
    token = str(state.get("token") or "")
    generation = str(state.get("generation") or "")
    if not file_dir or not token or not generation:
        return
    request_id = uuid.uuid4().hex
    response_name = f"{request_id}.response.json"
    request = {
        "op": "shutdown",
        "token": token,
        "generation": generation,
        "response": response_name,
    }
    requests_dir = file_dir / "requests"
    responses_dir = file_dir / "responses"
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    request_file = requests_dir / f"{request_id}.request.json"
    request_file.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")
    response_file = responses_dir / response_name
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if response_file.exists():
            return
        time.sleep(0.1)


def build_report(run_id: str, rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    list_rows = [row for row in rows if row.get("kind") == "list_dbs"]
    search_rows = [row for row in rows if row.get("kind") == "search" and not row.get("warmup")]
    case_by_id = {case["id"]: case for case in DEFAULT_CASES}
    for row in search_rows:
        case = case_by_id.get(str(row.get("case_id")))
        if case:
            for key in (
                "json_pass",
                "stdout_json_pure",
                "negative_exact_pass",
                "expected_unmatched_pass",
                "false_exact",
                "positive_exact_pass",
            ):
                row.pop(key, None)
            row.update(quality_flags(case, row))
    lines = [
        f"# RAG性能評価レポート {run_id}",
        "",
        "## 実行条件",
        "",
        f"- OS: {platform.platform()}",
        f"- Python: {platform.python_version()}",
        f"- profiles: {', '.join(args.profiles)}",
        f"- daemon repeats: {args.daemon_repeats}",
        f"- no-daemon repeats: {args.no_daemon_repeats}",
        f"- budget_tokens: {args.budget_tokens}",
        f"- max_chars: {args.max_chars}",
        "",
    ]
    lines.extend(list_summary(list_rows))
    lines.extend(search_summary(search_rows))
    lines.extend(quality_summary(search_rows))
    return "\n".join(lines) + "\n"


def list_summary(rows: list[dict[str, Any]]) -> list[str]:
    latencies = [float(row["latency_seconds"]) for row in rows if row.get("exit_code") == 0]
    dbs = rows[0].get("dbs") if rows else []
    return [
        "## list_dbs",
        "",
        f"- repeats: {len(rows)}",
        f"- dbs: {', '.join(dbs or [])}",
        f"- JSON errors: {sum(1 for row in rows if not row.get('json_ok'))}",
        f"- p50: {fmt(percentile(latencies, 50))} sec",
        f"- p95: {fmt(percentile(latencies, 95))} sec",
        "",
    ]


def search_summary(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## latency by DB / profile / execution",
        "",
        "|DB|profile|execution|N|errors|p50 sec|p95 sec|max sec|",
        "|--|--|--|--:|--:|--:|--:|--:|",
    ]
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["db"], row["profile"], row["execution"])].append(row)
    for key in sorted(grouped):
        group = grouped[key]
        latencies = [float(row["latency_seconds"]) for row in group if row.get("exit_code") == 0 and row.get("json_ok")]
        errors = sum(1 for row in group if row.get("exit_code") != 0 or not row.get("json_ok"))
        lines.append(
            f"|{key[0]}|{key[1]}|{key[2]}|{len(group)}|{errors}|{fmt(percentile(latencies, 50))}|{fmt(percentile(latencies, 95))}|{fmt(max(latencies) if latencies else None)}|"
        )
    lines.append("")
    return lines


def quality_summary(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## quality gates observed",
        "",
        "|Gate|N|Pass|Fail|",
        "|--|--:|--:|--:|",
    ]
    gates = [
        ("JSON stdout parse", "json_pass"),
        ("stdout JSON purity", "stdout_json_pure"),
        ("Exact negative collision", "negative_exact_pass"),
        ("Expected unmatched identifier", "expected_unmatched_pass"),
        ("Exact positive candidate", "positive_exact_pass"),
    ]
    for label, key in gates:
        applicable = [row for row in rows if key in row]
        passed = sum(1 for row in applicable if row.get(key))
        lines.append(f"|{label}|{len(applicable)}|{passed}|{len(applicable) - passed}|")
    lines.extend(["", "## slowest searches", "", "|case|db|profile|execution|latency sec|status|top1|", "|--|--|--|--|--:|--|--|"])
    for row in sorted(rows, key=lambda item: float(item.get("latency_seconds") or 0), reverse=True)[:10]:
        lines.append(
            f"|{row.get('case_id')}|{row.get('db')}|{row.get('profile')}|{row.get('execution')}|{fmt(float(row.get('latency_seconds') or 0))}|{row.get('status')}|{row.get('top1_path') or ''}|"
        )
    lines.append("")
    return lines


def percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = math.ceil((pct / 100) * len(ordered)) - 1
    return ordered[max(0, min(rank, len(ordered) - 1))]


def fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
