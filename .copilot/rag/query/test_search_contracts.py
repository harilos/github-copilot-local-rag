from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
SEARCH_SPEC = importlib.util.spec_from_file_location("rag_search_cli", QUERY_ROOT / "search.py")
assert SEARCH_SPEC and SEARCH_SPEC.loader
SEARCH = importlib.util.module_from_spec(SEARCH_SPEC)
SEARCH_SPEC.loader.exec_module(SEARCH)

TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))
from software_rag_tool.search_api import _add_identifier_diagnostics, _raw_identifier_occurs, payload_to_prompt


class FakeStore:
    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict]:
        del top_k, source
        if "A2W" in question:
            return []
        return []


class BrokenStore:
    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict]:
        del question, top_k, source
        raise RuntimeError("diagnostic backend unavailable")


class SixCandidateStore:
    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict]:
        del source
        if question == "A2L":
            if top_k < 6:
                raise AssertionError("diagnostics must inspect more than the first five candidates")
            return [
                {"text": "A2L", "metadata": {"path": f"match-{index}.txt"}}
                for index in range(5)
            ] + [{"text": "not the identifier", "metadata": {"path": "bad.txt"}}]
        return []


class NoHitContractTests(unittest.TestCase):
    def test_plain_acronym_is_not_treated_as_conclusive_unmatched_identifier(self) -> None:
        evidence = [{"id": "R1", "source": {"path": "related.txt"}, "text": "related"}]
        payload = {
            "status": "ok",
            "evidence": list(evidence),
            "contexts": list(evidence),
            "warnings": [],
        }
        _add_identifier_diagnostics(
            payload,
            FakeStore(),
            "RAGでポーランドについて教えて",
            source="any",
        )
        self.assertEqual("ok", payload["status"])
        self.assertEqual(evidence, payload["evidence"])
        self.assertNotIn("unmatched_identifiers", payload)

    def test_unmatched_identifier_isolated_from_normal_contexts(self) -> None:
        evidence = [{"id": "R1", "source": {"path": "related.txt"}, "text": "related"}]
        payload = {
            "status": "ok",
            "evidence": list(evidence),
            "contexts": list(evidence),
            "related_context": [],
            "results": [{"id": "raw-1"}],
            "related_results": [],
            "warnings": [],
        }
        _add_identifier_diagnostics(payload, FakeStore(), "A2W", source="any")
        self.assertEqual("partial", payload["status"])
        self.assertEqual([], payload["evidence"])
        self.assertEqual([], payload["contexts"])
        self.assertEqual(evidence, payload["related_context"])
        self.assertEqual([], payload["results"])
        self.assertEqual([{"id": "raw-1"}], payload["related_results"])

    def test_raw_occurrence_uses_identifier_boundaries(self) -> None:
        self.assertTrue(_raw_identifier_occurs({"text": "A2L is supported", "metadata": {}}, "A2L"))
        self.assertFalse(_raw_identifier_occurs({"text": "XA2LY", "metadata": {}}, "A2L"))

    def test_identifier_backend_exception_is_not_reported_as_no_match(self) -> None:
        payload = {
            "status": "ok",
            "evidence": [{"text": "related"}],
            "contexts": [{"text": "related"}],
            "warnings": [],
        }
        _add_identifier_diagnostics(payload, BrokenStore(), "A2W", source="any")
        self.assertFalse(payload["identifiers"]["diagnostics_complete"])
        self.assertEqual([], payload["unmatched_identifiers"])
        self.assertIn("identifier_diagnostics_error", payload)
        self.assertEqual([{"text": "related"}], payload["evidence"])

    def test_raw_occurrence_requires_every_returned_candidate(self) -> None:
        payload = {"status": "ok", "evidence": [], "contexts": [], "warnings": []}
        _add_identifier_diagnostics(payload, SixCandidateStore(), "A2L", source="any")
        match = payload["identifiers"]["matches"][0]
        self.assertEqual(6, match["candidate_count"])
        self.assertEqual(5, match["verified_candidate_count"])
        self.assertFalse(match["raw_occurrence_verified"])

    def test_no_hit_prompt_marks_related_context_as_non_evidence(self) -> None:
        prompt = payload_to_prompt(
            {
                "status": "partial",
                "query": "A2Wについて",
                "db": "ac-rag",
                "unmatched_identifiers": ["A2W"],
                "evidence": [],
                "contexts": [],
                "related_context": [{"id": "R1", "source": {"path": "related.txt"}, "text": "related"}],
            }
        )
        self.assertIn("完全一致を確認できませんでした", prompt)
        self.assertIn("関連検索結果", prompt)
        self.assertIn("根拠として引用しないこと", prompt)


