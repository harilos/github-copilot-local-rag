from __future__ import annotations

import ast
import base64
import re
import shutil
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[2]
AGENTS_ROOT = HERE / "agents"

CANDIDATES = {
    "agent-002-a-current-improved.agent.md": {
        "name": "社内文書検索候補A（現行改良）",
        "markers": ("# Candidate A: 現行改良", "現行の番号付き手順"),
    },
    "agent-002-b-minimal-linear.agent.md": {
        "name": "社内文書検索候補B（最小直線）",
        "markers": ("# Candidate B: 最小直線", "# Credit cap"),
    },
    "agent-002-c-state-machine.agent.md": {
        "name": "社内文書検索候補C（状態機械）",
        "markers": ("# Candidate C: 状態機械", "| 状態 | 動作 | 次状態 |"),
    },
    "agent-002-d-allowed-trace.agent.md": {
        "name": "社内文書検索候補D（許可トレース）",
        "markers": ("# Candidate D: 許可tool trace", "# 許可トレース"),
    },
}

LIST_COMMAND = (
    '& "$env:USERPROFILE\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe" '
    '-B "$env:USERPROFILE\\.copilot\\rag\\list_dbs.py" --format json'
)
SEARCH_COMMAND = (
    '& "$env:USERPROFILE\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe" '
    '-B "$env:USERPROFILE\\.copilot\\rag\\search.py" --db \'<DB_NAME>\' '
    "--include-db-hint --compact-json --result-delivery file --format json "
    "'<Q_SINGLE_QUOTED>'"
)
READ_SUMMARY_COMMAND = (
    "Get-Content -LiteralPath '<SUMMARY_FILE_SINGLE_QUOTED>' -Raw"
)
EXPECTED_PLACEHOLDERS = {
    "<DB_NAME>",
    "<Q_SINGLE_QUOTED>",
    "<SUMMARY_FILE_SINGLE_QUOTED>",
}

