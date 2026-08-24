from __future__ import annotations

import concurrent.futures
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[2]
QUERY_ROOT = REPOSITORY_ROOT / ".copilot" / "rag" / "query"
SERVER_PATH = QUERY_ROOT / "mcp_server.py"
sys.path.insert(0, str(QUERY_ROOT))

import agent003_answer_packet as packet  # noqa: E402
import mcp_server  # noqa: E402
import result_bundle  # noqa: E402


REAL_POPEN = subprocess.Popen
REAL_RUN = subprocess.run
DATABASE = "alpha-rag"
QUESTION = "Orion の承認時間帯を教えてください。"
PRIVATE_PATH = r"C:\private\orion\approval.md"

LIST_SCRIPT = r"""
import json

print(json.dumps({
    "schema": "local-rag.database-list.v2",
    "status": "ok",
    "databases": [{
        "name": "alpha-rag",
        "title": "Alpha fixture",
        "query_hint": "Synthetic Alpha facts; source_path=C:\\private\\routing.json",
        "content_summary": "one canonical result",
        "sources": [{"path": "C:\\private\\must-not-leak.json"}],
        "secret_internal_field": "must-not-leak"
    }]
}, ensure_ascii=False, separators=(",", ":")))
"""

SEARCH_SCRIPT = r"""
import json
import os
import sys
import time
from pathlib import Path

argv_log = Path(os.environ["AGENT003_TEST_ARGV"])
argv_log.write_text(
    json.dumps(sys.argv[1:], ensure_ascii=False, separators=(",", ":")),
    encoding="utf-8",
)
if os.environ.get("AGENT003_TEST_MODE") == "block":
    Path(os.environ["AGENT003_TEST_CHILD"]).write_text(
        str(os.getpid()),
        encoding="ascii",
    )
    time.sleep(60)
pointer = Path(os.environ["AGENT003_TEST_POINTER"]).read_text(encoding="utf-8")
print(pointer)
"""


def canonical_payload() -> dict[str, object]:
    excerpt = (
        "Orion の承認時間帯は 02:00-02:15 です。 "
        r"summary_file=C:\private\orion\summary.json"
    )
    return {
        "schema": "local-rag.search.v1",
        "status": "ok",
        "answerability": "full",
        "selected_db": DATABASE,
        "query": QUESTION,
        "evidence": [
            {
                "id": "R1",
                "text": excerpt,
                "matched_excerpt": excerpt,
                "context_before": r"file:///C:/private/orion/before.txt",
                "context_after": "The window is authoritative.",
                "context_reason": "same_section_neighbor",
                "source_ranges": [
                    {
                        "kind": "matched",
                        "chunk_uid": "chunk-orion-1",
                        "section": "Approval",
                    }
                ],
                "source": {
                    "path": PRIVATE_PATH,
                    "title": PRIVATE_PATH,
                    "revision": "sha256:synthetic",
                },
                "location": {"section": "Approval"},
                "signals": ["lexical"],
            }
        ],
        "background_context": [],
        "related_context": [],
        "document_results": [
            {
                "path": PRIVATE_PATH,
                "title": PRIVATE_PATH,
                "section": "Approval",
                "preview": excerpt,
                "support_level": "direct",
                "authoritative": True,
                "relationship": "Contains the approval window.",
            }
        ],
        "warnings": [r"detail_file=C:\private\orion\detail.json"],
        "coverage": {"returned_distinct_documents": 1},
    }


@dataclass
class RuntimeFixture:
    temporary: tempfile.TemporaryDirectory[str]
    root: Path
    spool: Path
    pointer: dict[str, object]
    pointer_file: Path
    argv_log: Path
    child_marker: Path
    paths: mcp_server.RuntimePaths
    tools: mcp_server.LocalRagTools

    @property
    def result_id(self) -> str:
        return str(self.pointer["result_set_id"])


