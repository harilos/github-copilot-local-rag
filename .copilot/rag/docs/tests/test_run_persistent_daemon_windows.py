from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest import mock


MODULE_PATH = Path(__file__).with_name(
    "run_persistent_daemon_windows.py"
)
SPEC = importlib.util.spec_from_file_location(
    "run_persistent_daemon_windows",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class JsonContractTests(unittest.TestCase):
    def test_parses_exactly_one_utf8_object(self) -> None:
        payload, error = MODULE.parse_one_json(
            '{"status":"ok","question":"日本語"}'.encode("utf-8")
        )
        self.assertIsNone(error)
        self.assertEqual("ok", payload["status"])

    def test_rejects_trailing_json(self) -> None:
        payload, error = MODULE.parse_one_json(b'{"status":"ok"}\\n{}')
        self.assertIsNone(payload)
        self.assertEqual("stdout_contains_trailing_data", error)

    def test_rejects_utf8_bom(self) -> None:
        payload, error = MODULE.parse_one_json(
            b"\\xef\\xbb\\xbf" + b'{"status":"ok"}'
        )
        self.assertIsNone(payload)
        self.assertIn("stdout_not_json", error)

    def test_duplicate_paths_are_windows_normalized(self) -> None:
        duplicates = MODULE.duplicate_document_paths(
            {
                "document_results": [
                    {"path": "Reports\\One.PDF"},
                    {"path": "reports/one.pdf"},
                    {"path": "reports/two.pdf"},
                ]
            }
        )
        self.assertEqual(["reports/one.pdf"], duplicates)

    def test_retrieval_projection_ignores_volatile_daemon_fields(self) -> None:
        base = {
            "status": "partial",
            "answerability": "none",
            "selected_db": "ac-rag",
            "unmatched_identifiers": ["A2W"],
            "exact_candidate_count": 0,
            "evidence": [],
            "document_results": [
                {"path": "one.pdf", "support_level": "weak"}
            ],
            "coverage": {
                "policy": "wide",
                "returned_distinct_documents": 1,
            },
            "dense_used": True,
            "dense_skipped_reason": None,
            "daemon_state": {"request_id": "volatile"},
        }
        other = dict(base, daemon_state={"request_id": "different"})
        self.assertEqual(
            MODULE.retrieval_contract_projection(base),
            MODULE.retrieval_contract_projection(other),
        )


class GateMathTests(unittest.TestCase):
    def test_percentile_uses_nearest_rank(self) -> None:
        self.assertEqual(18, MODULE.percentile(list(range(20)), 95))

    def test_monotonic_requires_every_step_to_increase(self) -> None:
        self.assertTrue(MODULE.is_monotonic_increase([1, 2, 3, 4]))
        self.assertFalse(MODULE.is_monotonic_increase([1, 2, 2, 4]))
        self.assertFalse(MODULE.is_monotonic_increase([1, 2, 3]))

    def test_median_absolute_deviation(self) -> None:
        self.assertEqual(1, MODULE.median_absolute_deviation([1, 2, 3]))

    def test_stable_identity_checks_manager_and_worker(self) -> None:
        base = {
            "result": "PASS",
            "manager_pid": 10,
            "worker_pid": 11,
            "manager_generation": "m",
            "worker_generation": "w",
            "model_load_count": 1,
        }
        self.assertTrue(MODULE.stable_identity([dict(base), dict(base)]))
        changed = dict(base, worker_generation="w2")
        self.assertFalse(MODULE.stable_identity([base, changed]))
        missing = dict(base, worker_pid=None)
        self.assertFalse(MODULE.stable_identity([missing, missing]))

    def test_raw_identifier_verification_accepts_text_or_source_path(self) -> None:
        self.assertTrue(
            MODULE.raw_identifier_occurs(
                "RFC10026",
                "The RFC10026 requirements apply.",
                "",
            )
        )
        self.assertTrue(
            MODULE.raw_identifier_occurs(
                "Report.PDF",
                "",
                r"Docs\report.pdf",
            )
        )
        self.assertFalse(
            MODULE.raw_identifier_occurs("A2W", "A2L", "a2l.pdf")
        )
        self.assertFalse(
            MODULE.raw_identifier_occurs(
                "RFC10002",
                "RFC10002X is a different identifier.",
                "",
            )
        )

    def test_weak_review_grade_cannot_be_strong_or_direct(self) -> None:
        self.assertTrue(
            MODULE.support_level_is_calibrated(
                grade=1,
                support="weak",
                authoritative=False,
                is_evidence=False,
            )
        )
        for support in ("strong", "direct"):
            self.assertFalse(
                MODULE.support_level_is_calibrated(
                    grade=1,
                    support=support,
                    authoritative=False,
                    is_evidence=False,
                )
            )


class CommandTests(unittest.TestCase):
    def _runner(self) -> MODULE.PersistentDaemonWindowsRunner:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "rag"
        query = root / "query"
        python = query / ".venv" / (
            Path("Scripts/python.exe")
            if sys.platform.startswith("win")
            else Path("bin/python")
        )
        python.parent.mkdir(parents=True)
        shutil.copy2(sys.executable, python)
        (query / "search.py").write_text("", encoding="utf-8")
        output = Path(temporary.name) / "artifacts"
        with mock.patch.object(MODULE.sys, "executable", str(python)):
            return MODULE.PersistentDaemonWindowsRunner(
                installed_rag=root,
                output_dir=output,
                run_id="unit",
                databases=("ac-rag",),
                deadline_seconds=15,
            )

    def test_client_command_uses_direct_venv_and_no_shell_wrapper(self) -> None:
        runner = self._runner()
        command = runner.client_command(
            MODULE.make_case("unit", "ac-rag", "H", 0)
        )
        self.assertEqual(str(runner.python), command[0])
        joined = " ".join(command).casefold()
        self.assertNotIn("cmd.exe", joined)
        self.assertNotIn("start-process", joined)
        self.assertNotIn("powershell", joined)
        self.assertNotIn(".bat", joined)
        self.assertNotIn(".cmd", joined)
        self.assertIn("--compact-json", command)
        self.assertIn("--require-daemon", command)

    def test_profiles_map_to_expected_retrieval_modes(self) -> None:
        runner = self._runner()
        lexical = runner.client_command(
            MODULE.make_case("unit", "ac-rag", "L", 0)
        )
        dense = runner.client_command(
            MODULE.make_case("unit", "ac-rag", "V", 0)
        )
        hybrid = runner.client_command(
            MODULE.make_case("unit", "ac-rag", "H", 0)
        )
        self.assertIn("lexical", lexical)
        self.assertIn("dense", dense)
        self.assertNotIn("--retrieval-mode", hybrid)

    def test_output_inside_installed_tree_is_rejected(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "rag"
        query = root / "query"
        python = query / ".venv" / (
            Path("Scripts/python.exe")
            if sys.platform.startswith("win")
            else Path("bin/python")
        )
        python.parent.mkdir(parents=True)
        shutil.copy2(sys.executable, python)
        (query / "search.py").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside"):
            MODULE.PersistentDaemonWindowsRunner(
                installed_rag=root,
                output_dir=root / "artifacts",
                run_id="unit",
                databases=("ac-rag",),
                deadline_seconds=15,
            )

    def test_summary_json_is_machine_readable(self) -> None:
        runner = self._runner()
        runner.gate("unit", "PASS", "ok")
        summary = runner.finalize()
        loaded = json.loads(
            runner.artifacts.summary_path.read_text(encoding="utf-8")
        )
        self.assertEqual(summary["overall"], loaded["overall"])
        self.assertEqual("PASS", loaded["gates"]["unit"]["result"])

    def test_missing_required_gate_cannot_be_reported_as_pass(self) -> None:
        runner = self._runner()
        runner.required_gates.update({"completed", "skipped"})
        runner.gate("completed", "PASS", "ok")
        summary = runner.finalize()
        self.assertEqual("NOT_RUN", summary["overall"])
        self.assertEqual("NOT_RUN", summary["gates"]["skipped"]["result"])

    def test_resource_limits_use_fixed_release_gate_allowances(self) -> None:
        runner = self._runner()
        baseline = [
            {
                "manager_handle_count": 100,
                "worker_handle_count": 100,
                "manager_thread_count": 10,
                "worker_thread_count": 10,
                "manager_rss_bytes": 100 * 1024 * 1024,
                "worker_rss_bytes": 100 * 1024 * 1024,
            }
        ] * 3
        cohort = [
            {
                "manager_handle_count": value,
                "worker_handle_count": 100,
                "manager_thread_count": 10,
                "worker_thread_count": 10,
                "manager_rss_bytes": 100 * 1024 * 1024,
                "worker_rss_bytes": 100 * 1024 * 1024,
            }
            for value in (100, 101, 102, 117)
        ]
        runner.evaluate_resource_gates(baseline, cohort)
        self.assertEqual(
            "FAIL",
            runner.gates["resource_manager_handles"]["result"],
        )
        self.assertEqual(
            "PASS",
            runner.gates["resource_worker_handles"]["result"],
        )

    def test_quiescence_requires_three_consecutive_empty_polls(self) -> None:
        runner = self._runner()
        runner.health = mock.Mock(
            side_effect=[
                {"active_requests": 1, "queue_depth": 0},
                {"active_requests": 0, "queue_depth": 0},
                {"active_requests": 0, "queue_depth": 1},
                {"active_requests": 0, "queue_depth": 0},
                {"active_requests": 0, "queue_depth": 0},
                {
                    "active_requests": 0,
                    "queue_depth": 0,
                    "manager_generation": "manager",
                },
            ]
        )
        with mock.patch.object(MODULE.time, "sleep"):
            health = runner.wait_for_daemon_quiescence(
                timeout_seconds=5,
                poll_seconds=0,
            )
        self.assertEqual("manager", health["manager_generation"])
        self.assertEqual(6, runner.health.call_count)

    def test_resource_warmup_runs_twenty_unrecorded_at_c4(self) -> None:
        runner = self._runner()
        runner.databases = (
            "ac-rag",
            "incident-rag",
            "rfc-full-20k-rag",
        )
        batches: list[tuple[int, bool]] = []
        identity = {
            "result": "PASS",
            "manager_pid": 10,
            "worker_pid": 11,
            "manager_generation": "manager",
            "worker_generation": "worker",
            "model_load_count": 1,
        }

        def run_concurrent(
            cases: list[MODULE.ClientCase],
            *,
            phase: str,
            record: bool,
        ) -> list[dict[str, object]]:
            self.assertEqual("soak-resource-warm", phase)
            batches.append((len(cases), record))
            return [dict(identity) for _ in cases]

        runner.run_concurrent = run_concurrent
        runner.wait_for_daemon_quiescence = mock.Mock(
            return_value={
                **identity,
                "active_requests": 0,
                "queue_depth": 0,
            }
        )
        result = runner.warm_soak_resource_paths()
        self.assertEqual("PASS", result["result"])
        self.assertEqual([(4, False)] * 5, batches)
        self.assertEqual(20, len(result["rows"]))
        self.assertFalse(result["event"]["recorded_as_soak_cases"])

    def test_rss_monotonic_gate_is_not_relaxed_by_warmup_change(self) -> None:
        runner = self._runner()
        mib = 1024 * 1024
        baseline = [
            {
                "manager_handle_count": 100,
                "worker_handle_count": 100,
                "manager_thread_count": 10,
                "worker_thread_count": 10,
                "manager_rss_bytes": 31 * mib,
                "worker_rss_bytes": 100 * mib,
            }
        ] * 3
        cohort = [
            {
                "manager_handle_count": 100,
                "worker_handle_count": 100,
                "manager_thread_count": 10,
                "worker_thread_count": 10,
                "manager_rss_bytes": value * mib,
                "worker_rss_bytes": 100 * mib,
            }
            for value in (32, 33, 34, 35, 36)
        ]
        runner.evaluate_resource_gates(baseline, cohort)
        self.assertEqual(
            "FAIL",
            runner.gates["resource_manager_rss"]["result"],
        )
        self.assertIn(
            "monotonic=True",
            runner.gates["resource_manager_rss"]["detail"],
        )

    def test_phase_gate_map_uses_canonical_gate_names(self) -> None:
        self.assertEqual(
            ("db_release_all",),
            MODULE.PHASE_GATE_NAMES["db-release"],
        )
        self.assertIn(
            "manager_job_recovery",
            MODULE.PHASE_GATE_NAMES["crash"],
        )
        self.assertEqual(
            ("structured_request_equivalence",),
            MODULE.PHASE_GATE_NAMES["structured-contract"],
        )

    def test_structured_json_and_repeated_argv_normalize_equally(self) -> None:
        rag_root = MODULE_PATH.parents[2]
        request = {
            "schema_version": "rag-search-request-v1",
            "original_question": '日本語 Mix-ID_2/Path.v1\\仕様 "引用" を確認。',
            "answer_goal": "evidence",
            "literal_identifiers": ["A2W", "Mix-ID_2/Path.v1"],
            "entities": ['製品 "Alpha"\\仕様'],
            "facets": [
                {"kind": "literal", "query": "A2W"},
                {"kind": "semantic", "query": "関連する方式"},
            ],
            "inferred_concepts": [
                {"term": "A2L", "semantic_only": True}
            ],
            "coverage": {},
        }
        module = MODULE.load_installed_search_request_module(rag_root)
        from_json, from_argv = MODULE.normalize_structured_pair(
            module,
            request,
        )
        self.assertEqual(from_json, from_argv)
        self.assertEqual(
            request["original_question"],
            from_argv["original_question"],
        )
        self.assertTrue(
            from_argv["inferred_concepts"][0]["semantic_only"]
        )
        self.assertEqual("wide", from_argv["coverage"]["policy"])

    def test_structured_phase_is_a_parser_choice(self) -> None:
        args = MODULE.build_parser().parse_args(
            [
                "--phase",
                "structured-contract",
                "--installed-rag",
                "rag",
                "--output-dir",
                "out",
                "--run-id",
                "unit",
            ]
        )
        self.assertEqual("structured-contract", args.phase)


if __name__ == "__main__":
    unittest.main()
