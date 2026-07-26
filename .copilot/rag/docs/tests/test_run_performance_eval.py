from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("run_performance_eval.py")
SPEC = importlib.util.spec_from_file_location("run_performance_eval", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def report_args(**overrides: object) -> argparse.Namespace:
    values = {
        "daemon_slo_p95": None,
        "p95_target_h": 8.0,
        "p95_target_l": 2.0,
        "p95_target_v": 8.0,
        "hard_latency_limit": 15.0,
        "timeout_rate_gate": 0.0,
        "min_samples_for_p95": 2,
        "degradation_ratio_limit": 1.2,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def success_row(**overrides: object) -> dict:
    row = {
        "kind": "search",
        "run_at": "2026-07-26T00:00:00+00:00",
        "db": "ac-rag",
        "db_hash": "version-a",
        "db_snapshot_hash": "snapshot-a",
        "profile": "H",
        "execution": "daemon",
        "latency_seconds": 1.0,
        "exit_code": 0,
        "json_ok": True,
        "request_success": True,
        "stdout_json_pure": True,
        "status": "ok",
    }
    row.update(overrides)
    return row


class CohortTests(unittest.TestCase):
    def test_legacy_sources_never_pool(self) -> None:
        rows = [
            success_row(report_source="old-a.jsonl"),
            success_row(report_source="old-b.jsonl"),
        ]
        groups = MODULE.compatible_run_groups(rows)
        self.assertEqual(2, len(groups))

    def test_current_rows_split_when_effective_explain_differs(self) -> None:
        base = {
            field: "same"
            for field in MODULE.RUN_IDENTITY_FIELDS
            if field != "explain_enabled"
        }
        rows = [
            success_row(**base, explain_enabled=False),
            success_row(**base, explain_enabled=True),
        ]
        self.assertEqual(2, len(MODULE.compatible_run_groups(rows)))

    def test_actual_route_and_missing_response_do_not_hide_timeout_from_cohort(self) -> None:
        base = {field: "same" for field in MODULE.RUN_IDENTITY_FIELDS}
        rows = [
            success_row(**base, actual_execution="daemon", identifier_diagnostics_enabled=True),
            success_row(
                **base,
                actual_execution=None,
                identifier_diagnostics_enabled="unknown",
                request_success=False,
                timed_out=True,
                status="timeout",
                exit_code=124,
            ),
        ]
        groups = MODULE.compatible_run_groups(rows)
        self.assertEqual(1, len(groups))
        self.assertEqual(2, len(groups[0][1]))

    def test_complete_runs_never_pool_across_sources(self) -> None:
        base = {field: "same" for field in MODULE.RUN_IDENTITY_FIELDS}
        rows = [
            success_row(**base, report_source="run-a.jsonl"),
            success_row(**base, report_source="run-b.jsonl"),
        ]
        self.assertEqual(2, len(MODULE.compatible_run_groups(rows)))

    def test_db_snapshot_maps_never_pool(self) -> None:
        base = {field: "same" for field in MODULE.RUN_IDENTITY_FIELDS if field != "db_identities"}
        rows = [
            success_row(**base, report_source="run.jsonl", db_identities={"ac-rag": {"db_snapshot_hash": "a"}}),
            success_row(**base, report_source="run.jsonl", db_identities={"ac-rag": {"db_snapshot_hash": "b"}}),
        ]
        self.assertEqual(2, len(MODULE.compatible_run_groups(rows)))


class LatencyGateTests(unittest.TestCase):
    def test_p95_and_hard_max_are_independent(self) -> None:
        args = report_args(min_samples_for_p95=20)
        rows = [success_row(latency_seconds=1.0) for _ in range(19)]
        rows.append(success_row(latency_seconds=16.0))
        self.assertEqual("PASS", MODULE.daemon_p95_gate_state(rows, args))
        self.assertEqual("FAIL", MODULE.hard_latency_gate_state(rows, args))

    def test_profile_specific_p95_threshold(self) -> None:
        args = report_args(min_samples_for_p95=2)
        rows = [
            success_row(profile="L", latency_seconds=2.1),
            success_row(profile="L", latency_seconds=2.1),
        ]
        self.assertEqual("FAIL", MODULE.daemon_p95_gate_state(rows, args))

    def test_low_n_is_not_pass_or_fail(self) -> None:
        args = report_args(min_samples_for_p95=3)
        rows = [success_row(), success_row()]
        self.assertEqual("INSUFFICIENT_N", MODULE.daemon_p95_gate_state(rows, args))

    def test_zero_rows_are_not_run(self) -> None:
        self.assertEqual("NOT_RUN", MODULE.state_for_rows(0, True))
        self.assertEqual("NOT_RUN", MODULE.daemon_p95_gate_state([], report_args()))

    def test_outer_deadline_covers_failed_rows_too(self) -> None:
        args = report_args()
        rows = [
            success_row(latency_seconds=1.0, outer_deadline_exceeded=False),
            success_row(
                latency_seconds=16.0,
                outer_deadline_exceeded=True,
                request_success=False,
                exit_code=124,
                status="error",
            ),
        ]
        self.assertEqual("FAIL", MODULE.outer_deadline_gate_state(rows, args))

    def test_time_degradation_uses_daemon_attempt_not_cli_overhead(self) -> None:
        rows = [
            success_row(
                sequence_plan="clean-mixed",
                sequence_index=index,
                latency_seconds=1.0 if index < 5 else 2.0,
                first_attempt_latency_seconds=0.5,
            )
            for index in range(10)
        ]
        self.assertEqual("PASS", MODULE.time_degradation_state(rows, report_args(min_samples_for_p95=2)))


class ExpectationTests(unittest.TestCase):
    def test_positive_case_does_not_create_unmatched_gate(self) -> None:
        case = {
            "expect_exact_positive": True,
            "expect_exact_negative": False,
            "expected_unmatched_identifiers": None,
            "expected_matched_identifiers": ["A2L"],
        }
        row = success_row(
            identifier_diagnostics_enabled=True,
            identifier_diagnostics_complete=True,
            exact_candidate_count=1,
            exact_signal_count=1,
            identifier_matches=[
                {
                    "identifier": "A2L",
                    "matched": True,
                    "raw_occurrence_verified": True,
                }
            ],
        )
        flags = MODULE.quality_flags(case, row)
        self.assertNotIn("expected_unmatched_pass", flags)
        self.assertTrue(flags["positive_exact_pass"])
        self.assertTrue(flags["matched_identifier_pass"])

    def test_positive_exact_requires_expected_identifier_and_raw_occurrence(self) -> None:
        case = {
            "expect_exact_positive": True,
            "expected_matched_identifiers": ["A2L"],
        }
        flags = MODULE.quality_flags(
            case,
            success_row(
                identifier_diagnostics_enabled=True,
                identifier_diagnostics_complete=True,
                exact_candidate_count=1,
                exact_signal_count=1,
                identifier_matches=[
                    {
                        "identifier": "WRONG",
                        "matched": True,
                        "raw_occurrence_verified": True,
                    }
                ],
            ),
        )
        self.assertFalse(flags["matched_identifier_pass"])
        self.assertFalse(flags["positive_exact_pass"])

    def test_diagnostics_off_makes_exact_not_applicable(self) -> None:
        case = {
            "expect_exact_negative": True,
            "expected_unmatched_identifiers": ["A2W"],
        }
        flags = MODULE.quality_flags(
            case,
            success_row(identifier_diagnostics_enabled=False, exact_candidate_count=None),
        )
        self.assertNotIn("negative_exact_pass", flags)
        self.assertNotIn("expected_unmatched_pass", flags)

    def test_unknown_diagnostics_do_not_enter_exact_denominator(self) -> None:
        case = {
            "expect_exact_negative": True,
            "expected_unmatched_identifiers": ["A2W"],
        }
        flags = MODULE.quality_flags(
            case,
            success_row(identifier_diagnostics_enabled="unknown"),
        )
        self.assertNotIn("negative_exact_pass", flags)

    def test_incomplete_diagnostics_cannot_pass_negative_exact(self) -> None:
        case = {
            "expect_exact_negative": True,
            "expected_unmatched_identifiers": ["A2W"],
        }
        flags = MODULE.quality_flags(
            case,
            success_row(
                identifier_diagnostics_enabled=True,
                identifier_diagnostics_complete=False,
                exact_candidate_count=0,
                exact_signal_count=0,
                unmatched_identifiers=["A2W"],
            ),
        )
        self.assertNotIn("negative_exact_pass", flags)

    def test_no_hit_rejects_legacy_evidence(self) -> None:
        case = {
            "answerable": False,
            "expected_unmatched_identifiers": ["A2W"],
        }
        flags = MODULE.quality_flags(
            case,
            success_row(
                exact_candidate_count=0,
                exact_signal_count=0,
                identifier_diagnostics_enabled=True,
                identifier_diagnostics_complete=True,
                unmatched_identifiers=["A2W"],
                evidence_count=1,
                context_count=0,
                results_count=0,
            ),
        )
        self.assertFalse(flags["no_hit_contract_pass"])


class CleanMixedTests(unittest.TestCase):
    def test_release_matrix_has_minimum_v_per_db(self) -> None:
        counts = MODULE.clean_mixed_cell_counts(
            total=500,
            db_names={"ac-rag", "incident-rag", "rfc-full-20k-rag"},
            profiles={"H", "L", "V"},
        )
        self.assertEqual(500, sum(counts.values()))
        for db_name in ("ac-rag", "incident-rag", "rfc-full-20k-rag"):
            self.assertEqual(20, counts[(db_name, "V")])

    def test_release_validation_rejects_nonstandard_bucket_count(self) -> None:
        class Parser:
            @staticmethod
            def error(message: str) -> None:
                raise ValueError(message)

        args = argparse.Namespace(
            min_samples_for_p95=20,
            report_only=None,
            sequence_plan="clean-mixed",
            pure_profile=True,
            explain_mode="off",
            diagnostics_level="off",
            executions=["daemon"],
            timeout=15,
            daemon_attempt_timeout=5.0,
            warmup_runs=5,
            mixed_total=500,
            profiles=["H", "L", "V"],
            time_buckets=5,
            p95_target_h=8.0,
            p95_target_l=2.0,
            p95_target_v=8.0,
            hard_latency_limit=15.0,
            timeout_rate_gate=0.0,
            restart_daemon=True,
        )
        with self.assertRaisesRegex(ValueError, "time-buckets 10"):
            MODULE.validate_args(Parser(), args)

    def test_release_validation_rejects_legacy_timeout_contract(self) -> None:
        class Parser:
            @staticmethod
            def error(message: str) -> None:
                raise ValueError(message)

        args = argparse.Namespace(
            min_samples_for_p95=20,
            report_only=None,
            sequence_plan="clean-mixed",
            pure_profile=True,
            explain_mode="off",
            diagnostics_level="off",
            executions=["daemon"],
            timeout=30,
            daemon_attempt_timeout=15.0,
            warmup_runs=5,
            mixed_total=500,
            profiles=["H", "L", "V"],
            time_buckets=10,
            p95_target_h=8.0,
            p95_target_l=2.0,
            p95_target_v=8.0,
            hard_latency_limit=15.0,
            timeout_rate_gate=0.0,
            restart_daemon=True,
        )
        with self.assertRaisesRegex(ValueError, "timeout 15"):
            MODULE.validate_args(Parser(), args)

    def test_release_validation_accepts_15_second_outer_and_5_second_soft_timeout(self) -> None:
        class Parser:
            @staticmethod
            def error(message: str) -> None:
                raise AssertionError(message)

        args = argparse.Namespace(
            min_samples_for_p95=20,
            report_only=None,
            sequence_plan="clean-mixed",
            pure_profile=True,
            explain_mode="off",
            diagnostics_level="off",
            executions=["daemon"],
            timeout=15,
            daemon_attempt_timeout=5.0,
            warmup_runs=5,
            mixed_total=500,
            profiles=["H", "L", "V"],
            time_buckets=10,
            p95_target_h=8.0,
            p95_target_l=2.0,
            p95_target_v=8.0,
            hard_latency_limit=15.0,
            timeout_rate_gate=0.0,
            restart_daemon=True,
        )
        MODULE.validate_args(Parser(), args)

    def test_formal_output_paths_can_live_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            results, report = MODULE.run_output_paths("release-sha", directory)
            self.assertEqual("performance-results-release-sha.jsonl", results.name)
            self.assertEqual("performance-report-release-sha.md", report.name)
            self.assertEqual(Path(directory).resolve(), results.parent)
            self.assertEqual(results.parent, report.parent)

    def test_db_identity_closes_read_only_catalog_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rag_root = Path(directory) / "rag"
            db_root = rag_root / "dbs" / "fixture-rag"
            db_root.mkdir(parents=True)
            catalog_path = db_root / "catalog.sqlite"
            with closing(sqlite3.connect(catalog_path)) as connection:
                with connection:
                    connection.execute(
                        """
                        CREATE TABLE chunk (
                          chunk_uid TEXT,
                          chunk_hash TEXT,
                          content_hash TEXT,
                          text_hash TEXT,
                          updated_at TEXT,
                          visible_until INTEGER
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO chunk VALUES (?, ?, ?, ?, ?, NULL)",
                        ("chunk-1", "chunk-hash", "content-hash", "text-hash", "now"),
                    )
            with mock.patch.object(MODULE, "RAG_ROOT", rag_root):
                identity = MODULE.read_db_identity("fixture-rag")
            self.assertNotEqual("unknown", identity["db_snapshot_hash"])
            renamed = catalog_path.with_suffix(".renamed")
            catalog_path.rename(renamed)
            self.assertTrue(renamed.exists())


class RouteIdentityTests(unittest.TestCase):
    def test_daemon_fingerprint_covers_runtime_package(self) -> None:
        names = {
            path.relative_to(MODULE.RAG_ROOT).as_posix()
            for path in MODULE.daemon_fingerprint_paths()
        }
        self.assertIn("gen_db/software_rag_tool/software_rag_tool/tokenize.py", names)
        self.assertIn("gen_db/software_rag_tool/software_rag_tool/store.py", names)
        self.assertIn("gen_db/software_rag_tool/software_rag_tool/db_runtime.py", names)
        self.assertIn("gen_db/software_rag_tool/requirements.txt", names)

    def test_legacy_daemon_route_is_unverified(self) -> None:
        self.assertEqual("UNVERIFIED", MODULE.daemon_first_attempt_state([success_row()]))

    def test_fallback_never_counts_as_first_attempt_success(self) -> None:
        row = success_row(
            first_attempt_success=False,
            final_user_visible_success=True,
            fallback_used=True,
            actual_execution="no-daemon",
        )
        self.assertFalse(MODULE.daemon_first_attempt_success(row))

    def test_fallback_success_preserves_daemon_attempt_timeout(self) -> None:
        row = success_row(
            first_attempt_success=False,
            final_user_visible_success=True,
            fallback_used=True,
            actual_execution="no-daemon",
            execution_metadata={
                "attempts": [
                    {
                        "route": "daemon",
                        "success": False,
                        "failure_kind": "timeout",
                    },
                    {"route": "no-daemon", "success": True},
                ]
            },
        )
        self.assertTrue(MODULE.daemon_first_attempt_timeout(row))


class SemanticMetricTests(unittest.TestCase):
    def test_gold_span_requires_a_source_path(self) -> None:
        span = {"span_text": "gold"}
        context = {"path": "doc.txt", "text": "gold"}
        self.assertFalse(MODULE.gold_span_matches_context(span, context))

    def test_rescue_and_harm_use_conditional_denominators(self) -> None:
        rows = [
            success_row(case_id="rescue", profile="L", semantic_gold_applicable=True, semantic_hit_at_5=False, context_recall=0.0),
            success_row(case_id="rescue", profile="H", semantic_gold_applicable=True, semantic_hit_at_5=True, context_recall=1.0),
            success_row(case_id="harm", profile="L", semantic_gold_applicable=True, semantic_hit_at_5=True, context_recall=1.0),
            success_row(case_id="harm", profile="H", semantic_gold_applicable=True, semantic_hit_at_5=False, context_recall=0.0),
            success_row(case_id="stable", profile="L", semantic_gold_applicable=True, semantic_hit_at_5=True, context_recall=1.0),
            success_row(case_id="stable", profile="H", semantic_gold_applicable=True, semantic_hit_at_5=True, context_recall=1.0),
        ]
        report = "\n".join(MODULE.semantic_summary(rows))
        self.assertIn("Vector Rescue Rate: 1.0 (1/1 L misses)", report)
        self.assertIn("Vector Harm Rate: 0.5 (1/2 L hits)", report)

    def test_related_context_is_not_scored_as_semantic_gold(self) -> None:
        case = {
            "id": "semantic-related-only",
            "db": "ac-rag",
            "class": "semantic",
            "question": "question",
            "gold_spans": [
                {
                    "path": "gold.txt",
                    "span_text": "authoritative gold text",
                }
            ],
        }
        payload = {
            "status": "partial",
            "evidence": [],
            "related_context": [
                {
                    "id": "R1",
                    "source": {"path": "gold.txt"},
                    "text": "authoritative gold text",
                }
            ],
            "execution_metadata": {},
        }
        completed = subprocess.CompletedProcess(
            args=["search"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        args = argparse.Namespace(
            stage_timing=False,
            timeout=15.0,
            run_metadata={},
        )
        row = MODULE.summarize_search(
            case,
            "H",
            "no-daemon",
            0,
            completed,
            payload,
            True,
            None,
            0.5,
            warmup=False,
            args=args,
            explain_enabled=False,
            sequence_index=0,
        )
        self.assertEqual(0, row["evidence_count"])
        self.assertEqual(1, row["related_context_count"])
        self.assertEqual([], row["retrieved_contexts"])
        self.assertFalse(row["semantic_hit_at_5"])
        self.assertEqual(0.0, row["context_recall"])

    def test_gold_group_accepts_any_alternative_span(self) -> None:
        case = {
            "id": "semantic-group-or",
            "db": "ac-rag",
            "class": "semantic",
            "question": "question",
            "gold_groups": [
                {
                    "id": "claim-1",
                    "required": True,
                    "alternatives": [
                        {"path": "first.txt", "span_text": "first wording"},
                        {"path": "second.txt", "span_text": "second wording"},
                    ],
                }
            ],
        }
        row = {
            "request_success": True,
            "profile": "H",
            "retrieved_contexts": [
                {"path": "second.txt", "text": "The second wording is supported."}
            ],
        }
        flags = MODULE.quality_flags(case, row)
        self.assertTrue(flags["semantic_hit_at_5"])
        self.assertEqual(1.0, flags["context_recall"])

    def test_required_gold_groups_use_and_recall(self) -> None:
        case = {
            "id": "semantic-group-and",
            "db": "ac-rag",
            "class": "semantic",
            "question": "question",
            "gold_groups": [
                {
                    "id": "claim-1",
                    "required": True,
                    "alternatives": [
                        {"path": "one.txt", "span_text": "supported one"}
                    ],
                },
                {
                    "id": "claim-2",
                    "required": True,
                    "alternatives": [
                        {"path": "two.txt", "span_text": "missing two"}
                    ],
                },
            ],
        }
        row = {
            "request_success": True,
            "profile": "H",
            "retrieved_contexts": [
                {"path": "one.txt", "text": "supported one"}
            ],
        }
        flags = MODULE.quality_flags(case, row)
        self.assertTrue(flags["semantic_hit_at_5"])
        self.assertEqual(0.5, flags["context_recall"])


if __name__ == "__main__":
    unittest.main()
