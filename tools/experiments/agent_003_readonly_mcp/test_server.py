from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
SERVER_PATH = HERE / "server.py"
REPOSITORY_ROOT = HERE.parents[2]
SPEC = importlib.util.spec_from_file_location("local_rag_mcp_server", SERVER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load MCP server")
mcp_server = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mcp_server
SPEC.loader.exec_module(mcp_server)


LIST_SCRIPT = r'''
import json
print(json.dumps({
    "schema": "local-rag.database-list.v2",
    "status": "ok",
    "databases": [
        {
            "name": "alpha-evidence-rag",
            "title": "Alpha fixture",
            "query_hint": "Synthetic Alpha facts only.",
            "content_summary": "one document",
            "source_count": 1,
            "source_types": [
                {"type": "other", "count": 1, "secret_nested": "must-not-leak"}
            ],
            "sources": [{"secret_internal_field": "must-not-leak"}]
        }
    ]
}, ensure_ascii=False, separators=(",", ":")))
'''


SEARCH_SCRIPT = r'''
import json
import os
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
log_path = os.environ.get("AGENT003_TEST_ARGV")
if log_path:
    Path(log_path).write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
database = args[args.index("--db") + 1]
result_id = str(uuid.uuid4())
spool = Path(os.environ["AGENT003_TEST_SPOOL"])
result_dir = spool / result_id
result_dir.mkdir(parents=True, exist_ok=True)
summary = {
    "schema_version": "rag-initial-answer-v1",
    "status": "ok",
    "answerability": "answerable",
    "selected_db": database,
    "result_set_id": result_id,
    "initial_response": {"answer_draft_markdown": "Alpha is 42. [E1]"},
    "evidence": [{"id": "E1", "excerpt": "Alpha is 42."}],
    "background_context": [],
    "document_results": [],
    "warnings": []
}
summary_path = result_dir / "summary.json"
summary_path.write_text(
    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
    encoding="utf-8"
)
if os.environ.get("AGENT003_TEST_ESCAPE") == "1":
    escaped = spool.parent / "escaped-summary.json"
    escaped.write_text(summary_path.read_text(encoding="utf-8"), encoding="utf-8")
    summary_path = escaped
pointer = {
    "status": "written",
    "schema_version": "rag-result-pointer-v1",
    "result_set_id": result_id,
    "summary_file": str(summary_path),
    "bytes": summary_path.stat().st_size
}
print(json.dumps(pointer, ensure_ascii=False, separators=(",", ":")))
'''


@contextmanager
def fake_runtime():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "rag"
        spool = Path(raw) / "spool"
        root.mkdir()
        spool.mkdir()
        (root / "list_dbs.py").write_text(LIST_SCRIPT, encoding="utf-8")
        (root / "search.py").write_text(SEARCH_SCRIPT, encoding="utf-8")
        argv_log = Path(raw) / "argv.json"
        paths = mcp_server.RuntimePaths.create(
            root,
            python=Path(sys.executable),
            spool_root=spool,
        )
        with mock.patch.dict(
            os.environ,
            {
                "AGENT003_TEST_SPOOL": str(spool),
                "AGENT003_TEST_ARGV": str(argv_log),
            },
            clear=False,
        ):
            yield root, spool, argv_log, mcp_server.LocalRagTool(paths)


class LocalRagToolContractTests(unittest.TestCase):
    def test_exposes_exactly_one_read_only_closed_world_tool(self) -> None:
        with fake_runtime() as (_root, _spool, _log, tool):
            definition = tool.definition()
        self.assertEqual("local_rag_search", definition["name"])
        self.assertEqual(
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            definition["annotations"],
        )
        self.assertFalse(definition["inputSchema"]["additionalProperties"])
        self.assertEqual(
            {"question", "database"},
            set(definition["inputSchema"]["properties"]),
        )

    def test_omitted_database_returns_only_sanitized_routing_metadata(self) -> None:
        with fake_runtime() as (_root, _spool, _log, tool):
            result = tool.call({"question": "Alphaは何ですか？"})
        self.assertEqual("database_required", result["status"])
        self.assertEqual("alpha-evidence-rag", result["candidates"][0]["name"])
        self.assertNotIn("sources", result["candidates"][0])
        self.assertNotIn("secret_internal_field", json.dumps(result))
        self.assertNotIn("secret_nested", json.dumps(result))

    def test_search_uses_exact_fixed_public_arguments_and_returns_summary(self) -> None:
        question = "Alpha の値は42ですか？"
        with fake_runtime() as (_root, _spool, argv_log, tool):
            result = tool.call(
                {"question": question, "database": "alpha-evidence-rag"}
            )
            argv = json.loads(argv_log.read_text(encoding="utf-8"))
        self.assertEqual("ok", result["status"])
        self.assertEqual("Alpha is 42.", result["summary"]["evidence"][0]["excerpt"])
        self.assertEqual(
            [
                "--db",
                "alpha-evidence-rag",
                "--include-db-hint",
                "--compact-json",
                "--result-delivery",
                "file",
                "--format",
                "json",
                question,
            ],
            argv,
        )

    def test_rejects_extra_arguments_database_injection_and_empty_question(self) -> None:
        with fake_runtime() as (_root, _spool, _log, tool):
            invalid = (
                {"question": "q", "command": "whoami"},
                {"question": "q", "database": "alpha-rag; whoami"},
                {"question": "  "},
            )
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(mcp_server.ToolInputError):
                        tool.call(arguments)

    def test_rejects_summary_pointer_outside_the_managed_spool(self) -> None:
        with fake_runtime() as (_root, _spool, _log, tool):
            with mock.patch.dict(
                os.environ, {"AGENT003_TEST_ESCAPE": "1"}, clear=False
            ):
                with self.assertRaises(mcp_server.PublicCommandError) as raised:
                    tool.call(
                        {"question": "q", "database": "alpha-evidence-rag"}
                    )
        self.assertEqual("invalid_search_pointer", raised.exception.code)


class McpProtocolContractTests(unittest.TestCase):
    def test_initialize_list_and_call_over_utf8_json_lines(self) -> None:
        with fake_runtime() as (root, spool, _log, _tool):
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "local_rag_search",
                        "arguments": {"question": "Alphaは？"},
                    },
                },
            ]
            wire = b"".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                + b"\n"
                for item in requests
            )
            environment = os.environ.copy()
            environment["AGENT003_TEST_SPOOL"] = str(spool)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SERVER_PATH),
                    "--rag-root",
                    str(root),
                    "--python",
                    sys.executable,
                    "--spool-root",
                    str(spool),
                ],
                input=wire,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
        self.assertEqual(b"", completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([1, 2, 3], [item["id"] for item in responses])
        self.assertEqual(
            "2025-11-25", responses[0]["result"]["protocolVersion"]
        )
        self.assertEqual(
            ["local_rag_search"],
            [item["name"] for item in responses[1]["result"]["tools"]],
        )
        self.assertEqual(
            "database_required",
            responses[2]["result"]["structuredContent"]["status"],
        )

    def test_requests_before_initialized_fail_closed(self) -> None:
        with fake_runtime() as (_root, _spool, _log, tool):
            server = mcp_server.McpServer(tool)
            response = server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
        self.assertEqual(-32600, response["error"]["code"])

    def test_unknown_tool_and_invalid_arguments_are_protocol_errors(self) -> None:
        with fake_runtime() as (_root, _spool, _log, tool):
            server = mcp_server.McpServer(tool)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"},
                }
            )
            server.handle(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            unknown = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "execute", "arguments": {}},
                }
            )
            invalid = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "local_rag_search",
                        "arguments": {"question": "q", "path": "C:\\"},
                    },
                }
            )
        self.assertEqual(-32602, unknown["error"]["code"])
        self.assertEqual(-32602, invalid["error"]["code"])


