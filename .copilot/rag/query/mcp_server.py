from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from agent003_answer_packet import (
    DETAIL_SCHEMA_VERSION,
    PacketContractError,
    SEARCH_SCHEMA_VERSION,
    build_error_packet,
    build_evidence_detail,
    build_search_packet,
    build_stale_evidence_detail,
    build_tool_result,
    packet_output_schema,
)
from result_bundle import load_expanded_result, load_initial_summary

SERVER_NAME, SERVER_VERSION = "local-rag-agent003", "1.0.0"
SEARCH_TOOL, EVIDENCE_TOOL = "local_rag_search", "local_rag_get_evidence"
LATEST_PROTOCOL = "2025-11-25"
SUPPORTED_PROTOCOLS = {
    LATEST_PROTOCOL, "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"
}
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_FILE_BYTES = 2 * 1024 * 1024
MAX_STATE_BYTES, MAX_QUESTION_CHARS = 64 * 1024, 16_000
LIST_TIMEOUT_SECONDS, SEARCH_TIMEOUT_SECONDS = 30.0, 180.0
SEARCH_RUNTIME_TIMEOUT_SECONDS = 170.0
EOF_JOIN_SECONDS, TOKEN_TTL_SECONDS, TOKEN_REGISTRY_SIZE = 2.0, 15 * 60, 32
MAX_OUTSTANDING_TOOL_CALLS = 8
_DATABASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_TOKEN_RE = re.compile(r"^lrt_[A-Za-z0-9_-]{20,92}$")
_EVIDENCE_ID_RE = re.compile(r"^E[1-9]\d?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ToolInputError(ValueError):
    pass


class RuntimeErrorCode(RuntimeError):
    def __init__(self, code: str, *, exit_code: int | None = None) -> None:
        super().__init__(code)
        self.code, self.exit_code = code, exit_code


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _strict_object(raw: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorCode(code) from exc
    if not isinstance(value, dict):
        raise RuntimeErrorCode(code)
    return value


def _regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attr = int(getattr(info, "st_file_attributes", 0) or 0)
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISREG(info.st_mode) and not (stat.S_ISLNK(info.st_mode) or attr & flag)


def _read_object(path: Path, maximum: int, code: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes() if _regular(path) else b""
    except OSError as exc:
        raise RuntimeErrorCode(code) from exc
    if not 2 <= len(raw) <= maximum:
        raise RuntimeErrorCode(code)
    return _strict_object(raw, code)


def _digest(path: Path) -> tuple[str, int]:
    try:
        before = path.lstat()
        if not _regular(path) or not 2 <= before.st_size <= MAX_BUNDLE_FILE_BYTES:
            raise RuntimeErrorCode("invalid_result_bundle")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(64 * 1024):
                digest.update(block)
        after = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(before) != identity(after) or not _regular(path):
            raise RuntimeErrorCode("invalid_result_bundle")
    except OSError as exc:
        raise RuntimeErrorCode("result_bundle_unavailable") from exc
    return digest.hexdigest(), before.st_size


@dataclass(frozen=True)
class RuntimePaths:
    rag_root: Path
    python: Path
    list_dbs: Path
    search: Path
    spool_root: Path
    daemon_state: Path

    @classmethod
    def create(cls, rag_root: Path, *, python: Path | None = None,
               spool_root: Path | None = None) -> "RuntimePaths":
        root = rag_root.expanduser().absolute()
        executable = python or (root / "query" / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        ))
        spool = spool_root or Path(tempfile.gettempdir()) / "GitHubCopilotLocalRAG/results"
        return cls(root, executable.expanduser().absolute(), root / "list_dbs.py",
                   root / "search.py", spool.expanduser().absolute(),
                   root / "query/run/ragd.json")


@dataclass(frozen=True)
class ResultBinding:
    result_set_id: str
    database: str
    evidence_ids: tuple[str, ...]
    expires_monotonic: float
    daemon_identity: tuple[int, str, str, str]
    manifest_integrity: tuple[str, int]
    summary_integrity: tuple[str, int]
    bundle_size: int


class TokenRegistry:
    def __init__(self, *, maximum: int = TOKEN_REGISTRY_SIZE) -> None:
        self.maximum = min(TOKEN_REGISTRY_SIZE, max(1, int(maximum)))
        self._entries: OrderedDict[str, ResultBinding] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, binding: ResultBinding) -> str:
        with self._lock:
            self._prune()
            token = "lrt_" + secrets.token_urlsafe(24)
            while token in self._entries:
                token = "lrt_" + secrets.token_urlsafe(24)
            self._entries[token] = binding
            while len(self._entries) > self.maximum:
                self._entries.popitem(last=False)
            return token

    def get(self, token: str) -> ResultBinding | None:
        if not _TOKEN_RE.fullmatch(token):
            return None
        with self._lock:
            self._prune()
            value = self._entries.get(token)
            if value:
                self._entries.move_to_end(token)
            return value

    def discard(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)

    def _prune(self) -> None:
        now = time.monotonic()
        for token, value in list(self._entries.items()):
            if value.expires_monotonic <= now:
                self._entries.pop(token, None)


class LocalRagTools:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths, self.registry = paths, TokenRegistry()
        self._children: dict[object, subprocess.Popen[bytes]] = {}
        self._children_lock = threading.Lock()

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        readonly = {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False}
        question = {"type": "string", "minLength": 1,
                    "maxLength": MAX_QUESTION_CHARS}
        database = {"type": "string", "pattern": _DATABASE_RE.pattern}
        token = {"type": "string", "pattern": _TOKEN_RE.pattern}
        ids = {"type": "array", "minItems": 1, "maxItems": 3,
               "uniqueItems": True,
               "items": {"type": "string", "pattern": _EVIDENCE_ID_RE.pattern}}
        def tool(name: str, title: str, description: str,
                 properties: dict[str, Any], required: list[str],
                 output_schema: dict[str, Any]) -> dict[str, Any]:
            return {"name": name, "title": title, "description": description,
                    "inputSchema": {"type": "object", "additionalProperties": False,
                                    "properties": properties, "required": required},
                    "outputSchema": output_schema, "annotations": readonly}
        return [
            tool(SEARCH_TOOL, "Local RAG search",
                 "Search the shared read-only Local RAG runtime. Omit database for routing; when one metadata match is clear, immediately call again in the same turn with the unchanged question and exact database name without asking the user.",
                 {"question": question, "database": database}, ["question"],
                 packet_output_schema(SEARCH_SCHEMA_VERSION)),
            tool(EVIDENCE_TOOL, "Local RAG evidence detail",
                 "Inspect up to three E evidence IDs from one opaque result token without searching.",
                 {"result_token": token, "evidence_ids": ids},
                 ["result_token", "evidence_ids"],
                 packet_output_schema(DETAIL_SCHEMA_VERSION)),
        ]

    def call(self, name: str, arguments: object, *, request_id: object,
             cancelled: threading.Event) -> dict[str, Any]:
        if name == SEARCH_TOOL:
            question, database = self._validate_search(arguments)
            packet = (self._search(question, database, request_id, cancelled)
                      if database else build_search_packet(
                          self._database_candidates(request_id, cancelled)))
        elif name == EVIDENCE_TOOL:
            token, ids = self._validate_evidence(arguments)
            packet = self._evidence_detail(token, ids)
        else:
            raise ToolInputError("unknown tool")
        return build_tool_result(packet)

    def cancel(self, request_id: object) -> None:
        with self._children_lock:
            child = self._children.get(request_id)
        if child is not None and child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass

    def cancel_all(self) -> None:
        with self._children_lock:
            request_ids = list(self._children)
        for request_id in request_ids:
            self.cancel(request_id)

    @staticmethod
    def _validate_search(value: object) -> tuple[str, str | None]:
        if not isinstance(value, dict) or set(value) - {"question", "database"}:
            raise ToolInputError("invalid search arguments")
        question, database = value.get("question"), value.get("database")
        if (not isinstance(question, str) or not question.strip() or
                len(question) > MAX_QUESTION_CHARS or "\x00" in question):
            raise ToolInputError("invalid question")
        if database is not None and (not isinstance(database, str) or
                                     not _DATABASE_RE.fullmatch(database)):
            raise ToolInputError("invalid database")
        return question, database

    @staticmethod
    def _validate_evidence(value: object) -> tuple[str, tuple[str, ...]]:
        if not isinstance(value, dict) or set(value) != {"result_token", "evidence_ids"}:
            raise ToolInputError("invalid evidence arguments")
        token, raw_ids = value.get("result_token"), value.get("evidence_ids")
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise ToolInputError("invalid result token")
        ids = tuple(raw_ids) if isinstance(raw_ids, list) else ()
        if (not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids) or
                any(not isinstance(item, str) or not _EVIDENCE_ID_RE.fullmatch(item)
                    for item in ids)):
            raise ToolInputError("invalid evidence IDs")
        return token, ids

    def _database_candidates(self, request_id: object,
                             cancelled: threading.Event) -> dict[str, Any]:
        candidates = self._database_catalog(request_id, cancelled)
        return {"status": "database_required", "candidates": candidates[:20],
                "instruction": "Routing only; retrieval has not run. When one metadata match is clear, the Agent must call local_rag_search again in the same turn with the unchanged question and exact database name without asking the user. Routing metadata is not evidence."}

    def _database_catalog(self, request_id: object,
                          cancelled: threading.Event) -> list[dict[str, str]]:
        raw = self._run_public(
            [str(self.paths.python), "-B", str(self.paths.list_dbs), "--format", "json"],
            LIST_TIMEOUT_SECONDS, request_id, cancelled, "invalid_database_list")
        if raw.get("status") != "ok" or not isinstance(raw.get("databases"), list):
            raise RuntimeErrorCode("database_list_failed")
        if len(raw["databases"]) > 10_000:
            raise RuntimeErrorCode("invalid_database_list")
        candidates: list[dict[str, str]] = []
        for item in raw["databases"]:
            name = str(item.get("name") or "") if isinstance(item, dict) else ""
            if not _DATABASE_RE.fullmatch(name):
                raise RuntimeErrorCode("invalid_database_list")
            candidate = {"name": name}
            for key in ("title", "query_hint", "content_summary"):
                text = item.get(key)
                if isinstance(text, str) and text.strip():
                    candidate[key] = text
            candidates.append(candidate)
        return candidates

    def _search(self, question: str, database: str, request_id: object,
                cancelled: threading.Event) -> dict[str, Any]:
        if database not in {
            item["name"] for item in self._database_catalog(request_id, cancelled)
        }:
            raise RuntimeErrorCode("unknown_database")
        argv = [str(self.paths.python), "-B", str(self.paths.search),
                "--db", database, "--include-db-hint", "--compact-json",
                "--require-daemon", "--timeout",
                str(int(SEARCH_RUNTIME_TIMEOUT_SECONDS)),
                "--result-delivery", "file",
                "--format", "json", question]
        pointer = self._run_public(argv, SEARCH_TIMEOUT_SECONDS, request_id,
                                   cancelled, "invalid_search_pointer")
        result_id, pointer_size = self._pointer(pointer)
        summary, expiry = load_initial_summary(
            result_id, database, spool_root=self.paths.spool_root)
        if summary is None or expiry is None:
            raise RuntimeErrorCode("invalid_result_bundle")
        binding = self._binding(result_id, database, summary, expiry, pointer_size)
        token = self.registry.add(binding) if binding.evidence_ids else ""
        return build_search_packet(summary, result_token=token,
                                   inspectable_evidence_ids=binding.evidence_ids)

    def _evidence_detail(self, token: str,
                         evidence_ids: tuple[str, ...]) -> dict[str, Any]:
        binding = self.registry.get(token)
        if binding is None or any(item not in binding.evidence_ids for item in evidence_ids):
            return build_stale_evidence_detail()
        try:
            self._revalidate(binding)
            expanded, _ = load_expanded_result(
                binding.result_set_id, evidence_ids, detail_level="standard",
                spool_root=self.paths.spool_root)
            if expanded.get("status") != "ok":
                raise RuntimeErrorCode("stale_result")
        except (RuntimeErrorCode, OSError, ValueError, json.JSONDecodeError):
            self.registry.discard(token)
            return build_stale_evidence_detail()
        return build_evidence_detail(expanded, result_token=token,
                                     evidence_ids=evidence_ids)

    def _run_public(self, argv: list[str], timeout: float, request_id: object,
                    cancelled: threading.Event, invalid_code: str) -> dict[str, Any]:
        if cancelled.is_set():
            raise RuntimeErrorCode("request_cancelled")
        if not _regular(self.paths.python) or not _regular(Path(argv[2])):
            raise RuntimeErrorCode("public_entry_point_unavailable")
        try:
            child = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               if os.name == "nt" else 0))
        except OSError as exc:
            raise RuntimeErrorCode("public_command_start_failed") from exc
        with self._children_lock:
            self._children[request_id] = child
        try:
            if cancelled.is_set():
                self.cancel(request_id)
            try:
                stdout, stderr = child.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self.cancel(request_id)
                stdout, stderr = child.communicate()
                raise RuntimeErrorCode("public_command_timeout") from exc
        finally:
            with self._children_lock:
                self._children.pop(request_id, None)
        if cancelled.is_set():
            raise RuntimeErrorCode("request_cancelled")
        if child.returncode:
            raise RuntimeErrorCode("public_command_failed", exit_code=child.returncode)
        if max(len(stdout), len(stderr)) > MAX_COMMAND_OUTPUT_BYTES:
            raise RuntimeErrorCode("public_command_output_too_large")
        return _strict_object(stdout, invalid_code)

    @staticmethod
    def _pointer(value: dict[str, Any]) -> tuple[str, int]:
        result_id, size = value.get("result_set_id"), value.get("bytes")
        if (value.get("status") != "written" or
                value.get("schema_version") != "rag-result-pointer-v1" or
                not isinstance(result_id, str) or not isinstance(size, int) or
                isinstance(size, bool) or size < 2):
            raise RuntimeErrorCode("invalid_search_pointer")
        try:
            canonical = str(uuid.UUID(result_id))
        except ValueError as exc:
            raise RuntimeErrorCode("invalid_search_pointer") from exc
        if canonical != result_id:
            raise RuntimeErrorCode("invalid_search_pointer")
        return result_id, size

    def _daemon_identity(self) -> tuple[int, str, str, str]:
        state = _read_object(self.paths.daemon_state, MAX_STATE_BYTES,
                             "shared_daemon_unavailable")
        pid, generation = state.get("pid"), state.get("generation")
        fingerprint, transport = state.get("code_fingerprint"), state.get("transport", "tcp")
        if (state.get("schema") != "local-rag.ragd.v2" or
                not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or
                not isinstance(generation, str) or len(generation) < 16 or
                not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint) or
                transport not in {"tcp", "file", "unix"}):
            raise RuntimeErrorCode("shared_daemon_unavailable")
        return pid, generation, fingerprint, transport

    def _binding(self, result_id: str, database: str, summary: dict[str, Any],
                 expiry: datetime, pointer_size: int) -> ResultBinding:
        folder = self.paths.spool_root / result_id
        manifest, summary_hash = _digest(folder / "manifest.json"), _digest(folder / "summary.json")
        meta = _read_object(folder / "meta.json", MAX_BUNDLE_FILE_BYTES,
                            "invalid_result_bundle")
        bundle_size = meta.get("bundle_bytes")
        if (summary_hash[1] != pointer_size or meta.get("result_set_id") != result_id or
                meta.get("selected_db") != database or not isinstance(bundle_size, int) or
                isinstance(bundle_size, bool) or bundle_size <= 0):
            raise RuntimeErrorCode("invalid_result_bundle")
        follow_up = summary.get("follow_up")
        available = follow_up.get("available_item_ids") if isinstance(follow_up, dict) else []
        ids = tuple(item for item in available
                    if isinstance(item, str) and _EVIDENCE_ID_RE.fullmatch(item))
        remaining = (expiry.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise RuntimeErrorCode("stale_result")
        return ResultBinding(result_id, database, ids,
                             time.monotonic() + min(TOKEN_TTL_SECONDS, remaining),
                             self._daemon_identity(), manifest, summary_hash, bundle_size)

    def _revalidate(self, binding: ResultBinding) -> None:
        folder = self.paths.spool_root / binding.result_set_id
        meta = _read_object(folder / "meta.json", MAX_BUNDLE_FILE_BYTES, "stale_result")
        if (self._daemon_identity() != binding.daemon_identity or
                _digest(folder / "manifest.json") != binding.manifest_integrity or
                _digest(folder / "summary.json") != binding.summary_integrity or
                meta.get("result_set_id") != binding.result_set_id or
                meta.get("selected_db") != binding.database or
                meta.get("bundle_bytes") != binding.bundle_size):
            raise RuntimeErrorCode("stale_result")


class McpServer:
    def __init__(self, tools: LocalRagTools) -> None:
        self.tools, self._state = tools, "new"
        self._state_lock = threading.Lock()

    def handle(self, message: object, *, cancelled: threading.Event | None = None
               ) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self.error(None, -32600, "Invalid Request")
        method, request_id, is_request = message.get("method"), message.get("id"), "id" in message
        if not isinstance(method, str):
            return self.error(request_id, -32600, "Invalid Request") if is_request else None
        if method == "notifications/initialized":
            with self._state_lock:
                if self._state == "initialize_sent":
                    self._state = "ready"
            return None
        if method in {"notifications/cancelled", "notifications/roots/list_changed"}:
            return None
        if not is_request:
            return None
        if method == "initialize":
            return self._initialize(request_id, message.get("params"))
        with self._state_lock:
            ready = self._state == "ready"
        if not ready:
            return self.error(request_id, -32002, "Server is not initialized")
        if method == "ping":
            return self.result(request_id, {})
        if method == "tools/list":
            return self.result(request_id, {"tools": self.tools.definitions()})
        if method == "tools/call":
            return self._call_tool(request_id, message.get("params"),
                                   cancelled or threading.Event())
        return self.error(request_id, -32601, "Method not found")

    def _initialize(self, request_id: object, params: object) -> dict[str, Any]:
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        with self._state_lock:
            self._state = "initialize_sent"
        return self.result(request_id, {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Two read-only Local RAG tools are available with a 1 MiB response safeguard.",
        })

    def _call_tool(self, request_id: object, params: object,
                   cancelled: threading.Event) -> dict[str, Any] | None:
        if cancelled.is_set():
            return None
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return self.error(request_id, -32602, "Invalid tool call")
        try:
            result = self.tools.call(params["name"], params.get("arguments", {}),
                                     request_id=request_id, cancelled=cancelled)
        except ToolInputError as exc:
            return self.error(request_id, -32602, str(exc))
        except (PacketContractError, RuntimeErrorCode) as exc:
            code = exc.code if isinstance(exc, RuntimeErrorCode) else "packet_contract_error"
            if code == "request_cancelled" or cancelled.is_set():
                return None
            schema = (DETAIL_SCHEMA_VERSION
                      if params["name"] == EVIDENCE_TOOL
                      else SEARCH_SCHEMA_VERSION)
            result = build_tool_result(build_error_packet(schema, code),
                                       is_error=True)
        return self.result(request_id, result)

    @staticmethod
    def result(request_id: object, result: object) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def error(request_id: object, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}


def serve(stdin: BinaryIO, stdout: BinaryIO, server: McpServer) -> int:
    output_lock, calls_lock = threading.Lock(), threading.Lock()
    calls: dict[object, threading.Event] = {}
    call_queue: queue.Queue[
        tuple[dict[str, Any], object, threading.Event]
    ] = queue.Queue(maxsize=MAX_OUTSTANDING_TOOL_CALLS)
    closing = threading.Event()

    def write(response: dict[str, Any] | None) -> None:
        if response is None or closing.is_set():
            return
        encoded = _compact_json(response).encode("utf-8") + b"\n"
        with output_lock:
            if not closing.is_set():
                stdout.write(encoded)
                stdout.flush()

    def run_calls() -> None:
        while True:
            try:
                message, request_id, event = call_queue.get(timeout=0.05)
            except queue.Empty:
                if closing.is_set():
                    return
                continue
            try:
                if not event.is_set():
                    try:
                        response = server.handle(message, cancelled=event)
                    except Exception:
                        response = McpServer.error(
                            request_id, -32603, "Internal error"
                        )
                    if not event.is_set():
                        write(response)
            finally:
                with calls_lock:
                    calls.pop(request_id, None)
                call_queue.task_done()

    worker = threading.Thread(target=run_calls, daemon=True)
    worker.start()

    while True:  # The reader stays responsive while one worker serializes calls.
        line = stdin.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            # A finite stdin is also used by deterministic smoke tests and
            # one-shot clients.  Give already accepted calls the bounded EOF
            # window to finish before cancelling genuinely stuck work.
            deadline = time.monotonic() + EOF_JOIN_SECONDS
            while True:
                with calls_lock:
                    outstanding = list(calls.items())
                if not outstanding or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            closing.set()
            with calls_lock:
                outstanding = list(calls.items())
            for request_id, event in outstanding:
                event.set()
                server.tools.cancel(request_id)
            server.tools.cancel_all()
            worker.join(0.5)
            return 0
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
            write(McpServer.error(None, -32700, "Parse error"))
            continue
        try:
            message = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            write(McpServer.error(None, -32700, "Parse error"))
            continue
        if isinstance(message, dict) and message.get("method") == "notifications/cancelled":
            params = message.get("params")
            request_id = params.get("requestId") if isinstance(params, dict) else None
            with calls_lock:
                event = calls.get(request_id)
            if event:
                event.set()
                server.tools.cancel(request_id)
            continue
        if isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message:
            request_id = message.get("id")
            if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                write(McpServer.error(None, -32600, "Invalid Request"))
                continue
            rejection: dict[str, Any] | None = None
            with calls_lock:
                if request_id in calls:
                    rejection = McpServer.error(
                        request_id, -32600, "Duplicate request id"
                    )
                elif len(calls) >= MAX_OUTSTANDING_TOOL_CALLS:
                    rejection = McpServer.error(
                        request_id, -32000, "Tool call queue is full"
                    )
                else:
                    event = threading.Event()
                    calls[request_id] = event
                    call_queue.put_nowait((message, request_id, event))
            write(rejection)
            continue
        write(server.handle(message))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Thin read-only Local RAG MCP server")
    parser.add_argument("--rag-root", type=Path, default=Path.home() / ".copilot/rag")
    parser.add_argument("--python", type=Path)
    parser.add_argument("--spool-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = RuntimePaths.create(args.rag_root, python=args.python,
                                spool_root=args.spool_root)
    return serve(sys.stdin.buffer, sys.stdout.buffer,
                 McpServer(LocalRagTools(paths)))


if __name__ == "__main__":
    raise SystemExit(main())
