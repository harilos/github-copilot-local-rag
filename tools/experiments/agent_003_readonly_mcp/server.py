from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


SERVER_NAME = "local-rag"
SERVER_VERSION = "0.1.0-agent003-poc"
TOOL_NAME = "local_rag_search"
RESULT_SCHEMA = "local-rag.mcp-result.v1"
SUMMARY_SCHEMA = "rag-initial-answer-v1"
LATEST_PROTOCOL = "2025-11-25"
SUPPORTED_PROTOCOLS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
)
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_QUESTION_CHARS = 16_000
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_SUMMARY_BYTES = 2 * 1024 * 1024
LIST_TIMEOUT_SECONDS = 30
SEARCH_TIMEOUT_SECONDS = 180
_DATABASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")


class ToolInputError(ValueError):
    pass


class PublicCommandError(RuntimeError):
    def __init__(self, code: str, *, exit_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_child(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _strict_object(raw: bytes, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicCommandError(code) from exc
    if not isinstance(value, dict):
        raise PublicCommandError(code)
    return value


@dataclass(frozen=True)
class RuntimePaths:
    rag_root: Path
    python: Path
    list_dbs: Path
    search: Path
    spool_root: Path

    @classmethod
    def create(
        cls,
        rag_root: Path,
        *,
        python: Path | None = None,
        spool_root: Path | None = None,
    ) -> "RuntimePaths":
        root = rag_root.expanduser().resolve(strict=False)
        if not root.is_absolute():
            raise ValueError("rag_root must be absolute")
        if python is None:
            executable = (
                root / "query" / ".venv" / "Scripts" / "python.exe"
                if os.name == "nt"
                else root / "query" / ".venv" / "bin" / "python"
            )
        else:
            executable = python
        selected_spool = spool_root or (
            Path(tempfile.gettempdir()) / "GitHubCopilotLocalRAG" / "results"
        )
        return cls(
            rag_root=root,
            python=executable.expanduser().resolve(strict=False),
            list_dbs=(root / "list_dbs.py").resolve(strict=False),
            search=(root / "search.py").resolve(strict=False),
            spool_root=selected_spool.expanduser().resolve(strict=False),
        )


class LocalRagTool:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths

    def definition(self) -> dict[str, Any]:
        return {
            "name": TOOL_NAME,
            "title": "Local RAG search",
            "description": (
                "Read-only lookup in installed Local RAG. Omit database to get "
                "routing candidates, then call again with exactly one returned "
                "database name. The question is passed unchanged to the public "
                "search entry point."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_QUESTION_CHARS,
                        "description": "The latest user-authored semantic question.",
                    },
                    "database": {
                        "type": "string",
                        "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$",
                        "description": (
                            "An exact database name returned by an earlier call. "
                            "Omit it to list routing candidates."
                        ),
                    },
                },
                "required": ["question"],
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }

    def call(self, arguments: object) -> dict[str, Any]:
        question, database = self._validate_arguments(arguments)
        if database is None:
            return self._database_candidates()
        return self._search(question, database)

    def _validate_arguments(self, arguments: object) -> tuple[str, str | None]:
        if not isinstance(arguments, dict):
            raise ToolInputError("arguments must be an object")
        unknown = set(arguments) - {"question", "database"}
        if unknown:
            raise ToolInputError("unknown tool argument")
        question = arguments.get("question")
        if not isinstance(question, str):
            raise ToolInputError("question must be a string")
        if not question.strip():
            raise ToolInputError("question must not be empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise ToolInputError("question is too long")
        if "\x00" in question:
            raise ToolInputError("question contains a null character")
        database = arguments.get("database")
        if database is None:
            return question, None
        if not isinstance(database, str) or not _DATABASE_RE.fullmatch(database):
            raise ToolInputError("database name is invalid")
        return question, database

    def _database_candidates(self) -> dict[str, Any]:
        payload = self._run_json(
            self.paths.list_dbs,
            ["--format", "json"],
            timeout=LIST_TIMEOUT_SECONDS,
            invalid_code="invalid_database_list",
        )
        if payload.get("status") != "ok" or not isinstance(
            payload.get("databases"), list
        ):
            raise PublicCommandError("database_list_failed")
        candidates: list[dict[str, Any]] = []
        for item in payload["databases"]:
            if not isinstance(item, dict):
                raise PublicCommandError("invalid_database_list")
            name = item.get("name")
            if not isinstance(name, str) or not _DATABASE_RE.fullmatch(name):
                raise PublicCommandError("invalid_database_list")
            candidate = {"name": name}
            for key in ("title", "query_hint", "content_summary"):
                value = item.get(key)
                if isinstance(value, str):
                    candidate[key] = value
            source_count = item.get("source_count")
            if isinstance(source_count, int) and not isinstance(
                source_count, bool
            ) and 0 <= source_count <= 1_000_000:
                candidate["source_count"] = source_count
            source_types: list[dict[str, Any]] = []
            raw_source_types = item.get("source_types")
            if isinstance(raw_source_types, list):
                for source_type in raw_source_types[:20]:
                    if not isinstance(source_type, dict):
                        continue
                    sanitized: dict[str, Any] = {}
                    for key in ("type", "label"):
                        value = source_type.get(key)
                        if isinstance(value, str):
                            sanitized[key] = value
                    count = source_type.get("count")
                    if isinstance(count, int) and not isinstance(
                        count, bool
                    ) and 0 <= count <= 1_000_000:
                        sanitized["count"] = count
                    if sanitized:
                        source_types.append(sanitized)
            if source_types:
                candidate["source_types"] = source_types
            candidates.append(candidate)
        return {
            "schema": RESULT_SCHEMA,
            "status": "database_required",
            "candidates": candidates,
            "instruction": (
                "Choose one database only when its routing metadata clearly "
                "matches the question, then call this tool again with that exact "
                "name. Routing metadata is not answer evidence."
            ),
        }

    def _search(self, question: str, database: str) -> dict[str, Any]:
        pointer = self._run_json(
            self.paths.search,
            [
                "--db",
                database,
                "--include-db-hint",
                "--compact-json",
                "--result-delivery",
                "file",
                "--format",
                "json",
                question,
            ],
            timeout=SEARCH_TIMEOUT_SECONDS,
            invalid_code="invalid_search_pointer",
        )
        summary = self._read_summary(pointer, database=database)
        return {
            "schema": RESULT_SCHEMA,
            "status": "ok",
            "database": database,
            "summary": summary,
        }

    def _run_json(
        self,
        script: Path,
        arguments: list[str],
        *,
        timeout: int,
        invalid_code: str,
    ) -> dict[str, Any]:
        self._validate_runtime_file(self.paths.python, "runtime_unavailable")
        self._validate_runtime_file(script, "public_entry_point_unavailable")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                [str(self.paths.python), "-B", str(script), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                shell=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise PublicCommandError("public_command_timeout") from exc
        except OSError as exc:
            raise PublicCommandError("public_command_start_failed") from exc
        if completed.returncode != 0:
            raise PublicCommandError(
                "public_command_failed", exit_code=completed.returncode
            )
        if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES:
            raise PublicCommandError("public_command_output_too_large")
        if len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES:
            raise PublicCommandError("public_command_output_too_large")
        return _strict_object(completed.stdout, code=invalid_code)

    def _validate_runtime_file(self, path: Path, code: str) -> None:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise PublicCommandError(code) from exc
        if not resolved.is_file():
            raise PublicCommandError(code)
        if path != self.paths.python and not _is_child(resolved, self.paths.rag_root):
            raise PublicCommandError(code)

    def _read_summary(
        self, pointer: dict[str, Any], *, database: str
    ) -> dict[str, Any]:
        result_set_id = pointer.get("result_set_id")
        summary_file = pointer.get("summary_file")
        if pointer.get("status") != "written" or not isinstance(
            result_set_id, str
        ) or not isinstance(summary_file, str):
            raise PublicCommandError("invalid_search_pointer")
        try:
            parsed_id = str(uuid.UUID(result_set_id))
        except ValueError as exc:
            raise PublicCommandError("invalid_search_pointer") from exc
        if parsed_id != result_set_id:
            raise PublicCommandError("invalid_search_pointer")
        try:
            summary_path = Path(summary_file).resolve(strict=True)
            spool_root = self.paths.spool_root.resolve(strict=True)
        except OSError as exc:
            raise PublicCommandError("search_summary_unavailable") from exc
        expected = spool_root / result_set_id / "summary.json"
        if summary_path != expected or not _is_child(summary_path, spool_root):
            raise PublicCommandError("invalid_search_pointer")
        try:
            size = summary_path.stat().st_size
            if size < 2 or size > MAX_SUMMARY_BYTES:
                raise PublicCommandError("invalid_search_summary")
            recorded_size = pointer.get("bytes")
            if not isinstance(recorded_size, int) or recorded_size != size:
                raise PublicCommandError("invalid_search_pointer")
            raw = summary_path.read_bytes()
        except OSError as exc:
            raise PublicCommandError("search_summary_unavailable") from exc
        summary = _strict_object(raw, code="invalid_search_summary")
        if (
            summary.get("schema_version") != SUMMARY_SCHEMA
            or summary.get("result_set_id") != result_set_id
            or summary.get("selected_db") != database
        ):
            raise PublicCommandError("invalid_search_summary")
        return summary


def _tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _compact_json(payload)}],
        "structuredContent": payload,
        "isError": is_error,
    }


class McpServer:
    def __init__(self, tool: LocalRagTool) -> None:
        self.tool = tool
        self.initialized = False

    def handle(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        method = message.get("method")
        request_id = message.get("id")
        is_request = "id" in message
        if not isinstance(method, str):
            if is_request:
                return self._error(request_id, -32600, "Invalid Request")
            return None
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method in {"notifications/cancelled", "notifications/roots/list_changed"}:
            return None
        if not is_request:
            return None
        if method == "initialize":
            return self._initialize(request_id, message.get("params"))
        if not self.initialized:
            return self._error(request_id, -32600, "Server is not initialized")
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": [self.tool.definition()]})
        if method == "tools/call":
            return self._call_tool(request_id, message.get("params"))
        return self._error(request_id, -32601, f"Method not found: {method}")

    def _initialize(self, request_id: object, params: object) -> dict[str, Any]:
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        negotiated = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        self.initialized = False
        return self._result(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "This server exposes one bounded read-only Local RAG tool."
                ),
            },
        )

    def _call_tool(self, request_id: object, params: object) -> dict[str, Any]:
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return self._error(request_id, -32602, "Unknown tool")
        try:
            payload = self.tool.call(params.get("arguments", {}))
        except ToolInputError as exc:
            return self._error(request_id, -32602, str(exc))
        except PublicCommandError as exc:
            payload = {
                "schema": RESULT_SCHEMA,
                "status": "error",
                "error": exc.code,
            }
            if exc.exit_code is not None:
                payload["exit_code"] = exc.exit_code
            return self._result(request_id, _tool_result(payload, is_error=True))
        return self._result(request_id, _tool_result(payload, is_error=False))

    @staticmethod
    def _result(request_id: object, result: object) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: object, code: int, message: str, data: object | None = None
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve(stdin: BinaryIO, stdout: BinaryIO, server: McpServer) -> int:
    while True:
        line = stdin.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
            response = McpServer._error(None, -32700, "Parse error")
        else:
            try:
                message = json.loads(line.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = McpServer._error(None, -32700, "Parse error")
            else:
                response = server.handle(message)
        if response is not None:
            stdout.write(_compact_json(response).encode("utf-8") + b"\n")
            stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded read-only Local RAG MCP stdio server"
    )
    parser.add_argument(
        "--rag-root",
        type=Path,
        default=Path.home() / ".copilot" / "rag",
    )
    parser.add_argument("--python", type=Path)
    parser.add_argument("--spool-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = RuntimePaths.create(
        args.rag_root,
        python=args.python,
        spool_root=args.spool_root,
    )
    return serve(
        sys.stdin.buffer,
        sys.stdout.buffer,
        McpServer(LocalRagTool(paths)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
