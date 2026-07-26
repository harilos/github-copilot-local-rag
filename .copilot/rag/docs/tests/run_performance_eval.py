from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
import urllib.request
from collections import defaultdict
from contextlib import closing
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
        "expect_exact_positive": False,
        "expect_exact_negative": True,
        "expected_unmatched_identifiers": ["A2W"],
        "answerable": False,
    },
    {
        "id": "AC_EXACT_LOWDF_001",
        "db": "ac-rag",
        "class": "exact_positive",
        "question": "A2Lに関する情報を教えて",
        "expect_exact_positive": True,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
        "expected_matched_identifiers": ["A2L"],
    },
    {
        "id": "AC_SEM_001",
        "db": "ac-rag",
        "class": "semantic",
        "question": "冷房需要が増える背景を資料から説明して",
        "expect_exact_positive": False,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
    },
    {
        "id": "AC_BROAD_001",
        "db": "ac-rag",
        "class": "broad",
        "question": "空調市場で効率化と冷媒転換が重要な理由を複数資料からまとめて",
        "expect_exact_positive": False,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
    },
    {
        "id": "INC_EXACT_001",
        "db": "incident-rag",
        "class": "exact_positive",
        "question": "ntsb_aviation_report_67438 の事故情報を教えて",
        "expect_exact_positive": True,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
        "expected_matched_identifiers": ["ntsb_aviation_report_67438"],
    },
    {
        "id": "INC_SEM_001",
        "db": "incident-rag",
        "class": "semantic",
        "question": "離陸後にエンジン故障が起きた事故の原因に関する根拠を探して",
        "expect_exact_positive": False,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
    },
    {
        "id": "INC_BROAD_001",
        "db": "incident-rag",
        "class": "broad",
        "question": "landing gear collapse に関する事故の傾向を資料から拾って",
        "expect_exact_positive": False,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
    },
    {
        "id": "RFC_EXACT_001",
        "db": "rfc-full-20k-rag",
        "class": "exact_positive",
        "question": "RFC10026 の内容を教えて",
        "expect_exact_positive": True,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
        "expected_matched_identifiers": ["RFC10026"],
    },
    {
        "id": "RFC_SEM_001",
        "db": "rfc-full-20k-rag",
        "class": "semantic",
        "question": "DNSSEC Delegation Signer automation は何を自動化する仕様か",
        "expect_exact_positive": False,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
    },
    {
        "id": "RFC_BROAD_001",
        "db": "rfc-full-20k-rag",
        "class": "broad",
        "question": "QUIC transport protocol の congestion control に関する根拠を探して",
        "expect_exact_positive": False,
        "expect_exact_negative": False,
        "expected_unmatched_identifiers": None,
    },
]

PROFILE_TO_MODE = {
    "H": "hybrid",
    "L": "lexical",
    "V": "dense",
}

