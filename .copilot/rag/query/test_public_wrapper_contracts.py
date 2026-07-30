from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAG_ROOT))

from wrapper import database_list, freshness, search_command


def _catalog(
    root: Path,
    documents: list[tuple[str | None, str, str, str]],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "catalog.sqlite")
    try:
        connection.execute(
            """
            CREATE TABLE document (
                doc_pk INTEGER PRIMARY KEY,
                source_id TEXT,
                path TEXT NOT NULL,
                content_hash TEXT,
                updated_at TEXT,
                visible_until INTEGER
            )
            """
        )
        for index, (source_id, path, content_hash, updated_at) in enumerate(
            documents,
            start=1,
        ):
            connection.execute(
                """
                INSERT INTO document(
                    doc_pk, source_id, path, content_hash, updated_at,
                    visible_until
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (index, source_id, path, content_hash, updated_at),
            )
        connection.commit()
    finally:
        connection.close()


def _completed(
    payload: object,
    *,
    stderr: bytes = b"",
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    stdout = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    )
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class PublicDatabaseListWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-public-list-")
        self.dbs = Path(self.temporary.name) / "dbs"
        self.dbs.mkdir()
        self.db = self.dbs / "example-rag"
        now = "2026-07-01T00:00:00+00:00"
        documents = [
            (f"private-{index}", f"Root/{index}.md", f"hash-{index}", now)
            for index in range(1, 10)
        ]
        documents.append(
            ("private-1", "Root/1-extra.md", "hash-1-extra", now)
        )
        documents.append((None, "Root/unattributed.md", "hash-u", now))
        _catalog(self.db, documents)
        (self.db / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-metadata-v1",
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "private-1",
                            "display_name": "Published source",
                            "source_type": "github",
                        },
                        {
                            "source_id": "sidecar-only",
                            "display_name": "Must not appear",
                            "source_type": "sharepoint",
                        },
                        {
                            "source_id": "private-2",
                            "display_name": "private-2",
                            "source_type": "folder",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.db / "source.json").write_text(
            '{"secret":"must-not-be-read"}',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_help_and_invalid_arguments_never_start_lower_cli(self) -> None:
        with mock.patch.object(database_list.subprocess, "run") as run:
            with self.assertRaises(SystemExit) as help_exit:
                database_list.main(["--help"])
            self.assertEqual(0, help_exit.exception.code)
            with self.assertRaises(SystemExit) as invalid_exit:
                database_list.main(["--unknown"])
            self.assertEqual(2, invalid_exit.exception.code)
            run.assert_not_called()

    def test_json_contract_is_bounded_and_never_exposes_raw_source_ids(
        self,
    ) -> None:
        lower = {
            "databases": [
                {
                    "name": "example-rag",
                    "title": "Example",
                    "query_hint": "Synthetic documents",
                    "lower_field": "preserved",
                }
            ]
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs)}),
            mock.patch.object(
                database_list.subprocess,
                "run",
                return_value=_completed(lower, stderr=b"lower warning\n"),
            ) as run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = database_list.main([])
        self.assertEqual(0, code)
        run.assert_called_once()
        self.assertEqual("lower warning\n", stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual("local-rag.database-list.v2", payload["schema"])
        item = payload["databases"][0]
        self.assertEqual("preserved", item["lower_field"])
        self.assertEqual(10, item["source_count"])
        self.assertEqual(1, item["unattributed_document_count"])
        self.assertEqual(3, len(item["sources"]))
        self.assertEqual(7, item["additional_source_count"])
        self.assertEqual("complete", item["content_summary_status"])
        self.assertIn("GitHub「Published source」", item["content_summary"])
        self.assertIn(
            "Published source",
            [source["name"] for source in item["sources"]],
        )
        other = next(
            source
            for source in item["sources"]
            if source["type"] == "other"
        )
        self.assertEqual("Other（8 Source）", other["name"])
        self.assertEqual(8, other["document_count"])
        self.assertEqual(
            {"other": 8, "folder": 1, "github": 1},
            {
                source_type["type"]: source_type["count"]
                for source_type in item["source_types"]
            },
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("private-", encoded)
        self.assertNotIn("sidecar-only", encoded)
        self.assertNotIn("Must not appear", encoded)
        self.assertNotIn("must-not-be-read", encoded)
        self.assertFalse((self.db / "catalog.sqlite-wal").exists())

    def test_source_cards_are_deterministically_limited_to_eight(self) -> None:
        (self.db / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-metadata-v1",
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": f"private-{index}",
                            "display_name": f"Public {index}",
                            "source_type": "github",
                        }
                        for index in range(1, 10)
                    ],
                }
            ),
            encoding="utf-8",
        )
        summary = database_list._content_summary(
            self.db,
            "example-rag",
        )
        self.assertEqual(10, summary["source_count"])
        self.assertEqual(8, len(summary["sources"]))
        self.assertEqual(2, summary["additional_source_count"])
        self.assertEqual("Public 1", summary["sources"][0]["name"])
        self.assertEqual(
            sorted(
                summary["sources"][1:],
                key=lambda value: (
                    value["name"].casefold(),
                    value["type"],
                ),
            ),
            summary["sources"][1:],
        )

    def test_safe_legacy_sharepoint_is_labeled_without_management_state(
        self,
    ) -> None:
        (self.db / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-links-v1",
                    "database": "example-rag",
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "private-1",
                            "display_name": "設計資料",
                            "mappings": [
                                {
                                    "mapping_id": (
                                        "00000000-0000-0000-"
                                        "0000-000000000001"
                                    ),
                                    "enabled": True,
                                    "path_prefix": "Root",
                                    "provider": "sharepoint",
                                    "strategy": "append-relative-path",
                                    "settings": {
                                        "source_web_root": (
                                            "https://example.invalid/"
                                            "Shared%20Documents"
                                        )
                                    },
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        summary = database_list._content_summary(
            self.db,
            "example-rag",
        )

        configured = next(
            source
            for source in summary["sources"]
            if source["name"] == "設計資料"
        )
        self.assertEqual("sharepoint", configured["type"])
        self.assertEqual("SharePoint", configured["label"])
        self.assertEqual("complete", summary["content_summary_status"])
        self.assertNotIn("private-1", json.dumps(summary, ensure_ascii=False))

    def test_gitlab_issue_label_never_exposes_link_settings(self) -> None:
        private_project_url = (
            "https://gitlab.example.invalid/private/group/project"
        )
        (self.db / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-metadata-v1",
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "private-1",
                            "display_name": "障害チケット",
                            "source_type": "gitlab_issues",
                            "link": {
                                "enabled": True,
                                "strategy": "regex-template",
                                "settings": {
                                    "path_pattern": (
                                        r"^issues/(?P<issue_iid>[0-9]+)"
                                        r"\.md$"
                                    ),
                                    "url_template": (
                                        f"{private_project_url}/-/issues/"
                                        "{issue_iid}"
                                    ),
                                },
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        summary = database_list._content_summary(
            self.db,
            "example-rag",
        )

        configured = next(
            source
            for source in summary["sources"]
            if source["name"] == "障害チケット"
        )
        self.assertEqual("gitlab_issues", configured["type"])
        self.assertEqual("GitLab Issue", configured["label"])
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(private_project_url, encoded)
        self.assertNotIn("issue_iid", encoded)
        self.assertNotIn("private-1", encoded)

    def test_text_output_is_rendered_from_one_lower_json_call(self) -> None:
        lower = {
            "databases": [
                {
                    "name": "example-rag",
                    "title": "Example",
                    "query_hint": "Synthetic documents",
                }
            ]
        }
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs)}),
            mock.patch.object(
                database_list.subprocess,
                "run",
                return_value=_completed(lower),
            ) as run,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(0, database_list.main(["--format", "text"]))
        run.assert_called_once()
        self.assertEqual(
            ["--format", "json"],
            run.call_args.args[0][-2:],
        )
        rendered = stdout.getvalue()
        self.assertIn("example-rag — Example", rendered)
        self.assertIn("内容:", rendered)
        self.assertIn("検索向け: Synthetic documents", rendered)

    def test_missing_catalog_is_unavailable_without_fabricated_sources(
        self,
    ) -> None:
        missing = {
            "databases": [
                {
                    "name": "missing-rag",
                    "title": "Missing",
                    "query_hint": "",
                }
            ]
        }
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs)}),
            mock.patch.object(
                database_list.subprocess,
                "run",
                return_value=_completed(missing),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(0, database_list.main(["--format", "json"]))
        item = json.loads(stdout.getvalue())["databases"][0]
        self.assertEqual("unavailable", item["content_summary_status"])
        self.assertEqual([], item["sources"])

    @unittest.skipIf(os.name == "nt", "symlink creation needs privileges")
    def test_catalog_and_sidecar_symlinks_are_never_followed(self) -> None:
        catalog = self.db / "catalog.sqlite"
        outside_catalog = Path(self.temporary.name) / "outside.sqlite"
        catalog.replace(outside_catalog)
        catalog.symlink_to(outside_catalog)
        unavailable = database_list._content_summary(
            self.db,
            "example-rag",
        )
        self.assertEqual("unavailable", unavailable["content_summary_status"])

        catalog.unlink()
        outside_catalog.replace(catalog)
        sidecar = self.db / "source-links.json"
        outside_sidecar = Path(self.temporary.name) / "outside.json"
        outside_sidecar.write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-metadata-v1",
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "private-1",
                            "display_name": "External secret name",
                            "source_type": "github",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        sidecar.unlink()
        sidecar.symlink_to(outside_sidecar)
        partial = database_list._content_summary(
            self.db,
            "example-rag",
        )
        self.assertEqual("partial", partial["content_summary_status"])
        self.assertNotIn(
            "External secret name",
            json.dumps(partial, ensure_ascii=False),
        )


class PublicSearchWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-public-search-"
        )
        self.dbs = Path(self.temporary.name) / "dbs"
        self.db = self.dbs / "example-rag"
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        _catalog(
            self.db,
            [
                ("private-source", "Root/docs/a.md", "abc123", old),
            ],
        )
        (self.db / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-metadata-v1",
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "private-source",
                            "source_type": "other",
                            "link": {
                                "enabled": True,
                                "strategy": "append-relative-path",
                                "settings": {
                                    "source_web_root": (
                                        "https://docs.example.invalid/base"
                                    )
                                },
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.db / "VERSION.json").write_text(
            json.dumps(
                {
                    "schema": "local-rag.db-version.v1",
                    "created_at": old,
                }
            ),
            encoding="utf-8",
        )
        (self.db / freshness.WRAPPER_METADATA_NAME).write_text(
            json.dumps(
                {
                    "schema_version": freshness.WRAPPER_METADATA_SCHEMA,
                    "content_snapshot_at": old,
                }
            ),
            encoding="utf-8",
        )
        self.lower_payload = {
            "schema": "local-rag.search.v1",
            "status": "ok",
            "db": "example-rag",
            "selected_db": "example-rag",
            "answerability": "full",
            "evidence": [
                {
                    "id": "E1",
                    "source": {
                        "path": "Root/docs/a.md",
                        "title": "A",
                        "revision": "sha256:abc123",
                        "_source_id": "private-source",
                    },
                    "text": "Evidence",
                }
            ],
            "background_context": [],
            "related_context": [],
            "document_results": [
                {
                    "id": "D1",
                    "path": "Root/docs/a.md",
                    "preview": "Preview",
                    "_source_id": "private-source",
                }
            ],
            "_result_detail_items": [
                {
                    "item_id": "E1",
                    "path": "Root/docs/a.md",
                    "matched_excerpt": "Evidence",
                    "_source_id": "private-source",
                }
            ],
            "warnings": [],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self,
        arguments: list[str],
        *,
        payload: object | None = None,
        returncode: int = 0,
    ) -> tuple[int, str, str, mock.Mock]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        runner = mock.Mock(
            return_value=_completed(
                self.lower_payload if payload is None else payload,
                stderr=b"child stderr\n",
                returncode=returncode,
            )
        )
        with (
            mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs)}),
            mock.patch.object(subprocess, "run", runner),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = search_command.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue(), runner

    def test_help_and_invalid_arguments_never_start_a_child(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            with self.assertRaises(SystemExit) as help_exit:
                search_command.main(["--help"])
            self.assertEqual(0, help_exit.exception.code)
            with self.assertRaises(SystemExit) as invalid_exit:
                search_command.main(["--unknown", "question"])
            self.assertEqual(2, invalid_exit.exception.code)
            with self.assertRaises(SystemExit) as mixed_exit:
                search_command.main(
                    ["--result-set-id", "id", "--db", "example-rag"]
                )
            self.assertEqual(2, mixed_exit.exception.code)
            run.assert_not_called()

    def test_search_calls_lower_once_adds_source_links_and_stale_notice(
        self,
    ) -> None:
        code, stdout, stderr, run = self._run(
            [
                "--db",
                "example-rag",
                "--compact-json",
                "--facet",
                "purpose",
                "--daemon-fallback",
                "on",
                "question",
            ]
        )
        self.assertEqual(0, code)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(
            ("query", "search.py"),
            Path(command[1]).parts[-2:],
        )
        self.assertIn("--facet", command)
        self.assertEqual(
            "on",
            command[command.index("--daemon-fallback") + 1],
        )
        self.assertNotIn("--compact-json", command)
        self.assertEqual("1", run.call_args.kwargs["env"][
            "LOCAL_RAG_WRAPPER_INTERNAL"
        ])
        self.assertEqual("child stderr\n", stderr)
        payload = json.loads(stdout)
        self.assertEqual(
            "https://docs.example.invalid/base/docs/a.md",
            payload["evidence"][0]["source_url"],
        )
        self.assertEqual(
            "https://docs.example.invalid/base/docs/a.md",
            payload["document_results"][0]["source_url"],
        )
        self.assertEqual("other", payload["evidence"][0]["source_provider"])
        self.assertNotIn("_source_id", stdout)
        self.assertNotIn('"uri"', stdout)
        self.assertEqual(
            "stale",
            payload["database_freshness"]["status"],
        )
        self.assertEqual(
            freshness.STALE_NOTICE_MESSAGE_JA,
            payload["database_freshness"]["chat_notice"]["message_ja"],
        )
        self.assertNotIn("chat_notice", payload)

    def test_exact_private_source_handoff_survives_duplicate_catalog_path(
        self,
    ) -> None:
        connection = sqlite3.connect(self.db / "catalog.sqlite")
        try:
            connection.execute(
                """
                INSERT INTO document(
                    doc_pk, source_id, path, content_hash, updated_at,
                    visible_until
                ) VALUES (2, 'other-source', 'Root/docs/a.md', 'abc123',
                          '2026-01-01T00:00:00+00:00', NULL)
                """
            )
            connection.commit()
        finally:
            connection.close()
        code, stdout, _stderr, _run = self._run(
            ["--db", "example-rag", "--format", "json", "question"]
        )
        self.assertEqual(0, code)
        payload = json.loads(stdout)
        self.assertEqual(
            "https://docs.example.invalid/base/docs/a.md",
            payload["evidence"][0]["source_url"],
        )
        self.assertEqual(
            "https://docs.example.invalid/base/docs/a.md",
            payload["document_results"][0]["source_url"],
        )

    def test_link_resolution_precedes_compact_path_projection(self) -> None:
        long_name = "a" * 450 + ".md"
        stored_path = f"Root/docs/{long_name}"
        connection = sqlite3.connect(self.db / "catalog.sqlite")
        try:
            connection.execute(
                """
                INSERT INTO document(
                    doc_pk, source_id, path, content_hash, updated_at,
                    visible_until
                ) VALUES (2, 'private-source', ?, 'long-hash',
                          '2026-01-01T00:00:00+00:00', NULL)
                """,
                (stored_path,),
            )
            connection.commit()
        finally:
            connection.close()
        payload = {
            **self.lower_payload,
            "evidence": [
                {
                    "id": "E1",
                    "_source_id": "private-source",
                    "source": {
                        "path": stored_path,
                        "title": "Long",
                        "revision": "sha256:long-hash",
                    },
                    "text": "Evidence",
                }
            ],
            "document_results": [],
            "_result_detail_items": [],
        }
        code, stdout, _stderr, _run = self._run(
            [
                "--db",
                "example-rag",
                "--compact-json",
                "--format",
                "json",
                "question",
            ],
            payload=payload,
        )
        self.assertEqual(0, code)
        item = json.loads(stdout)["evidence"][0]
        self.assertTrue(item["source_url"].endswith(f"docs/{long_name}"))

    def test_catalog_change_during_resolution_discards_every_uri(self) -> None:
        real = search_command._catalog_fingerprint(self.db)
        with mock.patch.object(
            search_command,
            "_catalog_fingerprint",
            side_effect=[
                real,
                "changed",
                "changed",
            ],
        ):
            code, stdout, _stderr, _run = self._run(
                ["--db", "example-rag", "--format", "json", "question"]
            )
        self.assertEqual(0, code)
        payload = json.loads(stdout)
        self.assertNotIn("source_url", payload["evidence"][0])
        self.assertNotIn("source_url", payload["document_results"][0])

    def test_lower_legacy_uri_fields_are_never_promoted(self) -> None:
        self.lower_payload["evidence"][0].update(
            {
                "source_url": "https://wrong.example.invalid/current",
                "source_permalink": "https://wrong.example.invalid/fixed",
                "source_provider": "wrong",
            }
        )
        self.lower_payload["document_results"][0]["source_url"] = (
            "https://wrong.example.invalid/document"
        )
        (self.db / "source-links.json").write_text(
            "{invalid",
            encoding="utf-8",
        )
        code, stdout, _stderr, _run = self._run(
            ["--db", "example-rag", "--format", "json", "question"]
        )
        self.assertEqual(0, code)
        payload = json.loads(stdout)
        for key in ("evidence", "document_results"):
            for item in payload[key]:
                self.assertNotIn("uri", item)
                self.assertNotIn("source_url", item)
                self.assertNotIn("source_permalink", item)
                self.assertNotIn("source_provider", item)

    def test_file_delivery_publishes_enriched_bundle_once_and_never_edits_it(
        self,
    ) -> None:
        immutable = Path(self.temporary.name) / "summary.json"
        immutable.write_bytes(b'{"unchanged":true}\n')
        before = immutable.read_bytes()
        pointer = {
            "status": "written",
            "schema_version": "rag-result-pointer-v1",
            "result_set_id": "00000000-0000-0000-0000-000000000001",
            "summary_file": str(immutable),
            "expires_at": "2026-07-29T00:00:00Z",
            "bytes": len(before),
        }
        with mock.patch.object(
            search_command,
            "_publish_bundle",
            return_value=pointer,
        ) as publish:
            code, stdout, _stderr, _run = self._run(
                [
                    "--db",
                    "example-rag",
                    "--result-delivery",
                    "file",
                    "question",
                ]
            )
        self.assertEqual(0, code)
        publish.assert_called_once()
        published_payload = publish.call_args.args[0]
        self.assertIn("source_url", published_payload["evidence"][0])
        self.assertIn(
            "source_url",
            published_payload["_result_detail_items"][0],
        )
        self.assertNotIn("_source_id", json.dumps(published_payload))
        self.assertEqual(before, immutable.read_bytes())
        output = json.loads(stdout)
        self.assertEqual(pointer["result_set_id"], output["result_set_id"])
        self.assertIn("database_freshness", output)

    @unittest.skipIf(os.name == "nt", "symlink creation needs privileges")
    def test_catalog_or_sidecar_symlink_never_produces_uri(self) -> None:
        catalog = self.db / "catalog.sqlite"
        outside_catalog = Path(self.temporary.name) / "outside.sqlite"
        catalog.replace(outside_catalog)
        catalog.symlink_to(outside_catalog)
        code, stdout, _stderr, _run = self._run(
            ["--db", "example-rag", "--format", "json", "question"]
        )
        self.assertEqual(0, code)
        self.assertNotIn(
            "source_url",
            json.loads(stdout)["evidence"][0],
        )

        catalog.unlink()
        outside_catalog.replace(catalog)
        sidecar = self.db / "source-links.json"
        outside_sidecar = Path(self.temporary.name) / "outside.json"
        sidecar.replace(outside_sidecar)
        sidecar.symlink_to(outside_sidecar)
        code, stdout, _stderr, _run = self._run(
            ["--db", "example-rag", "--format", "json", "question"]
        )
        self.assertEqual(0, code)
        self.assertNotIn(
            "source_url",
            json.loads(stdout)["evidence"][0],
        )

    def test_hung_lower_process_is_bounded_and_never_retried(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        expired = subprocess.TimeoutExpired(
            cmd=["python", "query/search.py"],
            timeout=1.1,
            stderr=b"child timeout diagnostic\n",
        )
        with (
            mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs)}),
            mock.patch.object(
                subprocess,
                "run",
                side_effect=expired,
            ) as run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = search_command.main(
                [
                    "--db",
                    "example-rag",
                    "--timeout",
                    "0.1",
                    "--format",
                    "json",
                    "question",
                ]
            )
        self.assertEqual(124, code)
        run.assert_called_once()
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 0.1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("error", payload["status"])
        self.assertEqual("timeout", payload["error_kind"])
        self.assertEqual(
            "stale",
            payload["database_freshness"]["status"],
        )
        self.assertEqual(
            freshness.STALE_NOTICE_DEDUPE_KEY,
            payload["database_freshness"]["chat_notice"]["dedupe_key"],
        )
        self.assertNotIn("child timeout diagnostic", stdout.getvalue())
        self.assertIn("child timeout diagnostic", stderr.getvalue())

    def test_zero_timeout_preserves_lower_no_deadline_contract(self) -> None:
        code, _stdout, _stderr, run = self._run(
            [
                "--db",
                "example-rag",
                "--timeout",
                "0",
                "--format",
                "json",
                "question",
            ]
        )
        self.assertEqual(0, code)
        run.assert_called_once()
        self.assertIsNone(run.call_args.kwargs["timeout"])
        child = run.call_args.args[0]
        self.assertIn("--timeout", child)
        self.assertEqual("0", child[child.index("--timeout") + 1])

    def test_detail_dispatch_calls_result_detail_once_and_not_search(self) -> None:
        detail = {
            "schema_version": "rag-expanded-answer-v1",
            "status": "ok",
            "result_set_id": "00000000-0000-0000-0000-000000000001",
            "expanded_items": [],
            "answer_draft_markdown": "",
            "warnings": [],
        }
        with mock.patch.object(
            search_command,
            "_detail_database_name",
            return_value="example-rag",
        ):
            code, stdout, _stderr, run = self._run(
                [
                    "--result-set-id",
                    detail["result_set_id"],
                    "--item-id",
                    "E1",
                    "--detail-level",
                    "expanded",
                    "--result-delivery",
                    "stdout",
                ],
                payload=detail,
            )
        self.assertEqual(0, code)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(
            ("query", "result_detail.py"),
            Path(command[1]).parts[-2:],
        )
        self.assertFalse(
            any(
                Path(value).parts[-2:] == ("query", "search.py")
                for value in command
            )
        )
        self.assertIn("database_freshness", json.loads(stdout))

    def test_prompt_output_includes_stale_freshness_notice(self) -> None:
        code, stdout, _stderr, run = self._run(
            ["--db", "example-rag", "question"]
        )
        self.assertEqual(0, code)
        run.assert_called_once()
        self.assertIn(freshness.STALE_NOTICE_MESSAGE_JA, stdout)

    def test_child_exit_code_and_non_json_stdout_are_preserved(self) -> None:
        code, stdout, stderr, run = self._run(
            ["--db", "example-rag", "--format", "json", "question"],
            payload=b"not-json\n",
            returncode=7,
        )
        self.assertEqual(7, code)
        run.assert_called_once()
        self.assertEqual("not-json\n", stdout)
        self.assertEqual("child stderr\n", stderr)


class FreshnessTests(unittest.TestCase):
    def test_unknown_and_stale_boundary(self) -> None:
        self.assertEqual(
            "unknown",
            freshness.database_freshness(None)["status"],
        )
        with tempfile.TemporaryDirectory(prefix="rag-freshness-") as directory:
            root = Path(directory)
            now = datetime(2026, 7, 29, tzinfo=timezone.utc)
            _catalog(
                root,
                [
                    (
                        "source",
                        "Root/a.md",
                        "hash",
                        (now - timedelta(days=30)).isoformat(),
                    )
                ],
            )
            (root / freshness.WRAPPER_METADATA_NAME).write_text(
                json.dumps(
                    {
                        "schema_version": freshness.WRAPPER_METADATA_SCHEMA,
                        "content_snapshot_at": (
                            now - timedelta(days=30)
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            value = freshness.database_freshness(root, now=now)
            self.assertEqual("stale", value["status"])
            self.assertEqual(30, value["age_days"])
            self.assertEqual(
                (now - timedelta(days=30))
                .isoformat()
                .replace("+00:00", "Z"),
                value["content_snapshot_at"],
            )
            self.assertEqual(
                {
                    "code": freshness.STALE_NOTICE_CODE,
                    "scope": freshness.STALE_NOTICE_SCOPE,
                    "dedupe_key": freshness.STALE_NOTICE_DEDUPE_KEY,
                    "message_ja": freshness.STALE_NOTICE_MESSAGE_JA,
                },
                freshness.add_freshness({}, root, now=now)[
                    "database_freshness"
                ]["chat_notice"],
            )

    def test_wrapper_metadata_is_the_only_freshness_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-freshness-") as directory:
            root = Path(directory)
            now = datetime(2026, 7, 29, tzinfo=timezone.utc)
            (root / "VERSION.json").write_text(
                json.dumps(
                    {
                        "schema": "local-rag.db-version.v1",
                        "created_at": (
                            now - timedelta(days=90)
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            (root / "db-snapshot.json").write_text(
                json.dumps(
                    {
                        "schema_version": "local-rag-db-snapshot-v1",
                        "snapshot_at": (
                            now - timedelta(days=1)
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            (root / freshness.WRAPPER_METADATA_NAME).write_text(
                json.dumps(
                    {
                        "schema_version": freshness.WRAPPER_METADATA_SCHEMA,
                        "content_snapshot_at": (
                            now - timedelta(days=90)
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                "stale",
                freshness.database_freshness(root, now=now)["status"],
            )
            (root / freshness.WRAPPER_METADATA_NAME).write_text(
                '{"schema_version":"wrong",'
                '"content_snapshot_at":"not-a-date"}',
                encoding="utf-8",
            )
            self.assertEqual(
                "unknown",
                freshness.database_freshness(root, now=now)["status"],
            )

    def test_future_snapshot_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-freshness-") as directory:
            root = Path(directory)
            now = datetime(2026, 7, 29, tzinfo=timezone.utc)
            (root / freshness.WRAPPER_METADATA_NAME).write_text(
                json.dumps(
                    {
                        "schema_version": freshness.WRAPPER_METADATA_SCHEMA,
                        "content_snapshot_at": (
                            now + timedelta(seconds=1)
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            value = freshness.database_freshness(root, now=now)
            self.assertEqual(
                {
                    "status": "unknown",
                    "content_snapshot_at": None,
                    "age_days": None,
                },
                value,
            )


if __name__ == "__main__":
    unittest.main()
