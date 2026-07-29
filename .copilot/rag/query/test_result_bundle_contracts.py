from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
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
        self.assertFalse(
            summary["initial_response"]["response_rules"][
                "use_only_this_summary"
            ]
        )
        self.assertTrue(
            summary["initial_response"]["response_rules"][
                "cached_detail_lookup_allowed"
            ]
        )

    def test_trivial_renderer_reads_only_summary(self) -> None:
        pointer = self.publish()
        summary_path = Path(pointer["summary_file"])
        rendered = json.loads(
            summary_path.read_text(encoding="utf-8")
        )["initial_response"]["answer_draft_markdown"]
        self.assertIn("## Answer", rendered)
        self.assertNotIn("manifest.json", rendered)

    def test_direct_units_use_only_direct_evidence(self) -> None:
        summary = self.read_summary(self.publish())
        points = summary["initial_response"]["key_points"]
        self.assertEqual(["direct"], [point["support"] for point in points])
        self.assertEqual([["E1"]], [point["source_ids"] for point in points])
        self.assertNotIn(
            "related lead must not become",
            " ".join(point["text"] for point in points).casefold(),
        )

    def test_related_documents_build_a_labelled_provisional_answer(
        self,
    ) -> None:
        payload = synthetic_payload()
        payload["query"] = "関連する内容を教えて"
        payload["status"] = "partial"
        payload["answerability"] = "none"
        payload["evidence"] = []
        payload["document_results"] = [payload["document_results"][1]]
        payload["document_results"][0]["source_url"] = (
            "https://example.invalid/related/document"
        )
        summary = self.read_summary(self.publish(payload))
        points = summary["initial_response"]["key_points"]
        draft = summary["initial_response"]["answer_draft_markdown"]
        self.assertEqual(["related"], [point["support"] for point in points])
        self.assertIn("関連資料から組み立てた暫定回答", draft)
        self.assertIn("[D1]", draft)
        self.assertNotIn("https://", draft)

    def test_answer_draft_keeps_body_citation_unlinked(self) -> None:
        payload = synthetic_payload()
        payload["evidence"][0]["source_url"] = (
            "https://example.invalid/fixed/document"
        )
        draft = self.read_summary(
            self.publish(payload)
        )["initial_response"]["answer_draft_markdown"]
        self.assertIn("[E1]", draft)
        self.assertNotIn("https://", draft)

    def test_explicitly_non_authoritative_evidence_is_not_a_factual_unit(
        self,
    ) -> None:
        payload = synthetic_payload()
        payload["evidence"][0]["authoritative"] = False
        summary = self.read_summary(self.publish(payload))
        self.assertNotIn(
            "direct",
            [
                point["support"]
                for point in summary["initial_response"]["key_points"]
            ],
        )
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
        self.assertNotIn(
            "direct",
            [
                point["support"]
                for point in summary["initial_response"]["key_points"]
            ],
        )

    def test_large_summary_preserves_links_evidence_and_documents(
        self,
    ) -> None:
        payload = synthetic_payload()
        payload["evidence"] = []
        expected_evidence_urls: list[str] = []
        for index in range(4):
            evidence = json.loads(
                json.dumps(synthetic_payload()["evidence"][0])
            )
            evidence["source"]["path"] = (
                f"<root-name>/<relative-subdirectory>/evidence-{index}.txt"
            )
            evidence["source"]["title"] = f"evidence-{index}.txt"
            evidence["text"] = (
                f"Authoritative evidence statement {index} is retained."
            )
            evidence["matched_excerpt"] = evidence["text"]
            source_url = (
                f"https://example.invalid/fixed/{index}/" + "b" * 5_000
            )
            evidence["source_url"] = source_url
            payload["evidence"].append(evidence)
            expected_evidence_urls.append(source_url)

        payload["document_results"] = [
            {
                "path": f"<root-name>/<relative-subdirectory>/file-{index}.txt",
                "title": f"file-{index}.txt",
                "section": "Section",
                "preview": f"Useful document preview {index}.",
                "support_level": "weak",
                "authoritative": False,
                "relationship": "Related research material.",
                "source_url": (
                    f"https://example.invalid/permalink/{index}/"
                    + "d" * 1_000
                ),
            }
            for index in range(10)
        ]
        expected_document_urls: list[str] = [
            str(item["source_url"])
            for item in payload["document_results"]
        ]
        pointer = self.publish(payload)
        summary = self.read_summary(pointer)
        self.assertGreater(
            Path(pointer["summary_file"]).stat().st_size,
            16_384,
        )
        self.assertEqual(4, len(summary["evidence"]))
        self.assertEqual(10, len(summary["document_results"]))
        self.assertEqual(
            expected_evidence_urls,
            [str(item["source_url"]) for item in summary["evidence"]],
        )
        self.assertEqual(
            expected_document_urls,
            [
                str(item["source_url"])
                for item in summary["document_results"]
            ],
        )

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
        payload["evidence"][0]["source_url"] = fixed_url
        payload["document_results"][0]["source_url"] = fixed_url
        pointer = self.publish(payload)
        summary = self.read_summary(pointer)
        self.assertEqual(fixed_url, summary["evidence"][0]["source_url"])
        self.assertEqual(
            fixed_url,
            summary["document_results"][0]["source_url"],
        )

        # A later configuration or in-memory payload change cannot alter the
        # already-published result set.
        payload["evidence"][0]["source_url"] = (
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
            packet["expanded_items"][0]["source_url"],
        )
        self.assertNotIn("uri", packet["expanded_items"][0])
        self.assertIn("[E1]", packet["answer_draft_markdown"])
        self.assertNotIn(fixed_url, packet["answer_draft_markdown"])

    def test_new_provider_links_survive_summary_and_detail(self) -> None:
        providers = (
            (
                "gitlab",
                "https://gitlab.example.invalid/group/repository"
                "/-/blob/main/document.pdf",
                None,
            ),
            (
                "azure_devops",
                "https://dev.azure.com/organization/project/_git/repository"
                "?path=/document.pdf&version=GBmain",
                None,
            ),
            (
                "svn",
                "https://svn-web.example.invalid/project/"
                "?view=summary#files",
                None,
            ),
            (
                "svn",
                "https://svn.example.invalid/repos/project/trunk/document.pdf",
                (
                    "https://svn.example.invalid/repos/project/trunk/"
                    "document.pdf?p=1234&r=1234"
                ),
            ),
        )
        for provider, source_url, source_permalink in providers:
            with self.subTest(provider=provider):
                payload = synthetic_payload()
                payload["evidence"][0]["source_provider"] = provider
                payload["evidence"][0]["source_url"] = source_url
                if source_permalink:
                    payload["evidence"][0]["source_permalink"] = (
                        source_permalink
                    )
                pointer = self.publish(payload)
                summary = self.read_summary(pointer)
                self.assertEqual(
                    source_url,
                    summary["evidence"][0]["source_url"],
                )
                self.assertEqual(
                    source_permalink,
                    summary["evidence"][0].get("source_permalink"),
                )
                packet, _expires = result_bundle.load_expanded_result(
                    pointer["result_set_id"],
                    ["E1"],
                    detail_level="expanded",
                    spool_root=self.spool,
                    now=self.now + timedelta(minutes=1),
                )
                self.assertEqual(
                    source_url,
                    packet["expanded_items"][0]["source_url"],
                )
                self.assertEqual(
                    source_permalink,
                    packet["expanded_items"][0].get(
                        "source_permalink"
                    ),
                )

    def test_summary_never_truncates_a_valid_source_url(self) -> None:
        payload = synthetic_payload()
        source_url = "https://example.invalid/" + ("a" * 2_500)
        payload["evidence"][0]["source_url"] = source_url
        summary = self.read_summary(self.publish(payload))
        self.assertEqual(
            source_url,
            summary["evidence"][0]["source_url"],
        )
        self.assertNotIn("uri", summary["evidence"][0])

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
        immutable_files = manifest["files"]
        self.assertEqual(
            {
                "summary.json",
                *(
                    str(entry["file"])
                    for entry in manifest["items"].values()
                ),
            },
            set(immutable_files),
        )
        for relative, integrity in immutable_files.items():
            raw = (result_dir / relative).read_bytes()
            self.assertEqual(len(raw), integrity["size"])
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(),
                integrity["sha256"],
            )
        for path in result_dir.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_detail_integrity_mismatch_fails_closed_for_only_that_item(
        self,
    ) -> None:
        pointer = self.publish()
        result_dir = Path(pointer["summary_file"]).parent
        manifest = json.loads(
            (result_dir / "manifest.json").read_text(encoding="utf-8")
        )
        item_id, entry = next(iter(manifest["items"].items()))
        item_path = result_dir / entry["file"]
        item_path.write_bytes(item_path.read_bytes() + b" ")

        packet, _expires = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            [item_id],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now,
        )

        self.assertEqual("error", packet["status"])
        self.assertEqual([], packet["expanded_items"])
        self.assertIn(f"item_not_available:{item_id}", packet["warnings"])

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

    def test_large_expanded_packet_is_published_without_pruning(self) -> None:
        pointer = self.publish()
        packet, expires_at = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["E1", "D2"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        assert expires_at is not None
        large_draft = "expanded result remains intact " * 100_000
        packet["answer_draft_markdown"] = large_draft
        packet["expanded_items"][0]["source_url"] = (
            "https://example.invalid/current/" + "u" * 10_000
        )
        packet["expanded_items"][0]["source_permalink"] = (
            "https://example.invalid/fixed/" + "p" * 10_000
        )
        detail_pointer = result_bundle.publish_expanded_packet(
            packet,
            result_set_id=pointer["result_set_id"],
            expires_at=expires_at,
            spool_root=self.spool,
        )
        response_file = Path(detail_pointer["detail_file"])
        self.assertGreater(response_file.stat().st_size, 2 * 1024 * 1024)
        stored = json.loads(response_file.read_text(encoding="utf-8"))
        self.assertEqual(large_draft, stored["answer_draft_markdown"])
        self.assertEqual(
            packet["expanded_items"][0]["source_url"],
            stored["expanded_items"][0]["source_url"],
        )
        self.assertEqual(
            packet["expanded_items"][0]["source_permalink"],
            stored["expanded_items"][0]["source_permalink"],
        )

    def test_expanded_response_history_is_storage_bounded(self) -> None:
        pointer = self.publish()
        packet, expires_at = result_bundle.load_expanded_result(
            pointer["result_set_id"],
            ["E1"],
            detail_level="expanded",
            spool_root=self.spool,
            now=self.now + timedelta(minutes=1),
        )
        assert expires_at is not None
        latest = None
        for index in range(
            result_bundle.MAX_EXPANDED_RESPONSES_PER_RESULT + 5
        ):
            packet["answer_draft_markdown"] = f"response {index}"
            latest = result_bundle.publish_expanded_packet(
                packet,
                result_set_id=pointer["result_set_id"],
                expires_at=expires_at,
                spool_root=self.spool,
            )
        response_dir = (
            self.spool / pointer["result_set_id"] / "responses"
        )
        files = list(response_dir.glob("*.json"))
        self.assertEqual(
            result_bundle.MAX_EXPANDED_RESPONSES_PER_RESULT,
            len(files),
        )
        assert latest is not None
        self.assertTrue(Path(latest["detail_file"]).is_file())

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
        meta_publishes = [
            call
            for call in replace.call_args_list
            if Path(call.args[1]).name == "meta.json"
        ]
        self.assertEqual(1, len(meta_publishes))

    def test_windows_atomic_publish_retries_transient_sharing_error(
        self,
    ) -> None:
        original = result_bundle.os.replace
        calls = 0
        failures = 0

        def transient(source: Path, target: Path) -> None:
            nonlocal calls, failures
            calls += 1
            if calls < 3:
                failures += 1
                error = PermissionError("synthetic sharing violation")
                error.winerror = 5
                raise error
            original(source, target)

        with (
            mock.patch.object(
                result_bundle,
                "_is_windows",
                return_value=True,
            ),
            mock.patch.object(
                result_bundle.os,
                "replace",
                side_effect=transient,
            ),
            mock.patch.object(
                result_bundle,
                "WINDOWS_REPLACE_RETRY_SECONDS",
                0.5,
            ),
        ):
            pointer = self.publish()
        self.assertEqual(2, failures)
        self.assertGreaterEqual(calls, 3)
        self.assertTrue(Path(pointer["summary_file"]).is_file())

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
        self.assertTrue(
            (
                REPO_ROOT
                / ".copilot"
                / "rag"
                / "make_distribution_package.py"
            ).is_file()
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
