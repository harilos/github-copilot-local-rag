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
from types import SimpleNamespace
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
SEARCH_SPEC = importlib.util.spec_from_file_location("rag_search_cli", QUERY_ROOT / "search.py")
assert SEARCH_SPEC and SEARCH_SPEC.loader
SEARCH = importlib.util.module_from_spec(SEARCH_SPEC)
SEARCH_SPEC.loader.exec_module(SEARCH)

TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))
QUERY_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "rag_query_script",
    TOOL_ROOT / "scripts" / "query.py",
)
assert QUERY_SCRIPT_SPEC and QUERY_SCRIPT_SPEC.loader
QUERY_SCRIPT = importlib.util.module_from_spec(QUERY_SCRIPT_SPEC)
QUERY_SCRIPT_SPEC.loader.exec_module(QUERY_SCRIPT)
from software_rag_tool.search_api import (
    _add_identifier_diagnostics,
    _finalize_search_payload,
    _raw_identifier_occurs,
    compact_search_contract,
    payload_to_prompt,
)


class FakeStore:
    def exact_search(self, question: str, *, top_k: int, source: str = "any") -> list[dict]:
        del top_k, source
        if "A2W" in question:
            return []
        return []


class ResultDeliveryContractTests(unittest.TestCase):
    def test_file_delivery_publishes_payload_and_prints_ascii_pointer(
        self,
    ) -> None:
        args = argparse.Namespace(
            result_delivery="file",
            format="json",
            compact_json=True,
            explain=False,
        )
        payload = {"status": "ok", "query": "日本語"}
        pointer = {
            "status": "written",
            "schema_version": "rag-result-pointer-v1",
            "result_set_id": "00000000-0000-4000-8000-000000000001",
            "summary_file": "/tmp/一時/summary.json",
            "expires_at": "2030-01-01T01:00:00Z",
            "bytes": 100,
        }
        stream = io.StringIO()
        with mock.patch.object(
            SEARCH,
            "publish_result_bundle",
            return_value=pointer,
        ) as publish:
            with contextlib.redirect_stdout(stream):
                SEARCH._print_search_payload(payload, args=args)
        publish.assert_called_once_with(payload)
        rendered = stream.getvalue().strip()
        rendered.encode("ascii")
        self.assertEqual(pointer, json.loads(rendered))
        self.assertNotIn("日本語", rendered)

    def test_stdout_delivery_preserves_direct_json_contract(self) -> None:
        args = argparse.Namespace(
            result_delivery="stdout",
            format="json",
            compact_json=False,
            explain=False,
        )
        payload = {
            "status": "ok",
            "query": "question",
            "_result_detail_items": [{"matched_excerpt": "private detail"}],
        }
        stream = io.StringIO()
        with mock.patch.object(
            SEARCH,
            "publish_result_bundle",
        ) as publish:
            with contextlib.redirect_stdout(stream):
                SEARCH._print_search_payload(payload, args=args)
        publish.assert_not_called()
        rendered = json.loads(stream.getvalue())
        self.assertEqual("ok", rendered["status"])
        self.assertEqual("question", rendered["query"])
        self.assertNotIn("_result_detail_items", rendered)

    def test_busy_bypasses_result_bundle_and_prints_direct_contract(
        self,
    ) -> None:
        args = argparse.Namespace(
            result_delivery="file",
            format="json",
            compact_json=True,
            explain=False,
        )
        payload = {
            "schema": "local-rag.search.v1",
            "status": "busy",
            "error": "daemon_overloaded",
            "db": "example-rag",
        }
        stream = io.StringIO()
        with mock.patch.object(
            SEARCH,
            "publish_result_bundle",
        ) as publish:
            with contextlib.redirect_stdout(stream):
                SEARCH._print_search_payload(payload, args=args)
        publish.assert_not_called()
        self.assertEqual(payload, json.loads(stream.getvalue()))