class SyncFallbackMetadataTests(unittest.TestCase):
    def test_sync_success_preserves_first_attempt_and_marks_final_success(self) -> None:
        args = argparse.Namespace(
            stdin=False,
            top_k=8,
            max_chars=1200,
            format="json",
            budget_tokens=1200,
            retrieval_mode="hybrid",
            explain=False,
            include_db_hint=False,
            disable_identifier_diagnostics=False,
            timeout=15,
        )
        completed = subprocess.CompletedProcess(
            args=["query"],
            returncode=0,
            stdout=json.dumps({"schema": "local-rag.search.v1", "status": "ok"}),
            stderr="",
        )
        output = io.StringIO()
        with mock.patch.object(SEARCH, "_run_sync_child", return_value=completed):
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    SEARCH._run_sync_script(
                        python="python",
                        env={},
                        args=args,
                        question="q",
                        db_name="ac-rag",
                        execution_metadata={
                            "request_id": "request-1",
                            "requested_execution": "daemon",
                            "first_attempt_success": False,
                            "fallback_used": True,
                            "attempts": [{"route": "daemon", "success": False}],
                        },
                    )
        self.assertEqual(0, raised.exception.code)
        payload = json.loads(output.getvalue())
        metadata = payload["execution_metadata"]
        self.assertFalse(metadata["first_attempt_success"])
        self.assertTrue(metadata["final_user_visible_success"])
        self.assertTrue(metadata["fallback_used"])
        self.assertEqual("no-daemon", metadata["actual_execution"])
        self.assertEqual(2, len(metadata["attempts"]))

    def test_deadline_exhaustion_does_not_spawn_fallback(self) -> None:
        args = argparse.Namespace(
            stdin=False,
            top_k=8,
            max_chars=1200,
            format="json",
            budget_tokens=1200,
            retrieval_mode="hybrid",
            explain=False,
            include_db_hint=False,
            disable_identifier_diagnostics=False,
            timeout=15,
        )
        output = io.StringIO()
        with mock.patch.object(SEARCH, "_run_sync_child") as run:
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    SEARCH._run_sync_script(
                        python="python",
                        env={},
                        args=args,
                        question="q",
                        db_name="ac-rag",
                        timeout_override=0.0,
                        execution_metadata={
                            "requested_execution": "daemon",
                            "first_attempt_success": False,
                            "attempts": [{"route": "daemon", "success": False}],
                        },
                    )
        self.assertEqual(124, raised.exception.code)
        run.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["execution_metadata"]["deadline_exhausted"])
        self.assertFalse(payload["execution_metadata"]["final_user_visible_success"])

    def test_invalid_fallback_json_is_wrapped_as_pure_json(self) -> None:
        args = argparse.Namespace(
            stdin=False,
            top_k=8,
            max_chars=1200,
            format="json",
            budget_tokens=1200,
            retrieval_mode="hybrid",
            explain=False,
            include_db_hint=False,
            disable_identifier_diagnostics=False,
            timeout=15,
        )
        completed = subprocess.CompletedProcess(
            args=["query"],
            returncode=0,
            stdout="model warning\nnot-json",
            stderr="",
        )
        output = io.StringIO()
        with mock.patch.object(SEARCH, "_run_sync_child", return_value=completed):
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    SEARCH._run_sync_script(
                        python="python",
                        env={},
                        args=args,
                        question="q",
                        db_name="ac-rag",
                        timeout_override=10.0,
                        execution_metadata={
                            "requested_execution": "daemon",
                            "first_attempt_success": False,
                            "fallback_used": True,
                            "attempts": [{"route": "daemon", "success": False}],
                        },
                    )
        self.assertEqual(1, raised.exception.code)
        payload = json.loads(output.getvalue())
        self.assertEqual("error", payload["status"])
        self.assertEqual("invalid_json", payload["execution_metadata"]["attempts"][-1]["failure_kind"])

    def test_sync_spawn_error_is_wrapped_as_pure_json(self) -> None:
        args = argparse.Namespace(
            stdin=False,
            top_k=8,
            max_chars=1200,
            format="json",
            budget_tokens=1200,
            retrieval_mode="hybrid",
            explain=False,
            include_db_hint=False,
            disable_identifier_diagnostics=False,
            timeout=15,
        )
        output = io.StringIO()
        with mock.patch.object(SEARCH, "_run_sync_child", side_effect=OSError("spawn failed")):
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    SEARCH._run_sync_script(
                        python="python",
                        env={},
                        args=args,
                        question="q",
                        db_name="ac-rag",
                        timeout_override=10.0,
                        execution_metadata={
                            "requested_execution": "daemon",
                            "first_attempt_success": False,
                            "fallback_used": True,
                            "attempts": [{"route": "daemon", "success": False}],
                        },
                    )
        self.assertEqual(1, raised.exception.code)
        payload = json.loads(output.getvalue())
        self.assertEqual("error", payload["status"])
        self.assertEqual("spawn_error", payload["execution_metadata"]["attempts"][-1]["failure_kind"])

    def test_bounded_timeout_uses_absolute_remaining_time(self) -> None:
        with mock.patch.object(SEARCH.time, "monotonic", return_value=100.0):
            self.assertEqual(5.0, SEARCH._bounded_timeout(110.0, 5.0))
            self.assertEqual(2.0, SEARCH._bounded_timeout(102.0, 5.0))
            self.assertEqual(0.0, SEARCH._bounded_timeout(99.0, 5.0))

    def test_cold_daemon_uses_outer_remaining_but_warm_uses_soft_timeout(self) -> None:
        with mock.patch.object(SEARCH.time, "monotonic", return_value=100.0):
            self.assertEqual(
                12.0,
                SEARCH._daemon_query_timeout(
                    attempt_timeout=5.0,
                    deadline=112.0,
                    cold_start=True,
                ),
            )
            self.assertEqual(
                5.0,
                SEARCH._daemon_query_timeout(
                    attempt_timeout=5.0,
                    deadline=112.0,
                    cold_start=False,
                ),
            )

    def test_sync_timeout_terminates_process_group_without_pipe_hang(self) -> None:
        child = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "time.sleep(30)"
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            SEARCH._run_sync_child(
                [sys.executable, "-c", child],
                env={},
                input_text=None,
                timeout=0.1,
            )
        self.assertLess(time.monotonic() - started, 2.0)


