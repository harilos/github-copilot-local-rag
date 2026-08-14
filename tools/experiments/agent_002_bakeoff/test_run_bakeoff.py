from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest import mock


HERE = Path(__file__).resolve().parent


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load test target: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bakeoff = _load_module("_agent_002_run_bakeoff_tests", HERE / "run_bakeoff.py")
fake_list_dbs = _load_module(
    "_agent_002_fake_list_dbs_tests", HERE / "fake_rag" / "list_dbs.py"
)
fake_search = _load_module(
    "_agent_002_fake_search_tests", HERE / "fake_rag" / "search.py"
)


def _write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
        newline="\n",
    )


def _tool_event(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    call_id: str,
) -> dict[str, Any]:
    return {
        "type": "tool.execution_start",
        "toolCallId": call_id,
        "toolName": tool_name,
        "arguments": arguments,
    }


def _assistant_event(content: str) -> dict[str, Any]:
    return {
        "type": "assistant.message",
        "data": {
            "phase": "final_answer",
            "content": content,
            "toolRequests": [],
        },
    }


def _otel_models(requested: str, selected: str) -> dict[str, Any]:
    return {
        "attributes": [
            {
                "key": "gen_ai.request.model",
                "value": {"stringValue": requested},
            },
            {
                "key": "gen_ai.response.model",
                "value": {"stringValue": selected},
            },
        ]
    }


def _passing_assessment(
    case: dict[str, Any],
    case_directory: Path,
    fixture: dict[str, str],
    *,
    assistant: str | None = None,
    requested_model: str = "gpt-5-mini",
    selected_model: str = "gpt-5-mini",
    trace_python: str | None = None,
    trace_script: str | None = None,
    extra_cli_events: list[Any] | None = None,
) -> dict[str, Any]:
    profile = Path(fixture["profile"])
    python_path = (
        profile / ".copilot" / "rag" / "query" / ".venv" / "Scripts" / "python.exe"
    )
    search_path = profile / ".copilot" / "rag" / "search.py"
    summary_path = (
        Path(fixture["summary_root"]) / f"{case['scenario']}.md"
    ).resolve()
    process_argv = [
        "--db",
        str(case["expected_db"]),
        "--include-db-hint",
        "--compact-json",
        "--result-delivery",
        "file",
        "--format",
        "json",
        str(case["question"]),
    ]
    cli_events = [
        _tool_event(
            "powershell",
            {"command": bakeoff._powershell_search_command(case)},
            call_id="search-1",
        ),
        _tool_event(
            "powershell",
            {"command": bakeoff._powershell_summary_command(summary_path)},
            call_id="read-1",
        ),
    ]
    cli_events.extend(extra_cli_events or [])
    cli_events.append(
        _assistant_event(
            assistant
            if assistant is not None
            else " ".join(str(value) for value in case.get("assistant_all") or [])
        )
    )
    stdout_path = case_directory / "copilot.jsonl"
    otel_path = case_directory / "otel.jsonl"
    _write_jsonl(stdout_path, cli_events)
    _write_jsonl(otel_path, [_otel_models(requested_model, selected_model)])
    _write_jsonl(
        case_directory / "tool-trace.jsonl",
        [
            {
                "schema_version": "lrr-agent-002-tool-trace-v1",
                "event": "search",
                "scenario": str(case["scenario"]),
                "python": trace_python or str(python_path),
                "script": trace_script or str(search_path),
                "argv": process_argv,
                "db": str(case["expected_db"]),
                "question": str(case["question"]),
                "summary_file": str(summary_path),
            }
        ],
    )
    return bakeoff._assess_case(
        case,
        case_directory,
        [
            {
                "turn": 1,
                "exit_code": 0,
                "elapsed_seconds": 0.01,
                "stdout": str(stdout_path),
                "otel": str(otel_path),
            }
        ],
        fixture,
        interim=False,
    )