class WorkspaceConfigurationContractTests(unittest.TestCase):
    def test_mcp_config_has_one_fixed_local_server_and_no_inputs_or_env(self) -> None:
        config = json.loads(
            (REPOSITORY_ROOT / ".vscode" / "mcp.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual({"servers"}, set(config))
        self.assertEqual({"localRagAgent003"}, set(config["servers"]))
        server = config["servers"]["localRagAgent003"]
        self.assertEqual("stdio", server["type"])
        self.assertEqual(
            "${userHome}\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe",
            server["command"],
        )
        self.assertEqual("${workspaceFolder}", server["cwd"])
        self.assertNotIn("env", server)
        self.assertNotIn("envFile", server)
        self.assertNotIn("inputs", config)
        self.assertIn(
            "${workspaceFolder}\\tools\\experiments\\"
            "agent_003_readonly_mcp\\server.py",
            server["args"],
        )

    def test_agent_has_only_the_mcp_server_tool_set(self) -> None:
        agent = (
            REPOSITORY_ROOT
            / ".github"
            / "agents"
            / "agent003-readonly-local-rag.agent.md"
        ).read_text(encoding="utf-8")
        frontmatter = agent.split("---", 2)[1]
        self.assertIn("target: vscode", frontmatter)
        self.assertIn("agents: []", frontmatter)
        self.assertIn("model: 'GPT-5 mini (copilot)'", frontmatter)
        self.assertIn("tools: ['localragagent003/*']", frontmatter)
        self.assertIn(
            "description: Local RAGを必ず検索し、"
            "取得したローカル資料の根拠に基づいて回答します。",
            frontmatter,
        )
        self.assertIn(
            "#tool:localragagent003/local_rag_search",
            agent,
        )
        self.assertIn("このAgentへの質問には、回答前に必ず", agent)
        self.assertIn("検索前に回答しない。", agent)
        self.assertNotIn("必要なときだけ", agent)
        self.assertNotIn("一般知識だけで十分", agent)
        self.assertNotIn("Local RAGが必要な質問では", agent)
        self.assertNotIn("tools: ['execute", agent)
        self.assertNotIn("tools: ['read", agent)
        self.assertNotIn("tools: ['web", agent)
        self.assertNotIn("tools: ['edit", agent)


if __name__ == "__main__":
    unittest.main()
