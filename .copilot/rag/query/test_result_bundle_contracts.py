from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
REPO_ROOT = QUERY_ROOT.parents[2]
sys.path.insert(0, str(QUERY_ROOT))

import result_bundle
import result_detail
import source_hygiene


def synthetic_payload() -> dict:
    evidence = {
        "id": "R1",
        "text": "The cached system keeps one immutable result for follow-up use.",
        "matched_excerpt": (
            "The cached system keeps one immutable result for follow-up use."
        ),
        "context_before": "The initial lookup is complete.",
        "context_after": "No retrieval is repeated for a detail request.",
        "context_reason": "same_section_neighbor",
        "source_ranges": [
            {
                "kind": "matched",
                "chunk_uid": "chunk-1",
                "section": "Result handling",
            }
        ],
        "source": {
            "path": "<root-name>/<relative-subdirectory>/<document.pdf>",
            "title": "<document.pdf>",
            "revision": "sha256:synthetic",
        },
        "location": {"section": "Result handling"},
        "signals": ["lexical"],
    }
    return {
        "schema": "local-rag.search.v1",
        "status": "ok",
        "answerability": "full",
        "selected_db": "<project-rag>",
        "query": "Explain the cached result.",
        "evidence": [evidence],
        "background_context": [],
        "related_context": [
            {
                "text": "A related lead must not become a factual bullet.",
            }
        ],
        "document_results": [
            {
                "path": "<root-name>/<relative-subdirectory>/<document.pdf>",
                "title": "<document.pdf>",
                "section": "Result handling",
                "preview": "A short direct document preview.",
                "support_level": "direct",
                "authoritative": True,
                "relationship": "Contains the direct cached evidence.",
            },
            {
                "path": "<root-name>/<relative-subdirectory>/<related.txt>",
                "title": "<related.txt>",
                "section": "Background",
                "preview": "A related lead must not become a factual bullet.",
                "support_level": "strong",
                "authoritative": False,
                "relationship": "Related material, not direct proof.",
            },
        ],
        "_result_detail_items": [
            {
                "path": "<root-name>/<relative-subdirectory>/<related.txt>",
                "document_id": "document-2",
                "chunk_uid": "chunk-2",
                "heading_path": ["Background"],
                "matched_excerpt": (
                    "A cached related section is available without a search."
                ),
                "context_before": "Related context before.",
                "context_after": "Related context after.",
                "additional_sections": [],
                "source_ranges": [],
                "warnings": [],
            }
        ],
        "warnings": ["Keep this limitation in the answer."],
        "coverage": {"returned_distinct_documents": 2},
    }


class ResultBundleContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-result-test-"
        )
        self.spool = Path(self.temporary.name) / "managed-results"
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publish(self, payload: dict | None = None) -> dict:
        return result_bundle.publish_result_bundle(
            payload or synthetic_payload(),
            spool_root=self.spool,
            now=self.now,
        )

    def read_summary(self, pointer: dict) -> dict:
        return json.loads(
            Path(pointer["summary_file"]).read_text(encoding="utf-8")
        )

    def test_initial_summary_is_independently_answerable(self) -> None:
        pointer = self.publish()
        summary = self.read_summary(pointer)
        draft = summary["initial_response"]["answer_draft_markdown"]
        self.assertIn("immutable result", draft)
        self.assertIn("[E1]", draft)
        self.assertTrue(
            summary["initial_response"]["response_rules"][
                "use_only_this_summary"
            ]
        )
        self.assertLessEqual(
            Path(pointer["summary_file"]).stat().st_size,
            result_bundle.SUMMARY_HARD_BYTES,
        )

    def test_trivial_renderer_reads_only_summary(self) -> None:
        pointer = self.publish()
        summary_path = Path(pointer["summary_file"])
        rendered = json.loads(
            summary_path.read_text(encoding="utf-8")
        )["initial_response"]["answer_draft_markdown"]
        self.assertIn("## Answer", rendered)
        self.assertNotIn("manifest.json", rendered)

    def test_factual_units_use_only_direct_evidence(self) -> None:
        summary = self.read_summary(self.publish())
        points = summary["initial_response"]["key_points"]
        self.assertEqual(["direct"], [point["support"] for point in points])
        self.assertEqual([["E1"]], [point["source_ids"] for point in points])
        self.assertNotIn(
            "related lead must not become",
            " ".join(point["text"] for point in points).casefold(),
        )

    def test_explicitly_non_authoritative_evidence_is_not_a_factual_unit(
        self,
    ) -> None:
        payload = synthetic_payload()
        payload["evidence"][0]["authoritative"] = False
        summary = self.read_summary(self.publish(payload))
        self.assertEqual([], summary["initial_response"]["key_points"])
        self.assertEqual([], summary["evidence"])

    def test_warnings_and_limitations_are_preserved(self) -> None:
        summary = self.read_summary(self.publish())
        limitations = summary["initial_response"]["limitations"]
        self.assertIn("Keep this limitation in the answer.", limitations)
        self.assertIn(
            "Keep this limitation in the answer.",
            summary["initial_response"]["answer_draft_markdown"],
        )

    def test_missing_table_headers_do_not_create_a_factual_unit(self) -> None:
        payload = synthetic_payload()
        payload["evidence"][0]["text"] = "Region | 10 | 20"
        payload["evidence"][0]["matched_excerpt"] = "Region | 10 | 20"
        payload["evidence"][0]["context_warnings"] = [
            "table_headers_incomplete"
        ]
        summary = self.read_summary(self.publish(payload))
        self.assertEqual([], summary["initial_response"]["key_points"])

    def test_large_summary_is_fitted_below_the_hard_limit(self) -> None:
        payload = synthetic_payload()
        payload["document_results"] = [
            {
                "path": f"<root-name>/<relative-subdirectory>/file-{index}.txt",
                "title": f"file-{index}.txt",
                "section": "Section",
                "preview": "x" * 2_000,
                "support_level": "weak",
                "authoritative": False,
                "relationship": "y" * 1_000,
            }
            for index in range(10)
        ]
        pointer = self.publish(payload)
        summary = self.read_summary(pointer)
        self.assertLessEqual(
            Path(pointer["summary_file"]).stat().st_size,
            result_bundle.SUMMARY_HARD_BYTES,
        )
        self.assertGreaterEqual(len(summary["document_results"]), 8)

    def test_detail_bundle_preserves_structural_context(self) -> None:
        pointer = self.publish()
        packet, _expires = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["E1"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        item = packet["expanded_items"][0]
        self.assertEqual("The initial lookup is complete.", item["context_before"])
        self.assertEqual(
            "No retrieval is repeated for a detail request.",
            item["context_after"],
        )

    def test_resolved_links_are_frozen_into_summary_and_detail(self) -> None:
        payload = synthetic_payload()
        fixed_url = "https://example.invalid/blob/revision/document.pdf"
        payload["evidence"][0].update(
            {
                "source_provider": "github",
                "source_url": "https://example.invalid/blob/current/document.pdf",
                "source_permalink": fixed_url,
            }
        )
        payload["document_results"][0].update(
            {
                "source_provider": "github",
                "source_permalink": fixed_url,
            }
        )
        pointer = self.publish(payload)
        summary = self.read_summary(pointer)
        self.assertEqual(fixed_url, summary["evidence"][0]["source_permalink"])
        self.assertEqual(
            fixed_url,
            summary["document_results"][0]["source_permalink"],
        )

        # A later configuration or in-memory payload change cannot alter the
        # already-published result set.
        payload["evidence"][0]["source_permalink"] = (
            "https://example.invalid/blob/later/document.pdf"
        )
        packet, _expires = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["E1"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(
            fixed_url,
            packet["expanded_items"][0]["source_permalink"],
        )

    def test_summary_never_truncates_a_valid_source_url(self) -> None:
        payload = synthetic_payload()
        source_url = "https://example.invalid/" + ("a" * 2_500)
        payload["evidence"][0]["source_url"] = source_url
        summary = self.read_summary(self.publish(payload))
        self.assertEqual(source_url, summary["evidence"][0]["source_url"])

    def test_detail_retrieval_reads_only_requested_cached_items(self) -> None:
        pointer = self.publish()
        packet, _expires = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["D2"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(
            ["D2"],
            [item["item_id"] for item in packet["expanded_items"]],
        )

    def test_database_changes_do_not_affect_cached_details(self) -> None:
        pointer = self.publish()
        unrelated_database_marker = Path(self.temporary.name) / "db-marker"
        unrelated_database_marker.write_text("before", encoding="utf-8")
        unrelated_database_marker.unlink()
        packet, _expires = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["E1"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual("ok", packet["status"])
        self.assertIn(
            "immutable result",
            packet["expanded_items"][0]["matched_excerpt"],
        )

    def test_expired_result_does_not_trigger_retrieval(self) -> None:
        pointer = self.publish()
        packet, expires = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["E1"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(hours=2),
        )
        self.assertEqual("result_expired", packet["status"])
        self.assertIsNone(expires)

    def test_concurrent_sessions_use_distinct_uuid_directories(self) -> None:
        def create(_index: int) -> dict:
            return result_bundle.publish_result_bundle(
                synthetic_payload(),
                spool_root=self.spool,
                now=self.now,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pointers = list(pool.map(create, range(8)))
        ids = {pointer["result_set_id"] for pointer in pointers}
        self.assertEqual(8, len(ids))
        self.assertTrue(
            all(Path(pointer["summary_file"]).exists() for pointer in pointers)
        )

    def test_every_published_file_is_utf8_json_and_items_use_uuids(self) -> None:
        pointer = self.publish()
        result_dir = Path(pointer["summary_file"]).parent
        manifest = json.loads(
            (result_dir / "manifest.json").read_text(encoding="utf-8")
        )
        for entry in manifest["items"].values():
            storage_name = Path(entry["file"]).stem
            uuid.UUID(storage_name)
        for path in result_dir.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_pointer_is_ascii_safe_and_contains_no_source_content(self) -> None:
        non_ascii_spool = Path(self.temporary.name) / "一時" / "results"
        pointer = result_bundle.publish_result_bundle(
            synthetic_payload(),
            spool_root=non_ascii_spool,
            now=self.now,
        )
        rendered = json.dumps(
            pointer,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        rendered.encode("ascii")
        self.assertNotIn("immutable result", rendered)
        self.assertNotIn("<document.pdf>", rendered)

    def test_result_set_remains_inside_total_size_limit(self) -> None:
        pointer = self.publish()
        result_dir = Path(pointer["summary_file"]).parent
        packet, expires_at = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["E1", "D2"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        assert expires_at is not None
        for _index in range(40):
            result_bundle.publish_expanded_packet(
                packet,
                result_set_id=pointer["result_set_id"],
                expires_at=expires_at,
                spool_root=self.spool,
            )
        total = sum(
            path.stat().st_size
            for path in result_dir.rglob("*")
            if path.is_file()
        )
        self.assertLessEqual(total, result_bundle.MAX_RESULT_SET_BYTES)

    def test_atomic_publish_uses_replace_and_leaves_no_tmp_file(self) -> None:
        with mock.patch.object(
            result_bundle.os,
            "replace",
            wraps=os.replace,
        ) as replace:
            pointer = self.publish()
        self.assertGreater(replace.call_count, 3)
        result_dir = Path(pointer["summary_file"]).parent
        self.assertEqual([], list(result_dir.rglob("*.tmp")))

    def test_stale_tmp_is_cleaned_without_deleting_ready_result(self) -> None:
        pointer = self.publish()
        stale = self.spool / "stale.json.tmp"
        stale.write_text("partial", encoding="utf-8")
        old = time.time() - result_bundle.STALE_TMP_SECONDS - 5
        os.utime(stale, (old, old))
        result_bundle.cleanup_result_spool(
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        self.assertFalse(stale.exists())
        self.assertTrue(Path(pointer["summary_file"]).exists())

    def test_bundle_is_never_created_under_repository_or_copilot(self) -> None:
        default = result_bundle.result_spool_root()
        self.assertFalse(default.is_relative_to(REPO_ROOT))
        self.assertNotIn(".copilot", default.parts)

    def test_follow_up_implementation_has_no_search_path(self) -> None:
        source = (QUERY_ROOT / "result_detail.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("search.py", source)
        self.assertNotIn("list_dbs.py", source)
        self.assertNotIn("software_rag_tool", source)

    def test_detail_cli_reads_cached_result_once(self) -> None:
        packet = {
            "schema_version": "rag-expanded-answer-v1",
            "status": "ok",
            "result_set_id": "00000000-0000-4000-8000-000000000001",
            "expanded_items": [],
            "answer_draft_markdown": "Cached detail.",
            "warnings": [],
        }
        expires = self.now + timedelta(minutes=30)
        pointer = {
            "status": "written",
            "schema_version": "rag-detail-pointer-v1",
            "result_set_id": packet["result_set_id"],
            "detail_file": "/tmp/detail.json",
            "expires_at": "2030-01-01T00:30:00Z",
            "bytes": 100,
        }
        argv = [
            "result_detail.py",
            "--result-set-id",
            packet["result_set_id"],
            "--item-id",
            "E1",
            "--result-delivery",
            "file",
        ]
        stream = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(
                result_detail,
                "cleanup_result_spool",
            ):
                with mock.patch.object(
                    result_detail,
                    "load_expanded_result",
                    return_value=(packet, expires),
                ) as load:
                    with mock.patch.object(
                        result_detail,
                        "publish_expanded_packet",
                        return_value=pointer,
                    ) as publish:
                        with contextlib.redirect_stdout(stream):
                            code = result_detail.main()
        self.assertEqual(0, code)
        load.assert_called_once_with(
            packet["result_set_id"],
            ["E1"],
            detail_level="expanded",
        )
        publish.assert_called_once()
        self.assertEqual(pointer, json.loads(stream.getvalue()))

    def test_existing_direct_payload_is_not_replaced_by_file_delivery(self) -> None:
        search_source = (QUERY_ROOT / "search.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('choices=["stdout", "file"]', search_source)
        self.assertIn('default="stdout"', search_source)

    def test_installer_excludes_local_hygiene_denylist(self) -> None:
        self.assertIn(
            "sensitive-terms.local",
            (REPO_ROOT / "install.sh").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "sensitive-terms.local",
            (REPO_ROOT / "install.ps1").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "sensitive-terms.local",
            (
                REPO_ROOT / ".copilot" / "rag" / "export_migration.sh"
            ).read_text(encoding="utf-8"),
        )


class SourceHygieneTests(unittest.TestCase):
    def test_tracked_sources_have_no_absolute_user_profile_paths(self) -> None:
        findings = source_hygiene.scan_tracked_hygiene(REPO_ROOT)
        absolute = [
            finding
            for finding in findings
            if finding["kind"] == "absolute_user_path"
        ]
        self.assertEqual([], absolute)

    def test_windows_forward_slash_user_profile_is_detected(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-hygiene-path-test-"
        ) as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            tracked = root / "tracked.txt"
            tracked.write_text(
                "C:" + "/Users/" + "synthetic/profile.txt\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "tracked.txt"],
                cwd=root,
                check=True,
            )
            findings = source_hygiene.scan_tracked_hygiene(root)
        self.assertEqual("absolute_user_path", findings[0]["kind"])
        self.assertEqual("tracked.txt", findings[0]["path"])

    def test_optional_denylist_reports_location_without_literal(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-hygiene-test-"
        ) as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "-q"],
                cwd=root,
                check=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_text("synthetic forbidden value\n", encoding="utf-8")
            denylist = root / "denylist.local"
            denylist.write_text(
                "synthetic forbidden value\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "tracked.txt"],
                cwd=root,
                check=True,
            )
            findings = source_hygiene.scan_tracked_hygiene(
                root,
                sensitive_terms_file=denylist,
            )
        self.assertEqual("sensitive_term", findings[0]["kind"])
        self.assertEqual("tracked.txt", findings[0]["path"])
        self.assertEqual(1, findings[0]["line"])
        self.assertNotIn("literal", findings[0])


if __name__ == "__main__":
    unittest.main()