class SourceLinkDiagnosticsContracts(unittest.TestCase):
    def test_enrichment_failure_warns_without_failing_search(self) -> None:
        payload = {
            "schema": "local-rag.search.v1",
            "status": "ok",
            "answerability": "full",
            "warnings": [],
            "evidence": [],
            "background_context": [],
            "related_context": [],
            "document_results": [],
        }
        store = SimpleNamespace(
            context=SimpleNamespace(root=Path("/synthetic/db"))
        )
        with mock.patch(
            "software_rag_tool.source_links.enrich_search_payload",
            side_effect=RuntimeError("synthetic"),
        ):
            finalized = _finalize_search_payload(
                payload,
                store=store,
                db_name="example-rag",
                explain=True,
            )
        self.assertEqual("ok", finalized["status"])
        self.assertIn(
            "source_link_enrichment_failed",
            finalized["warnings"],
        )
        self.assertEqual(
            "resolution_failed",
            finalized["source_link_status"],
        )
        self.assertEqual("RuntimeError", finalized["source_link_error"])


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
        self.assertFalse(_raw_identifier_occurs({"text": "A2L-extra", "metadata": {}}, "A2L"))
        self.assertFalse(_raw_identifier_occurs({"text": "A2L_extra", "metadata": {}}, "A2L"))

    def test_mixed_identifier_query_preserves_supported_exact_evidence(self) -> None:
        evidence = {
            "id": "R1",
            "text": "A2L is supported",
            "source": {"path": "supported.txt"},
        }
        payload = {
            "status": "ok",
            "evidence": [evidence],
            "contexts": [evidence],
            "background_context": [{"id": "R2", "text": "related"}],
            "results": [
                {
                    "id": "raw-1",
                    "text": "A2L is supported",
                    "metadata": {"path": "supported.txt"},
                },
                {"id": "raw-2", "text": "related"},
            ],
            "background_results": [{"id": "raw-2", "text": "related"}],
            "warnings": [],
        }
        _add_identifier_diagnostics(
            payload,
            BrokenStore(),
            "A2LとA2Wについて教えて",
            source="any",
            precomputed_exact_rows=[
                {
                    "id": "raw-1",
                    "text": "A2L is supported",
                    "metadata": {"path": "supported.txt"},
                }
            ],
        )
        self.assertEqual("partial", payload["status"])
        self.assertEqual("partial", payload["answerability"])
        self.assertEqual([evidence], payload["evidence"])
        self.assertEqual(["A2W"], payload["unmatched_identifiers"])
        self.assertEqual(["raw-1"], [row["id"] for row in payload["results"]])
        self.assertEqual(["raw-2"], [row["id"] for row in payload["related_results"]])
        prompt = payload_to_prompt(payload)
        evidence_section, related_section = prompt.split(
            "## Related search candidates (not exact evidence)",
            maxsplit=1,
        )
        self.assertIn("## Retrieved evidence", evidence_section)
        self.assertIn("A2L is supported", evidence_section)
        self.assertIn("related", related_section)
        self.assertIn("A2W", prompt)
        self.assertIn(
            "Direct evidence may support matched portions",
            prompt,
        )

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
        self.assertEqual(6, payload["exact_candidate_count"])

    def test_precomputed_exact_bundle_avoids_diagnostic_requery(self) -> None:
        payload = {"status": "ok", "evidence": [], "contexts": [], "warnings": []}
        _add_identifier_diagnostics(
            payload,
            BrokenStore(),
            "A2L",
            source="any",
            precomputed_exact_rows=[
                {"id": "verified", "text": "A2L evidence", "metadata": {"path": "a.txt"}},
                {"id": "false", "text": "A2W only", "metadata": {"path": "b.txt"}},
            ],
        )
        match = payload["identifiers"]["matches"][0]
        self.assertTrue(match["matched"])
        self.assertEqual(2, match["candidate_count"])
        self.assertEqual(1, match["verified_candidate_count"])
        self.assertFalse(match["raw_occurrence_verified"])
        self.assertEqual(2, payload["exact_candidate_count"])
        self.assertTrue(payload["identifiers"]["diagnostics_complete"])

    def test_selected_db_name_is_not_treated_as_unmatched_query_identifier(self) -> None:
        payload = {
            "status": "ok",
            "evidence": [{"text": "A2L evidence"}],
            "contexts": [{"text": "A2L evidence"}],
            "warnings": [],
        }
        _add_identifier_diagnostics(
            payload,
            SixCandidateStore(),
            "ac-ragを使って、A2Lについて教えて",
            source="any",
            excluded_identifiers={"ac-rag"},
        )
        self.assertEqual([], payload["unmatched_identifiers"])
        self.assertEqual(["A2L"], payload["identifiers"]["anchors"])
        self.assertEqual("ok", payload["status"])

    def test_compact_contract_omits_duplicate_legacy_result_arrays(self) -> None:
        evidence = [{"id": "R1", "text": "evidence"}]
        payload = compact_search_contract(
            {
                "schema": "local-rag.search.v1",
                "db": "incident-rag",
                "query": "question",
                "status": "ok",
                "evidence": evidence,
                "contexts": evidence,
                "background_context": [],
                "related_context": [],
                "results": [{"large": "legacy"}],
                "background_results": [{"large": "legacy"}],
                "warnings": [],
            }
        )
        self.assertEqual(evidence, payload["evidence"])
        self.assertNotIn("contexts", payload)
        self.assertNotIn("results", payload)
        self.assertNotIn("background_results", payload)

    def test_compact_contract_preserves_anchored_neighbor_support_metadata(self) -> None:
        anchored = {
            "id": "R2",
            "text": "probable cause",
            "signals": ["neighbor"],
            "support_kind": "anchored_neighbor",
            "anchor_chunk_uid": "anchor-uid",
            "anchor_term": "M-4",
            "neighbor_distance": 1,
            "independent_signals": ["dense"],
        }
        payload = compact_search_contract(
            {
                "status": "ok",
                "evidence": [anchored],
                "background_context": [],
                "related_context": [],
                "warnings": [],
            }
        )
        self.assertEqual("anchored_neighbor", payload["evidence"][0]["support_kind"])
        self.assertEqual("anchor-uid", payload["evidence"][0]["anchor_chunk_uid"])
        self.assertEqual(["dense"], payload["evidence"][0]["independent_signals"])

    def test_compact_contract_preserves_structure_context_fields(self) -> None:
        evidence = {
            "id": "R1",
            "text": "matched text",
            "matched_excerpt": "matched text",
            "heading": "Results",
            "context_before": "preceding explanation",
            "context_after": "following explanation",
            "context_reason": "same_section_neighbor",
            "source_ranges": [
                {
                    "kind": "matched",
                    "chunk_uid": "primary",
                    "chunk_index": 1,
                    "section": "Results",
                },
                {
                    "kind": "context_after",
                    "chunk_uid": "after",
                    "chunk_index": 2,
                    "section": "Results",
                    "relationship": "same_section_neighbor",
                },
            ],
        }
        payload = compact_search_contract(
            {
                "status": "ok",
                "evidence": [evidence],
                "document_results": [
                    {
                        "path": f"docs/{index}.md",
                        "preview": "short",
                    }
                    for index in range(6)
                ],
            }
        )
        selected = payload["evidence"][0]
        self.assertEqual("matched text", selected["matched_excerpt"])
        self.assertEqual(
            "preceding explanation",
            selected["context_before"],
        )
        self.assertEqual(
            "following explanation",
            selected["context_after"],
        )
        self.assertEqual(2, len(selected["source_ranges"]))
        self.assertEqual(6, len(payload["document_results"]))
        self.assertLessEqual(
            len(
                json.dumps(payload, ensure_ascii=False, indent=2).encode(
                    "utf-8"
                )
            ),
            10_240,
        )

    def test_compact_cli_output_stays_below_lightweight_tool_limit(self) -> None:
        evidence = [{"id": "R1", "text": "x" * 900}]
        payload = {
            "schema": "local-rag.search.v1",
            "db": "incident-rag",
            "query": "question",
            "status": "ok",
            "evidence": evidence,
            "contexts": evidence,
            "background_context": [
                {"id": f"R{index}", "text": "x" * 900}
                for index in range(2, 9)
            ],
            "related_context": [],
            "results": [{"text": "x" * 900} for _ in range(8)],
            "background_results": [{"text": "x" * 900} for _ in range(7)],
            "warnings": [],
        }
        args = argparse.Namespace(format="json", compact_json=True, explain=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            SEARCH._print_search_payload(payload, args=args)
        rendered = output.getvalue()
        parsed = json.loads(rendered)
        self.assertLess(len(rendered.encode("utf-8")), 10_000)
        self.assertEqual(2, len(parsed["background_context"]))
        self.assertEqual(0, len(parsed["related_context"]))
        self.assertNotIn("contexts", parsed)
        self.assertNotIn("results", parsed)

    def test_compact_contract_does_not_fit_projected_content_to_a_byte_cap(self) -> None:
        source_url = "https://example.invalid/current/" + ("a" * 8_000)
        source_permalink = "https://example.invalid/fixed/" + ("b" * 8_000)
        context = {
            "id": "R1",
            "source": {"path": "資料/" + "長" * 500, "title": "題" * 500},
            "location": {"section": "節" * 500},
            "text": "日本語の根拠" * 2_000,
            "signals": ["exact"],
            "debug": {"huge": "x" * 20_000},
            "source_provider": "github",
            "source_url": source_url,
            "source_permalink": source_permalink,
        }
        payload = {
            "schema": "local-rag.search.v1",
            "db": "ac-rag",
            "query": "質問" * 2_000,
            "status": "ok",
            "answerability": "full",
            "evidence": [context],
            "background_context": [context] * 5,
            "related_context": [context] * 5,
            "warnings": ["警告" * 1_000] * 20,
        }
        compact = compact_search_contract(payload, explain=False)
        rendered = json.dumps(compact, ensure_ascii=False, indent=2).encode("utf-8")
        self.assertGreater(len(rendered), 10_240)
        self.assertEqual("R1", compact["evidence"][0]["id"])
        self.assertIn("path", compact["evidence"][0]["source"])
        self.assertNotIn("debug", compact["evidence"][0])
        self.assertEqual(source_url, compact["evidence"][0]["source_url"])
        self.assertEqual(
            source_permalink,
            compact["evidence"][0]["source_permalink"],
        )
        self.assertIn("compact_output_truncated", compact["warnings"])

        explained = compact_search_contract(
            {
                "status": "ok",
                "evidence": [{"id": "R1", "text": "evidence", "debug": {"rank": 1}}],
            },
            explain=True,
        )
        self.assertEqual({"rank": 1}, explained["evidence"][0]["debug"])

    def test_compact_contract_never_truncates_a_source_url(self) -> None:
        source_url = "https://example.invalid/" + ("a" * 2_500)
        compact = compact_search_contract(
            {
                "status": "ok",
                "answerability": "full",
                "evidence": [
                    {
                        "id": "R1",
                        "text": "Evidence.",
                        "source": {"path": "Root/document.txt"},
                        "source_url": source_url,
                    }
                ],
            },
            explain=False,
        )
        self.assertEqual(source_url, compact["evidence"][0]["source_url"])

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
    def test_adaptive_child_keeps_runtime_messages_off_json_stdout(self) -> None:
        def noisy_payload(**_kwargs: object) -> dict:
            print("runtime initialization message")
            return {
                "schema": "local-rag.search.v1",
                "db": "incident-rag",
                "status": "ok",
                "evidence": [],
                "background_context": [],
                "related_context": [],
                "warnings": [],
            }

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "query.py",
            "--db",
            "incident-rag",
            "--adaptive-hybrid",
            "--format",
            "json",
            "question",
        ]
        with (
            mock.patch.object(QUERY_SCRIPT, "run_adaptive_search_payload", side_effect=noisy_payload),
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            QUERY_SCRIPT.main()
        json.loads(stdout.getvalue())
        self.assertNotIn("runtime initialization message", stdout.getvalue())
        self.assertIn("runtime initialization message", stderr.getvalue())

    def test_default_hybrid_child_runs_one_adaptive_operation_under_timeout(self) -> None:
        args = argparse.Namespace(
            stdin=False,
            top_k=8,
            max_chars=900,
            format="json",
            compact_json=True,
            budget_tokens=0,
            retrieval_mode="hybrid",
            explain=False,
            include_db_hint=False,
            disable_identifier_diagnostics=False,
            timeout=15,
            search_request={
                "original_question": "original complete question",
                "literal_identifiers": ["A2W"],
                "facets": ["A2W", "A2Wの意味と用途"],
            },
        )
        completed = subprocess.CompletedProcess(
            args=["query"],
            returncode=0,
            stdout=json.dumps(
                {
                    "schema": "local-rag.search.v1",
                    "status": "ok",
                    "db": "ac-rag",
                    "evidence": [],
                    "retrieval_route": "adaptive_hybrid_dense",
                    "dense_used": True,
                }
            ),
            stderr="",
        )
        output = io.StringIO()
        with (
            mock.patch.object(SEARCH, "_run_sync_child", return_value=completed) as child,
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit) as raised:
                SEARCH._run_sync_script(
                    python=r"C:\path\to\.copilot\rag\query\.venv\Scripts\python.exe",
                    env={},
                    args=args,
                    db_name="ac-rag",
                    question="original complete question",
                    timeout_override=14.5,
                    execution_metadata={
                        "requested_execution": "daemon",
                        "route_selection": "cold_local_adaptive",
                    },
                )
        self.assertEqual(0, raised.exception.code)
        command = child.call_args.args[0]
        self.assertEqual(
            r"C:\path\to\.copilot\rag\query\.venv\Scripts\python.exe",
            command[0],
        )
        self.assertIn("--adaptive-hybrid", command)
        self.assertIn("--literal-identifier", command)
        self.assertIn("A2Wの意味と用途", command)
        self.assertEqual(1, command.count("original complete question"))
        self.assertIsNone(child.call_args.kwargs["input_text"])
        self.assertEqual(14.5, child.call_args.kwargs["timeout"])
        payload = json.loads(output.getvalue())
        self.assertEqual("no-daemon", payload["execution_metadata"]["actual_execution"])

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

    def test_windows_bounded_fallback_degrades_hybrid_to_lexical(self) -> None:
        with mock.patch.object(SEARCH.sys, "platform", "win32"):
            self.assertEqual(
                "lexical",
                SEARCH._fallback_retrieval_mode(
                    "hybrid",
                    remaining_seconds=7.5,
                ),
            )
            self.assertEqual(
                "hybrid",
                SEARCH._fallback_retrieval_mode(
                    "hybrid",
                    remaining_seconds=10.0,
                ),
            )
            self.assertEqual(
                "dense",
                SEARCH._fallback_retrieval_mode(
                    "dense",
                    remaining_seconds=7.5,
                ),
            )

    def test_deadline_bounded_fallback_reports_lexical_degradation(self) -> None:
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
            stdout=json.dumps(
                {
                    "schema": "local-rag.search.v1",
                    "status": "no_hit",
                    "warnings": [],
                }
            ),
            stderr="",
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                SEARCH,
                "_run_sync_child",
                return_value=completed,
            ) as child,
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit) as raised:
                SEARCH._run_sync_script(
                    python="python",
                    env={},
                    args=args,
                    question="q",
                    db_name="incident-rag",
                    timeout_override=7.5,
                    retrieval_mode_override="lexical",
                    execution_metadata={
                        "requested_execution": "daemon",
                        "first_attempt_success": False,
                        "fallback_used": True,
                        "fallback_retrieval_mode": "lexical",
                        "fallback_dense_skipped": True,
                        "fallback_dense_skipped_reason": (
                            "deadline_bounded_windows_fallback"
                        ),
                        "attempts": [
                            {"route": "daemon", "success": False}
                        ],
                    },
                )
        self.assertEqual(0, raised.exception.code)
        command = child.call_args.args[0]
        self.assertIn("--retrieval-mode", command)
        self.assertIn("lexical", command)
        self.assertNotIn("--adaptive-hybrid", command)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["dense_used"])
        self.assertEqual(
            "deadline_bounded_windows_fallback",
            payload["dense_skipped_reason"],
        )
        self.assertIn(SEARCH.DEADLINE_FALLBACK_WARNING, payload["warnings"])
        self.assertEqual(
            "lexical",
            payload["execution_metadata"]["attempts"][-1][
                "retrieval_mode"
            ],
        )

    def test_prompt_fallback_reports_lexical_degradation(self) -> None:
        args = argparse.Namespace(
            stdin=False,
            top_k=8,
            max_chars=1200,
            format="prompt",
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
            stdout=json.dumps(
                {
                    "schema": "local-rag.search.v1",
                    "status": "partial",
                    "db": "incident-rag",
                    "query": "q",
                    "evidence": [
                        {
                            "id": "R1",
                            "source": {"path": "incident.txt"},
                            "text": "supported evidence",
                        }
                    ],
                    "warnings": [],
                }
            ),
            stderr="",
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                SEARCH,
                "_run_sync_child",
                return_value=completed,
            ) as child,
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit) as raised:
                SEARCH._run_sync_script(
                    python="python",
                    env={},
                    args=args,
                    question="q",
                    db_name="incident-rag",
                    timeout_override=7.5,
                    retrieval_mode_override="lexical",
                    execution_metadata={
                        "requested_execution": "daemon",
                        "first_attempt_success": False,
                        "fallback_used": True,
                        "fallback_retrieval_mode": "lexical",
                        "fallback_dense_skipped": True,
                        "fallback_dense_skipped_reason": (
                            "deadline_bounded_windows_fallback"
                        ),
                        "attempts": [
                            {"route": "daemon", "success": False}
                        ],
                    },
                )
        self.assertEqual(0, raised.exception.code)
        command = child.call_args.args[0]
        format_index = command.index("--format")
        self.assertEqual("json", command[format_index + 1])
        prompt = output.getvalue()
        self.assertIn("## Warnings", prompt)
        self.assertIn(SEARCH.DEADLINE_FALLBACK_WARNING, prompt)
        self.assertIn("supported evidence", prompt)

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

    def test_compact_json_reserves_only_bounded_serialization_time(self) -> None:
        self.assertEqual(
            0.5,
            SEARCH._output_reserve_seconds(
                output_format="json",
                compact_json=True,
            ),
        )
        self.assertEqual(
            2.0,
            SEARCH._output_reserve_seconds(
                output_format="json",
                compact_json=False,
            ),
        )

    def test_compact_json_defaults_to_1200_token_retrieval_budget(self) -> None:
        compact_args = argparse.Namespace(
            format="json",
            compact_json=True,
            budget_tokens=0,
        )
        legacy_args = argparse.Namespace(
            format="json",
            compact_json=False,
            budget_tokens=0,
        )
        explicit_args = argparse.Namespace(
            format="json",
            compact_json=True,
            budget_tokens=700,
        )
        self.assertEqual(1200, SEARCH._effective_budget_tokens(compact_args))
        self.assertIsNone(SEARCH._effective_budget_tokens(legacy_args))
        self.assertEqual(700, SEARCH._effective_budget_tokens(explicit_args))
        self.assertEqual(
            2.0,
            SEARCH._output_reserve_seconds(
                output_format="prompt",
                compact_json=True,
            ),
        )

    def test_every_cold_daemon_receives_the_remaining_deadline(self) -> None:
        with mock.patch.object(SEARCH.time, "monotonic", return_value=100.0):
            self.assertEqual(
                12.0,
                SEARCH._daemon_query_timeout(
                    attempt_timeout=5.0,
                    deadline=112.0,
                    cold_start=True,
                    require_daemon=True,
                ),
            )
            self.assertEqual(
                12.0,
                SEARCH._daemon_query_timeout(
                    attempt_timeout=5.0,
                    deadline=112.0,
                    cold_start=False,
                    require_daemon=True,
                ),
            )
            self.assertEqual(
                12.0,
                SEARCH._daemon_query_timeout(
                    attempt_timeout=5.0,
                    deadline=112.0,
                    cold_start=True,
                    require_daemon=False,
                ),
            )

    def test_dense_readiness_is_tracked_separately_from_process_readiness(self) -> None:
        self.assertTrue(SEARCH._request_may_use_dense({"retrieval_mode": "hybrid"}))
        self.assertTrue(SEARCH._request_may_use_dense({"retrieval_mode": "dense"}))
        self.assertFalse(SEARCH._request_may_use_dense({"retrieval_mode": "lexical"}))

    def test_process_is_alive_reaps_an_exited_child(self) -> None:
        with (
            mock.patch.object(SEARCH.os, "waitpid", return_value=(43210, 0)),
            mock.patch.object(SEARCH.os, "kill") as kill,
        ):
            self.assertFalse(SEARCH._process_is_alive(43210))
        kill.assert_not_called()

    def test_windows_liveness_probe_never_calls_os_kill(self) -> None:
        with (
            mock.patch.object(SEARCH.sys, "platform", "win32"),
            mock.patch.object(SEARCH, "_windows_process_is_alive", return_value=True) as probe,
            mock.patch.object(SEARCH.os, "kill") as kill,
        ):
            self.assertTrue(SEARCH._process_is_alive(43210))
        probe.assert_called_once_with(43210)
        kill.assert_not_called()

    def test_sync_child_uses_utf8_for_japanese_stdin_stdout_and_stderr(self) -> None:
        child = (
            "import json,sys;"
            "question=sys.stdin.buffer.read().decode('utf-8');"
            "sys.stdout.buffer.write(json.dumps("
            "{'question':question,'message':'根拠'},ensure_ascii=False).encode('utf-8'));"
            "sys.stderr.buffer.write('診断'.encode('utf-8'))"
        )
        with mock.patch("locale.getpreferredencoding", return_value="cp932"):
            completed = SEARCH._run_sync_child(
                [sys.executable, "-c", child],
                env={},
                input_text="日本語の質問",
                timeout=2.0,
            )
        self.assertEqual(
            {"question": "日本語の質問", "message": "根拠"},
            json.loads(completed.stdout),
        )
        self.assertEqual("診断", completed.stderr)

    def test_cli_standard_streams_are_forced_to_utf8(self) -> None:
        stdin = mock.Mock()
        stdout = mock.Mock()
        stderr = mock.Mock()
        with (
            mock.patch.object(SEARCH.sys, "stdin", stdin),
            mock.patch.object(SEARCH.sys, "stdout", stdout),
            mock.patch.object(SEARCH.sys, "stderr", stderr),
        ):
            SEARCH._configure_standard_streams()
        for stream in (stdin, stdout, stderr):
            stream.reconfigure.assert_called_once_with(
                encoding="utf-8",
                errors="replace",
            )

    def test_windows_timeout_cleanup_is_bounded_by_output_reserve(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(SEARCH.sys, "platform", "win32"),
            mock.patch.object(
                SEARCH.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["taskkill"],
                    returncode=0,
                ),
            ) as run,
        ):
            SEARCH._terminate_process_tree(
                process,
                timeout=SEARCH.WINDOWS_TASKKILL_TIMEOUT_SECONDS,
            )
        self.assertLessEqual(
            run.call_args.kwargs["timeout"],
            SEARCH.WINDOWS_TASKKILL_TIMEOUT_SECONDS,
        )
        self.assertLess(
            SEARCH.WINDOWS_TASKKILL_TIMEOUT_SECONDS,
            SEARCH.COMPACT_JSON_OUTPUT_RESERVE_SECONDS,
        )
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("text", run.call_args.kwargs)

    def test_windows_failed_taskkill_falls_back_to_process_kill(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(SEARCH.sys, "platform", "win32"),
            mock.patch.object(
                SEARCH.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["taskkill"],
                    returncode=1,
                ),
            ),
        ):
            SEARCH._terminate_process_tree(process)
        process.kill.assert_called_once_with()

    def test_windows_failed_taskkill_does_not_kill_exited_process(self) -> None:
        process = mock.Mock()
        process.poll.side_effect = [None, 0]
        with (
            mock.patch.object(SEARCH.sys, "platform", "win32"),
            mock.patch.object(
                SEARCH.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["taskkill"],
                    returncode=1,
                ),
            ),
        ):
            SEARCH._terminate_process_tree(process)
        process.kill.assert_not_called()

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
    def test_starting_and_busy_requests_stay_on_daemon_route(self) -> None:
        for lifecycle in ("STARTING", "BUSY", "READY"):
            self.assertEqual(
                "daemon_ready",
                SEARCH._select_daemon_route(
                    lifecycle,
                    require_daemon=False,
                ),
            )
        for lifecycle in ("MISSING", "DEAD"):
            self.assertEqual(
                "daemon_start",
                SEARCH._select_daemon_route(
                    lifecycle,
                    require_daemon=False,
                ),
            )
            self.assertEqual(
                "daemon_required",
                SEARCH._select_daemon_route(
                    lifecycle,
                    require_daemon=True,
                ),
            )
        self.assertEqual(
            "daemon_draining",
            SEARCH._select_daemon_route(
                "DRAINING",
                require_daemon=True,
            ),
        )

    def test_retirement_failure_is_recorded_for_fallback_gate(self) -> None:
        attempt = SEARCH._record_retirement_outcome(
            {"route": "daemon", "success": False},
            {"process_exited": False},
        )
        self.assertTrue(attempt["retirement_failed"])

    def test_daemon_state_machine_requires_ready_and_distinguishes_busy(self) -> None:
        state = {
            "schema": "local-rag.ragd.v2",
            "pid": 43210,
            "generation": "generation",
            "transport": "tcp",
            "host": "127.0.0.1",
            "port": 12345,
            "token": "token",
            "code_fingerprint": "current",
        }
        with (
            mock.patch.object(SEARCH, "_read_state", return_value=state),
            mock.patch.object(SEARCH, "_runtime_code_fingerprint", return_value="current"),
            mock.patch.object(
                SEARCH,
                "_daemon_health_payload",
                return_value={
                    "status": "ok",
                    "pid": 43210,
                    "generation": "generation",
                    "code_fingerprint": "current",
                    "ready": False,
                    "lifecycle_state": "BUSY",
                    "active_requests": 1,
                },
            ),
        ):
            lifecycle, observed, _health = SEARCH._inspect_daemon_state(timeout=0.5)
            active = SEARCH._active_daemon_state(timeout=0.5)
        self.assertEqual("BUSY", lifecycle)
        self.assertEqual(state, observed)
        self.assertEqual(state, active)

    def test_daemon_state_machine_preserves_draining(self) -> None:
        state = {
            "schema": "local-rag.ragd.v2",
            "pid": 43210,
            "generation": "generation",
            "transport": "tcp",
            "host": "127.0.0.1",
            "port": 12345,
            "token": "token",
            "code_fingerprint": "current",
        }
        with (
            mock.patch.object(SEARCH, "_read_state", return_value=state),
            mock.patch.object(
                SEARCH,
                "_runtime_code_fingerprint",
                return_value="current",
            ),
            mock.patch.object(
                SEARCH,
                "_daemon_health_payload",
                return_value={
                    "status": "ok",
                    "pid": 43210,
                    "generation": "generation",
                    "code_fingerprint": "current",
                    "lifecycle_state": "DRAINING",
                },
            ),
        ):
            lifecycle, observed, _health = SEARCH._inspect_daemon_state(
                timeout=0.5
            )
        self.assertEqual("DRAINING", lifecycle)
        self.assertEqual(state, observed)

    def test_wait_for_draining_generation_requires_state_retirement(self) -> None:
        state = {
            "pid": 43210,
            "generation": "generation",
        }
        with (
            mock.patch.object(
                SEARCH,
                "_read_state",
                side_effect=[state, state, None],
            ),
            mock.patch.object(SEARCH.time, "sleep"),
        ):
            retired = SEARCH._wait_for_daemon_retirement(
                state,
                timeout=1.0,
            )
        self.assertTrue(retired)

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

    def test_windows_start_accepts_authenticated_venv_launcher_child_pid(self) -> None:
        process = mock.Mock()
        process.pid = 43210
        process.poll.return_value = None
        child_state = {
            "schema": "local-rag.ragd.v2",
            "pid": 54321,
            "generation": "expected-generation",
            "transport": "tcp",
            "host": "127.0.0.1",
            "port": 12345,
            "token": "expected-generation",
        }
        with (
            mock.patch.object(SEARCH.sys, "platform", "win32"),
            mock.patch.object(SEARCH, "_acquire_start_lock", return_value=99),
            mock.patch.object(SEARCH, "_release_start_lock"),
            mock.patch.object(SEARCH, "_free_port", return_value=12345),
            mock.patch.object(
                SEARCH.secrets,
                "token_hex",
                return_value="expected-generation",
            ),
            mock.patch.object(
                SEARCH.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
            mock.patch.object(SEARCH, "_read_state", return_value=child_state),
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
        self.assertEqual(child_state, state)
        terminate.assert_not_called()
        creationflags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(
            creationflags
            & getattr(
                SEARCH.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
            )
        )

    def test_non_windows_start_rejects_different_launcher_and_daemon_pids(self) -> None:
        state = {
            "pid": 54321,
            "generation": "generation",
            "token": "generation",
            "transport": "tcp",
        }
        with mock.patch.object(SEARCH.sys, "platform", "darwin"):
            self.assertFalse(
                SEARCH._spawned_daemon_state_matches(
                    state,
                    generation="generation",
                    launcher_pid=43210,
                    transport="tcp",
                )
            )


if __name__ == "__main__":
    unittest.main()