@contextmanager
def fake_runtime(*, registry_size: int = mcp_server.TOKEN_REGISTRY_SIZE):
    temporary = tempfile.TemporaryDirectory(prefix="agent003-mcp-test-")
    try:
        base = Path(temporary.name)
        root = base / "rag"
        spool = base / "managed-results"
        run_root = root / "query" / "run"
        run_root.mkdir(parents=True)
        list_script = root / "list_dbs.py"
        search_script = root / "search.py"
        list_script.write_text(LIST_SCRIPT, encoding="utf-8")
        search_script.write_text(SEARCH_SCRIPT, encoding="utf-8")
        daemon_state = run_root / "ragd.json"
        daemon_state.write_text(
            json.dumps(
                {
                    "schema": "local-rag.ragd.v2",
                    "pid": os.getpid(),
                    "generation": "generation-0123456789abcdef",
                    "code_fingerprint": "a" * 64,
                    "transport": "file",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        pointer = result_bundle.publish_result_bundle(
            canonical_payload(),
            spool_root=spool,
            now=datetime.now(timezone.utc),
        )
        pointer_file = base / "pointer.json"
        pointer_file.write_text(
            json.dumps(pointer, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        argv_log = base / "argv.json"
        child_marker = base / "child.pid"
        paths = mcp_server.RuntimePaths.create(
            root,
            python=Path(sys.executable),
            spool_root=spool,
        )
        tools = mcp_server.LocalRagTools(paths)
        tools.registry = mcp_server.TokenRegistry(maximum=registry_size)
        fixture = RuntimeFixture(
            temporary,
            root,
            spool,
            pointer,
            pointer_file,
            argv_log,
            child_marker,
            paths,
            tools,
        )
        environment = {
            "AGENT003_TEST_ARGV": str(argv_log),
            "AGENT003_TEST_CHILD": str(child_marker),
            "AGENT003_TEST_MODE": "normal",
            "AGENT003_TEST_POINTER": str(pointer_file),
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            try:
                yield fixture
            finally:
                tools.cancel_all()
    finally:
        temporary.cleanup()


@contextmanager
def unrelated_process():
    process = REAL_POPEN(
        [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def request(
    request_id: object,
    method: str,
    params: object | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        value["params"] = params
    return value


def notification(method: str, params: object | None = None) -> dict[str, object]:
    value: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        value["params"] = params
    return value


def wire(*messages: dict[str, object]) -> bytes:
    return b"".join(
        json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for message in messages
    )


def initialized_server(
    tools: mcp_server.LocalRagTools,
) -> mcp_server.McpServer:
    server = mcp_server.McpServer(tools)
    response = server.handle(
        request(
            1,
            "initialize",
            {
                "protocolVersion": mcp_server.LATEST_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
    )
    if response is None or response.get("result", {}).get(
        "protocolVersion"
    ) != mcp_server.LATEST_PROTOCOL:
        raise AssertionError("test server did not initialize")
    server.handle(notification("notifications/initialized"))
    return server


def call_packet(
    server: mcp_server.McpServer,
    request_id: object,
    name: str,
    arguments: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    response = server.handle(
        request(
            request_id,
            "tools/call",
            {"name": name, "arguments": arguments},
        )
    )
    if response is None or "error" in response:
        raise AssertionError(f"tool call failed: {response!r}")
    result = response["result"]
    if not isinstance(result, dict):
        raise AssertionError("tool result is not an object")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise AssertionError("structuredContent is not an object")
    return response, structured


def rendered(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class CoordinatedInput:
    def __init__(
        self,
        prefix: bytes,
        fixture: RuntimeFixture,
        *,
        cancel_request: object | None,
    ) -> None:
        self._lines = deque(prefix.splitlines(keepends=True))
        self.fixture = fixture
        self.cancel_request = cancel_request
        self.waited = False
        self.timed_out = False
        self.child: subprocess.Popen[bytes] | None = None

    def readline(self, _maximum: int) -> bytes:
        if self._lines:
            return self._lines.popleft()
        if not self.waited:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.fixture.tools._children_lock:
                    child = self.fixture.tools._children.get(41)
                if self.fixture.child_marker.is_file() and child is not None:
                    self.child = child
                    break
                time.sleep(0.01)
            else:
                self.timed_out = True
            self.waited = True
            if self.cancel_request is not None:
                return wire(
                    notification(
                        "notifications/cancelled",
                        {"requestId": self.cancel_request, "reason": "test"},
                    )
                )
        return b""


class QueuedCallsInput:
    def __init__(
        self,
        prefix: bytes,
        all_requests_delivered: threading.Event,
        all_responses_written: threading.Event,
    ) -> None:
        self._lines = deque(prefix.splitlines(keepends=True))
        self.all_requests_delivered = all_requests_delivered
        self.all_responses_written = all_responses_written
        self.timed_out = False

    def readline(self, _maximum: int) -> bytes:
        if self._lines:
            line = self._lines.popleft()
            if not self._lines:
                self.all_requests_delivered.set()
            return line
        if not self.all_responses_written.wait(3):
            self.timed_out = True
        return b""


class ResponseTrackingOutput(io.BytesIO):
    def __init__(self, all_responses_written: threading.Event) -> None:
        super().__init__()
        self.all_responses_written = all_responses_written
        self.tool_response_ids: set[object] = set()

    def write(self, value: bytes) -> int:
        written = super().write(value)
        response = json.loads(value)
        if response.get("id") in {41, 42}:
            self.tool_response_ids.add(response["id"])
            if self.tool_response_ids == {41, 42}:
                self.all_responses_written.set()
        return written


class SerialExecutionProbeTools:
    def __init__(self, all_requests_delivered: threading.Event) -> None:
        self.all_requests_delivered = all_requests_delivered
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.call_order: list[object] = []
        self.delivery_timed_out = False

    def call(
        self,
        _name: str,
        _arguments: object,
        *,
        request_id: object,
        cancelled: threading.Event,
    ) -> dict[str, object]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.call_order.append(request_id)
        try:
            if not self.all_requests_delivered.wait(2):
                self.delivery_timed_out = True
            time.sleep(0.05)
            if cancelled.is_set():
                raise AssertionError("probe call was unexpectedly cancelled")
            return {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"status": "ok"},
            }
        finally:
            with self._lock:
                self.active -= 1

    @staticmethod
    def cancel(_request_id: object) -> None:
        return None

    @staticmethod
    def cancel_all() -> None:
        return None


class FirstCallFailureProbeTools(SerialExecutionProbeTools):
    def call(
        self,
        name: str,
        arguments: object,
        *,
        request_id: object,
        cancelled: threading.Event,
    ) -> dict[str, object]:
        if request_id == 41:
            with self._lock:
                self.call_order.append(request_id)
            if not self.all_requests_delivered.wait(2):
                self.delivery_timed_out = True
            raise RuntimeError("synthetic unexpected failure")
        return super().call(
            name,
            arguments,
            request_id=request_id,
            cancelled=cancelled,
        )


class McpLifecycleContractTests(unittest.TestCase):
    def test_initialize_initialized_ping_and_preinit_rejection(self) -> None:
        with fake_runtime() as fixture:
            server = mcp_server.McpServer(fixture.tools)
            rejected = server.handle(request(1, "tools/list"))
            self.assertEqual(-32002, rejected["error"]["code"])
            initialized = server.handle(
                request(
                    2,
                    "initialize",
                    {"protocolVersion": "2099-01-01"},
                )
            )
            self.assertEqual(
                mcp_server.LATEST_PROTOCOL,
                initialized["result"]["protocolVersion"],
            )
            still_rejected = server.handle(request(3, "ping"))
            self.assertEqual(-32002, still_rejected["error"]["code"])
            self.assertIsNone(
                server.handle(notification("notifications/initialized"))
            )
            self.assertEqual({}, server.handle(request(4, "ping"))["result"])

    def test_tools_list_is_exactly_two_closed_world_read_only_tools(self) -> None:
        expected_output = {
            mcp_server.SEARCH_TOOL: packet.packet_output_schema(
                packet.SEARCH_SCHEMA_VERSION
            ),
            mcp_server.EVIDENCE_TOOL: packet.packet_output_schema(
                packet.DETAIL_SCHEMA_VERSION
            ),
        }
        with fake_runtime() as fixture:
            server = initialized_server(fixture.tools)
            response = server.handle(request(2, "tools/list"))
        definitions = response["result"]["tools"]
        self.assertEqual(
            [mcp_server.SEARCH_TOOL, mcp_server.EVIDENCE_TOOL],
            [definition["name"] for definition in definitions],
        )
        for definition in definitions:
            with self.subTest(tool=definition["name"]):
                self.assertEqual(
                    {
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                    definition["annotations"],
                )
                self.assertFalse(
                    definition["inputSchema"]["additionalProperties"]
                )
                self.assertIn("outputSchema", definition)
                if "outputSchema" in definition:
                    self.assertEqual(
                        expected_output[definition["name"]],
                        definition["outputSchema"],
                    )
                    self.assertFalse(
                        definition["outputSchema"]["additionalProperties"]
                    )
        search_definition = definitions[0]
        self.assertIn("again in the same turn", search_definition["description"])
        self.assertIn("without asking the user", search_definition["description"])

    def test_unknown_tool_and_unknown_arguments_are_invalid_params(self) -> None:
        with fake_runtime() as fixture:
            server = initialized_server(fixture.tools)
            unknown = server.handle(
                request(
                    2,
                    "tools/call",
                    {"name": "execute", "arguments": {}},
                )
            )
            extra = server.handle(
                request(
                    3,
                    "tools/call",
                    {
                        "name": mcp_server.SEARCH_TOOL,
                        "arguments": {
                            "question": "q",
                            "database": DATABASE,
                            "path": PRIVATE_PATH,
                        },
                    },
                )
            )
            oversized = server.handle(
                request(
                    4,
                    "tools/call",
                    {
                        "name": mcp_server.SEARCH_TOOL,
                        "arguments": {
                            "question": "x"
                            * (mcp_server.MAX_QUESTION_CHARS + 1),
                            "database": DATABASE,
                        },
                    },
                )
            )
        for response in (unknown, extra, oversized):
            self.assertEqual(-32602, response["error"]["code"])

    def test_utf8_json_lines_lifecycle_has_ping_and_exact_tool_list(self) -> None:
        with fake_runtime() as fixture:
            stdin = io.BytesIO(
                wire(
                    request(
                        1,
                        "initialize",
                        {"protocolVersion": mcp_server.LATEST_PROTOCOL},
                    ),
                    notification("notifications/initialized"),
                    request(2, "ping"),
                    request(3, "tools/list"),
                )
            )
            stdout = io.BytesIO()
            return_code = mcp_server.serve(
                stdin,
                stdout,
                mcp_server.McpServer(fixture.tools),
            )
        self.assertEqual(0, return_code)
        responses = [
            json.loads(line) for line in stdout.getvalue().splitlines()
        ]
        self.assertEqual([1, 2, 3], [item["id"] for item in responses])
        self.assertEqual({}, responses[1]["result"])
        self.assertEqual(
            [mcp_server.SEARCH_TOOL, mcp_server.EVIDENCE_TOOL],
            [
                item["name"]
                for item in responses[2]["result"]["tools"]
            ],
        )

    def test_two_outstanding_tool_calls_are_serialized_without_busy_error(
        self,
    ) -> None:
        all_requests_delivered = threading.Event()
        all_responses_written = threading.Event()
        tools = SerialExecutionProbeTools(all_requests_delivered)
        stdin = QueuedCallsInput(
            wire(
                request(
                    1,
                    "initialize",
                    {"protocolVersion": mcp_server.LATEST_PROTOCOL},
                ),
                notification("notifications/initialized"),
                request(
                    41,
                    "tools/call",
                    {
                        "name": mcp_server.SEARCH_TOOL,
                        "arguments": {"question": "first"},
                    },
                ),
                request(
                    42,
                    "tools/call",
                    {
                        "name": mcp_server.SEARCH_TOOL,
                        "arguments": {"question": "second"},
                    },
                ),
            ),
            all_requests_delivered,
            all_responses_written,
        )
        stdout = ResponseTrackingOutput(all_responses_written)

        return_code = mcp_server.serve(
            stdin,
            stdout,
            mcp_server.McpServer(tools),
        )

        responses = [
            json.loads(line) for line in stdout.getvalue().splitlines()
        ]
        tool_responses = [
            item for item in responses if item.get("id") in {41, 42}
        ]
        self.assertEqual(0, return_code)
        self.assertFalse(stdin.timed_out)
        self.assertFalse(tools.delivery_timed_out)
        self.assertEqual([41, 42], tools.call_order)
        self.assertEqual(1, tools.max_active)
        self.assertEqual([41, 42], [item["id"] for item in tool_responses])
        self.assertTrue(all("result" in item for item in tool_responses))
        self.assertTrue(all("error" not in item for item in tool_responses))
        self.assertNotIn("busy", rendered(tool_responses).lower())

    def test_worker_contains_one_unexpected_failure_and_serves_next_call(
        self,
    ) -> None:
        all_requests_delivered = threading.Event()
        all_responses_written = threading.Event()
        tools = FirstCallFailureProbeTools(all_requests_delivered)
        stdin = QueuedCallsInput(
            wire(
                request(
                    1,
                    "initialize",
                    {"protocolVersion": mcp_server.LATEST_PROTOCOL},
                ),
                notification("notifications/initialized"),
                request(
                    41,
                    "tools/call",
                    {
                        "name": mcp_server.SEARCH_TOOL,
                        "arguments": {"question": "first"},
                    },
                ),
                request(
                    42,
                    "tools/call",
                    {
                        "name": mcp_server.SEARCH_TOOL,
                        "arguments": {"question": "second"},
                    },
                ),
            ),
            all_requests_delivered,
            all_responses_written,
        )
        stdout = ResponseTrackingOutput(all_responses_written)

        return_code = mcp_server.serve(
            stdin,
            stdout,
            mcp_server.McpServer(tools),
        )

        responses = {
            item["id"]: item
            for item in (
                json.loads(line) for line in stdout.getvalue().splitlines()
            )
            if item.get("id") in {41, 42}
        }
        self.assertEqual(0, return_code)
        self.assertFalse(stdin.timed_out)
        self.assertFalse(tools.delivery_timed_out)
        self.assertEqual([41, 42], tools.call_order)
        self.assertEqual(-32603, responses[41]["error"]["code"])
        self.assertEqual("Internal error", responses[41]["error"]["message"])
        self.assertIn("result", responses[42])
        self.assertNotIn("error", responses[42])


class SearchAndTokenContractTests(unittest.TestCase):
    def _search(
        self,
        fixture: RuntimeFixture,
        *,
        request_id: int = 2,
    ) -> tuple[
        mcp_server.McpServer,
        dict[str, object],
        dict[str, object],
    ]:
        server = initialized_server(fixture.tools)
        response, structured = call_packet(
            server,
            request_id,
            mcp_server.SEARCH_TOOL,
            {"question": QUESTION, "database": DATABASE},
        )
        return server, response, structured

    def assert_no_locator_leak(
        self,
        value: object,
        fixture: RuntimeFixture,
    ) -> None:
        text = rendered(value).casefold()
        forbidden = (
            str(fixture.root),
            str(fixture.spool),
            fixture.result_id,
            PRIVATE_PATH,
            "summary_file",
            "detail_file",
            "result_set_id",
            "file://",
        )
        for needle in forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(str(needle).casefold(), text)

    def test_routing_is_sanitized_and_does_not_run_search(self) -> None:
        with fake_runtime() as fixture:
            server = initialized_server(fixture.tools)
            response, structured = call_packet(
                server,
                2,
                mcp_server.SEARCH_TOOL,
                {"question": QUESTION},
            )
            self.assertEqual("database_required", structured["status"])
            self.assertEqual(DATABASE, structured["candidates"][0]["name"])
            guidance = " ".join(structured["missing_information"])
            self.assertIn("retrieval has not run", guidance)
            self.assertIn("again in the same turn", guidance)
            self.assertIn("without asking the user", guidance)
            self.assertFalse(fixture.argv_log.exists())
            self.assert_no_locator_leak(response, fixture)

    def test_search_uses_fixed_public_argv_shell_false_and_returns_token(
        self,
    ) -> None:
        launches: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def launch(*args: object, **kwargs: object):
            launches.append((args, kwargs))
            return REAL_POPEN(*args, **kwargs)

        with fake_runtime() as fixture, mock.patch.object(
            mcp_server.subprocess,
            "Popen",
            side_effect=launch,
        ):
            server, response, structured = self._search(fixture)
            argv = json.loads(fixture.argv_log.read_text(encoding="utf-8"))
            token = structured["result_token"]
            self.assertRegex(token, mcp_server._TOKEN_RE)
            self.assertNotIn(fixture.result_id, token)
            self.assertEqual(
                [
                    "--db",
                    DATABASE,
                    "--include-db-hint",
                    "--compact-json",
                    "--require-daemon",
                    "--timeout",
                    "170",
                    "--result-delivery",
                    "file",
                    "--format",
                    "json",
                    QUESTION,
                ],
                argv,
            )
            self.assertEqual(2, len(launches), launches)
            self.assertEqual(
                [
                    str(fixture.paths.list_dbs),
                    str(fixture.paths.search),
                ],
                [str(call[0][0][2]) for call in launches],
            )
            for _args, kwargs in launches:
                self.assertIs(False, kwargs["shell"])
            self.assertLessEqual(
                len(rendered(response["result"]).encode("utf-8")),
                8 * 1024,
            )
            self.assertLessEqual(
                packet.tool_result_size(response["result"]),
                packet.MAX_TOOL_RESULT_BYTES,
            )
            self.assert_no_locator_leak(response, fixture)
            _detail_response, detail = call_packet(
                server,
                3,
                mcp_server.EVIDENCE_TOOL,
                {"result_token": token, "evidence_ids": ["E1"]},
            )
            self.assertEqual("ok", detail["status"])
            self.assertEqual(["E1"], detail["requested_evidence_ids"])
            self.assertEqual("E1", detail["evidence"][0]["id"])
            self.assert_no_locator_leak(detail, fixture)

    def test_evidence_cap_and_duplicate_ids_are_rejected(self) -> None:
        with fake_runtime() as fixture:
            server, _response, structured = self._search(fixture)
            token = structured["result_token"]
            duplicate = server.handle(
                request(
                    3,
                    "tools/call",
                    {
                        "name": mcp_server.EVIDENCE_TOOL,
                        "arguments": {
                            "result_token": token,
                            "evidence_ids": ["E1", "E1"],
                        },
                    },
                )
            )
            too_many = server.handle(
                request(
                    4,
                    "tools/call",
                    {
                        "name": mcp_server.EVIDENCE_TOOL,
                        "arguments": {
                            "result_token": token,
                            "evidence_ids": ["E1", "E2", "E3", "E4"],
                        },
                    },
                )
            )
        self.assertEqual(-32602, duplicate["error"]["code"])
        self.assertEqual(-32602, too_many["error"]["code"])

    def test_wrong_expired_evicted_and_different_registry_tokens_are_stale(
        self,
    ) -> None:
        with fake_runtime(registry_size=2) as fixture:
            server, _response, structured = self._search(fixture)
            first_token = structured["result_token"]
            binding = fixture.tools.registry.get(first_token)
            self.assertIsNotNone(binding)
            assert binding is not None

            wrong = fixture.tools._evidence_detail(
                "lrt_" + "Z" * 24,
                ("E1",),
            )
            self.assertEqual("stale_result", wrong["status"])

            different_registry = mcp_server.LocalRagTools(
                fixture.paths
            )._evidence_detail(first_token, ("E1",))
            self.assertEqual("stale_result", different_registry["status"])

            fixture.tools.registry._entries[first_token] = replace(
                binding,
                expires_monotonic=time.monotonic() - 1,
            )
            expired = fixture.tools._evidence_detail(first_token, ("E1",))
            self.assertEqual("stale_result", expired["status"])

            tokens: list[str] = []
            for request_id in (10, 11, 12):
                _server, _response, item = self._search(
                    fixture,
                    request_id=request_id,
                )
                tokens.append(str(item["result_token"]))
            self.assertIsNone(fixture.tools.registry.get(tokens[0]))
            self.assertIsNotNone(fixture.tools.registry.get(tokens[1]))
            self.assertIsNotNone(fixture.tools.registry.get(tokens[2]))

    def test_registry_never_reuses_a_duplicate_generated_token(self) -> None:
        with fake_runtime() as fixture:
            _server, _response, structured = self._search(fixture)
            binding = fixture.tools.registry.get(
                str(structured["result_token"])
            )
            self.assertIsNotNone(binding)
            assert binding is not None
            registry = mcp_server.TokenRegistry()
            with mock.patch.object(
                mcp_server.secrets,
                "token_urlsafe",
                side_effect=["A" * 24, "A" * 24, "B" * 24],
            ):
                first = registry.add(binding)
                second = registry.add(binding)
            self.assertNotEqual(first, second)
            self.assertIsNotNone(registry.get(first))
            self.assertIsNotNone(registry.get(second))

    def test_token_from_another_server_process_is_stale(self) -> None:
        with fake_runtime() as fixture:
            _server, _response, structured = self._search(fixture)
            token = str(structured["result_token"])
            input_bytes = wire(
                request(
                    1,
                    "initialize",
                    {"protocolVersion": mcp_server.LATEST_PROTOCOL},
                ),
                notification("notifications/initialized"),
                request(
                    2,
                    "tools/call",
                    {
                        "name": mcp_server.EVIDENCE_TOOL,
                        "arguments": {
                            "result_token": token,
                            "evidence_ids": ["E1"],
                        },
                    },
                ),
            )
            completed = REAL_RUN(
                [
                    sys.executable,
                    "-B",
                    str(SERVER_PATH),
                    "--rag-root",
                    str(fixture.root),
                    "--python",
                    sys.executable,
                    "--spool-root",
                    str(fixture.spool),
                ],
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
                shell=False,
            )
        self.assertEqual(
            0,
            completed.returncode,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(b"", completed.stderr)
        responses = [
            json.loads(line) for line in completed.stdout.splitlines()
        ]
        self.assertEqual([1, 2], [item["id"] for item in responses])
        self.assertEqual(
            "stale_result",
            responses[1]["result"]["structuredContent"]["status"],
        )

    def test_daemon_revision_change_invalidates_token_without_leaking(
        self,
    ) -> None:
        with fake_runtime() as fixture:
            server, _response, structured = self._search(fixture)
            token = str(structured["result_token"])
            state = json.loads(
                fixture.paths.daemon_state.read_text(encoding="utf-8")
            )
            state["generation"] = "replacement-0123456789abcdef"
            fixture.paths.daemon_state.write_text(
                json.dumps(state, separators=(",", ":")),
                encoding="utf-8",
            )
            response, detail = call_packet(
                server,
                3,
                mcp_server.EVIDENCE_TOOL,
                {"result_token": token, "evidence_ids": ["E1"]},
            )
            self.assertEqual("stale_result", detail["status"])
            self.assertEqual("", detail["result_token"])
            self.assert_no_locator_leak(response, fixture)


class SizeAndErrorContractTests(unittest.TestCase):
    def test_contract_errors_use_the_called_tool_output_schema(self) -> None:
        broken = mock.Mock()
        broken.call.side_effect = packet.PacketContractError("synthetic")
        server = initialized_server(broken)
        cases = (
            (
                mcp_server.SEARCH_TOOL,
                {"question": QUESTION, "database": DATABASE},
                packet.SEARCH_SCHEMA_VERSION,
                "inspectable_evidence_ids",
            ),
            (
                mcp_server.EVIDENCE_TOOL,
                {
                    "result_token": "lrt_" + "A" * 24,
                    "evidence_ids": ["E1"],
                },
                packet.DETAIL_SCHEMA_VERSION,
                "requested_evidence_ids",
            ),
        )
        for index, (tool, arguments, schema, extra) in enumerate(cases, 2):
            with self.subTest(tool=tool):
                response, structured = call_packet(
                    server, index, tool, arguments
                )
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(schema, structured["schema_version"])
                self.assertEqual("error", structured["status"])
                self.assertEqual([], structured[extra])

    def test_one_mib_overflow_returns_errors_without_process_fallback(self) -> None:
        marker = "NO-FALLBACK-MARKER-" + "X" * packet.MAX_TOOL_RESULT_BYTES
        search_packet = packet.build_search_packet({
            "status": "ok",
            "database": DATABASE,
            "summary": {
                "status": "ok",
                "answerability": "full",
                "selected_db": DATABASE,
                "evidence": [{"id": "E1", "text": marker, "title": "Large"}],
            },
        })
        detail_packet = packet.build_evidence_detail(
            {
                "status": "ok",
                "database": DATABASE,
                "expanded_items": [{
                    "item_id": "E1",
                    "matched_excerpt": marker,
                    "title": "Large",
                }],
            },
            result_token="lrt_" + "A" * 24,
            evidence_ids=["E1"],
        )
        with fake_runtime() as fixture, mock.patch.object(
            fixture.tools, "_search", return_value=search_packet
        ) as search_call, mock.patch.object(
            fixture.tools, "_evidence_detail", return_value=detail_packet
        ) as detail_call, mock.patch.object(
            mcp_server.subprocess, "Popen"
        ) as process_start:
            server = initialized_server(fixture.tools)
            search_response, search = call_packet(
                server,
                2,
                mcp_server.SEARCH_TOOL,
                {"question": QUESTION, "database": DATABASE},
            )
            detail_response, detail = call_packet(
                server,
                3,
                mcp_server.EVIDENCE_TOOL,
                {
                    "result_token": "lrt_" + "A" * 24,
                    "evidence_ids": ["E1"],
                },
            )
        self.assertEqual("response_too_large", search["status"])
        self.assertEqual(packet.SEARCH_SCHEMA_VERSION, search["schema_version"])
        self.assertEqual("response_too_large", detail["status"])
        self.assertEqual(packet.DETAIL_SCHEMA_VERSION, detail["schema_version"])
        self.assertTrue(search_response["result"]["isError"])
        self.assertTrue(detail_response["result"]["isError"])
        self.assertNotIn(marker[:24], rendered((search_response, detail_response)))
        search_call.assert_called_once()
        detail_call.assert_called_once()
        process_start.assert_not_called()


class CancellationAndEofContractTests(unittest.TestCase):
    def _blocking_wire(self) -> bytes:
        return wire(
            request(
                1,
                "initialize",
                {"protocolVersion": mcp_server.LATEST_PROTOCOL},
            ),
            notification("notifications/initialized"),
            request(
                41,
                "tools/call",
                {
                    "name": mcp_server.SEARCH_TOOL,
                    "arguments": {
                        "question": QUESTION,
                        "database": DATABASE,
                    },
                },
            ),
        )

    def _exercise_reader_stop(
        self,
        fixture: RuntimeFixture,
        *,
        cancel_request: object | None,
    ) -> tuple[
        float,
        CoordinatedInput,
        list[dict[str, object]],
    ]:
        reader = CoordinatedInput(
            self._blocking_wire(),
            fixture,
            cancel_request=cancel_request,
        )
        stdout = io.BytesIO()
        start = time.monotonic()
        return_code = mcp_server.serve(
            reader,
            stdout,
            mcp_server.McpServer(fixture.tools),
        )
        elapsed = time.monotonic() - start
        self.assertEqual(0, return_code)
        responses = [
            json.loads(line) for line in stdout.getvalue().splitlines()
        ]
        return elapsed, reader, responses

    def test_cancel_notification_is_bounded_and_kills_only_active_child(
        self,
    ) -> None:
        with fake_runtime() as fixture, mock.patch.dict(
            os.environ,
            {"AGENT003_TEST_MODE": "block"},
            clear=False,
        ), unrelated_process() as unrelated:
            elapsed, reader, responses = self._exercise_reader_stop(
                fixture,
                cancel_request=41,
            )
            self.assertLess(elapsed, mcp_server.EOF_JOIN_SECONDS + 1)
            self.assertFalse(reader.timed_out)
            self.assertIsNotNone(reader.child)
            assert reader.child is not None
            self.assertIsNotNone(reader.child.poll())
            self.assertIsNone(unrelated.poll())
            self.assertEqual(os.getpid(), json.loads(
                fixture.paths.daemon_state.read_text(encoding="utf-8")
            )["pid"])
            self.assertEqual([1], [item["id"] for item in responses])
            self.assertEqual({}, fixture.tools._children)

    def test_eof_is_bounded_and_kills_only_active_child(self) -> None:
        with fake_runtime() as fixture, mock.patch.dict(
            os.environ,
            {"AGENT003_TEST_MODE": "block"},
            clear=False,
        ), unrelated_process() as unrelated:
            elapsed, reader, responses = self._exercise_reader_stop(
                fixture,
                cancel_request=None,
            )
            self.assertLess(elapsed, mcp_server.EOF_JOIN_SECONDS + 1)
            self.assertFalse(reader.timed_out)
            self.assertIsNotNone(reader.child)
            assert reader.child is not None
            self.assertIsNotNone(reader.child.poll())
            self.assertIsNone(unrelated.poll())
            self.assertEqual([1], [item["id"] for item in responses])
            self.assertEqual({}, fixture.tools._children)

    def test_public_timeout_is_bounded_and_kills_only_active_child(
        self,
    ) -> None:
        with fake_runtime() as fixture, mock.patch.dict(
            os.environ,
            {"AGENT003_TEST_MODE": "block"},
            clear=False,
        ), mock.patch.object(
            mcp_server,
            "SEARCH_TIMEOUT_SECONDS",
            0.2,
        ), unrelated_process() as unrelated:
            server = initialized_server(fixture.tools)
            call = request(
                41,
                "tools/call",
                {
                    "name": mcp_server.SEARCH_TOOL,
                    "arguments": {
                        "question": QUESTION,
                        "database": DATABASE,
                    },
                },
            )
            start = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1
            ) as executor:
                future = executor.submit(server.handle, call)
                deadline = time.monotonic() + 3
                child: subprocess.Popen[bytes] | None = None
                while time.monotonic() < deadline:
                    with fixture.tools._children_lock:
                        child = fixture.tools._children.get(41)
                    if fixture.child_marker.is_file() and child is not None:
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(child)
                response = future.result(timeout=3)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 2)
            self.assertIsNotNone(response)
            self.assertEqual(
                "error",
                response["result"]["structuredContent"]["status"],
            )
            assert child is not None
            self.assertIsNotNone(child.poll())
            self.assertIsNone(unrelated.poll())
            self.assertEqual({}, fixture.tools._children)


if __name__ == "__main__":
    unittest.main()