class PromptBudgetAndSessionTests(unittest.TestCase):
    def test_authenticated_copilot_home_is_required_before_prompt_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".copilot"
            home.mkdir()
            config = home / "config.json"
            config.write_text(
                "// managed\n{\"lastLoggedInUser\": null, \"loggedInUsers\": []}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"COPILOT_HOME": str(home)}):
                with self.assertRaisesRegex(RuntimeError, "not logged in"):
                    bakeoff._authenticated_copilot_home()
                config.write_text(
                    "// managed\n{\"lastLoggedInUser\": {\"login\": \"fixture\"}, "
                    "\"loggedInUsers\": [{\"login\": \"fixture\"}]}\n",
                    encoding="utf-8",
                )
                self.assertEqual(home.resolve(), bakeoff._authenticated_copilot_home())

    def test_case_matrix_has_stage_caps_and_global_24_prompt_cap(self) -> None:
        data = bakeoff._case_data()
        candidates = bakeoff._candidate_map(data)
        stage1 = sum(len(case["turns"]) for case in data["stage1"]) * len(
            candidates
        )
        stage2 = sum(len(case["turns"]) for case in data["stage2"]) * 2
        stage1_by_id = {case["id"]: case for case in data["stage1"]}
        stage3 = sum(
            len(stage1_by_id[case_id]["turns"])
            for case_id in data["stage3_order"]
        )
        self.assertEqual(16, stage1)
        self.assertEqual(4, stage2)
        self.assertEqual(4, stage3)
        self.assertEqual(bakeoff.PROMPT_LIMIT, stage1 + stage2 + stage3)

    def test_ledger_refuses_25th_launch_before_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            for index in range(bakeoff.PROMPT_LIMIT):
                prompt_number = bakeoff._begin_prompt(
                    output_root,
                    stage="stage1",
                    candidate="A",
                    case_id=f"case-{index}",
                    turn=1,
                    session_id=f"session-{index}",
                )
                self.assertEqual(index + 1, prompt_number)

            run_mock = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            with mock.patch.object(bakeoff.subprocess, "run", run_mock):
                with self.assertRaisesRegex(RuntimeError, "24-prompt limit"):
                    bakeoff._run_turn(
                        output_root=output_root,
                        stage="stage1",
                        candidate="A",
                        agent="agent-a",
                        case={"id": "overflow", "scenario": "grounded"},
                        turn_number=1,
                        prompt="must not launch",
                        session_id="overflow-session",
                        turn_directory=output_root / "overflow-turn",
                        fixture={
                            "workspace": str(output_root / "workspace"),
                            "profile": str(output_root / "profile"),
                            "copilot_home": str(output_root / "copilot-home"),
                            "summary_root": str(output_root / "summaries"),
                        },
                        copilot_path=output_root / "copilot.exe",
                        model=bakeoff.MODEL_ID,
                        max_ai_credits=30,
                        timeout_seconds=1,
                        trace_path=output_root / "trace.jsonl",
                    )
            run_mock.assert_not_called()
            ledger = bakeoff._load_ledger(output_root)
            self.assertEqual(bakeoff.PROMPT_LIMIT, ledger["count"])
            self.assertEqual(bakeoff.PROMPT_LIMIT, len(ledger["entries"]))

    def test_runner_lock_rejects_a_concurrent_stage_writer_and_then_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            lock_path = output_root / ".agent-002-runner.lock"
            with bakeoff._runner_lock(output_root):
                self.assertTrue(lock_path.is_file())
                with self.assertRaisesRegex(RuntimeError, "another runner"):
                    with bakeoff._runner_lock(output_root):
                        self.fail("a second writer acquired the same runner lock")
            self.assertFalse(lock_path.exists())

    def test_reviewed_stage_selection_never_exceeds_two_then_one_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            (output_root / "stage1").mkdir()
            bakeoff._write_json(
                output_root / "stage1" / "reviewed-stage-summary.json",
                {"ranking": ["D", "B", "A", "C"]},
            )
            self.assertEqual(
                ["D", "B"],
                bakeoff._select_candidates(output_root, "stage2", None),
            )
            with self.assertRaises(RuntimeError):
                bakeoff._select_candidates(output_root, "stage2", ["D"])

            (output_root / "stage2").mkdir()
            bakeoff._write_json(
                output_root / "stage2" / "reviewed-stage-summary.json",
                {"ranking": ["B", "D"]},
            )
            self.assertEqual(
                ["B"],
                bakeoff._select_candidates(output_root, "stage3", None),
            )

    def test_first_turn_uses_new_session_and_second_turn_only_resumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            fixture = {
                "workspace": str(output_root / "workspace"),
                "profile": str(output_root / "profile"),
                "copilot_home": str(output_root / "copilot-home"),
                "summary_root": str(output_root / "summaries"),
            }
            session_id = "11111111-1111-4111-8111-111111111111"
            run_mock = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            real_profile = str(output_root / "authenticated-user-profile")
            with (
                mock.patch.dict(
                    os.environ, {"USERPROFILE": real_profile}, clear=False
                ),
                mock.patch.object(bakeoff.subprocess, "run", run_mock),
            ):
                for turn, prompt in ((1, "original question"), (2, "beta-rag")):
                    bakeoff._run_turn(
                        output_root=output_root,
                        stage="stage1",
                        candidate="A",
                        agent="agent-002-a-current-improved",
                        case={"id": "two-turn", "scenario": "db_select"},
                        turn_number=turn,
                        prompt=prompt,
                        session_id=session_id,
                        turn_directory=output_root / f"turn-{turn}",
                        fixture=fixture,
                        copilot_path=output_root / "copilot.exe",
                        model=bakeoff.MODEL_ID,
                        max_ai_credits=30,
                        timeout_seconds=1,
                        trace_path=output_root / "trace.jsonl",
                    )

            self.assertEqual(2, run_mock.call_count)
            first = run_mock.call_args_list[0].args[0]
            second = run_mock.call_args_list[1].args[0]
            self.assertEqual(1, first.count("--session-id"))
            self.assertEqual(session_id, first[first.index("--session-id") + 1])
            self.assertFalse(any(value.startswith("--resume") for value in first))
            self.assertNotIn("--session-id", second)
            self.assertEqual([f"--resume={session_id}"], [
                value for value in second if value.startswith("--resume")
            ])
            for arguments in (first, second):
                self.assertEqual(1, arguments.count("--available-tools=execute"))
                self.assertEqual(bakeoff.MODEL_ID, arguments[arguments.index("--model") + 1])
            for call in run_mock.call_args_list:
                environment = call.kwargs["env"]
                self.assertEqual(real_profile, environment["USERPROFILE"])
                self.assertEqual(
                    str(Path(fixture["profile"]) / ".copilot"),
                    environment["LRR_AGENT_HOME"],
                )
                self.assertEqual(fixture["copilot_home"], environment["COPILOT_HOME"])

    def test_timeout_is_counted_once_and_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            run_mock = mock.Mock(
                side_effect=subprocess.TimeoutExpired(["copilot.exe"], timeout=1)
            )
            with mock.patch.object(bakeoff.subprocess, "run", run_mock):
                meta = bakeoff._run_turn(
                    output_root=output_root,
                    stage="stage1",
                    candidate="A",
                    agent="agent-a",
                    case={"id": "timeout", "scenario": "grounded"},
                    turn_number=1,
                    prompt="question",
                    session_id="timeout-session",
                    turn_directory=output_root / "turn-1",
                    fixture={
                        "workspace": str(output_root / "workspace"),
                        "profile": str(output_root / "profile"),
                        "copilot_home": str(output_root / "copilot-home"),
                        "summary_root": str(output_root / "summaries"),
                    },
                    copilot_path=output_root / "copilot.exe",
                    model=bakeoff.MODEL_ID,
                    max_ai_credits=30,
                    timeout_seconds=1,
                    trace_path=output_root / "trace.jsonl",
                )
            self.assertEqual(1, run_mock.call_count)
            self.assertTrue(meta["timed_out"])
            self.assertEqual(124, meta["exit_code"])
            ledger = bakeoff._load_ledger(output_root)
            self.assertEqual(1, ledger["count"])
            self.assertEqual("completed", ledger["entries"][0]["status"])

    def test_each_case_gets_a_fresh_session_but_two_turn_case_reuses_its_id(self) -> None:
        cases = [
            {"id": "two", "turns": ["question", "beta-rag"], "assistant_all": []},
            {"id": "one", "turns": ["question"], "assistant_all": []},
        ]

        def fake_turn(**kwargs: Any) -> dict[str, Any]:
            return {
                "turn": kwargs["turn_number"],
                "elapsed_seconds": 0.01,
            }

        def pass_assessment(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "status": "PASS",
                "strict_machine_pass": True,
                "failures": [],
                "observed": {
                    "elapsed_seconds": [0.01],
                    "assistant_by_turn": [""],
                },
            }

        data = bakeoff._case_data()
        agent = bakeoff._candidate_map(data)["A"]
        first_uuid = uuid.UUID("11111111-1111-4111-8111-111111111111")
        second_uuid = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            run_mock = mock.Mock(side_effect=fake_turn)
            with (
                mock.patch.object(bakeoff, "_run_turn", run_mock),
                mock.patch.object(bakeoff, "_assess_case", side_effect=pass_assessment),
                mock.patch.object(
                    bakeoff.uuid, "uuid4", side_effect=[first_uuid, second_uuid]
                ),
            ):
                result = bakeoff._run_candidate(
                    output_root=output_root,
                    stage="stage1",
                    candidate="A",
                    agent=agent,
                    cases=cases,
                    fixture={},
                    copilot_path=output_root / "copilot.exe",
                    model=bakeoff.MODEL_ID,
                    max_ai_credits=30,
                    timeout_seconds=1,
                )
            sessions = [call.kwargs["session_id"] for call in run_mock.call_args_list]
            turns = [call.kwargs["turn_number"] for call in run_mock.call_args_list]
            self.assertEqual([str(first_uuid), str(first_uuid), str(second_uuid)], sessions)
            self.assertEqual([1, 2, 1], turns)
            self.assertTrue(result["eligible"])

    def test_failed_two_turn_interim_excludes_before_paid_followup(self) -> None:
        cases = [
            {"id": "two", "turns": ["question", "beta-rag"], "assistant_all": []},
            {"id": "later", "turns": ["must not run"], "assistant_all": []},
        ]
        failed = {
            "status": "FAIL",
            "strict_machine_pass": False,
            "failures": ["hard_gate"],
            "observed": {
                "elapsed_seconds": [0.01],
                "assistant_by_turn": [""],
            },
        }
        data = bakeoff._case_data()
        agent = bakeoff._candidate_map(data)["A"]
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            run_mock = mock.Mock(
                return_value={"turn": 1, "elapsed_seconds": 0.01}
            )
            with (
                mock.patch.object(bakeoff, "_run_turn", run_mock),
                mock.patch.object(bakeoff, "_assess_case", return_value=failed),
            ):
                result = bakeoff._run_candidate(
                    output_root=output_root,
                    stage="stage1",
                    candidate="A",
                    agent=agent,
                    cases=cases,
                    fixture={},
                    copilot_path=output_root / "copilot.exe",
                    model=bakeoff.MODEL_ID,
                    max_ai_credits=30,
                    timeout_seconds=1,
                )
            self.assertEqual(1, run_mock.call_count)
            self.assertEqual(1, run_mock.call_args.kwargs["turn_number"])
            self.assertEqual("EXCLUDED", result["status"])
            self.assertEqual(1, result["completed_case_count"])


class FakeRagContractTests(unittest.TestCase):
    def test_db_selection_fixture_is_exactly_two_simple_databases(self) -> None:
        data = bakeoff._case_data()
        case = next(
            item for item in data["stage1"] if item["scenario"] == "db_select"
        )
        self.assertEqual(2, len(case["turns"]))
        self.assertEqual("beta-rag", case["turns"][1])
        self.assertNotIn("alpha-rag", case["turns"][0])
        self.assertNotIn("beta-rag", case["turns"][0])
        self.assertEqual(["list_dbs"], case["interim_tool_sequence"])
        self.assertEqual(
            ["list_dbs", "search", "read_summary"],
            case["expected_tool_sequence"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_path = root / "trace.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LRR_AGENT_TRACE_PATH": str(trace_path),
                        "LRR_AGENT_SCENARIO": "db_select",
                    },
                    clear=False,
                ),
                mock.patch.object(sys, "argv", ["list_dbs.py", "--format", "json"]),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = fake_list_dbs.main()
            self.assertEqual(0, exit_code, stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                ["alpha-rag", "beta-rag"],
                [item["name"] for item in payload["databases"]],
            )

    def test_db_selection_interim_requires_an_actual_selection_request(self) -> None:
        data = bakeoff._case_data()
        case = next(
            item for item in data["stage1"] if item["scenario"] == "db_select"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_directory = root / "case"
            case_directory.mkdir()
            summary_root = root / "summaries"
            summary_root.mkdir()
            fixture = {
                "workspace": str(root / "workspace"),
                "profile": str(root / "profile"),
                "copilot_home": str(root / "copilot-home"),
                "summary_root": str(summary_root),
            }
            stdout_path = case_directory / "copilot.jsonl"
            otel_path = case_directory / "otel.jsonl"
            _write_jsonl(
                stdout_path,
                [
                    _tool_event(
                        "powershell",
                        {"command": bakeoff._powershell_list_command()},
                        call_id="list-1",
                    ),
                    _assistant_event("alpha-rag beta-rag"),
                ],
            )
            _write_jsonl(
                otel_path,
                [_otel_models(bakeoff.MODEL_ID, bakeoff.MODEL_ID)],
            )
            _write_jsonl(
                case_directory / "tool-trace.jsonl",
                [
                    {
                        "schema_version": "lrr-agent-002-tool-trace-v1",
                        "event": "list_dbs",
                        "scenario": "db_select",
                        "python": str(
                            Path(fixture["profile"])
                            / ".copilot"
                            / "rag"
                            / "query"
                            / ".venv"
                            / "Scripts"
                            / "python.exe"
                        ),
                        "script": str(
                            Path(fixture["profile"])
                            / ".copilot"
                            / "rag"
                            / "list_dbs.py"
                        ),
                        "argv": ["--format", "json"],
                    }
                ],
            )
            assessment = bakeoff._assess_case(
                case,
                case_directory,
                [
                    {
                        "turn": 1,
                        "exit_code": 0,
                        "elapsed_seconds": 0.01,
                        "stdout": str(stdout_path),
                        "otel": str(otel_path),
                    }
                ],
                fixture,
                interim=True,
            )
            self.assertIn("interim_assistant_ask_missing", assessment["failures"])
            _write_jsonl(
                stdout_path,
                [
                    _tool_event(
                        "powershell",
                        {"command": bakeoff._powershell_list_command()},
                        call_id="list-1",
                    ),
                    _assistant_event(
                        "alpha-rag と beta-rag のどちらを選択しますか？"
                    ),
                ],
            )
            assessment = bakeoff._assess_case(
                case,
                case_directory,
                [
                    {
                        "turn": 1,
                        "exit_code": 0,
                        "elapsed_seconds": 0.01,
                        "stdout": str(stdout_path),
                        "otel": str(otel_path),
                    }
                ],
                fixture,
                interim=True,
            )
            self.assertEqual([], assessment["failures"])

    def test_unicode_question_round_trips_as_one_exact_process_argv_element(self) -> None:
        data = bakeoff._case_data()
        case = data["stage2"][0]
        question = str(case["question"])
        self.assertTrue(all(value in question for value in ('"', "'", "`", "$()", "\n", "Ω", "☃")))
        argv = [
            "--db",
            str(case["expected_db"]),
            "--include-db-hint",
            "--compact-json",
            "--result-delivery",
            "file",
            "--format",
            "json",
            question,
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_root = root / "summaries"
            summary_root.mkdir()
            (summary_root / f"{case['scenario']}.md").write_text(
                "sealed", encoding="utf-8"
            )
            trace_path = root / "trace.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LRR_AGENT_TRACE_PATH": str(trace_path),
                        "LRR_AGENT_SCENARIO": str(case["scenario"]),
                        "LRR_AGENT_SUMMARY_ROOT": str(summary_root),
                    },
                    clear=False,
                ),
                mock.patch.object(sys, "argv", [str(HERE / "fake_rag" / "search.py"), *argv]),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = fake_search.main()
            self.assertEqual(0, exit_code, stderr.getvalue())
            pointer = json.loads(stdout.getvalue())
            self.assertEqual("rag-result-pointer-v1", pointer["schema_version"])
            trace, invalid = bakeoff._load_jsonl(trace_path)
            self.assertEqual(0, invalid)
            self.assertEqual(argv, trace[0]["argv"])
            self.assertEqual(question, trace[0]["question"])
            self.assertEqual(1, sum(value == question for value in trace[0]["argv"]))

    def test_fake_rag_rejects_mutated_list_and_search_argv(self) -> None:
        data = bakeoff._case_data()
        case = data["stage2"][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_root = root / "summaries"
            summary_root.mkdir()
            (summary_root / f"{case['scenario']}.md").write_text(
                "sealed", encoding="utf-8"
            )
            environment = {
                "LRR_AGENT_TRACE_PATH": str(root / "trace.jsonl"),
                "LRR_AGENT_SCENARIO": str(case["scenario"]),
                "LRR_AGENT_SUMMARY_ROOT": str(summary_root),
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(sys, "argv", ["list_dbs.py", "--format", "yaml"]),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(64, fake_list_dbs.main())
            mutated = [
                "search.py",
                "--db",
                str(case["expected_db"]),
                "--include-db-hint",
                "--compact-json",
                "--result-delivery",
                "file",
                "--format",
                "json",
                str(case["question"]),
                "unexpected-extra-argv",
            ]
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(sys, "argv", mutated),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(64, fake_search.main())


class HumanReviewGateTests(unittest.TestCase):
    @staticmethod
    def _stage_summary(candidates: list[str], case_ids: list[str]) -> dict[str, Any]:
        return {
            "schema_version": "lrr-agent-002-stage-summary-v1",
            "machine_ranking": candidates,
            "results": [
                {
                    "candidate": candidate,
                    "eligible": True,
                    "case_results": [{"case_id": case_id} for case_id in case_ids],
                }
                for candidate in candidates
            ],
        }

    @staticmethod
    def _human_review(
        stage: str,
        statuses: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "schema_version": "lrr-agent-002-human-review-v1",
            "stage": stage,
            "reviews": {
                candidate: {
                    case_id: {"status": status, "note": "test review"}
                    for case_id, status in cases.items()
                }
                for candidate, cases in statuses.items()
            },
        }

    def test_stage2_refuses_to_select_without_finalized_stage1_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            (output_root / "stage1").mkdir()
            bakeoff._write_json(
                output_root / "stage1" / "stage-summary.json",
                self._stage_summary(["A", "B"], ["case-1"]),
            )
            with self.assertRaisesRegex(RuntimeError, "finalized human review"):
                bakeoff._select_candidates(output_root, "stage2", None)

    def test_finalize_rejects_pending_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            stage = output_root / "stage1"
            stage.mkdir()
            bakeoff._write_json(
                stage / "stage-summary.json",
                self._stage_summary(["A"], ["case-1"]),
            )
            bakeoff._write_json(
                stage / "human-review.json",
                self._human_review("stage1", {"A": {"case-1": "PENDING"}}),
            )
            with self.assertRaisesRegex(RuntimeError, "remains incomplete"):
                bakeoff._finalize_review(
                    Namespace(stage="stage1", output_root=str(output_root))
                )
            self.assertFalse((output_root / ".agent-002-runner.lock").exists())
            self.assertFalse((stage / "reviewed-stage-summary.json").exists())

    def test_finalize_keeps_only_candidates_with_all_reviews_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            stage = output_root / "stage1"
            stage.mkdir()
            bakeoff._write_json(
                stage / "stage-summary.json",
                self._stage_summary(["A", "B"], ["case-1", "case-2"]),
            )
            bakeoff._write_json(
                stage / "human-review.json",
                self._human_review(
                    "stage1",
                    {
                        "A": {"case-1": "PASS", "case-2": "PASS"},
                        "B": {"case-1": "PASS", "case-2": "FAIL"},
                    },
                ),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = bakeoff._finalize_review(
                    Namespace(stage="stage1", output_root=str(output_root))
                )
            self.assertEqual(0, exit_code)
            finalized = bakeoff._read_json(stage / "reviewed-stage-summary.json")
            self.assertEqual(["A"], finalized["ranking"])
            self.assertTrue(finalized["review_details"]["A"]["evidence_pass"])
            self.assertFalse(finalized["review_details"]["B"]["evidence_pass"])

    def test_stage3_winner_requires_same_candidate_in_both_prior_reviews(self) -> None:
        for included_in_stage2 in (False, True):
            with self.subTest(included_in_stage2=included_in_stage2):
                with tempfile.TemporaryDirectory() as temporary:
                    output_root = Path(temporary)
                    for stage_name, ranking in (
                        ("stage1", ["A"]),
                        ("stage2", ["A"] if included_in_stage2 else ["B"]),
                    ):
                        directory = output_root / stage_name
                        directory.mkdir()
                        bakeoff._write_json(
                            directory / "reviewed-stage-summary.json",
                            {"ranking": ranking},
                        )

                    stage3 = output_root / "stage3"
                    stage3.mkdir()
                    case_ids = ["case-1", "case-2", "case-3"]
                    bakeoff._write_json(
                        stage3 / "stage-summary.json",
                        self._stage_summary(["A"], case_ids),
                    )
                    bakeoff._write_json(
                        stage3 / "human-review.json",
                        self._human_review(
                            "stage3",
                            {"A": {case_id: "PASS" for case_id in case_ids}},
                        ),
                    )
                    with contextlib.redirect_stdout(io.StringIO()):
                        exit_code = bakeoff._finalize_review(
                            Namespace(stage="stage3", output_root=str(output_root))
                        )
                    self.assertEqual(0, exit_code)
                    finalized = bakeoff._read_json(
                        stage3 / "reviewed-stage-summary.json"
                    )
                    expected_winner = "A" if included_in_stage2 else None
                    self.assertEqual(expected_winner, finalized["winner"])
                    self.assertEqual(
                        "PASS" if included_in_stage2 else "FAIL",
                        finalized["mini_stable_at_2_lite"]["status"],
                    )


class FailClosedScorerTests(unittest.TestCase):
    def _grounded_case_and_fixture(
        self, root: Path
    ) -> tuple[dict[str, Any], dict[str, str], Path]:
        case = dict(bakeoff._case_data()["stage1"][0])
        summary_root = root / "summaries"
        summary_root.mkdir(parents=True)
        summary_path = summary_root / f"{case['scenario']}.md"
        summary_path.write_text("sealed", encoding="utf-8")
        fixture = {
            "workspace": str(root / "workspace"),
            "profile": str(root / "profile"),
            "copilot_home": str(root / "copilot-home"),
            "summary_root": str(summary_root),
        }
        return case, fixture, summary_path

    def test_known_good_tool_assistant_argv_and_model_assessment_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case, fixture, _ = self._grounded_case_and_fixture(root)
            assessment = _passing_assessment(case, root / "case", fixture)
            self.assertEqual([], assessment["failures"])
            self.assertTrue(assessment["strict_machine_pass"])

    def test_missing_assistant_evidence_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case, fixture, _ = self._grounded_case_and_fixture(root)
            assessment = _passing_assessment(
                case,
                root / "case",
                fixture,
                assistant="ORION-73 without the required evidence identifier",
            )
            self.assertFalse(assessment["strict_machine_pass"])
            self.assertIn("assistant_missing:E1", assessment["failures"])

    def test_missing_or_non_mini_telemetry_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case, fixture, _ = self._grounded_case_and_fixture(root)
            malicious = _passing_assessment(
                case,
                root / "malicious-model",
                fixture,
                requested_model="not-gpt-5-mini-plus",
                selected_model="not-gpt-5-mini-plus",
            )
            self.assertFalse(malicious["strict_machine_pass"])
            self.assertIn("non_mini_model_observed", malicious["failures"])

            missing_root = root / "missing-model"
            _passing_assessment(case, missing_root, fixture)
            (missing_root / "otel.jsonl").write_text("", encoding="utf-8")
            reassessed = bakeoff._assess_case(
                case,
                missing_root,
                [
                    {
                        "turn": 1,
                        "exit_code": 0,
                        "elapsed_seconds": 0.01,
                        "stdout": str(missing_root / "copilot.jsonl"),
                        "otel": str(missing_root / "otel.jsonl"),
                    }
                ],
                fixture,
                interim=False,
            )
            self.assertFalse(reassessed["strict_machine_pass"])
            self.assertIn("model_not_observed", reassessed["failures"])

    def test_model_classifier_rejects_prefix_suffix_and_similar_names(self) -> None:
        self.assertTrue(bakeoff._is_mini_model("gpt-5-mini"))
        self.assertTrue(bakeoff._is_mini_model("GPT-5 mini"))
        for value in (
            "not-gpt-5-mini",
            "gpt-5-mini-plus",
            "gpt5miniature",
            "prefix-gpt-5-mini-suffix",
        ):
            with self.subTest(value=value):
                self.assertFalse(bakeoff._is_mini_model(value))

    def test_cli_rejects_noncanonical_model_before_any_stage_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_stage = mock.Mock(return_value=0)
            with mock.patch.object(bakeoff, "_run_stage", run_stage):
                with self.assertRaises(SystemExit):
                    bakeoff.main(
                        [
                            "--stage",
                            "stage1",
                            "--output-root",
                            temporary,
                            "--allow-metered-run",
                            "--model",
                            "gpt-5-mini-plus",
                        ]
                    )
            run_stage.assert_not_called()

    def test_summary_read_requires_an_approved_tool_and_exact_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary_path = (Path(temporary) / "grounded.md").resolve()
            exact = bakeoff._powershell_summary_command(summary_path)
            case = dict(bakeoff._case_data()["stage1"][0])
            good = _tool_event(
                "powershell", {"command": exact}, call_id="good-read"
            )
            sequence, unexpected = bakeoff._tool_observations(
                [good], case=case, summary_path=summary_path
            )
            self.assertEqual(["read_summary"], sequence)
            self.assertEqual([], unexpected)

            for bad in (
                _tool_event("web", {"command": exact}, call_id="wrong-tool"),
                _tool_event(
                    "powershell",
                    {"command": f"{exact}; Get-ChildItem Env:"},
                    call_id="chained-command",
                ),
            ):
                with self.subTest(call_id=bad["toolCallId"]):
                    sequence, unexpected = bakeoff._tool_observations(
                        [bad], case=case, summary_path=summary_path
                    )
                    self.assertEqual([], sequence)
                    self.assertEqual(1, len(unexpected))

    def test_search_tool_requires_the_exact_unicode_question_command(self) -> None:
        case = dict(bakeoff._case_data()["stage2"][0])
        with tempfile.TemporaryDirectory() as temporary:
            summary_path = (Path(temporary) / "unicode_argv.md").resolve()
            exact = bakeoff._powershell_search_command(case)
            good = _tool_event(
                "powershell", {"command": exact}, call_id="good-search"
            )
            sequence, unexpected = bakeoff._tool_observations(
                [good], case=case, summary_path=summary_path
            )
            self.assertEqual(["search"], sequence)
            self.assertEqual([], unexpected)

            for bad_arguments in (
                {"command": exact + " extra-argv"},
                {"command": exact.replace("beta-rag", "alpha-rag", 1)},
                {"argv": ["powershell.exe", "-Command", exact]},
            ):
                with self.subTest(arguments=bad_arguments):
                    sequence, unexpected = bakeoff._tool_observations(
                        [
                            _tool_event(
                                "powershell",
                                bad_arguments,
                                call_id="bad-search",
                            )
                        ],
                        case=case,
                        summary_path=summary_path,
                    )
                    self.assertEqual([], sequence)
                    self.assertEqual(1, len(unexpected))

    def test_unapproved_tool_event_fails_whole_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case, fixture, _ = self._grounded_case_and_fixture(root)
            assessment = _passing_assessment(
                case,
                root / "case",
                fixture,
                extra_cli_events=[
                    _tool_event(
                        "web",
                        {"query": "must not be exposed"},
                        call_id="unexpected-web",
                    )
                ],
            )
            self.assertFalse(assessment["strict_machine_pass"])
            self.assertIn("unapproved_tool_call", assessment["failures"])

    def test_process_trace_requires_fixture_python_and_script_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case, fixture, _ = self._grounded_case_and_fixture(root)
            assessment = _passing_assessment(
                case,
                root / "case",
                fixture,
                trace_python=str(root / "evil" / "python.exe"),
                trace_script=str(root / "evil" / "search.py"),
            )
            self.assertFalse(assessment["strict_machine_pass"])
            self.assertTrue(
                {
                    "process_trace_python_mismatch",
                    "process_trace_search_script_mismatch",
                }.issubset(assessment["failures"]),
                assessment["failures"],
            )


class StepwiseHumanCheckpointTests(unittest.TestCase):
    def _exercise(
        self,
        root: Path,
        *,
        decision: str,
        tamper_first: bool = False,
        bypass_progress_first: bool = False,
    ) -> tuple[mock.Mock, dict[str, Any]]:
        copilot = root / "copilot.exe"
        copilot.write_bytes(b"fixture")
        installed_venv = root / "venv"
        installed_venv.mkdir()
        fixture_workspace = root / "fixture-workspace"
        fixture_workspace.mkdir()
        fixture = {
            "workspace": str(fixture_workspace),
            "profile": str(root / "profile"),
            "summary_root": str(root / "summaries"),
            "copilot_home": str(root / "copilot-home"),
            "installed_venv": str(installed_venv),
        }
        cases = [
            {
                "id": "case-1",
                "scenario": "grounded",
                "question": "Q1",
                "turns": ["Q1"],
                "expected_db": "alpha-rag",
                "expected_tool_sequence": ["search", "read_summary"],
                "assistant_all": [],
                "assistant_any_groups": [],
            },
            {
                "id": "case-2",
                "scenario": "grounded",
                "question": "Q2",
                "turns": ["Q2"],
                "expected_db": "alpha-rag",
                "expected_tool_sequence": ["search", "read_summary"],
                "assistant_all": [],
                "assistant_any_groups": [],
            },
        ]
        args = Namespace(
            stage="stage1",
            candidates=None,
            copilot_path=str(copilot),
            installed_venv=str(installed_venv),
            model=bakeoff.MODEL_ID,
            max_ai_credits=30,
            timeout_seconds=30,
        )
        manifest = {
            "schema_version": "test-stepwise-manifest",
            "stage": "stage1",
            "selected_candidates": ["A"],
            "case_ids": ["case-1", "case-2"],
            "output_root": str(root),
            "fixture": fixture,
        }

        def run_case(**kwargs: Any) -> dict[str, Any]:
            case = kwargs["case"]
            case_directory = (
                root / "stage1" / "A" / str(case["id"])
            )
            case_directory.mkdir(parents=True, exist_ok=False)
            assessment = {
                "schema_version": "lrr-agent-002-case-assessment-v1",
                "case_id": case["id"],
                "interim": False,
                "status": "PASS",
                "strict_machine_pass": True,
                "failures": [],
                "observed": {
                    "tool_sequence": ["search", "read_summary"],
                    "unexpected_tools": [],
                    "process_trace_sequence": ["search"],
                    "requested_models": ["gpt-5-mini"],
                    "selected_models": ["gpt-5-mini"],
                    "assistant_by_turn": ["supported"],
                    "elapsed_seconds": [0.01],
                },
                "human_evidence_review": "PENDING",
                "case": case,
            }
            bakeoff._write_json(case_directory / "assessment.json", assessment)
            ledger = bakeoff._load_ledger(root)
            ledger["count"] = int(ledger["count"]) + 1
            ledger["entries"].append(
                {"prompt_number": ledger["count"], "status": "completed"}
            )
            bakeoff._write_json(bakeoff._ledger_path(root), ledger)
            return assessment

        run_one = mock.Mock(side_effect=run_case)
        patches = (
            mock.patch.object(bakeoff, "_prepare_fixture", return_value=fixture),
            mock.patch.object(bakeoff, "_candidate_map", return_value={"A": "agent-a"}),
            mock.patch.object(bakeoff, "_select_candidates", return_value=["A"]),
            mock.patch.object(bakeoff, "_stage_cases", return_value=cases),
            mock.patch.object(bakeoff, "_stepwise_manifest", return_value=manifest),
            mock.patch.object(bakeoff, "_run_one_case", run_one),
            mock.patch.object(bakeoff, "_finalize_stepwise_stage", return_value=0),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            self.assertEqual(0, bakeoff._run_stage_locked(args, root))
            self.assertEqual(1, run_one.call_count)
            progress_path = root / "stage1" / "progress.json"
            if bypass_progress_first:
                original_progress = bakeoff._read_json(progress_path)
                bypass = json.loads(json.dumps(original_progress))
                bypass["awaiting_review"] = None
                bypass["candidate_states"]["A"]["next_case_index"] = 1
                bakeoff._write_json(progress_path, bypass)
                with self.assertRaisesRegex(RuntimeError, "PASS review prefix"):
                    bakeoff._run_stage_locked(args, root)
                self.assertEqual(1, run_one.call_count)
                bakeoff._write_json(progress_path, original_progress)
            self.assertEqual(0, bakeoff._run_stage_locked(args, root))
            self.assertEqual(1, run_one.call_count, "pending review launched a prompt")
            review_path = root / "stage1" / "A" / "case-1" / "human-review.json"
            review = bakeoff._read_json(
                root / "stage1" / "A" / "case-1" / "human-review-template.json"
            )
            if tamper_first:
                tampered = dict(review)
                tampered["case_id"] = "case-2"
                bakeoff._write_json(review_path, tampered)
                with self.assertRaisesRegex(RuntimeError, "binding mismatch"):
                    bakeoff._run_stage_locked(args, root)
                self.assertEqual(1, run_one.call_count)
            review["status"] = decision
            review["note"] = "manual evidence decision"
            bakeoff._write_json(review_path, review)
            self.assertEqual(0, bakeoff._run_stage_locked(args, root))
            self.assertEqual(1, run_one.call_count, "decision application launched a prompt")
            progress = bakeoff._read_json(root / "stage1" / "progress.json")
            if decision == "PASS":
                self.assertEqual(0, bakeoff._run_stage_locked(args, root))
                self.assertEqual(2, run_one.call_count)
            else:
                self.assertEqual("EXCLUDED", progress["candidate_states"]["A"]["status"])
                self.assertEqual(1, run_one.call_count, "FAIL launched a later case")
            self.assertEqual(run_one.call_count, bakeoff._load_ledger(root)["count"])
        return run_one, progress

    def test_pass_review_resumes_only_on_a_later_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_one, _ = self._exercise(Path(temporary), decision="PASS")
            self.assertEqual(2, run_one.call_count)

    def test_fail_review_excludes_before_next_prompt_and_replay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_one, progress = self._exercise(
                Path(temporary),
                decision="FAIL",
                tamper_first=True,
                bypass_progress_first=True,
            )
            self.assertEqual(1, run_one.call_count)
            self.assertEqual("READY_TO_FINALIZE", progress["status"])


if __name__ == "__main__":
    unittest.main()