class DaemonLifecycleTests(unittest.TestCase):
    def test_active_daemon_rejects_stale_runtime_fingerprint_before_healthcheck(self) -> None:
        state = {
            "schema": "local-rag.ragd.v2",
            "pid": 43210,
            "generation": "generation",
            "transport": "file",
            "file_dir": "/tmp/file-transport",
            "heartbeat_file": "/tmp/heartbeat.json",
            "token": "token",
            "code_fingerprint": "old",
        }
        with (
            mock.patch.object(SEARCH, "_read_state", return_value=state),
            mock.patch.object(SEARCH, "_runtime_code_fingerprint", return_value="new"),
            mock.patch.object(SEARCH, "_healthcheck") as healthcheck,
        ):
            active = SEARCH._active_daemon_state(timeout=1.0)
        self.assertIsNone(active)
        healthcheck.assert_not_called()

    def test_daemon_identity_requires_authenticated_matching_process(self) -> None:
        state = {
            "pid": 43210,
            "generation": "generation",
            "code_fingerprint": "fingerprint",
        }
        self.assertTrue(
            SEARCH._daemon_identity_matches(
                state,
                {
                    "status": "ok",
                    "pid": 43210,
                    "generation": "generation",
                    "code_fingerprint": "fingerprint",
                },
            )
        )
        self.assertFalse(
            SEARCH._daemon_identity_matches(
                state,
                {
                    "status": "ok",
                    "pid": 43210,
                    "generation": "other",
                    "code_fingerprint": "fingerprint",
                },
            )
        )

    def test_start_timeout_rejects_foreign_state_and_reaps_spawned_process(self) -> None:
        process = mock.Mock()
        process.pid = 43210
        process.poll.return_value = None
        process.wait.return_value = 0
        foreign_state = {
            "schema": "local-rag.ragd.v2",
            "pid": process.pid,
            "generation": "foreign-generation",
            "transport": "tcp",
            "host": "127.0.0.1",
            "port": 12345,
            "token": "foreign-token",
        }
        with (
            mock.patch.object(SEARCH, "_acquire_start_lock", return_value=99),
            mock.patch.object(SEARCH, "_release_start_lock") as release,
            mock.patch.object(SEARCH, "_free_port", return_value=12345),
            mock.patch.object(SEARCH.secrets, "token_hex", return_value="expected-generation"),
            mock.patch.object(SEARCH.subprocess, "Popen", return_value=process),
            mock.patch.object(SEARCH, "_read_state", return_value=foreign_state),
            mock.patch.object(SEARCH, "_healthcheck", return_value=True),
            mock.patch.object(SEARCH, "_terminate_process_tree") as terminate,
            mock.patch.object(Path, "open", mock.mock_open()),
        ):
            state = SEARCH._start_daemon(
                python="python",
                env={},
                idle_timeout=60,
                startup_timeout=0.01,
                transport="tcp",
            )
        self.assertIsNone(state)
        terminate.assert_called_once_with(process)
        process.wait.assert_called_once_with(timeout=0.5)
        release.assert_called_once_with(99)

    def test_start_returns_only_matching_generation_pid_and_transport(self) -> None:
        process = mock.Mock()
        process.pid = 43210
        process.poll.return_value = None
        expected_state = {
            "schema": "local-rag.ragd.v2",
            "pid": process.pid,
            "generation": "expected-generation",
            "transport": "tcp",
            "host": "127.0.0.1",
            "port": 12345,
            "token": "expected-generation",
        }
        with (
            mock.patch.object(SEARCH, "_acquire_start_lock", return_value=99),
            mock.patch.object(SEARCH, "_release_start_lock"),
            mock.patch.object(SEARCH, "_free_port", return_value=12345),
            mock.patch.object(SEARCH.secrets, "token_hex", return_value="expected-generation"),
            mock.patch.object(SEARCH.subprocess, "Popen", return_value=process),
            mock.patch.object(SEARCH, "_read_state", return_value=expected_state),
            mock.patch.object(SEARCH, "_healthcheck", return_value=True),
            mock.patch.object(SEARCH, "_terminate_process_tree") as terminate,
            mock.patch.object(Path, "open", mock.mock_open()),
        ):
            state = SEARCH._start_daemon(
                python="python",
                env={},
                idle_timeout=60,
                startup_timeout=1.0,
                transport="tcp",
            )
        self.assertEqual(expected_state, state)
        terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