FIXTURE_PYTHON = r"C:\Users\fixture\.copilot\rag\query\.venv\Scripts\python.exe"
FIXTURE_LIST = r"C:\Users\fixture\.copilot\rag\list_dbs.py"
FIXTURE_SEARCH = r"C:\Users\fixture\.copilot\rag\search.py"
FIXTURE_SUMMARY = r"C:\result\summary.json"
SPECIAL_QUESTION = (
    "第一行\n"
    "O'Brien `backtick $(Write-Output INJECTION_SENTINEL) "
    '"double quote" の値は？'
)


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise AssertionError("agent frontmatter is missing")
    try:
        header, body = normalized[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise AssertionError("agent frontmatter is not closed") from exc
    values: dict[str, object] = {}
    for line in header.splitlines():
        key, separator, raw = line.partition(":")
        if not separator or not key or key in values:
            raise AssertionError(f"invalid agent frontmatter line: {line!r}")
        value = raw.strip()
        if value.startswith("["):
            values[key] = ast.literal_eval(value)
        elif value in {"true", "false"}:
            values[key] = value == "true"
        else:
            values[key] = value
    return values, body


def _powershell_blocks(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.findall(
        r"```powershell\n(.*?)\n```",
        normalized,
        re.DOTALL,
    )


def _powershell_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_native_argument_literal(value: str) -> str:
    native_encoded = value.replace('"', r'\"')
    return _powershell_single_quoted(native_encoded)


def _decode_powershell_single_quoted(literal: str) -> str:
    if len(literal) < 2 or not literal.startswith("'") or not literal.endswith("'"):
        raise ValueError("not a PowerShell single-quoted literal")
    source = literal[1:-1]
    decoded: list[str] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character != "'":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(source) or source[index + 1] != "'":
            raise ValueError("unescaped apostrophe in PowerShell literal")
        decoded.append("'")
        index += 2
    return "".join(decoded)


def _assigned_literal(path: Path, variable: str) -> object:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == variable
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{variable} assignment is missing from {path}")


@dataclass(frozen=True)
class TraceEvent:
    turn: int
    action: str
    tool: str | None = None
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class TraceCase:
    name: str
    question: str
    events: tuple[TraceEvent, ...]
    follow_up_message: str | None = None


def _list_event(turn: int = 1) -> TraceEvent:
    return TraceEvent(
        turn,
        "LIST_DBS",
        "execute",
        (FIXTURE_PYTHON, "-B", FIXTURE_LIST, "--format", "json"),
    )


def _search_event(
    question: str,
    *,
    turn: int = 1,
    database: str = "alpha-rag",
) -> TraceEvent:
    return TraceEvent(
        turn,
        "SEARCH",
        "execute",
        (
            FIXTURE_PYTHON,
            "-B",
            FIXTURE_SEARCH,
            "--db",
            database,
            "--include-db-hint",
            "--compact-json",
            "--result-delivery",
            "file",
            "--format",
            "json",
            question,
        ),
    )


def _read_event(turn: int = 1) -> TraceEvent:
    return TraceEvent(
        turn,
        "READ_SUMMARY",
        "execute",
        ("Get-Content", "-LiteralPath", FIXTURE_SUMMARY, "-Raw"),
    )


def _event(turn: int, action: str) -> TraceEvent:
    return TraceEvent(turn, action)


TRACE_CASES = (
    TraceCase(
        "explicit_db_success",
        SPECIAL_QUESTION,
        (
            _search_event(SPECIAL_QUESTION),
            _read_event(),
            _event(1, "ANSWER"),
        ),
    ),
    TraceCase(
        "implicit_single_db_success",
        SPECIAL_QUESTION,
        (
            _list_event(),
            _search_event(SPECIAL_QUESTION),
            _read_event(),
            _event(1, "ANSWER"),
        ),
    ),
    TraceCase(
        "multiple_db_resume_with_original_question",
        SPECIAL_QUESTION,
        (
            _list_event(),
            _event(1, "ASK_DB"),
            _event(1, "STOP"),
            _search_event(SPECIAL_QUESTION, turn=2, database="beta-rag"),
            _read_event(turn=2),
            _event(2, "ANSWER"),
        ),
        follow_up_message="beta-rag",
    ),
    TraceCase(
        "multiple_db_invalid_reply_stays_stopped",
        SPECIAL_QUESTION,
        (
            _list_event(),
            _event(1, "ASK_DB"),
            _event(1, "STOP"),
            _event(2, "ASK_DB"),
            _event(2, "STOP"),
        ),
        follow_up_message="not-a-candidate-rag",
    ),
    TraceCase(
        "zero_databases",
        SPECIAL_QUESTION,
        (_list_event(), _event(1, "FAIL"), _event(1, "STOP")),
    ),
    TraceCase(
        "search_failure",
        SPECIAL_QUESTION,
        (
            _search_event(SPECIAL_QUESTION),
            _event(1, "FAIL"),
            _event(1, "STOP"),
        ),
    ),
    TraceCase(
        "summary_read_failure",
        SPECIAL_QUESTION,
        (
            _search_event(SPECIAL_QUESTION),
            _read_event(),
            _event(1, "FAIL"),
            _event(1, "STOP"),
        ),
    ),
    TraceCase(
        "no_evidence",
        SPECIAL_QUESTION,
        (
            _search_event(SPECIAL_QUESTION),
            _read_event(),
            _event(1, "NO_EVIDENCE"),
            _event(1, "STOP"),
        ),
    ),
)

TOOL_ACTIONS = {"LIST_DBS", "SEARCH", "READ_SUMMARY"}
NON_TOOL_ACTIONS = {"ASK_DB", "ANSWER", "FAIL", "NO_EVIDENCE", "STOP"}


def _trace_errors(case: TraceCase) -> list[str]:
    errors: list[str] = []
    events = case.events
    actions = [event.action for event in events]

    unknown = set(actions) - TOOL_ACTIONS - NON_TOOL_ACTIONS
    if unknown:
        errors.append(f"unknown actions: {sorted(unknown)}")

    for event in events:
        if event.action in TOOL_ACTIONS:
            if event.tool != "execute":
                errors.append(f"{event.action} must use execute")
            if not event.argv:
                errors.append(f"{event.action} argv is empty")
        elif event.tool is not None or event.argv:
            errors.append(f"{event.action} must not issue a tool call")

    list_events = [event for event in events if event.action == "LIST_DBS"]
    search_events = [event for event in events if event.action == "SEARCH"]
    read_events = [event for event in events if event.action == "READ_SUMMARY"]
    if len(list_events) > 1:
        errors.append("LIST_DBS repeated")
    if len(search_events) > 1:
        errors.append("SEARCH repeated")
    if len(read_events) > 1:
        errors.append("READ_SUMMARY repeated")
    if len(list_events) + len(search_events) + len(read_events) > 3:
        errors.append("execute credit cap exceeded")

    for event in list_events:
        expected = (FIXTURE_PYTHON, "-B", FIXTURE_LIST, "--format", "json")
        if event.argv != expected:
            errors.append("LIST_DBS argv changed")

    for event in search_events:
        if len(event.argv) != 12:
            errors.append("SEARCH argv length changed")
            continue
        if event.argv[:4] != (
            FIXTURE_PYTHON,
            "-B",
            FIXTURE_SEARCH,
            "--db",
        ):
            errors.append("SEARCH fixed prefix changed")
        if not event.argv[4].endswith("-rag"):
            errors.append("SEARCH database is invalid")
        if event.argv[5:11] != (
            "--include-db-hint",
            "--compact-json",
            "--result-delivery",
            "file",
            "--format",
            "json",
        ):
            errors.append("SEARCH fixed flags changed")
        if event.argv[-1] != case.question:
            errors.append("SEARCH did not preserve Q in final argv")
        if sum(argument == case.question for argument in event.argv) != 1:
            errors.append("Q must occupy exactly one argv element")

    for event in read_events:
        if event.argv != (
            "Get-Content",
            "-LiteralPath",
            FIXTURE_SUMMARY,
            "-Raw",
        ):
            errors.append("READ_SUMMARY read something other than summary_file")

    for index, event in enumerate(events):
        if event.action == "READ_SUMMARY" and "SEARCH" not in actions[:index]:
            errors.append("READ_SUMMARY occurred before SEARCH")
        if event.action == "ANSWER":
            if "READ_SUMMARY" not in actions[:index]:
                errors.append("ANSWER occurred before READ_SUMMARY")
            if any(
                action in {"FAIL", "NO_EVIDENCE"} for action in actions[:index]
            ):
                errors.append("ANSWER supplemented a failed or empty search")
        if event.action in {"ASK_DB", "FAIL", "NO_EVIDENCE"}:
            if index + 1 >= len(events) or events[index + 1] != TraceEvent(
                event.turn, "STOP"
            ):
                errors.append(f"{event.action} must be followed by STOP")
        if event.action == "STOP":
            same_turn_tail = [
                later for later in events[index + 1 :] if later.turn == event.turn
            ]
            if same_turn_tail:
                errors.append("event occurred after STOP in the same turn")
            later_turns = [
                later for later in events[index + 1 :] if later.turn > event.turn
            ]
            prior_turn = [
                prior.action for prior in events[:index] if prior.turn == event.turn
            ]
            if later_turns and "ASK_DB" not in prior_turn:
                errors.append("terminal STOP was resumed without DB selection")

    for terminal in ("FAIL", "NO_EVIDENCE"):
        if terminal in actions:
            terminal_index = actions.index(terminal)
            if actions[terminal_index + 1 :] != ["STOP"]:
                errors.append(f"tool or answer occurred after {terminal}")

    if case.follow_up_message is not None and search_events:
        if case.follow_up_message == case.question:
            errors.append("fixture does not exercise DB-only follow-up")
        if search_events[0].argv[-1] != case.question:
            errors.append("DB-only follow-up replaced the original Q")

    return errors


class CandidatePreflightTests(unittest.TestCase):
    maxDiff = None

    def test_candidate_set_and_frontmatter_are_fixed(self) -> None:
        actual = {path.name for path in AGENTS_ROOT.iterdir() if path.is_file()}
        self.assertEqual(set(CANDIDATES), actual)
        bodies: dict[str, str] = {}
        for filename, expected in CANDIDATES.items():
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            frontmatter, body = _frontmatter(text)
            self.assertEqual(expected["name"], frontmatter["name"], filename)
            self.assertTrue(str(frontmatter["description"]).strip(), filename)
            self.assertEqual(["execute"], frontmatter["tools"], filename)
            self.assertEqual("GPT-5 mini", frontmatter["model"], filename)
            self.assertEqual([], frontmatter["agents"], filename)
            self.assertIs(True, frontmatter["user-invocable"], filename)
            self.assertIs(True, frontmatter["disable-model-invocation"], filename)
            self.assertNotIn("target", frontmatter, filename)
            for marker in expected["markers"]:
                self.assertIn(marker, body, filename)
            bodies[filename] = body
        self.assertEqual(len(CANDIDATES), len(set(bodies.values())))
        self.assertLess(
            len(bodies["agent-002-b-minimal-linear.agent.md"]),
            min(
                len(body)
                for filename, body in bodies.items()
                if filename != "agent-002-b-minimal-linear.agent.md"
            ),
        )

    def test_fixed_commands_counts_order_and_result_boundary(self) -> None:
        for filename in CANDIDATES:
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            blocks = _powershell_blocks(text)
            self.assertEqual(
                [LIST_COMMAND, SEARCH_COMMAND, READ_SUMMARY_COMMAND],
                blocks,
                filename,
            )
            self.assertEqual(1, text.count(r"rag\list_dbs.py"), filename)
            self.assertEqual(1, text.count(r"rag\search.py"), filename)
            self.assertEqual(
                1, text.count("Get-Content -LiteralPath"), filename
            )
            self.assertEqual(1, text.count("--result-delivery file"), filename)
            positions = [text.index(command) for command in blocks]
            self.assertEqual(sorted(positions), positions, filename)
            self.assertNotIn("--result-delivery stdout", text, filename)
            self.assertNotIn("--no-daemon", text, filename)
            self.assertNotIn('"<Q_SINGLE_QUOTED>"', text, filename)
            placeholders = set(re.findall(r"<[A-Z_]+>", text))
            self.assertEqual(EXPECTED_PLACEHOLDERS, placeholders, filename)

    def test_question_state_quoting_stop_and_no_template_placeholders(self) -> None:
        required = (
            "DB名だけの次ターンでも同一Q",
            "Qを要約・分割・正規化・翻訳せず",
            "state file、marker file、一時fileを作らない",
            "LIST_DBSは会話全体で1回だけ",
            "SEARCHはexactly once",
            "失敗してもretryしない",
            "使用可能なtoolはexecuteだけ",
            "double quoteをbackslash+double quote",
            "Windows native argv用",
            "各 `'` を `''`",
            "PowerShell single-quoted literal",
            "改行、backtick、`$()`",
            "元のdouble quoteへ戻す",
            "実argv",
            "no evidence",
            "補完",
            "STOP",
            "summary_file",
        )
        for filename in CANDIDATES:
            text = (AGENTS_ROOT / filename).read_text(encoding="utf-8")
            for fragment in required:
                self.assertIn(fragment, text, f"{filename}: {fragment}")
            self.assertLess(
                text.index("double quoteをbackslash+double quote"),
                text.index("各 `'` を `''`"),
                filename,
            )
            self.assertNotIn("{{", text, filename)
            self.assertNotIn("}}", text, filename)

    def test_single_quoted_literal_round_trips_special_question(self) -> None:
        literal = _powershell_single_quoted(SPECIAL_QUESTION)
        self.assertEqual(SPECIAL_QUESTION, _decode_powershell_single_quoted(literal))
        self.assertTrue(literal.startswith("'") and literal.endswith("'"))
        self.assertIn("O''Brien", literal)
        self.assertIn("\n", literal)
        self.assertIn("`backtick", literal)
        self.assertIn("$(Write-Output INJECTION_SENTINEL)", literal)
        self.assertIn('"double quote"', literal)
        native_literal = _powershell_native_argument_literal(SPECIAL_QUESTION)
        self.assertEqual(
            SPECIAL_QUESTION.replace('"', r'\"'),
            _decode_powershell_single_quoted(native_literal),
        )

    def test_real_powershell_process_argv_preserves_special_question(self) -> None:
        powershell = (
            shutil.which("powershell.exe")
            or shutil.which("powershell")
            or shutil.which("pwsh")
        )
        if powershell is None:
            self.skipTest("PowerShell is unavailable")
        child = (
            "import base64,sys;"
            "print(len(sys.argv));"
            "print(base64.b64encode(sys.argv[-1].encode()).decode())"
        )
        command = " ".join(
            (
                "&",
                _powershell_single_quoted(sys.executable),
                "-B",
                "-c",
                _powershell_single_quoted(child),
                _powershell_native_argument_literal(SPECIAL_QUESTION),
            )
        )
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            0,
            completed.returncode,
            completed.stderr.decode(errors="replace"),
        )
        output = completed.stdout.decode("ascii").splitlines()
        self.assertEqual("2", output[0])
        self.assertEqual(
            SPECIAL_QUESTION,
            base64.b64decode(output[1]).decode("utf-8"),
        )

    def test_allowed_scripted_traces_preserve_q_and_credit_caps(self) -> None:
        for case in TRACE_CASES:
            with self.subTest(case=case.name):
                self.assertEqual([], _trace_errors(case))

    def test_trace_validator_rejects_retry_mutation_and_extra_tools(self) -> None:
        bad_cases = (
            TraceCase(
                "search_retry",
                SPECIAL_QUESTION,
                (
                    _search_event(SPECIAL_QUESTION),
                    _search_event(SPECIAL_QUESTION),
                    _read_event(),
                    _event(1, "ANSWER"),
                ),
            ),
            TraceCase(
                "question_mutation",
                SPECIAL_QUESTION,
                (
                    _search_event("要約された質問"),
                    _read_event(),
                    _event(1, "ANSWER"),
                ),
            ),
            TraceCase(
                "web_tool",
                SPECIAL_QUESTION,
                (TraceEvent(1, "WEB", "web", ("query",)),),
            ),
            TraceCase(
                "read_before_search",
                SPECIAL_QUESTION,
                (_read_event(), _search_event(SPECIAL_QUESTION)),
            ),
            TraceCase(
                "tool_after_failure",
                SPECIAL_QUESTION,
                (
                    _search_event(SPECIAL_QUESTION),
                    _event(1, "FAIL"),
                    _event(1, "STOP"),
                    _read_event(turn=2),
                ),
            ),
        )
        for case in bad_cases:
            with self.subTest(case=case.name):
                self.assertTrue(_trace_errors(case))

    def test_candidates_are_outside_all_product_agent_payloads(self) -> None:
        product_root = REPOSITORY_ROOT / ".copilot"
        candidate_paths = [AGENTS_ROOT / filename for filename in CANDIDATES]
        for path in candidate_paths:
            self.assertFalse(path.is_relative_to(product_root), path)
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            self.assertTrue(
                relative.startswith("tools/experiments/agent_002_bakeoff/agents/"),
                relative,
            )

        packages_path = (
            REPOSITORY_ROOT
            / ".copilot"
            / "rag"
            / "source_manager"
            / "packages.py"
        )
        product_agents = set(_assigned_literal(packages_path, "_PROJECT_AGENTS"))
        self.assertTrue(product_agents)
        self.assertTrue(set(CANDIDATES).isdisjoint(product_agents))

        build_script = (
            REPOSITORY_ROOT / "tools" / "windows_portable" / "build_package.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"--payload-root", (Join-Path $RepositoryRoot ".copilot")',
            build_script,
        )
        self.assertFalse(
            any(path.is_relative_to(product_root) for path in candidate_paths)
        )

        installer_templates = (
            REPOSITORY_ROOT
            / "tools"
            / "windows_portable"
            / "install-template.ps1",
            REPOSITORY_ROOT
            / ".copilot"
            / "rag"
            / "source_manager"
            / "windows-install-template.ps1",
        )
        for template in installer_templates:
            text = template.read_text(encoding="utf-8")
            for filename in CANDIDATES:
                self.assertNotIn(filename, text, str(template))


if __name__ == "__main__":
    unittest.main()