SEQUENCE_PLANS = {"clean-mixed", "default", "profile-transition", "db-switch", "explain-compare"}
RUN_IDENTITY_FIELDS = (
    "run_id",
    "git_commit",
    "worktree_fingerprint",
    "db_identities",
    "explain_enabled",
    "diagnostics_level",
    "identifier_diagnostics_requested",
    "pure_profile",
    "sequence_plan",
    "timeout_seconds",
    "warmup_runs",
    "budget_tokens",
    "max_chars",
    "top_k",
    "execution_os",
    "python_version",
    "daemon_code_fingerprint_expected",
    "daemon_attempt_timeout_seconds",
    "daemon_fallback_policy",
    "case_spec_fingerprint",
    "mixed_total",
    "sequence_seed",
    "time_buckets",
)
PROFILE_P95_DEFAULTS = {"H": 8.0, "L": 2.0, "V": 8.0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--profiles", nargs="+", default=["H", "L", "V"], choices=sorted(PROFILE_TO_MODE))
    parser.add_argument("--daemon-repeats", type=int, default=3)
    parser.add_argument("--no-daemon-repeats", type=int, default=1)
    parser.add_argument("--list-repeats", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--daemon-attempt-timeout", type=float, default=5.0)
    parser.add_argument("--budget-tokens", type=int, default=1200)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--restart-daemon", action="store_true")
    parser.add_argument("--diagnostics-level", choices=["off", "basic", "full"], default="basic")
    parser.add_argument("--disable-identifier-diagnostics", action="store_true")
    parser.add_argument("--pure-profile", action="store_true", help="Disable optional diagnostics and explain for pure H/L/V timing.")
    parser.add_argument("--stage-timing", action="store_true", help="Record timing fields when the response exposes them.")
    parser.add_argument("--sequence-plan", choices=sorted(SEQUENCE_PLANS), default="default")
    parser.add_argument("--explain-mode", choices=["on", "off"], default="on")
    parser.add_argument("--min-samples-for-p95", type=int, default=20)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--quality-channel", choices=["all", "exact", "dense", "no-hit"], default="all")
    parser.add_argument("--p95-target-h", type=float, default=PROFILE_P95_DEFAULTS["H"])
    parser.add_argument("--p95-target-l", type=float, default=PROFILE_P95_DEFAULTS["L"])
    parser.add_argument("--p95-target-v", type=float, default=PROFILE_P95_DEFAULTS["V"])
    parser.add_argument("--hard-latency-limit", type=float, default=15.0)
    parser.add_argument("--daemon-slo-p95", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--timeout-rate-gate", type=float, default=0.0)
    parser.add_argument("--mixed-total", type=int, default=500)
    parser.add_argument("--sequence-seed", type=int, default=20260726)
    parser.add_argument("--time-buckets", type=int, default=10)
    parser.add_argument("--degradation-ratio-limit", type=float, default=1.2)
    parser.add_argument("--cases-file", help="Optional JSONL cases with explicit expectations or semantic gold spans.")
    parser.add_argument("--db", action="append", help="Limit execution to one or more DB names.")
    parser.add_argument("--case-id", action="append", help="Limit execution to one or more case IDs.")
    parser.add_argument("--executions", nargs="+", default=["daemon", "no-daemon"], choices=["daemon", "no-daemon"])
    parser.add_argument(
        "--output-dir",
        help="Write raw JSONL and report outside the checkout for formal release runs.",
    )
    parser.add_argument("--report-only", nargs="+", help="Build a markdown report from existing performance-results JSONL file(s).")
    args = parser.parse_args()
    validate_args(parser, args)

    run_id = args.run_id
    results_path, report_path = run_output_paths(run_id, args.output_dir)

    if args.report_only:
        sources = [Path(value) for value in args.report_only]
        rows = []
        for source in sources:
            source_rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
            for row in source_rows:
                row.setdefault("report_source", source.name)
                row.setdefault("run_id", source.stem.removeprefix("performance-results-"))
            rows.extend(source_rows)
        report = build_report(run_id, rows, args)
        report_path.write_text(report, encoding="utf-8")
        print(json.dumps({"run_id": run_id, "results": [str(source) for source in sources], "report": str(report_path)}, ensure_ascii=False, indent=2))
        return
    if results_path.exists():
        raise FileExistsError(f"refusing to append to existing run: {results_path}")

    if args.restart_daemon:
        shutdown_daemon()

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    python = query_python()

    cases = load_cases(Path(args.cases_file)) if args.cases_file else list(DEFAULT_CASES)
    cases = [
        case
        for case in cases
        if (not args.db or case["db"] in set(args.db))
        and (not args.case_id or case["id"] in set(args.case_id))
        and case_matches_quality_channel(case, args.quality_channel)
    ]
    validate_cases_for_run(cases, args)
    args.run_metadata = collect_run_metadata(
        args,
        run_id=run_id,
        db_names=sorted({case["db"] for case in cases}),
        cases=cases,
    )

    metadata_row = {"kind": "run_metadata", "run_at": now(), **args.run_metadata}
    rows: list[dict[str, Any]] = [metadata_row]
    append_jsonl(results_path, metadata_row)
    list_rows = run_list_dbs(python, env, repeats=args.list_repeats, timeout=args.timeout)
    for row in list_rows:
        row.update(args.run_metadata)
    rows.extend(list_rows)
    for row in list_rows:
        append_jsonl(results_path, row)

    if (
        "daemon" in args.executions
        and (args.daemon_repeats > 0 or args.sequence_plan == "clean-mixed")
        and cases
        and args.warmup_runs > 0
    ):
        for warmup_repeat, warmup_case in enumerate(build_warmup_cases(cases, args.warmup_runs)):
            warmup_row = run_search_case(
                python,
                env,
                warmup_case,
                profile="H" if "H" in args.profiles else args.profiles[0],
                execution="daemon",
                repeat=warmup_repeat,
                sequence_index=-(args.warmup_runs - warmup_repeat),
                args=args,
                warmup=True,
                explain_enabled=explain_enabled(args),
            )
            rows.append(warmup_row)
            append_jsonl(results_path, warmup_row)
            if args.sequence_plan == "clean-mixed" and not clean_warmup_passes(warmup_row):
                raise RuntimeError(
                    "clean-mixed warmup failed daemon/build identity checks; "
                    f"case={warmup_case['id']} db={warmup_case['db']}"
                )

    for step in build_sequence(cases, args):
        row = run_search_case(
            python,
            env,
            step["case"],
            profile=step["profile"],
            execution=step["execution"],
            repeat=step["repeat"],
            sequence_index=step.get("sequence_index"),
            args=args,
            explain_enabled=step["explain_enabled"],
        )
        rows.append(row)
        append_jsonl(results_path, row)

    report = build_report(run_id, rows, args)
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps({"run_id": run_id, "results": str(results_path), "report": str(report_path)}, ensure_ascii=False, indent=2))


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.min_samples_for_p95 <= 0:
        parser.error("--min-samples-for-p95 must be positive")
    if args.report_only:
        return
    if args.sequence_plan != "clean-mixed":
        return
    if not args.pure_profile or args.explain_mode != "off" or args.diagnostics_level != "off":
        parser.error("clean-mixed requires --pure-profile --explain-mode off --diagnostics-level off")
    if set(args.executions) != {"daemon"}:
        parser.error("clean-mixed requires --executions daemon")
    if args.timeout != 15:
        parser.error("clean-mixed release run requires --timeout 15")
    if args.daemon_attempt_timeout != 5:
        parser.error("clean-mixed release run requires --daemon-attempt-timeout 5")
    if args.warmup_runs < 5:
        parser.error("clean-mixed release run requires at least --warmup-runs 5")
    if args.mixed_total != 500:
        parser.error("clean-mixed release run requires --mixed-total 500")
    if set(args.profiles) != {"H", "L", "V"}:
        parser.error("clean-mixed release run requires --profiles H L V")
    if args.time_buckets != 10:
        parser.error("clean-mixed release run requires --time-buckets 10")
    if args.min_samples_for_p95 != 20:
        parser.error("clean-mixed release run requires --min-samples-for-p95 20")
    if (
        args.p95_target_h != 8.0
        or args.p95_target_l != 2.0
        or args.p95_target_v != 8.0
        or args.hard_latency_limit != 15.0
        or args.timeout_rate_gate != 0.0
    ):
        parser.error("clean-mixed release run requires H/V p95=8, L p95=2, hard max=15, timeout rate=0")
    if not args.restart_daemon:
        parser.error("clean-mixed release run requires --restart-daemon")


def validate_cases_for_run(cases: list[dict[str, Any]], args: argparse.Namespace) -> None:
    if not cases:
        raise ValueError("no cases selected")
    if args.sequence_plan == "clean-mixed":
        expected_dbs = {"ac-rag", "incident-rag", "rfc-full-20k-rag"}
        actual_dbs = {str(case["db"]) for case in cases}
        if actual_dbs != expected_dbs:
            raise ValueError(
                "clean-mixed release run requires exactly ac-rag, incident-rag, "
                f"rfc-full-20k-rag; got {sorted(actual_dbs)}"
            )
    db_identities = {
        db_name: read_db_identity(db_name)
        for db_name in sorted({str(case["db"]) for case in cases})
    }
    for case in cases:
        if not (case.get("gold_spans") or case.get("gold_groups")):
            continue
        expected = normalize_hash(case.get("db_snapshot_hash"))
        actual = normalize_hash(db_identities[str(case["db"])]["db_snapshot_hash"])
        if not expected:
            raise ValueError(f"{case['id']}: semantic gold requires db_snapshot_hash")
        if expected != actual:
            raise ValueError(
                f"{case['id']}: semantic gold DB snapshot mismatch: expected={expected} actual={actual}"
            )


def query_python() -> str:
    candidate = QUERY_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    return str(candidate if candidate.exists() else Path(sys.executable))


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    required = {"id", "db", "question"}
    for index, case in enumerate(cases, start=1):
        if not case.get("question") and case.get("query"):
            case["question"] = case["query"]
        missing = sorted(required - set(case))
        if missing:
            raise ValueError(f"{path}:{index}: missing required case fields: {', '.join(missing)}")
        case.setdefault("class", "semantic")
        case.setdefault("expect_exact_positive", False)
        case.setdefault("expect_exact_negative", False)
        case.setdefault("expected_unmatched_identifiers", None)
        if case["expect_exact_positive"] and case["expect_exact_negative"]:
            raise ValueError(f"{path}:{index}: Exact positive and negative cannot both be true")
        if case["expect_exact_negative"] and not case["expected_unmatched_identifiers"]:
            raise ValueError(f"{path}:{index}: negative Exact requires expected_unmatched_identifiers")
        if case["expect_exact_positive"] and not case.get("expected_matched_identifiers"):
            raise ValueError(f"{path}:{index}: positive Exact requires expected_matched_identifiers")
        semantic_spans = list(case.get("gold_spans") or [])
        for group in case.get("gold_groups") or []:
            alternatives = group.get("alternatives") or []
            if not alternatives:
                raise ValueError(f"{path}:{index}: every gold group requires alternatives")
            semantic_spans.extend(alternatives)
        for span in semantic_spans:
            if not span.get("span_text") and not span.get("text"):
                raise ValueError(f"{path}:{index}: every gold span requires span_text")
            if not span.get("path") and not span.get("document_id"):
                raise ValueError(f"{path}:{index}: every gold span requires path or document_id")
        if semantic_spans and not case.get("db_snapshot_hash"):
            raise ValueError(f"{path}:{index}: semantic gold requires db_snapshot_hash")
    return cases


def normalize_hash(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return text.removeprefix("sha256:")


def case_matches_quality_channel(case: dict[str, Any], channel: str) -> bool:
    if channel == "all":
        return True
    if channel == "exact":
        return bool(case.get("expect_exact_positive") or case.get("expect_exact_negative"))
    if channel == "dense":
        return bool(case.get("gold_spans") or case.get("gold_groups"))
    if channel == "no-hit":
        return case.get("answerable") is False
    return False


def collect_run_metadata(
    args: argparse.Namespace,
    *,
    run_id: str,
    db_names: list[str],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    source_paths = [
        Path(__file__).resolve(),
        QUERY_ROOT / "search.py",
        QUERY_ROOT / "ragd.py",
        RAG_ROOT / "gen_db" / "software_rag_tool" / "scripts" / "query.py",
        RAG_ROOT / "gen_db" / "software_rag_tool" / "software_rag_tool" / "search_api.py",
    ]
    digest = hashlib.sha256()
    for path in source_paths:
        digest.update(str(path.relative_to(RAG_ROOT)).encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    db_identities = {db_name: read_db_identity(db_name) for db_name in db_names}
    cases_digest = hashlib.sha256(
        json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    git_status = git_value(
        ["status", "--short", "--untracked-files=all"],
        allow_empty=True,
    )
    return {
        "run_id": run_id,
        "git_commit": git_value(["rev-parse", "HEAD"]),
        "git_dirty": "unknown" if git_status == "unknown" else bool(git_status),
        "worktree_fingerprint": digest.hexdigest(),
        "daemon_code_fingerprint_expected": expected_daemon_code_fingerprint(),
        "db_identities": db_identities,
        "explain_enabled": explain_enabled(args),
        "diagnostics_level": args.diagnostics_level,
        "identifier_diagnostics_enabled": identifier_diagnostics_enabled(args),
        "identifier_diagnostics_requested": identifier_diagnostics_enabled(args),
        "pure_profile": args.pure_profile,
        "sequence_plan": args.sequence_plan,
        "timeout_seconds": args.timeout,
        "warmup_runs": args.warmup_runs,
        "budget_tokens": args.budget_tokens,
        "max_chars": args.max_chars,
        "top_k": args.top_k,
        "execution_os": platform.platform(),
        "python_version": platform.python_version(),
        "daemon_attempt_timeout_seconds": args.daemon_attempt_timeout,
        "daemon_fallback_policy": "off",
        "case_spec_fingerprint": cases_digest,
        "mixed_total": args.mixed_total,
        "sequence_seed": args.sequence_seed,
        "time_buckets": args.time_buckets,
    }


def expected_daemon_code_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in daemon_fingerprint_paths():
        digest.update(str(path.relative_to(RAG_ROOT)).encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def daemon_fingerprint_paths() -> list[Path]:
    tool_root = RAG_ROOT / "gen_db" / "software_rag_tool"
    package_root = tool_root / "software_rag_tool"
    paths = [
        QUERY_ROOT / "ragd.py",
        tool_root / "pyproject.toml",
        tool_root / "requirements.txt",
        *package_root.rglob("*.py"),
    ]
    return sorted(set(paths), key=lambda path: str(path.relative_to(RAG_ROOT)))


def git_value(arguments: list[str], *, allow_empty: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(RAG_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    output = completed.stdout.strip()
    return output if output or allow_empty else "unknown"


def run_output_paths(run_id: str, output_dir: str | None) -> tuple[Path, Path]:
    if output_dir:
        root = Path(output_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return (
            root / f"performance-results-{run_id}.jsonl",
            root / f"performance-report-{run_id}.md",
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return (
        DATA_DIR / f"performance-results-{run_id}.jsonl",
        REPORT_DIR / f"performance-report-{run_id}.md",
    )


def read_db_identity(db_name: str) -> dict[str, str]:
    db_dir = RAG_ROOT / "dbs" / db_name
    version_hash = "unknown"
    try:
        payload = json.loads((db_dir / "VERSION.json").read_text(encoding="utf-8"))
        version_hash = str(payload.get("db_hash") or "unknown")
    except (OSError, json.JSONDecodeError):
        pass
    digest = hashlib.sha256()
    for relative in ("VERSION.json", "db.json", "index/manifest.json", "logs/index_state.json"):
        path = db_dir / relative
        digest.update(relative.encode("utf-8"))
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except (OSError, json.JSONDecodeError):
            digest.update(b"<missing-or-invalid>")
    catalog_path = db_dir / "catalog.sqlite"
    try:
        with closing(
            sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
        ) as connection:
            for row in connection.execute(
                """
                SELECT chunk_uid, chunk_hash, content_hash, text_hash, updated_at
                FROM chunk
                WHERE visible_until IS NULL
                ORDER BY chunk_uid
                """
            ):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (OSError, sqlite3.Error):
        digest.update(b"<catalog-unavailable>")
    return {"version_db_hash": version_hash, "db_snapshot_hash": digest.hexdigest()}


def explain_enabled(args: argparse.Namespace) -> bool:
    return args.explain_mode == "on" and not args.pure_profile


def identifier_diagnostics_enabled(args: argparse.Namespace) -> bool:
    return not (args.disable_identifier_diagnostics or args.pure_profile or args.diagnostics_level == "off")


def build_sequence(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.sequence_plan == "clean-mixed":
        return build_clean_mixed_sequence(cases, args)
    if args.sequence_plan == "profile-transition":
        return build_profile_transition_sequence(cases, args)
    if args.sequence_plan == "db-switch":
        return build_db_switch_sequence(cases, args)
    if args.sequence_plan == "explain-compare":
        return build_explain_compare_sequence(cases, args)
    return build_default_sequence(cases, args)


def build_warmup_cases(cases: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    by_db: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_db[str(case["db"])].append(case)
    ordered_dbs = [db for db in ("ac-rag", "incident-rag", "rfc-full-20k-rag") if db in by_db]
    ordered_dbs.extend(sorted(set(by_db) - set(ordered_dbs)))
    if not ordered_dbs:
        return []
    offsets: dict[str, int] = defaultdict(int)
    output = []
    for index in range(count):
        db_name = ordered_dbs[index % len(ordered_dbs)]
        choices = by_db[db_name]
        output.append(choices[offsets[db_name] % len(choices)])
        offsets[db_name] += 1
    return output


def build_clean_mixed_sequence(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if "daemon" not in args.executions:
        raise ValueError("clean-mixed requires daemon execution")
    if args.mixed_total <= 0:
        return []
    rng = random.Random(args.sequence_seed)
    db_cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        db_cases[str(case["db"])].append(case)
    cell_counts = clean_mixed_cell_counts(
        total=args.mixed_total,
        db_names=set(db_cases),
        profiles=set(args.profiles),
    )
    bucket_count = max(1, min(args.time_buckets, args.mixed_total))
    buckets: list[list[tuple[str, str]]] = [[] for _ in range(bucket_count)]
    for cell_index, ((db_name, profile), count) in enumerate(sorted(cell_counts.items())):
        for offset in range(count):
            buckets[(cell_index + offset) % bucket_count].append((db_name, profile))
    for bucket in buckets:
        rng.shuffle(bucket)
    cells = [cell for bucket in buckets for cell in bucket]
    for values in db_cases.values():
        rng.shuffle(values)
    db_offsets: dict[str, int] = defaultdict(int)
    steps: list[dict[str, Any]] = []
    for sequence_index, (db_name, profile) in enumerate(cells):
        candidates = db_cases[db_name]
        case = candidates[db_offsets[db_name] % len(candidates)]
        db_offsets[db_name] += 1
        steps.append(
            {
                "case": case,
                "profile": profile,
                "execution": "daemon",
                "repeat": sequence_index,
                "sequence_index": sequence_index,
                "explain_enabled": explain_enabled(args),
            }
        )
    return steps


def clean_mixed_cell_counts(
    *, total: int, db_names: set[str], profiles: set[str]
) -> dict[tuple[str, str], int]:
    standard_dbs = {"ac-rag", "incident-rag", "rfc-full-20k-rag"}
    standard_profiles = {"H", "L", "V"}
    if total == 500 and db_names == standard_dbs and profiles == standard_profiles:
        return {
            ("ac-rag", "H"): 120,
            ("ac-rag", "L"): 35,
            ("ac-rag", "V"): 20,
            ("incident-rag", "H"): 100,
            ("incident-rag", "L"): 30,
            ("incident-rag", "V"): 20,
            ("rfc-full-20k-rag", "H"): 120,
            ("rfc-full-20k-rag", "L"): 35,
            ("rfc-full-20k-rag", "V"): 20,
        }
    db_weights = {"ac-rag": 0.35, "incident-rag": 0.30, "rfc-full-20k-rag": 0.35}
    profile_weights = {"H": 0.70, "L": 0.20, "V": 0.10}
    weights = {
        (db_name, profile): db_weights.get(db_name, 1.0) * profile_weights.get(profile, 1.0)
        for db_name in db_names
        for profile in profiles
    }
    labels = weighted_population({f"{db_name}\0{profile}": value for (db_name, profile), value in weights.items()}, total)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for label in labels:
        db_name, profile = label.split("\0", 1)
        counts[(db_name, profile)] += 1
    return dict(counts)


def weighted_population(weights: dict[str, float], total: int) -> list[str]:
    if not weights:
        raise ValueError("at least one weighted label is required")
    weight_sum = sum(max(0.0, value) for value in weights.values())
    if weight_sum <= 0:
        raise ValueError("weights must include a positive value")
    raw_counts = {key: total * max(0.0, value) / weight_sum for key, value in weights.items()}
    counts = {key: math.floor(value) for key, value in raw_counts.items()}
    remaining = total - sum(counts.values())
    for key in sorted(weights, key=lambda item: (raw_counts[item] - counts[item], item), reverse=True)[:remaining]:
        counts[key] += 1
    return [key for key in sorted(counts) for _ in range(counts[key])]


def build_default_sequence(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    steps = []
    for execution, repeats in [("daemon", args.daemon_repeats), ("no-daemon", args.no_daemon_repeats)]:
        if execution not in args.executions:
            continue
        for repeat in range(repeats):
            for case in cases:
                for profile in args.profiles:
                    steps.append(
                        {
                            "case": case,
                            "profile": profile,
                            "execution": execution,
                            "repeat": repeat,
                            "explain_enabled": explain_enabled(args),
                        }
                    )
    return steps


def build_profile_transition_sequence(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    transition = ["V", "L", "H", "L", "L", "V"]
    profiles = [profile for profile in transition if profile in set(args.profiles)]
    if not profiles:
        profiles = args.profiles
    execution = "daemon" if "daemon" in args.executions else args.executions[0]
    repeats = args.daemon_repeats if execution == "daemon" else args.no_daemon_repeats
    steps = []
    for repeat in range(repeats):
        for case in cases:
            for profile in profiles:
                steps.append(
                    {
                        "case": case,
                        "profile": profile,
                        "execution": execution,
                        "repeat": repeat,
                        "explain_enabled": explain_enabled(args),
                    }
                )
    return steps


def build_db_switch_sequence(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    by_db: dict[str, dict[str, Any]] = {}
    for case in cases:
        by_db.setdefault(case["db"], case)
    ordered_dbs = [db for db in ["ac-rag", "incident-rag", "rfc-full-20k-rag", "ac-rag"] if db in by_db]
    if not ordered_dbs:
        ordered_dbs = list(by_db)
    execution = "daemon" if "daemon" in args.executions else args.executions[0]
    repeats = args.daemon_repeats if execution == "daemon" else args.no_daemon_repeats
    steps = []
    for repeat in range(repeats):
        for db in ordered_dbs:
            for profile in args.profiles:
                steps.append(
                    {
                        "case": by_db[db],
                        "profile": profile,
                        "execution": execution,
                        "repeat": repeat,
                        "explain_enabled": explain_enabled(args),
                    }
                )
    return steps


def build_explain_compare_sequence(cases: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    steps = []
    for execution, repeats in [("daemon", args.daemon_repeats), ("no-daemon", args.no_daemon_repeats)]:
        if execution not in args.executions:
            continue
        for repeat in range(repeats):
            for explain in [False, True]:
                for case in cases:
                    for profile in args.profiles:
                        steps.append(
                            {
                                "case": case,
                                "profile": profile,
                                "execution": execution,
                                "repeat": repeat,
                                "explain_enabled": explain and not args.pure_profile,
                            }
                        )
    return steps


def run_list_dbs(python: str, env: dict[str, str], *, repeats: int, timeout: int) -> list[dict[str, Any]]:
    rows = []
    cmd = [python, str(QUERY_ROOT / "list_dbs.py")]
    for repeat in range(repeats):
        started = time.perf_counter()
        completed = subprocess.run(
            cmd,
            cwd=str(RAG_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
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
    sequence_index: int | None = None,
    args: argparse.Namespace,
    warmup: bool = False,
    explain_enabled: bool = True,
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
        "--budget-tokens",
        str(args.budget_tokens),
        "--max-chars",
        str(args.max_chars),
        "--top-k",
        str(args.top_k),
        "--timeout",
        str(args.timeout),
        "--daemon-attempt-timeout",
        str(args.daemon_attempt_timeout),
    ]
    if explain_enabled:
        cmd.append("--explain")
    if not identifier_diagnostics_enabled(args):
        cmd.append("--disable-identifier-diagnostics")
    if execution == "no-daemon":
        cmd.append("--no-daemon")
    else:
        cmd.append("--require-daemon")
    cmd.append(case["question"])

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(RAG_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout + 1,
        )
        elapsed = time.perf_counter() - started
        payload, json_ok, parse_error = parse_json(completed.stdout)
        if not isinstance(payload, dict):
            payload = {}
        return summarize_search(
            case,
            profile,
            execution,
            repeat,
            completed,
            payload,
            json_ok,
            parse_error,
            elapsed,
            warmup=warmup,
            args=args,
            explain_enabled=explain_enabled,
            sequence_index=sequence_index,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        row = {
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
            "sequence_index": sequence_index,
            "exit_code": 124,
            "latency_seconds": round(elapsed, 6),
            "json_ok": False,
            "parse_error": f"TimeoutExpired: {exc}",
            "status": "timeout",
            "request_success": False,
            "request_completed": False,
            "timed_out": True,
            "failure_kind": "timeout",
            "stdout_json_pure": False,
            "outer_timeout_seconds": args.timeout or None,
            "outer_deadline_exceeded": elapsed > args.timeout if args.timeout > 0 else False,
        }
        row.update(row_run_metadata(args, db_name=str(case["db"]), explain=explain_enabled))
        row["identifier_diagnostics_enabled"] = "unknown"
        row.update(expectation_snapshot(case))
        return row


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
    args: argparse.Namespace,
    explain_enabled: bool,
    sequence_index: int | None,
) -> dict[str, Any]:
    authoritative_evidence = payload.get("evidence") or []
    related_context = payload.get("related_context") or []
    evidence = authoritative_evidence
    results = payload.get("results") or []
    top = evidence[0] if evidence else {}
    signals = sorted({signal for item in evidence for signal in item.get("signals", [])})
    exact_signal_count = sum(1 for item in evidence if "exact" in (item.get("signals") or []))
    neighbor_signal_count = sum(1 for item in evidence if "neighbor" in (item.get("signals") or []))
    unmatched = payload.get("unmatched_identifiers") or (payload.get("identifiers") or {}).get("unmatched_identifiers") or []
    exact_candidate_count = payload.get("exact_candidate_count")
    if exact_candidate_count is None:
        exact_candidate_count = (payload.get("identifiers") or {}).get("exact_candidate_count")
    request_success = completed.returncode == 0 and json_ok and payload.get("status") != "error"
    failure_kind = None
    if completed.returncode == 124:
        failure_kind = "timeout"
    elif completed.returncode != 0:
        failure_kind = "nonzero_exit"
    elif not json_ok:
        failure_kind = "json_parse"
    elif payload.get("status") == "error":
        failure_kind = "payload_error"
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
        "sequence_index": sequence_index,
        "exit_code": completed.returncode,
        "latency_seconds": round(elapsed, 6),
        "json_ok": json_ok,
        "parse_error": parse_error,
        "request_success": request_success,
        "request_completed": True,
        "timed_out": completed.returncode == 124,
        "failure_kind": failure_kind,
        "status": payload.get("status"),
        "evidence_count": len(authoritative_evidence),
        "display_evidence_count": len(evidence),
        "context_count": len(payload.get("contexts") or []),
        "related_context_count": len(related_context),
        "results_count": len(results),
        "signals": signals,
        "exact_signal_count": exact_signal_count,
        "neighbor_signal_count": neighbor_signal_count,
        "exact_candidate_count": exact_candidate_count,
        "unmatched_identifiers": unmatched,
        "warnings": payload.get("warnings") or [],
        "stage_timing_enabled": args.stage_timing,
        "timings": payload.get("timings") or payload.get("latency_breakdown") or {},
        "daemon": payload.get("daemon") or payload.get("daemon_state") or {},
        "execution_metadata": payload.get("execution_metadata") or {},
        "actual_execution": (payload.get("execution_metadata") or {}).get("actual_execution"),
        "first_attempt_success": (payload.get("execution_metadata") or {}).get("first_attempt_success"),
        "final_user_visible_success": (payload.get("execution_metadata") or {}).get("final_user_visible_success"),
        "fallback_used": bool((payload.get("execution_metadata") or {}).get("fallback_used")),
        "outer_timeout_seconds": (payload.get("execution_metadata") or {}).get(
            "outer_timeout_seconds",
            args.timeout or None,
        ),
        "outer_deadline_exhausted": bool(
            (payload.get("execution_metadata") or {}).get("deadline_exhausted")
        ),
        "outer_deadline_exceeded": bool(args.timeout > 0 and elapsed > args.timeout),
        "first_attempt_latency_seconds": next(
            (
                attempt.get("latency_seconds")
                for attempt in (payload.get("execution_metadata") or {}).get("attempts") or []
                if attempt.get("route") == "daemon"
            ),
            None,
        ),
        "fallback_latency_seconds": next(
            (
                attempt.get("latency_seconds")
                for attempt in (payload.get("execution_metadata") or {}).get("attempts") or []
                if attempt.get("route") == "no-daemon" and (payload.get("execution_metadata") or {}).get("fallback_used")
            ),
            None,
        ),
        "identifier_matches": (payload.get("identifiers") or {}).get("matches") or [],
        "identifier_diagnostics_complete": (payload.get("identifiers") or {}).get(
            "diagnostics_complete",
            payload.get("identifier_diagnostics_error") is None,
        ),
        "identifier_diagnostics_error": payload.get("identifier_diagnostics_error"),
        "retrieved_contexts": [
            {
                "rank": index,
                "path": str(((item.get("source") or {}).get("path") or "")),
                "text": str(item.get("text") or ""),
                "signals": list(item.get("signals") or []),
                "debug": item.get("debug") or {},
            }
            for index, item in enumerate(evidence, start=1)
            if isinstance(item, dict)
        ],
        "top1_path": ((top.get("source") or {}).get("path") if isinstance(top, dict) else None),
        "top1_section": ((top.get("location") or {}).get("section") if isinstance(top, dict) else None),
        "stdout_prefix": completed.stdout[:80],
        "stderr_tail": completed.stderr[-500:],
    }
    row.update(row_run_metadata(args, db_name=str(case["db"]), explain=explain_enabled))
    row["identifier_diagnostics_enabled"] = payload.get("identifier_diagnostics_enabled", "unknown")
    row.update(expectation_snapshot(case))
    row.update(quality_flags(case, row))
    return row


def row_run_metadata(args: argparse.Namespace, *, db_name: str, explain: bool) -> dict[str, Any]:
    metadata = dict(getattr(args, "run_metadata", {}))
    metadata["explain_enabled"] = explain
    db_identity = (metadata.get("db_identities") or {}).get(db_name, {})
    metadata["db_hash"] = db_identity.get("version_db_hash", "unknown")
    metadata["db_snapshot_hash"] = db_identity.get("db_snapshot_hash", "unknown")
    return metadata


def expectation_snapshot(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "expect_exact_positive": bool(case.get("expect_exact_positive")),
        "expect_exact_negative": bool(case.get("expect_exact_negative")),
        "exact_profiles": list(case.get("exact_profiles") or ["H", "L"]),
        "expected_unmatched_identifiers": case.get("expected_unmatched_identifiers"),
        "expected_matched_identifiers": list(case.get("expected_matched_identifiers") or []),
        "answerable": case.get("answerable"),
        "db_snapshot_hash_expected": case.get("db_snapshot_hash"),
        "gold_spans": list(case.get("gold_spans") or []),
        "gold_groups": list(case.get("gold_groups") or []),
    }


def quality_flags(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    request_success = bool(row.get("request_success", row.get("exit_code") == 0 and row.get("json_ok")))
    stdout_prefix = str(row.get("stdout_prefix") or "")
    flags: dict[str, Any] = {
        "json_pass": request_success,
        "request_success": request_success,
        "stdout_json_pure": request_success and stdout_prefix.lstrip().startswith("{"),
    }
    diagnostics_value = row.get("identifier_diagnostics_enabled")
    diagnostics_enabled = (
        diagnostics_value is True
        and row.get("identifier_diagnostics_complete") is True
    )
    exact_channel = diagnostics_enabled
    exact_profiles = set(case.get("exact_profiles") or ["H", "L"])
    if exact_channel and case.get("expect_exact_negative") and row.get("profile") in exact_profiles:
        flags["negative_exact_pass"] = (
            request_success and (row.get("exact_candidate_count") or 0) == 0 and row.get("exact_signal_count") == 0
        )
        flags["false_exact"] = not flags["negative_exact_pass"]
    expected_unmatched = case.get("expected_unmatched_identifiers")
    if exact_channel and expected_unmatched is not None and row.get("profile") in exact_profiles:
        flags["expected_unmatched_pass"] = request_success and sorted(row.get("unmatched_identifiers") or []) == sorted(
            expected_unmatched
        )
    if exact_channel and case.get("expect_exact_positive") and row.get("profile") in exact_profiles:
        expected_matched = set(case.get("expected_matched_identifiers") or [])
        identifier_matches = {
            str(item.get("identifier"))
            for item in row.get("identifier_matches") or []
            if item.get("matched") and item.get("raw_occurrence_verified")
        }
        flags["matched_identifier_pass"] = (
            request_success
            and bool(expected_matched)
            and expected_matched <= identifier_matches
        )
        flags["positive_exact_pass"] = (
            request_success
            and ((row.get("exact_candidate_count") or 0) > 0 or row.get("exact_signal_count", 0) > 0)
            and flags["matched_identifier_pass"]
        )
    gold_groups = list(case.get("gold_groups") or [])
    gold_spans = list(case.get("gold_spans") or [])
    if gold_groups:
        group_matches = [
            any(
                gold_span_matches_context(span, context)
                for span in group.get("alternatives") or []
                for context in row.get("retrieved_contexts") or []
            )
            for group in gold_groups
        ]
        group_top5_matches = [
            any(
                gold_span_matches_context(span, context)
                for span in group.get("alternatives") or []
                for context in (row.get("retrieved_contexts") or [])[:5]
            )
            for group in gold_groups
        ]
        required = [
            index
            for index, group in enumerate(gold_groups)
            if group.get("required", True)
        ]
        required_matches = [group_matches[index] for index in required]
        required_top5_matches = [group_top5_matches[index] for index in required]
        flags["semantic_gold_applicable"] = True
        flags["semantic_hit_at_5"] = any(required_top5_matches)
        flags["context_recall"] = (
            sum(1 for value in required_matches if value) / len(required_matches)
            if required_matches
            else None
        )
    elif gold_spans:
        matched = [
            any(gold_span_matches_context(span, context) for context in row.get("retrieved_contexts") or [])
            for span in gold_spans
        ]
        required = [index for index, span in enumerate(gold_spans) if span.get("required", True)]
        required_matches = [matched[index] for index in required]
        required_spans = [gold_spans[index] for index in required]
        flags["semantic_gold_applicable"] = True
        flags["semantic_hit_at_5"] = any(
            gold_span_matches_context(span, context)
            for span in required_spans
            for context in (row.get("retrieved_contexts") or [])[:5]
        )
        flags["context_recall"] = (
            sum(1 for value in required_matches if value) / len(required_matches) if required_matches else None
        )
    if (
        exact_channel
        and case.get("answerable") is False
        and expected_unmatched is not None
        and "context_count" in row
    ):
        flags["no_hit_contract_pass"] = (
            request_success
            and sorted(row.get("unmatched_identifiers") or []) == sorted(expected_unmatched)
            and (row.get("exact_candidate_count") or 0) == 0
            and (row.get("exact_signal_count") or 0) == 0
            and int(row.get("evidence_count") or 0) == 0
            and int(row.get("context_count") or 0) == 0
            and int(row.get("results_count") or 0) == 0
        )
    return flags


def gold_span_matches_context(span: dict[str, Any], context: dict[str, Any]) -> bool:
    expected_path = str(span.get("path") or span.get("document_id") or "")
    actual_path = str(context.get("path") or "")
    if not expected_path or not actual_path:
        return False
    expected_normalized = expected_path.replace("\\", "/").strip("/").casefold()
    actual_normalized = actual_path.replace("\\", "/").strip("/").casefold()
    if "/" in expected_normalized:
        path_matches = actual_normalized == expected_normalized or actual_normalized.endswith(
            "/" + expected_normalized
        )
    else:
        path_matches = Path(actual_normalized).name == expected_normalized
    if not path_matches:
        return False
    text = normalize_text(str(context.get("text") or ""))
    quotes = [span.get("span_text"), span.get("text")]
    normalized_quotes = [normalize_text(str(value)) for value in quotes if value]
    return bool(text and normalized_quotes and any(value in text for value in normalized_quotes))


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


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
    transport = state.get("transport", "tcp")
    if transport == "tcp":
        try:
            request = urllib.request.Request(
                f"http://{state['host']}:{state['port']}/shutdown",
                data=b"{}",
                headers={"X-RAGD-Token": str(state["token"])},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            wait_daemon_retired(str(state.get("generation") or ""), timeout=5)
            return
        except Exception:
            return
    if transport == "unix":
        request = {
            "op": "shutdown",
            "token": str(state.get("token") or ""),
            "generation": str(state.get("generation") or ""),
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(str(state["socket_file"]))
                sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
                sock.recv(65536)
            wait_daemon_retired(str(state.get("generation") or ""), timeout=5)
            return
        except OSError:
            return
    if transport != "file":
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
            wait_daemon_retired(generation, timeout=5)
            return
        time.sleep(0.1)


def wait_daemon_retired(generation: str, *, timeout: float) -> bool:
    state_path = QUERY_ROOT / "run" / "ragd.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        except (FileNotFoundError, json.JSONDecodeError):
            return True
        if str(current.get("generation") or "") != generation:
            return True
        time.sleep(0.1)
    return False


def build_report(run_id: str, rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    list_rows = [row for row in rows if row.get("kind") == "list_dbs"]
    search_rows = [row for row in rows if row.get("kind") == "search" and not row.get("warmup")]
    case_by_id = {case["id"]: case for case in DEFAULT_CASES}
    for row in search_rows:
        has_snapshot = any(
            key in row
            for key in (
                "expect_exact_positive",
                "expect_exact_negative",
                "expected_unmatched_identifiers",
                "gold_spans",
                "gold_groups",
            )
        )
        case = case_from_row(row) if has_snapshot else case_by_id.get(str(row.get("case_id")))
        if case:
            for key in (
                "json_pass",
                "stdout_json_pure",
                "negative_exact_pass",
                "expected_unmatched_pass",
                "false_exact",
                "positive_exact_pass",
                "matched_identifier_pass",
                "semantic_gold_applicable",
                "semantic_hit_at_5",
                "context_recall",
                "no_hit_contract_pass",
            ):
                row.pop(key, None)
            row.update(quality_flags(case, row))
    lines = [
        f"# RAG性能評価ダッシュボード {run_id}",
        "",
        "異なるrun条件は統合せず、互換条件ごとのcellとして比較する。",
        "",
        "## 判定基準",
        "",
        f"- H p95 target: {fmt(profile_p95_target(args, 'H'))} sec",
        f"- L p95 target: {fmt(profile_p95_target(args, 'L'))} sec",
        f"- V p95 target: {fmt(profile_p95_target(args, 'V'))} sec",
        f"- hard latency limit: {fmt(args.hard_latency_limit)} sec",
        f"- timeout rate limit: {args.timeout_rate_gate:.3%}",
        f"- p95 minimum N: {args.min_samples_for_p95}",
        "",
    ]
    lines.extend(list_summary(list_rows))
    groups = compatible_run_groups(search_rows)
    lines.extend(dashboard_summary(groups, args))
    for index, (identity, group) in enumerate(groups, start=1):
        lines.extend(run_condition_summary(index, identity, group))
        lines.extend(search_summary(group, args, heading="###"))
        lines.extend(quality_summary(group, args, heading="###"))
        lines.extend(semantic_summary(group, heading="###"))
    return "\n".join(lines) + "\n"


def case_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("case_id") or not row.get("db"):
        return None
    return {
        "id": row.get("case_id"),
        "db": row.get("db"),
        "class": row.get("class") or "semantic",
        "question": row.get("question") or "",
        "expect_exact_positive": bool(row.get("expect_exact_positive")),
        "expect_exact_negative": bool(row.get("expect_exact_negative")),
        "exact_profiles": row.get("exact_profiles") or ["H", "L"],
        "expected_unmatched_identifiers": row.get("expected_unmatched_identifiers"),
        "expected_matched_identifiers": row.get("expected_matched_identifiers") or [],
        "answerable": row.get("answerable"),
        "db_snapshot_hash": row.get("db_snapshot_hash_expected"),
        "gold_spans": row.get("gold_spans") or [],
        "gold_groups": row.get("gold_groups") or [],
    }


def compatible_run_groups(rows: list[dict[str, Any]]) -> list[tuple[tuple[Any, ...], list[dict[str, Any]]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        source = row.get("report_source") or row.get("run_id") or "legacy-unknown"
        missing_identity = any(field not in row for field in RUN_IDENTITY_FIELDS)
        if missing_identity:
            identity = ("legacy-source", source) + tuple(
                json.dumps(row.get(field, "<unknown>"), sort_keys=True) for field in RUN_IDENTITY_FIELDS
            )
        else:
            identity = ("compatible", source) + tuple(
                json.dumps(row.get(field), sort_keys=True) for field in RUN_IDENTITY_FIELDS
            )
        grouped[identity].append(row)
    return sorted(grouped.items(), key=lambda item: min(str(row.get("run_at") or "") for row in item[1]))


def dashboard_summary(
    groups: list[tuple[tuple[Any, ...], list[dict[str, Any]]]], args: argparse.Namespace
) -> list[str]:
    lines = [
        "## Run比較",
        "",
        "|#|区分|commit|fingerprint|条件|N|success|timeout|max sec|p95 gate|hard max|",
        "|--:|--|--|--|--|--:|--:|--:|--:|--|--|",
    ]
    for index, (identity, group) in enumerate(groups, start=1):
        success = [row for row in group if is_success(row)]
        timeout = sum(1 for row in group if is_timeout(row))
        first = group[0]
        conditions = (
            f"seq={first.get('sequence_plan', 'legacy')}, "
            f"explain={first.get('explain_enabled', 'unknown')}, "
            f"diag={first.get('diagnostics_level', 'unknown')}, "
            f"pure={first.get('pure_profile', 'unknown')}"
        )
        max_latency = max((float(row.get("latency_seconds") or 0) for row in success), default=None)
        lines.append(
            f"|{index}|{run_label(identity, group)}|{short_value(first.get('git_commit'))}|"
            f"{short_value(first.get('worktree_fingerprint'))}|{conditions}|{len(group)}|{len(success)}|{timeout}|"
            f"{fmt(max_latency)}|{daemon_p95_gate_state(group, args)}|{hard_latency_gate_state(group, args)}|"
        )
    lines.append("")
    return lines


def run_label(identity: tuple[Any, ...], rows: list[dict[str, Any]]) -> str:
    first = rows[0]
    plan = first.get("sequence_plan")
    suffix = " (identity incomplete)" if identity[0] == "legacy-source" else ""
    if plan == "clean-mixed":
        return f"Current clean mixed{suffix}"
    if plan == "profile-transition":
        return f"Profile transition{suffix}"
    if plan == "db-switch":
        return f"DB switch{suffix}"
    if plan == "explain-compare":
        return f"Explain comparison{suffix}"
    if first.get("pure_profile"):
        return f"Current pure-profile{suffix}"
    if identity[0] == "legacy-source":
        return "Historical baseline"
    return "Current compatible run"


def run_condition_summary(index: int, identity: tuple[Any, ...], rows: list[dict[str, Any]]) -> list[str]:
    first = rows[0]
    db_hashes = sorted(
        {
            f"{row.get('db')}={short_value(row.get('db_hash'))}/{short_value(row.get('db_snapshot_hash'))}"
            for row in rows
        }
    )
    return [
        f"## Run {index}: {run_label(identity, rows)}",
        "",
        f"- source/run_id: {first.get('report_source') or first.get('run_id') or 'unknown'}",
        f"- git_commit: {first.get('git_commit', 'unknown')}",
        f"- git_dirty: {first.get('git_dirty', 'unknown')}",
        f"- worktree_fingerprint: {first.get('worktree_fingerprint', 'unknown')}",
        f"- daemon_code_fingerprint_expected: {first.get('daemon_code_fingerprint_expected', 'unknown')}",
        f"- OS: {first.get('execution_os', 'unknown')}",
        f"- Python: {first.get('python_version', 'unknown')}",
        f"- db_hash/db_snapshot_hash: {', '.join(db_hashes) if db_hashes else 'unknown'}",
        f"- explain_enabled: {first.get('explain_enabled', 'unknown')}",
        f"- diagnostics_level: {first.get('diagnostics_level', 'unknown')}",
        f"- identifier_diagnostics_enabled: {first.get('identifier_diagnostics_enabled', 'unknown')}",
        f"- identifier_diagnostics_requested: {first.get('identifier_diagnostics_requested', 'unknown')}",
        f"- pure_profile: {first.get('pure_profile', 'unknown')}",
        f"- sequence_plan: {first.get('sequence_plan', 'legacy')}",
        f"- timeout_seconds: {first.get('timeout_seconds', 'unknown')}",
        f"- daemon_attempt_timeout_seconds: {first.get('daemon_attempt_timeout_seconds', 'unknown')}",
        f"- daemon_fallback_policy: {first.get('daemon_fallback_policy', 'unknown')}",
        f"- case_spec_fingerprint: {first.get('case_spec_fingerprint', 'unknown')}",
        f"- mixed_total/seed/time_buckets: {first.get('mixed_total', 'unknown')}/{first.get('sequence_seed', 'unknown')}/{first.get('time_buckets', 'unknown')}",
        f"- warmup_runs: {first.get('warmup_runs', 'unknown')}",
        "",
    ]


def list_summary(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## list_dbs",
        "",
        "list_dbs latency is shown per source/run and is never pooled.",
        "",
        "|source/run|repeats|dbs|JSON errors|p50 sec|p95 sec|",
        "|--|--:|--|--:|--:|--:|",
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("report_source") or row.get("run_id") or "legacy-unknown")].append(row)
    if not grouped:
        lines.append("|NOT_RUN|0||0|||")
    for source, group in sorted(grouped.items()):
        latencies = [float(row["latency_seconds"]) for row in group if row.get("exit_code") == 0]
        dbs = sorted({str(db) for row in group for db in (row.get("dbs") or [])})
        lines.append(
            f"|{source}|{len(group)}|{', '.join(dbs)}|"
            f"{sum(1 for row in group if not row.get('json_ok'))}|"
            f"{fmt(percentile(latencies, 50))}|{fmt(percentile(latencies, 95))}|"
        )
    lines.append("")
    return lines


def search_summary(rows: list[dict[str, Any]], args: argparse.Namespace, *, heading: str = "##") -> list[str]:
    lines = [
        f"{heading} latency by compatible cell",
        "",
        "|DB|db hash|snapshot|profile|requested|actual|N|success|timeout|errors|p50|p95|target|max|p95 gate|hard max|low-N|",
        "|--|--|--|--|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--|--|--|",
    ]
    grouped: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                str(row.get("db")),
                str(row.get("db_hash") or "unknown"),
                str(row.get("db_snapshot_hash") or "unknown"),
                str(row.get("profile")),
                str(row.get("execution")),
                str(row.get("actual_execution") or "unverified"),
            )
        ].append(row)
    for key in sorted(grouped):
        group = grouped[key]
        success = [row for row in group if is_success(row)]
        latencies = [float(row["latency_seconds"]) for row in success]
        timeouts = sum(1 for row in group if is_timeout(row))
        errors = len(group) - len(success)
        p95_value = percentile(latencies, 95)
        target = profile_p95_target(args, key[3])
        low_n = len(latencies) < args.min_samples_for_p95
        p95_status = "INSUFFICIENT_N" if low_n else gate(p95_value is not None and p95_value <= target)
        hard_status = state_for_rows(
            len(latencies),
            all(latency <= args.hard_latency_limit for latency in latencies),
        )
        lines.append(
            f"|{key[0]}|{short_value(key[1])}|{short_value(key[2])}|{key[3]}|{key[4]}|{key[5]}|{len(group)}|{len(success)}|{timeouts}|{errors}|"
            f"{fmt(percentile(latencies, 50))}|{fmt(p95_value)}|{fmt(target)}|"
            f"{fmt(max(latencies) if latencies else None)}|{p95_status}|{hard_status}|"
            f"{'YES' if low_n else 'NO'}|"
        )
    lines.append("")
    return lines


def quality_summary(rows: list[dict[str, Any]], args: argparse.Namespace, *, heading: str = "##") -> list[str]:
    all_total = len(rows)
    all_success = sum(1 for row in rows if is_success(row))
    all_timeout = sum(1 for row in rows if is_timeout(row))
    daemon_rows = [row for row in rows if row.get("execution") == "daemon"]
    daemon_success = sum(1 for row in daemon_rows if is_success(row))
    daemon_timeout = sum(1 for row in daemon_rows if daemon_first_attempt_timeout(row))
    actual_daemon_success_rows = [
        row
        for row in daemon_rows
        if row.get("actual_execution") in {None, "daemon"} and is_success(row)
    ]
    no_daemon_rows = [row for row in rows if row.get("execution") == "no-daemon"]
    no_daemon_success = sum(1 for row in no_daemon_rows if is_success(row))
    completed_success = [row for row in rows if is_success(row)]
    json_pure = sum(1 for row in completed_success if row.get("stdout_json_pure"))
    exact_negative_rows = [row for row in rows if "negative_exact_pass" in row]
    exact_positive_rows = [row for row in rows if "positive_exact_pass" in row]
    no_hit_rows = [row for row in rows if "no_hit_contract_pass" in row]
    exact_negative_potential = [
        row
        for row in rows
        if row.get("expect_exact_negative") and row.get("profile") in set(row.get("exact_profiles") or ["H", "L"])
    ]
    exact_positive_potential = [
        row
        for row in rows
        if row.get("expect_exact_positive") and row.get("profile") in set(row.get("exact_profiles") or ["H", "L"])
    ]
    no_hit_potential = [
        row
        for row in rows
        if row.get("answerable") is False and row.get("expected_unmatched_identifiers") is not None
    ]
    timeout_rate = (daemon_timeout / len(daemon_rows)) if daemon_rows else None
    fallback_count = sum(1 for row in daemon_rows if row.get("fallback_used"))
    diagnostics_matches = sum(
        1
        for row in completed_success
        if row.get("identifier_diagnostics_enabled") in {True, False}
        and row.get("identifier_diagnostics_enabled") == row.get("identifier_diagnostics_requested")
        and (
            row.get("identifier_diagnostics_requested") is not True
            or row.get("identifier_diagnostics_complete", True) is True
        )
    )
    final_visible_success = sum(
        1
        for row in daemon_rows
        if row.get("final_user_visible_success") is True
        or (row.get("final_user_visible_success") is None and is_success(row))
    )
    p95_counts = daemon_p95_gate_counts(rows, args)
    lines = [
        f"{heading} separated gates",
        "",
        "|Gate|判定|N|Pass|Fail|Timeout|理由|",
        "|--|--|--:|--:|--:|--:|--|",
        expectation_gate_row(
            "Exact positive",
            exact_positive_rows,
            exact_positive_potential,
            "positive_exact_pass",
            "expectation対象行のみ",
        ),
        expectation_gate_row(
            "Exact negative",
            exact_negative_rows,
            exact_negative_potential,
            "negative_exact_pass",
            "expectation対象行のみ",
        ),
        f"|JSON stdout purity|{state_for_rows(len(completed_success), json_pure == len(completed_success))}|{len(completed_success)}|{json_pure}|{len(completed_success) - json_pure}|0|完了行のみを分母にする|",
        f"|全検索の正常完了率|{state_for_rows(all_total, all_success == all_total)}|{all_total}|{all_success}|{all_total - all_success}|{all_timeout}|exit 0・JSON parse・payload errorなし|",
        f"|daemon first-attempt成功率|{daemon_first_attempt_state(daemon_rows)}|{len(daemon_rows)}|{sum(1 for row in daemon_rows if daemon_first_attempt_success(row))}|{sum(1 for row in daemon_rows if not daemon_first_attempt_success(row))}|{daemon_timeout}|fallback後の成功と分離|",
        f"|final user-visible成功率|{final_visible_state(daemon_rows)}|{len(daemon_rows)}|{final_visible_success}|{len(daemon_rows) - final_visible_success}|0|fallback後の最終結果|",
        f"|fallback rate|{fallback_rate_state(daemon_rows)}|{len(daemon_rows)}|{len(daemon_rows) - fallback_count}|{fallback_count}|0|clean daemon runでは0|",
        f"|daemon build identity|{daemon_build_state(daemon_rows)}|{len(daemon_rows)}|{sum(1 for row in daemon_rows if daemon_build_matches(row))}|{sum(1 for row in daemon_rows if not daemon_build_matches(row))}|0|daemon応答code fingerprintと測定側を比較|",
        f"|diagnostics mode contract|{diagnostics_contract_state(rows)}|{len(completed_success)}|{diagnostics_matches}|{len(completed_success) - diagnostics_matches}|0|応答実値と要求値を照合|",
        f"|daemon timeout rate|{state_for_optional(timeout_rate, timeout_rate is not None and timeout_rate <= args.timeout_rate_gate)}|{len(daemon_rows)}|{len(daemon_rows) - daemon_timeout}|{daemon_timeout}|{daemon_timeout}|limit={args.timeout_rate_gate:.3%}|",
        f"|latency_p95_slo|{daemon_p95_gate_state(rows, args)}|{p95_counts['cells']}|{p95_counts['passed']}|{p95_counts['failed']}|0|profile別target、low-N cells={p95_counts['insufficient']}|",
        f"|hard_latency_limit|{hard_latency_gate_state(rows, args)}|{len(actual_daemon_success_rows)}|{sum(1 for row in actual_daemon_success_rows if float(row.get('latency_seconds') or 0) <= args.hard_latency_limit)}|{sum(1 for row in actual_daemon_success_rows if float(row.get('latency_seconds') or 0) > args.hard_latency_limit)}|0|max limit={fmt(args.hard_latency_limit)} sec|",
        f"|outer deadline adherence|{outer_deadline_gate_state(rows, args)}|{len(rows)}|{sum(1 for row in rows if not row.get('outer_deadline_exceeded') and float(row.get('latency_seconds') or 0) <= args.hard_latency_limit)}|{sum(1 for row in rows if row.get('outer_deadline_exceeded') or float(row.get('latency_seconds') or 0) > args.hard_latency_limit)}|{all_timeout}|成功・失敗を問わずwall time ≤ {fmt(args.hard_latency_limit)} sec|",
        f"|time degradation|{time_degradation_state(rows, args)}|{len(daemon_rows)}|0|0|0|daemon first-attempt後半p95/前半p95 ≤ {args.degradation_ratio_limit:.2f}かつ絶対target内|",
        f"|daemon generation stability|{daemon_generation_state(daemon_rows)}|{len(daemon_rows)}|0|0|0|clean run中のgeneration変更0|",
        f"|no-daemon smoke|{state_for_rows(len(no_daemon_rows), no_daemon_success == len(no_daemon_rows))}|{len(no_daemon_rows)}|{no_daemon_success}|{len(no_daemon_rows) - no_daemon_success}|{sum(1 for row in no_daemon_rows if is_timeout(row))}|N=0はNOT_RUN|",
        expectation_gate_row(
            "no-hit contract",
            no_hit_rows,
            no_hit_potential,
            "no_hit_contract_pass",
            "通常contexts/evidence/results空・related_context隔離",
        ),
        "|Vector有効性|NOT_RUN|0|0|0|0|gold span付きsemantic setが必要|",
        "|最終リリース|NOT_RUN|0|0|0|0|clean mixed・Vector・no-hit・Windows・fallback確認を別々に満たす|",
        "",
        f"{heading} expectation-scoped observations",
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
        ("Matched identifier + raw occurrence", "matched_identifier_pass"),
        ("No-hit contract", "no_hit_contract_pass"),
    ]
    for label, key in gates:
        applicable = [row for row in rows if key in row]
        passed = sum(1 for row in applicable if row.get(key))
        lines.append(f"|{label}|{len(applicable)}|{passed}|{len(applicable) - passed}|")
    lines.extend(["", f"{heading} slowest searches", "", "|case|db|profile|execution|latency sec|status|top1|", "|--|--|--|--|--:|--|--|"])
    for row in sorted(rows, key=lambda item: float(item.get("latency_seconds") or 0), reverse=True)[:10]:
        lines.append(
            f"|{row.get('case_id')}|{row.get('db')}|{row.get('profile')}|{row.get('execution')}|{fmt(float(row.get('latency_seconds') or 0))}|{row.get('status')}|{row.get('top1_path') or ''}|"
        )
    lines.append("")
    return lines


def semantic_summary(rows: list[dict[str, Any]], *, heading: str = "##") -> list[str]:
    semantic = [row for row in rows if row.get("semantic_gold_applicable")]
    lines = [
        f"{heading} Semantic gold",
        "",
        "|profile|N|Hit@5|Context Recall@budget|",
        "|--|--:|--:|--:|",
    ]
    for profile in ("H", "L", "V"):
        group = [row for row in semantic if row.get("profile") == profile and is_success(row)]
        recalls = [float(row["context_recall"]) for row in group if row.get("context_recall") is not None]
        hits = sum(1 for row in group if row.get("semantic_hit_at_5"))
        hit_value: str | float = (hits / len(group)) if group else "NOT_RUN"
        recall_value: str | float = statistics.mean(recalls) if recalls else "NOT_RUN"
        lines.append(f"|{profile}|{len(group)}|{hit_value}|{recall_value}|")
    by_case_profile = {(row.get("case_id"), row.get("profile")): row for row in semantic if is_success(row)}
    l_ids = {case_id for case_id, profile in by_case_profile if profile == "L"}
    h_ids = {case_id for case_id, profile in by_case_profile if profile == "H"}
    comparable_ids = sorted(l_ids & h_ids)
    rescue = sum(
        1
        for case_id in comparable_ids
        if not by_case_profile[(case_id, "L")].get("semantic_hit_at_5")
        and by_case_profile[(case_id, "H")].get("semantic_hit_at_5")
    )
    harm = sum(
        1
        for case_id in comparable_ids
        if by_case_profile[(case_id, "L")].get("semantic_hit_at_5")
        and not by_case_profile[(case_id, "H")].get("semantic_hit_at_5")
    )
    rescue_denominator = sum(
        1
        for case_id in comparable_ids
        if not by_case_profile[(case_id, "L")].get("semantic_hit_at_5")
    )
    harm_denominator = sum(
        1
        for case_id in comparable_ids
        if by_case_profile[(case_id, "L")].get("semantic_hit_at_5")
    )
    lines.extend(
        [
            "",
            f"- comparable H/L cases: {len(comparable_ids)}",
            f"- Vector Rescue Rate: {(rescue / rescue_denominator) if rescue_denominator else 'NOT_APPLICABLE'} "
            f"({rescue}/{rescue_denominator} L misses)",
            f"- Vector Harm Rate: {(harm / harm_denominator) if harm_denominator else 'NOT_APPLICABLE'} "
            f"({harm}/{harm_denominator} L hits)",
            "",
        ]
    )
    return lines


def gate_row(label: str, rows: list[dict[str, Any]], key: str, reason: str) -> str:
    passed = sum(1 for row in rows if row.get(key))
    return (
        f"|{label}|{state_for_rows(len(rows), passed == len(rows))}|{len(rows)}|{passed}|"
        f"{len(rows) - passed}|{sum(1 for row in rows if is_timeout(row))}|{reason}|"
    )


def expectation_gate_row(
    label: str,
    applicable_rows: list[dict[str, Any]],
    potential_rows: list[dict[str, Any]],
    key: str,
    reason: str,
) -> str:
    if applicable_rows:
        return gate_row(label, applicable_rows, key, reason)
    if potential_rows and all(row.get("identifier_diagnostics_requested") is False for row in potential_rows):
        return f"|{label}|NOT_APPLICABLE|0|0|0|0|identifier diagnostics無効のrun|"
    if potential_rows:
        return f"|{label}|UNVERIFIED|0|0|0|0|legacy/incomplete diagnosticsでは判定しない|"
    return f"|{label}|NOT_RUN|0|0|0|0|{reason}|"


def profile_p95_target(args: argparse.Namespace, profile: str) -> float:
    if args.daemon_slo_p95 is not None:
        return float(args.daemon_slo_p95)
    return float(getattr(args, f"p95_target_{profile.lower()}", PROFILE_P95_DEFAULTS.get(profile, 8.0)))


def daemon_p95_gate_state(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    counts = daemon_p95_gate_counts(rows, args)
    if counts["cells"] == 0:
        return "NOT_RUN"
    if counts["failed"]:
        return "FAIL"
    return "INSUFFICIENT_N" if counts["insufficient"] else "PASS"


def daemon_p95_gate_counts(rows: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, int]:
    daemon = [
        row
        for row in rows
        if row.get("execution") == "daemon"
        and row.get("actual_execution") in {None, "daemon"}
    ]
    if not daemon:
        return {"cells": 0, "passed": 0, "failed": 0, "insufficient": 0}
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in daemon:
        grouped[
            (
                str(row.get("db")),
                str(row.get("db_hash") or "unknown"),
                str(row.get("db_snapshot_hash") or "unknown"),
                str(row.get("profile")),
            )
        ].append(row)
    counts = {"cells": len(grouped), "passed": 0, "failed": 0, "insufficient": 0}
    for (_db, _db_hash, _snapshot, profile), group in grouped.items():
        latencies = [float(row.get("latency_seconds") or 0) for row in group if is_success(row)]
        if len(latencies) < args.min_samples_for_p95:
            counts["insufficient"] += 1
            continue
        if float(percentile(latencies, 95) or 0) > profile_p95_target(args, profile):
            counts["failed"] += 1
        else:
            counts["passed"] += 1
    return counts


def hard_latency_gate_state(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    latencies = [
        float(row.get("latency_seconds") or 0)
        for row in rows
        if row.get("execution") == "daemon"
        and row.get("actual_execution") in {None, "daemon"}
        and is_success(row)
    ]
    return state_for_rows(len(latencies), all(value <= args.hard_latency_limit for value in latencies))


def outer_deadline_gate_state(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    if not rows:
        return "NOT_RUN"
    return gate(
        all(
            not row.get("outer_deadline_exceeded")
            and float(row.get("latency_seconds") or 0) <= args.hard_latency_limit
            for row in rows
        )
    )


def daemon_first_attempt_success(row: dict[str, Any]) -> bool:
    value = row.get("first_attempt_success")
    if value is None:
        actual = row.get("actual_execution")
        if actual:
            return actual == "daemon" and is_success(row)
        return row.get("execution") == "daemon" and is_success(row) and not row.get("fallback_used")
    return bool(value)


def daemon_first_attempt_timeout(row: dict[str, Any]) -> bool:
    attempts = (row.get("execution_metadata") or {}).get("attempts") or []
    daemon_attempts = [attempt for attempt in attempts if attempt.get("route") == "daemon"]
    if daemon_attempts:
        return str(daemon_attempts[0].get("failure_kind") or "") == "timeout"
    if row.get("first_attempt_success") is False:
        return str(row.get("first_attempt_failure_kind") or row.get("failure_kind") or "") == "timeout"
    return is_timeout(row)


def clean_warmup_passes(row: dict[str, Any]) -> bool:
    return (
        is_success(row)
        and daemon_first_attempt_success(row)
        and row.get("actual_execution") == "daemon"
        and daemon_build_matches(row)
    )


def daemon_first_attempt_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NOT_RUN"
    if any(row.get("first_attempt_success") is None and not row.get("actual_execution") for row in rows):
        return "UNVERIFIED"
    return gate(all(daemon_first_attempt_success(row) for row in rows))


def final_visible_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NOT_RUN"
    if any(row.get("final_user_visible_success") is None for row in rows):
        return "UNVERIFIED"
    return gate(all(row.get("final_user_visible_success") is True for row in rows))


def fallback_rate_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NOT_RUN"
    if any("fallback_used" not in row for row in rows):
        return "UNVERIFIED"
    return gate(not any(row.get("fallback_used") for row in rows))


def daemon_build_matches(row: dict[str, Any]) -> bool:
    expected = row.get("daemon_code_fingerprint_expected")
    actual = (row.get("daemon") or {}).get("code_fingerprint")
    return bool(expected and actual and expected == actual)


def daemon_build_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NOT_RUN"
    if any(not row.get("daemon_code_fingerprint_expected") or not (row.get("daemon") or {}).get("code_fingerprint") for row in rows):
        return "UNVERIFIED"
    return gate(all(daemon_build_matches(row) for row in rows))


def diagnostics_contract_state(rows: list[dict[str, Any]]) -> str:
    completed = [row for row in rows if is_success(row)]
    if not completed:
        return "NOT_RUN"
    if any(row.get("identifier_diagnostics_enabled") not in {True, False} for row in completed):
        return "UNVERIFIED"
    if any(
        row.get("identifier_diagnostics_requested") is True
        and row.get("identifier_diagnostics_complete", True) is not True
        for row in completed
    ):
        return "FAIL"
    return gate(
        all(
            row.get("identifier_diagnostics_enabled") == row.get("identifier_diagnostics_requested")
            for row in completed
        )
    )


def time_degradation_state(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    measured = [
        row
        for row in rows
        if row.get("sequence_plan") == "clean-mixed"
        and row.get("sequence_index") is not None
        and is_success(row)
    ]
    if not measured:
        return "NOT_APPLICABLE"
    ordered = sorted(measured, key=lambda row: int(row.get("sequence_index") or 0))
    midpoint = len(ordered) // 2
    first_half = ordered[:midpoint]
    second_half = ordered[midpoint:]
    insufficient = False
    for profile in sorted({str(row.get("profile")) for row in ordered}):
        first = [
            daemon_attempt_latency(row)
            for row in first_half
            if row.get("profile") == profile
        ]
        second = [
            daemon_attempt_latency(row)
            for row in second_half
            if row.get("profile") == profile
        ]
        if min(len(first), len(second)) < max(5, args.min_samples_for_p95 // 2):
            insufficient = True
            continue
        first_p95 = float(percentile(first, 95) or 0)
        second_p95 = float(percentile(second, 95) or 0)
        ratio = second_p95 / first_p95 if first_p95 > 0 else float("inf")
        if second_p95 > profile_p95_target(args, profile) or ratio > args.degradation_ratio_limit:
            return "FAIL"
    return "INSUFFICIENT_N" if insufficient else "PASS"


def daemon_attempt_latency(row: dict[str, Any]) -> float:
    value = row.get("first_attempt_latency_seconds")
    if isinstance(value, (int, float)):
        return float(value)
    return float(row.get("latency_seconds") or 0)


def daemon_generation_state(rows: list[dict[str, Any]]) -> str:
    clean = [row for row in rows if row.get("sequence_plan") == "clean-mixed"]
    if not clean:
        return "NOT_APPLICABLE"
    generations = {
        str((row.get("daemon") or {}).get("generation"))
        for row in clean
        if (row.get("daemon") or {}).get("generation")
    }
    if any(not (row.get("daemon") or {}).get("generation") for row in clean):
        return "UNVERIFIED"
    return gate(len(generations) == 1)


def state_for_rows(count: int, passed: bool) -> str:
    if count == 0:
        return "NOT_RUN"
    return gate(passed)


def state_for_optional(value: Any, passed: bool) -> str:
    if value is None:
        return "NOT_RUN"
    return gate(passed)


def short_value(value: Any, length: int = 10) -> str:
    text = str(value or "unknown")
    return text if text == "unknown" else text[:length]


def is_timeout(row: dict[str, Any]) -> bool:
    return bool(row.get("timed_out")) or row.get("status") == "timeout" or row.get("exit_code") == 124


def is_success(row: dict[str, Any]) -> bool:
    if is_timeout(row):
        return False
    return bool(row.get("request_success", row.get("exit_code") == 0 and row.get("json_ok")))


def gate(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


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
