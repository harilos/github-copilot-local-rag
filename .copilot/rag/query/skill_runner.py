#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
sys.path.insert(0, str(QUERY_ROOT))

from agent003_answer_packet import (
    DETAIL_SCHEMA_VERSION,
    SEARCH_SCHEMA_VERSION,
    build_error_packet,
    build_evidence_detail,
    build_search_packet,
    build_stale_evidence_detail,
    sanitize_visible_text,
    serialize_packet,
)
from result_bundle import load_expanded_result, load_initial_summary, result_spool_root
from result_gateway import (
    DiskTokenRegistry,
    GatewayError,
    create_result_binding,
    default_registry_root,
    parse_search_pointer,
    revalidate_result_binding,
    validated_item_ids,
)


DBS_ROOT = RAG_ROOT / "dbs"
SPOOL_ROOT = result_spool_root()
REGISTRY_ROOT = default_registry_root()
MAX_QUESTION_CHARS = 16_000
MAX_NORMALIZED_REQUEST_BYTES = 3_072
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_SEARCH_PACKET_BYTES = 64 * 1024
MAX_DETAIL_PACKET_BYTES = 128 * 1024
MAX_ERROR_PACKET_BYTES = 4 * 1024
MAX_CATALOG_PACKET_BYTES = 256 * 1024
MAX_SETUP_PACKET_BYTES = 64 * 1024
LIST_TIMEOUT_SECONDS = 30.0
SEARCH_TIMEOUT_SECONDS = 180.0
SEARCH_RUNTIME_TIMEOUT_SECONDS = 170
SETUP_TIMEOUT_SECONDS = 20 * 60.0
CATALOG_SCHEMA_VERSION = "local-rag-catalog-v1"
SETUP_SCHEMA_VERSION = "local-rag-setup-status-v1"

_DATABASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_TOKEN_RE = re.compile(r"^lrt_[A-Za-z0-9_-]{32}$")
_ITEM_ID_RE = re.compile(r"^[ED][1-9]\d?$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ANSWER_GOALS = ("comparison", "definition", "evidence", "history", "procedure", "survey")
_REPEATABLE_SEARCH_OPTIONS = (
    ("literal_identifier", "--literal-identifier", 3),
    ("entity", "--entity", 5),
    ("facet", "--facet", 4),
    ("semantic_hypothesis", "--semantic-hypothesis", 3),
)
_VALUE_OPTIONS = frozenset({
    "--answer-goal", "--db", "--detail-level", "--entity", "--facet",
    "--item-id", "--literal-identifier", "--question", "--result-token",
    "--semantic-hypothesis",
})


class RunnerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if _SAFE_CODE_RE.fullmatch(code) else "runner_error"


def _database_name(value: str) -> str:
    if not _DATABASE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("database must match '<name>-rag'")
    return value


def _nonempty_text(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise argparse.ArgumentTypeError("value must be non-empty and contain no NUL")
    return value


def _question(value: str) -> str:
    question = _nonempty_text(value)
    if len(question) > MAX_QUESTION_CHARS:
        raise argparse.ArgumentTypeError(f"question must contain at most {MAX_QUESTION_CHARS} characters")
    return question


def _result_token(value: str) -> str:
    if not _TOKEN_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("result-token must be an opaque lrt_ token")
    return value


def _item_id(value: str) -> str:
    if not _ITEM_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("item-id must match E1..E99 or D1..D99")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded command gateway used by the Local RAG Skill.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List installed databases.", allow_abbrev=False)
    search = commands.add_parser("search", help="Run one bounded search.", allow_abbrev=False)
    search.add_argument("--db", required=True, type=_database_name)
    search.add_argument("--question", required=True, type=_question)
    search.add_argument("--answer-goal", choices=_ANSWER_GOALS)
    for attribute, option, _maximum in _REPEATABLE_SEARCH_OPTIONS:
        search.add_argument(option, dest=attribute, action="append", default=[], type=_nonempty_text)
    detail = commands.add_parser("detail", help="Read cached detail through an opaque token.", allow_abbrev=False)
    detail.add_argument("--result-token", required=True, type=_result_token)
    detail.add_argument("--item-id", action="append", required=True, type=_item_id)
    detail.add_argument("--detail-level", choices=("expanded", "deep"), default="expanded")
    commands.add_parser("setup", help="Run bounded initial setup.", allow_abbrev=False)
    return parser


def _protect_option_values(arguments: Sequence[str]) -> list[str]:
    protected: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in _VALUE_OPTIONS and index + 1 < len(arguments):
            protected.append(f"{value}={arguments[index + 1]}")
            index += 2
            continue
        protected.append(value)
        index += 1
    return protected


def _deduplicated(values: Sequence[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


def _normalized_request_size(args: argparse.Namespace) -> int:
    identifiers = _deduplicated(args.literal_identifier)
    facets = [{
        "kind": "literal" if value in identifiers else "semantic",
        "query": value,
        "purpose": "Find literal occurrences and identifier evidence." if value in identifiers else "Find related local documents.",
    } for value in _deduplicated(args.facet)]
    normalized = {
        "schema_version": "rag-search-request-v1",
        "original_question": args.question,
        "answer_goal": args.answer_goal or "evidence",
        "literal_identifiers": identifiers,
        "entities": _deduplicated(args.entity),
        "facets": facets,
        "inferred_concepts": [{"term": value, "confidence": "medium", "semantic_only": True} for value in _deduplicated(args.semantic_hypothesis)],
        "coverage": {
            "policy": "wide", "target_distinct_documents": 8,
            "minimum_desired_documents": 6, "maximum_distinct_documents": 10,
            "max_chunks_per_document": 2, "allow_weak_related": True,
        },
    }
    return len(_compact_json(normalized).encode("utf-8"))


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "search":
        for attribute, option, maximum in _REPEATABLE_SEARCH_OPTIONS:
            if len(getattr(args, attribute)) > maximum:
                parser.error(f"{option} accepts at most {maximum} values")
        if _normalized_request_size(args) > MAX_NORMALIZED_REQUEST_BYTES:
            parser.error(f"normalized search request exceeds {MAX_NORMALIZED_REQUEST_BYTES} UTF-8 bytes")
    elif args.command == "detail":
        maximum = 1 if args.detail_level == "deep" else 3
        try:
            validated_item_ids(args.item_id, maximum=maximum)
        except GatewayError:
            parser.error(f"--detail-level {args.detail_level} accepts unique valid item IDs up to {maximum} value(s)")


def _search_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable, "-I", "-B", str(RAG_ROOT / "search.py"),
        "--db", args.db, "--include-db-hint", "--compact-json",
        "--timeout", str(SEARCH_RUNTIME_TIMEOUT_SECONDS),
        "--result-delivery", "file", "--format", "json", "--stdin",
    ]
    if args.answer_goal:
        command.extend(["--answer-goal", args.answer_goal])
    for attribute, option, _maximum in _REPEATABLE_SEARCH_OPTIONS:
        command.extend(f"{option}={value}" for value in getattr(args, attribute))
    return command


def _list_command() -> list[str]:
    return [sys.executable, "-I", "-B", str(RAG_ROOT / "list_dbs.py"), "--format", "json"]


def _setup_command() -> list[str]:
    return [sys.executable, "-I", "-B", str(RAG_ROOT / "setup.py"), "--format", "json"]


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"):
        environment.pop(key, None)
    environment.update({
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "RAG_DBS_ROOT": str(DBS_ROOT),
    })
    return environment


def _run_public(
    command: list[str], *, input_bytes: bytes | None, timeout: float,
    invalid_code: str, allow_nonzero: bool = False,
) -> tuple[dict[str, Any], int]:
    _validate_fixed_command(command)
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "cwd": str(RAG_ROOT), "env": _child_environment(), "shell": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        raise RunnerError("public_command_start_failed") from exc
    job = _WindowsJob(child) if os.name == "nt" else None
    stdout, stderr, overflow = bytearray(), bytearray(), threading.Event()
    readers = [
        threading.Thread(target=_bounded_reader, args=(child.stdout, stdout, overflow), daemon=True),
        threading.Thread(target=_bounded_reader, args=(child.stderr, stderr, overflow), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        if input_bytes is not None and child.stdin is not None:
            try:
                child.stdin.write(input_bytes)
                child.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                child.stdin.close()
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            if overflow.is_set():
                _terminate_tree(child, job)
                break
            if time.monotonic() >= deadline:
                _terminate_tree(child, job)
                raise RunnerError("public_command_timeout")
            time.sleep(0.01)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            _terminate_tree(child, job)
            raise RunnerError("public_command_timeout") from exc
    finally:
        if child.poll() is None:
            _terminate_tree(child, job)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if job is not None:
            job.close()
    if overflow.is_set():
        raise RunnerError("public_command_output_too_large")
    try:
        stdout_text = bytes(stdout).decode("utf-8", errors="strict")
        bytes(stderr).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RunnerError("invalid_command_utf8") from exc
    if child.returncode and not allow_nonzero:
        raise RunnerError("public_command_failed")
    try:
        value = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RunnerError(invalid_code) from exc
    if not isinstance(value, dict):
        raise RunnerError(invalid_code)
    return value, int(child.returncode or 0)


def _bounded_reader(stream: Any, output: bytearray, overflow: threading.Event) -> None:
    if stream is None:
        return
    try:
        while True:
            block = stream.read(64 * 1024)
            if not block:
                return
            remaining = MAX_COMMAND_OUTPUT_BYTES - len(output)
            if remaining > 0:
                output.extend(block[:remaining])
            if len(block) > remaining:
                overflow.set()
    except OSError:
        overflow.set()


def _validate_fixed_command(command: list[str]) -> None:
    if len(command) < 4 or command[0] != sys.executable or command[1:3] != ["-I", "-B"]:
        raise RunnerError("invalid_public_command")
    try:
        executable = Path(sys.executable).resolve(strict=True)
    except OSError as exc:
        raise RunnerError("runtime_python_unavailable") from exc
    script = Path(command[3])
    allowed = {RAG_ROOT / "list_dbs.py", RAG_ROOT / "search.py", RAG_ROOT / "setup.py"}
    if not executable.is_file() or script not in allowed or not _safe_regular(script):
        raise RunnerError("public_entry_point_unavailable")


def _safe_regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not attributes & reparse


class _WindowsJob:
    def __init__(self, child: subprocess.Popen[bytes]) -> None:
        self.handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            child.kill()
            raise RunnerError("process_tree_control_failed")
        if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(handle), ctypes.c_void_p(child._handle)):
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            child.kill()
            raise RunnerError("process_tree_control_failed")
        self.handle = int(handle)

    def terminate(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), 1)

    def close(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


def _terminate_tree(child: subprocess.Popen[bytes], job: _WindowsJob | None) -> None:
    try:
        if job is not None:
            job.terminate()
        elif os.name != "nt":
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
    except (OSError, ProcessLookupError):
        try:
            child.kill()
        except OSError:
            pass


def _run_list() -> dict[str, Any]:
    raw, _code = _run_public(
        _list_command(), input_bytes=None, timeout=LIST_TIMEOUT_SECONDS,
        invalid_code="invalid_database_list",
    )
    if (
        set(raw) != {"schema", "status", "databases"}
        or raw.get("schema") != "local-rag.database-list.v2"
        or raw.get("status") != "ok"
        or not isinstance(raw.get("databases"), list)
    ):
        raise RunnerError("invalid_database_list")
    databases: list[dict[str, Any]] = []
    for item in raw["databases"]:
        if not isinstance(item, dict) or set(item) - {
            "name", "title", "query_hint", "content_summary", "source_count",
            "unattributed_document_count", "source_types", "sources",
            "additional_source_count", "content_summary_status",
        }:
            raise RunnerError("invalid_database_list")
        name = item.get("name")
        if not isinstance(name, str) or not _DATABASE_RE.fullmatch(name):
            raise RunnerError("invalid_database_list")
        projected: dict[str, Any] = {"name": name}
        for key, fallback in (("title", name), ("query_hint", "Not specified"), ("content_summary", "Not specified")):
            text, _changed, _truncated = sanitize_visible_text(item.get(key), 2048)
            projected[key] = text or fallback
        sources: list[dict[str, str]] = []
        raw_sources = item.get("sources") or []
        if not isinstance(raw_sources, list) or len(raw_sources) > 100:
            raise RunnerError("invalid_database_list")
        for source in raw_sources:
            if not isinstance(source, Mapping) or set(source) != {
                "name", "type", "label", "document_count"
            }:
                raise RunnerError("invalid_database_list")
            display, _changed, _truncated = sanitize_visible_text(source.get("name"), 512)
            source_type, _changed, _truncated = sanitize_visible_text(source.get("type"), 128)
            if display and source_type:
                sources.append({"display_name": display, "type": source_type})
        projected["sources"] = sources
        databases.append(projected)
    return {"schema_version": CATALOG_SCHEMA_VERSION, "status": "ok", "payload_complete": True, "databases": databases}


def _run_search(args: argparse.Namespace) -> dict[str, Any]:
    pointer, _code = _run_public(
        _search_command(args), input_bytes=args.question.encode("utf-8"),
        timeout=SEARCH_TIMEOUT_SECONDS, invalid_code="invalid_search_pointer",
    )
    result_id, pointer_size = parse_search_pointer(pointer)
    summary, expiry = load_initial_summary(result_id, args.db, spool_root=SPOOL_ROOT)
    if summary is None or expiry is None:
        raise RunnerError("invalid_result_bundle")
    binding = create_result_binding(result_id, args.db, summary, expiry, pointer_size, spool_root=SPOOL_ROOT)
    token = DiskTokenRegistry(REGISTRY_ROOT).add(binding) if binding.evidence_ids else ""
    return build_search_packet(summary, result_token=token, inspectable_evidence_ids=binding.evidence_ids)


def _run_detail(args: argparse.Namespace) -> dict[str, Any]:
    registry = DiskTokenRegistry(REGISTRY_ROOT)
    binding = registry.get(args.result_token)
    requested = validated_item_ids(args.item_id, maximum=1 if args.detail_level == "deep" else 3)
    if binding is None or any(item not in binding.evidence_ids for item in requested):
        return build_stale_evidence_detail()
    try:
        revalidate_result_binding(binding, spool_root=SPOOL_ROOT)
        expanded, _expiry = load_expanded_result(
            binding.result_set_id, requested, detail_level=args.detail_level,
            spool_root=SPOOL_ROOT,
        )
        revalidate_result_binding(binding, spool_root=SPOOL_ROOT)
        if expanded.get("status") != "ok":
            raise GatewayError("stale_result")
    except (GatewayError, OSError, ValueError, json.JSONDecodeError):
        registry.discard(args.result_token)
        return build_stale_evidence_detail()
    expanded = dict(expanded)
    expanded["selected_db"] = binding.selected_db
    return build_evidence_detail(expanded, result_token=args.result_token, evidence_ids=requested)


def _run_setup() -> tuple[dict[str, Any], int]:
    raw, returncode = _run_public(
        _setup_command(), input_bytes=None, timeout=SETUP_TIMEOUT_SECONDS,
        invalid_code="invalid_setup_result", allow_nonzero=True,
    )
    _validate_setup_result(raw)
    setup_complete = raw.get("setup_complete") is True and returncode == 0
    phase_value = raw.get("failed_check") or raw.get("status") or "unknown"
    error_value = raw.get("error_kind") or ("" if setup_complete else "setup_failed")
    packet = {
        "schema_version": SETUP_SCHEMA_VERSION,
        "status": "ok" if setup_complete else "error",
        "payload_complete": True,
        "setup_complete": setup_complete,
        "phase": _safe_code(phase_value, "unknown"),
        "retry_required": not setup_complete,
        "error_code": _safe_code(error_value, "setup_failed") if error_value else "",
    }
    return packet, 0 if setup_complete else 1


def _validate_setup_result(raw: Mapping[str, Any]) -> None:
    required = {
        "status", "setup_complete", "lookup_ready", "runtime", "network",
        "databases", "warnings", "next_action",
    }
    databases = raw.get("databases")
    if (
        not required.issubset(raw)
        or not isinstance(raw.get("status"), str)
        or not isinstance(raw.get("setup_complete"), bool)
        or not isinstance(raw.get("lookup_ready"), bool)
        or not isinstance(raw.get("runtime"), Mapping)
        or not isinstance(raw.get("network"), Mapping)
        or not isinstance(databases, Mapping)
        or set(databases) != {"healthy", "unhealthy"}
        or not isinstance(databases.get("healthy"), list)
        or not isinstance(databases.get("unhealthy"), list)
        or not isinstance(raw.get("warnings"), list)
        or not (
            raw.get("next_action") is None
            or isinstance(raw.get("next_action"), str)
        )
    ):
        raise RunnerError("invalid_setup_result")
    if raw["setup_complete"] is False and (
        not isinstance(raw.get("failed_check"), str)
        or not isinstance(raw.get("error_kind"), str)
    ):
        raise RunnerError("invalid_setup_result")


def _safe_code(value: object, fallback: str) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    return normalized if _SAFE_CODE_RE.fullmatch(normalized) else fallback


def _generic_error(schema: str, code: str) -> dict[str, Any]:
    safe = _safe_code(code, "runner_error")
    return {
        "schema_version": schema,
        "status": "response_too_large" if safe in {"response_too_large", "catalog_too_large"} else "error",
        "payload_complete": True,
        "error_code": safe,
    }


def _render(packet: Mapping[str, Any], *, limit: int, command: str) -> tuple[str, bool]:
    text = serialize_packet(packet) if command in {"search", "detail"} else _compact_json(packet)
    if len(text.encode("utf-8")) <= limit:
        return text, False
    if command in {"search", "detail"}:
        schema = SEARCH_SCHEMA_VERSION if command == "search" else DETAIL_SCHEMA_VERSION
        text = serialize_packet(build_error_packet(schema, "response_too_large"))
    else:
        schema = CATALOG_SCHEMA_VERSION if command == "list" else SETUP_SCHEMA_VERSION
        text = _compact_json(_generic_error(schema, "response_too_large"))
    if len(text.encode("utf-8")) > MAX_ERROR_PACKET_BYTES:
        raise RunnerError("error_packet_too_large")
    return text, True


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_protect_option_values(arguments))
    _validate(parser, args)
    try:
        exit_code = 0
        if args.command == "list":
            packet, limit = _run_list(), MAX_CATALOG_PACKET_BYTES
        elif args.command == "search":
            packet, limit = _run_search(args), MAX_SEARCH_PACKET_BYTES
        elif args.command == "detail":
            packet, limit = _run_detail(args), MAX_DETAIL_PACKET_BYTES
        else:
            packet, exit_code = _run_setup()
            limit = MAX_SETUP_PACKET_BYTES
        output, oversized = _render(packet, limit=limit, command=args.command)
        if oversized:
            exit_code = 0
    except (RunnerError, GatewayError) as exc:
        if args.command in {"search", "detail"}:
            schema = SEARCH_SCHEMA_VERSION if args.command == "search" else DETAIL_SCHEMA_VERSION
            packet = build_error_packet(schema, exc.code)
            output = serialize_packet(packet)
        else:
            schema = CATALOG_SCHEMA_VERSION if args.command == "list" else SETUP_SCHEMA_VERSION
            output = _compact_json(_generic_error(schema, exc.code))
        if len(output.encode("utf-8")) > MAX_ERROR_PACKET_BYTES:
            output = '{"schema_version":"local-rag-error-v1","status":"error","payload_complete":true,"error_code":"runner_error"}'
        exit_code = 1
    sys.stdout.write(output + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
