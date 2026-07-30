from __future__ import annotations

import json
import os
import socket
import sqlite3
import ssl
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from source_manager import (
    REDMINE_RETRY_POLICY,
    SourceManagerError,
    SourceStore,
    generated_redmine_link,
    build_fetch_plan,
    confirm_add_success,
    execute_fetch_plan,
    list_sources,
    redmine_batches,
    parse_redmine_project_url,
    redmine_updated_on_cutoff,
    repair_generated_redmine_link,
    register_source,
    resolve_environment_root,
    update_all_sources,
    update_source,
    update_source_configuration,
    validate_provider_config,
)
from source_manager.metadata import _canonical_source
from source_manager.metadata import publish_source_metadata
from source_manager.errors import sanitize_diagnostic
from source_manager import execution as execution_module


def _valid_add_summary(source_id: str, *, file_count: int = 1) -> dict:
    return {
        "operation": "add",
        "source_id": source_id,
        "file_count": file_count,
        "indexed_files": file_count,
        "skipped_files": 0,
        "error_files": 0,
        "upserted_records": file_count,
        "deleted_records": 0,
    }


class SourceStoreContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-manager-"
        )
        self.db_root = Path(self.temporary.name) / "fixture-rag"
        self.db_root.mkdir()
        self.store = SourceStore(self.db_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registration_allocates_provisional_key_and_fixed_root(self) -> None:
        result = register_source(
            self.db_root,
            source_type="github",
            display_name="Synthetic repository",
            fetch={
                "repository_url": (
                    "https://git.example.invalid/group/repository.git"
                )
            },
        )
        key = result["local_source_key"]
        self.assertRegex(key, r"^src_[a-z0-9-]+-[0-9a-f]{12}$")
        self.assertIsNone(result["source_id"])
        self.assertEqual(
            f"sources/{key}/work/ingest/{key}",
            result["paths"]["work_directory"],
        )
        self.assertEqual(key, result["paths"]["logical_root_name"])
        stored = self.store.read_source(key)
        self.assertEqual("local-rag-source-manager-v1", stored.payload["schema_version"])
        self.assertIsNone(stored.payload["source_id"])
        self.assertFalse(stored.payload["metadata_sync_pending"])

    def test_local_key_is_stable_and_source_id_waits_for_add_success(self) -> None:
        key = "src_fixture-0123456789ab"
        first = register_source(
            self.db_root,
            source_type="other",
            display_name="Fixture files",
            fetch={},
            local_source_key=key,
        )
        self.assertEqual(key, first["local_source_key"])
        self.assertIsNone(first["source_id"])
        planned = update_source(self.db_root, key)
        self.assertNotIn("--source-type", planned["add_request"]["arguments"])
        self.assertEqual(key, planned["add_request"]["source_id"])
        confirmed = confirm_add_success(
            self.db_root,
            key,
            source_id="trusted-source",
        )
        self.assertEqual(key, confirmed["local_source_key"])
        self.assertEqual("trusted-source", confirmed["source_id"])

    def test_delete_source_removes_only_selected_management_directory(
        self,
    ) -> None:
        first = self.store.create_source(
            source_type="github",
            display_name="First",
            fetch={
                "repository_url": (
                    "https://git.example.invalid/team/first.git"
                )
            },
        )
        second = self.store.create_source(
            source_type="github",
            display_name="Second",
            fetch={
                "repository_url": (
                    "https://git.example.invalid/team/second.git"
                )
            },
        )
        first_key = first.payload["local_source_key"]
        second_key = second.payload["local_source_key"]
        self.store.delete_source(
            first_key,
            expected_revision=first.revision,
            expected_etag=first.etag,
        )
        self.assertFalse(
            (self.db_root / self.store.paths(first_key).source_dir).exists()
        )
        self.assertTrue(
            (self.db_root / self.store.paths(second_key).source_json).is_file()
        )

    def test_one_link_cas_and_metadata_pending(self) -> None:
        stored = self.store.create_source(
            source_type="github",
            display_name="Fixture",
            fetch={"repository_url": "https://git.example.invalid/o/r"},
            link={
                "enabled": True,
                "strategy": "github-blob",
                "settings": {
                    "repository_url": "https://git.example.invalid/o/r",
                    "ref": "main",
                },
            },
        )
        self.assertEqual(
            {"enabled", "strategy", "settings"},
            set(stored.payload["pending_metadata"]["link"]),
        )
        self.assertFalse(stored.payload["metadata_sync_pending"])
        confirmed = self.store.confirm_source_id(
            stored.payload["local_source_key"],
            "indexed-source",
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        self.assertTrue(confirmed.payload["metadata_sync_pending"])
        synced = self.store.mark_metadata_synced(
            stored.payload["local_source_key"],
            expected_revision=confirmed.revision,
            expected_etag=confirmed.etag,
        )
        self.assertFalse(synced.payload["metadata_sync_pending"])
        self.assertNotIn("pending_metadata", synced.payload)
        edited = dict(synced.payload)
        edited["display_name"] = "Edited"
        changed = self.store.save_source(
            edited,
            expected_revision=synced.revision,
            expected_etag=synced.etag,
        )
        self.assertTrue(changed.payload["metadata_sync_pending"])
        with self.assertRaisesRegex(
            SourceManagerError,
            "source_configuration_changed",
        ):
            self.store.save_source(
                edited,
                expected_revision=synced.revision,
                expected_etag=synced.etag,
            )

    def test_credentials_absolute_paths_and_other_uri_are_rejected(self) -> None:
        with self.assertRaisesRegex(SourceManagerError, "credentials"):
            register_source(
                self.db_root,
                source_type="github",
                display_name="Unsafe",
                fetch={
                    "repository_url": (
                        "https://user:secret@git.example.invalid/o/r"
                    )
                },
            )
        absolute_fixture = str(
            Path(self.temporary.name).resolve() / "outside"
        )
        with self.assertRaises(SourceManagerError):
            register_source(
                self.db_root,
                source_type="sharepoint",
                display_name="Unsafe",
                fetch={
                    "root_env": "RAG_SHAREPOINT_ROOT",
                    "relative_path": absolute_fixture,
                },
            )
        with self.assertRaisesRegex(SourceManagerError, "do not publish"):
            self.store.create_source(
                source_type="other",
                display_name="Other",
                fetch={},
                link={
                    "enabled": True,
                    "strategy": "append-relative-path",
                    "settings": {
                        "source_web_root": "https://files.example.invalid"
                    },
                },
            )

    def test_all_path_components_and_event_target_reject_symlinks(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        key = "src_fixture-0123456789ab"
        source_dir = self.db_root / "sources" / key
        source_dir.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (source_dir / "work").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SourceManagerError, "links"):
            self.store.create_source(
                source_type="other",
                display_name="Linked work",
                fetch={},
                local_source_key=key,
            )
        self.assertFalse((source_dir / "source.json").exists())

        (source_dir / "work").unlink()
        stored = self.store.create_source(
            source_type="other",
            display_name="Safe",
            fetch={},
            local_source_key=key,
        )
        external_events = outside / "events.jsonl"
        external_events.write_text("sentinel\n", encoding="utf-8")
        events = stored.path.parent / "events.jsonl"
        events.symlink_to(external_events)
        with self.assertRaisesRegex(SourceManagerError, "links"):
            self.store.append_event(key, "fetch.completed")
        self.assertEqual("sentinel\n", external_events.read_text(encoding="utf-8"))


class ProviderAndRunnerContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-runner-"
        )
        self.db_root = Path(self.temporary.name) / "fixture-rag"
        self.db_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_final_manager_provider_forms_are_accepted(self) -> None:
        values = {
            "github": {
                "repository_url": "https://git.example.invalid/o/r.git"
            },
            "svn": {
                "repository_url": "https://svn.example.invalid/project",
                "recursive": False,
            },
            "redmine": {
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": 30,
                "api_key_env": "LOCAL_RAG_REDMINE_API_KEY",
            },
            "sharepoint": {
                "root_env": "RAG_SHAREPOINT_ROOT",
                "relative_path": "Documents/Reference",
            },
            "other": {"one_shot": True},
        }
        normalized = {
            provider: validate_provider_config(provider, settings)
            for provider, settings in values.items()
        }
        self.assertEqual(False, normalized["svn"]["recursive"])
        self.assertEqual("fixture", normalized["redmine"]["project_id"])
        self.assertEqual(
            "LOCAL_RAG_REDMINE_API_KEY",
            normalized["redmine"]["api_key_env"],
        )

    def test_redmine_project_url_preserves_mount_path_and_port(self) -> None:
        project = parse_redmine_project_url(
            "https://issues.example.invalid:8443/tools/redmine/"
            "projects/fixture/"
        )
        self.assertEqual(
            "https://issues.example.invalid:8443/tools/redmine/"
            "projects/fixture",
            project.project_url,
        )
        self.assertEqual(
            "https://issues.example.invalid:8443/tools/redmine",
            project.api_root,
        )
        self.assertEqual(
            "https://issues.example.invalid:8443/tools/redmine/issues.json",
            project.issues_api_url,
        )
        self.assertEqual(
            "https://issues.example.invalid:8443/tools/redmine/"
            "issues/{issue_id}",
            project.issue_link_template,
        )

    def test_redmine_project_url_rejects_noncanonical_authority_suffixes(
        self,
    ) -> None:
        for value in (
            "https://user:secret@issues.example.invalid/projects/fixture",
            "https://issues.example.invalid/projects/fixture?key=value",
            "https://issues.example.invalid/projects/fixture#section",
            "https://issues.example.invalid/projects/fixture/other",
        ):
            with self.subTest(value=value), self.assertRaises(
                SourceManagerError
            ):
                parse_redmine_project_url(value)

    def test_redmine_legacy_derivatives_are_recomputed_from_project_url(
        self,
    ) -> None:
        normalized = validate_provider_config(
            "redmine",
            {
                "project_url": (
                    "https://issues.example.invalid:8443/redmine/"
                    "projects/fixture/"
                ),
                "base_url": "https://stale.example.invalid",
                "project_id": "stale",
                "updated_within_days": 30,
            },
        )
        self.assertEqual(
            "https://issues.example.invalid:8443/redmine",
            normalized["base_url"],
        )
        self.assertEqual("fixture", normalized["project_id"])

    def test_only_known_generated_redmine_link_is_repaired(self) -> None:
        project_url = (
            "https://issues.example.invalid/redmine/projects/fixture"
        )
        legacy = {
            "enabled": True,
            "strategy": "regex-template",
            "settings": {
                "path_pattern": r"^issues/(?P<issue_id>[0-9]+)\.md$",
                "url_template": (
                    "https://issues.example.invalid/issues/{issue_id}"
                ),
            },
        }
        self.assertEqual(
            generated_redmine_link(project_url),
            repair_generated_redmine_link(project_url, legacy),
        )
        manual = {
            **legacy,
            "settings": {
                **legacy["settings"],
                "url_template": (
                    "https://links.example.invalid/items/{issue_id}"
                ),
            },
        }
        self.assertEqual(
            manual,
            repair_generated_redmine_link(project_url, manual),
        )

    def test_runner_repairs_generated_link_and_legacy_fetch_derivatives(
        self,
    ) -> None:
        project_url = (
            "https://issues.example.invalid/redmine/projects/fixture"
        )
        legacy_link = {
            "enabled": True,
            "strategy": "regex-template",
            "settings": {
                "path_pattern": r"^issues/(?P<issue_id>[0-9]+)\.md$",
                "url_template": (
                    "https://issues.example.invalid/issues/{issue_id}"
                ),
            },
        }
        registered = register_source(
            self.db_root,
            source_type="redmine",
            display_name="Mounted tracker",
            fetch={
                "project_url": project_url,
                "updated_within_days": None,
            },
            link=legacy_link,
        )
        key = registered["local_source_key"]
        stored = SourceStore(self.db_root).read_source(key)
        self.assertEqual(
            generated_redmine_link(project_url),
            stored.payload["pending_metadata"]["link"],
        )

        legacy_payload = dict(stored.payload)
        legacy_payload["fetch"] = {
            **stored.payload["fetch"],
            "base_url": "https://stale.example.invalid",
            "project_id": "stale",
        }
        stale = SourceStore(self.db_root).save_source(
            legacy_payload,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )
        manual_link = {
            "enabled": True,
            "strategy": "regex-template",
            "settings": {
                "path_pattern": r"^issues/(?P<issue_id>[0-9]+)\.md$",
                "url_template": (
                    "https://links.example.invalid/items/{issue_id}"
                ),
            },
        }
        manual_payload = dict(stale.payload)
        manual_payload["pending_metadata"] = {
            "source_type": "redmine",
            "link": manual_link,
        }
        SourceStore(self.db_root).save_source(
            manual_payload,
            expected_revision=stale.revision,
            expected_etag=stale.etag,
        )

        update_source(self.db_root, key)

        repaired = SourceStore(self.db_root).read_source(key).payload
        self.assertEqual(
            "https://issues.example.invalid/redmine",
            repaired["fetch"]["base_url"],
        )
        self.assertEqual("fixture", repaired["fetch"]["project_id"])
        self.assertEqual(
            manual_link,
            repaired["pending_metadata"]["link"],
        )

    def test_redmine_cutoff_is_absolute_utc_and_resume_stable(self) -> None:
        first = redmine_updated_on_cutoff(
            30,
            {},
            clock=lambda: datetime(
                2026, 7, 29, 23, 30, tzinfo=timezone.utc
            ),
        )
        self.assertEqual("2026-06-29", first)
        resumed = redmine_updated_on_cutoff(
            30,
            {"redmine_updated_on_cutoff": first},
            clock=lambda: datetime(
                2027, 1, 1, 0, 0, tzinfo=timezone.utc
            ),
        )
        self.assertEqual(first, resumed)
        self.assertIsNone(
            redmine_updated_on_cutoff(
                None,
                {},
                clock=lambda: datetime(
                    2026, 7, 29, tzinfo=timezone.utc
                ),
            )
        )

    def test_redmine_batches_five_and_never_retries_http_500(self) -> None:
        self.assertEqual(
            [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11]],
            redmine_batches(range(1, 12)),
        )
        self.assertTrue(
            REDMINE_RETRY_POLICY.should_retry(attempt=1, status_code=503)
        )
        self.assertFalse(
            REDMINE_RETRY_POLICY.should_retry(attempt=1, status_code=500)
        )
        self.assertFalse(
            REDMINE_RETRY_POLICY.should_retry(attempt=3, status_code=503)
        )

    def test_redmine_http_status_diagnostics_are_complete_and_bounded(
        self,
    ) -> None:
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                calls = 0

                def getter(_url, _headers, _timeout):
                    nonlocal calls
                    calls += 1
                    return (
                        status,
                        (
                            b'{"password":"body-secret","detail":"'
                            + b"x" * 70_000
                            + b'"}'
                        ),
                        {
                            "Content-Type": "application/json; charset=utf-8",
                            "Set-Cookie": "session=response-secret",
                        },
                    )

                with self.assertRaises(SourceManagerError) as captured:
                    execution_module._get_with_retry(
                        getter,
                        (
                            "https://issues.example.invalid/issues.json"
                            "?api_key=url-secret"
                        ),
                        {
                            "X-Redmine-API-Key": "header-secret",
                            "Accept": "application/json",
                        },
                    )
                self.assertEqual(1, calls)
                diagnostic = captured.exception.diagnostic
                self.assertEqual("GET", diagnostic["method"])
                self.assertEqual(status, diagnostic["status"])
                self.assertEqual(1, diagnostic["attempt"])
                self.assertEqual(3, diagnostic["max_attempts"])
                self.assertFalse(diagnostic["retry"])
                self.assertEqual(
                    "application/json; charset=utf-8",
                    diagnostic["content_type"],
                )
                self.assertGreater(diagnostic["body_bytes"], 65_536)
                self.assertTrue(diagnostic["body_truncated"])
                self.assertLessEqual(
                    len(diagnostic["body_preview"]),
                    65_536,
                )
                serialized = json.dumps(
                    diagnostic,
                    ensure_ascii=False,
                ) + str(captured.exception)
                for secret in (
                    "url-secret",
                    "header-secret",
                    "response-secret",
                    "body-secret",
                ):
                    self.assertNotIn(secret, serialized)

    def test_redmine_http_retry_progress_reports_retry_after_immediately(
        self,
    ) -> None:
        responses = [
            (
                429,
                b'{"error":"rate limited"}',
                {
                    "Retry-After": "2",
                    "Content-Type": "application/json",
                },
            ),
            (200, b'{"issues":[]}', {"Content-Type": "application/json"}),
        ]
        events: list[dict[str, object]] = []

        def getter(_url, _headers, _timeout):
            return responses.pop(0)

        with mock.patch.object(execution_module.time, "sleep") as sleeper:
            status, _body = execution_module._get_with_retry(
                getter,
                "https://issues.example.invalid/issues.json",
                {"X-Redmine-API-Key": "secret"},
                progress_callback=events.append,
            )
        self.assertEqual(200, status)
        self.assertEqual(2, len(events))
        self.assertEqual(429, events[0]["status"])
        self.assertTrue(events[0]["retry"])
        self.assertEqual("2", events[0]["retry_after"])
        self.assertEqual(2.0, events[0]["wait_seconds"])
        self.assertEqual(200, events[1]["status"])
        self.assertFalse(events[1]["retry"])
        sleeper.assert_called_once_with(2.0)
        self.assertNotIn("secret", json.dumps(events))

    def test_redmine_429_final_failure_retains_retry_diagnostics(self) -> None:
        calls = 0

        def getter(_url, _headers, _timeout):
            nonlocal calls
            calls += 1
            return (
                429,
                b'{"error":"rate limited"}',
                {"Retry-After": "0", "Content-Type": "application/json"},
            )

        with (
            mock.patch.object(execution_module.time, "sleep"),
            self.assertRaises(SourceManagerError) as captured,
        ):
            execution_module._get_with_retry(
                getter,
                "https://issues.example.invalid/issues.json",
                {"X-Redmine-API-Key": "secret"},
            )
        self.assertEqual(3, calls)
        diagnostic = captured.exception.diagnostic
        self.assertEqual(429, diagnostic["status"])
        self.assertEqual("Too Many Requests", diagnostic["reason"])
        self.assertEqual(3, diagnostic["attempt"])
        self.assertEqual("0", diagnostic["retry_after"])
        self.assertFalse(diagnostic["retry"])
        self.assertEqual(0.0, diagnostic["wait_seconds"])

    def test_retry_after_http_date_is_respected(self) -> None:
        with mock.patch.object(
            execution_module.time,
            "time",
            return_value=1_000.0,
        ):
            value = execution_module._retry_after_seconds(
                "Thu, 01 Jan 1970 00:16:50 GMT"
            )
        self.assertEqual(10.0, value)

    def test_redmine_malformed_200_retains_safe_response_diagnostic(
        self,
    ) -> None:
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"
        work = Path(self.temporary.name) / "malformed-redmine"
        work.mkdir()
        plan = build_fetch_plan(
            source_key=source_key,
            provider="redmine",
            settings={
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": None,
            },
            logical_root=relative_work,
            work_path=relative_work,
        ).to_dict()

        def getter(_url, _headers, _timeout):
            return (
                200,
                b'{"issues":[{"password":"body-secret"}',
                {
                    "Content-Type": "application/json",
                    "X-Api-Key": "response-secret",
                },
            )

        with self.assertRaises(SourceManagerError) as captured:
            execute_fetch_plan(
                plan,
                work,
                {},
                http_get=getter,
                environment={"RAG_REDMINE_API_KEY": "request-secret"},
            )
        diagnostic = captured.exception.diagnostic
        self.assertEqual(200, diagnostic["status"])
        self.assertEqual(
            "response_parse_failed",
            diagnostic["error_kind"],
        )
        self.assertEqual(
            "application/json",
            diagnostic["content_type"],
        )
        self.assertEqual(
            "<REDACTED>",
            diagnostic["request_headers"]["X-Redmine-API-Key"],
        )
        self.assertEqual(
            "<REDACTED>",
            diagnostic["response_headers"]["X-Api-Key"],
        )
        self.assertIn("response_parse_failed", diagnostic["reason"])
        rendered = json.dumps(diagnostic, ensure_ascii=False)
        for secret in ("request-secret", "response-secret", "body-secret"):
            self.assertNotIn(secret, rendered)

    def test_redmine_http_attempt_is_persisted_before_fetch_failure(
        self,
    ) -> None:
        registered = register_source(
            self.db_root,
            source_type="redmine",
            display_name="Issue tracker",
            fetch={
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": None,
            },
        )
        key = registered["local_source_key"]

        def getter(_url, _headers, _timeout):
            return (
                401,
                b'{"password":"body-secret"}',
                {
                    "Content-Type": "application/json",
                    "X-Auth-Token": "response-secret",
                },
            )

        with self.assertRaises(SourceManagerError):
            update_source(
                self.db_root,
                key,
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                http_get=getter,
                environment={"RAG_REDMINE_API_KEY": "request-secret"},
            )
        store = SourceStore(self.db_root)
        relative_events = store.paths(key).events_jsonl
        events_path = store.paths(key).absolute(
            self.db_root,
            relative_events,
        )
        raw = events_path.read_text(encoding="utf-8")
        events = [
            json.loads(line)
            for line in raw.splitlines()
            if line.strip()
        ]
        attempts = [
            item
            for item in events
            if item.get("event") == "redmine.http_attempt"
        ]
        self.assertEqual(1, len(attempts))
        self.assertEqual(401, attempts[0]["details"]["status"])
        self.assertFalse(attempts[0]["details"]["retry"])
        for secret in ("request-secret", "response-secret", "body-secret"):
            self.assertNotIn(secret, raw)

    def test_redmine_network_failure_kinds_and_retry_counts(self) -> None:
        cases = (
            (
                "timeout",
                lambda: TimeoutError("timed out"),
                "connection_timeout",
                3,
            ),
            (
                "dns",
                lambda: urllib.error.URLError(
                    socket.gaierror(-2, "name lookup failed")
                ),
                "dns_resolution_failed",
                3,
            ),
            (
                "tls",
                lambda: urllib.error.URLError(
                    ssl.SSLError("certificate verify failed")
                ),
                "tls_verification_failed",
                1,
            ),
        )
        for name, exception_factory, error_kind, expected_calls in cases:
            with self.subTest(name=name):
                calls = 0
                events: list[dict[str, object]] = []

                def getter(_url, _headers, _timeout):
                    nonlocal calls
                    calls += 1
                    raise exception_factory()

                with (
                    mock.patch.object(execution_module.time, "sleep"),
                    self.assertRaises(SourceManagerError) as captured,
                ):
                    execution_module._get_with_retry(
                        getter,
                        "https://issues.example.invalid/issues.json",
                        {"X-Redmine-API-Key": "secret"},
                        progress_callback=events.append,
                    )
                self.assertEqual(expected_calls, calls)
                self.assertEqual(error_kind, captured.exception.error_kind)
                self.assertEqual(error_kind, events[-1]["error_kind"])
                self.assertFalse(events[-1]["retry"])
                self.assertNotIn(
                    "secret",
                    json.dumps(captured.exception.diagnostic),
                )

    def test_sharepoint_runtime_root_is_not_persisted(self) -> None:
        runtime_root = Path(self.temporary.name).resolve() / "sharepoint"
        settings = {
            "root_env": "RAG_SHAREPOINT_ROOT",
            "relative_path": "Documents",
        }
        resolved = resolve_environment_root(
            settings,
            provider="sharepoint",
            environment={"RAG_SHAREPOINT_ROOT": str(runtime_root)},
        )
        self.assertEqual(runtime_root / "Documents", resolved)
        registered = register_source(
            self.db_root,
            source_type="sharepoint",
            display_name="SharePoint",
            fetch=settings,
        )
        raw = SourceStore(self.db_root).read_source(
            registered["local_source_key"]
        ).path.read_text(encoding="utf-8")
        self.assertNotIn(str(runtime_root), raw)
        with mock.patch.dict(
            os.environ,
            {"RAG_SHAREPOINT_ROOT": str(runtime_root)},
            clear=False,
        ):
            with self.assertRaisesRegex(
                SourceManagerError,
                "unavailable",
            ):
                resolve_environment_root(
                    settings,
                    provider="sharepoint",
                    environment={},
                )

    def test_sharepoint_uses_external_add_root_without_copying(
        self,
    ) -> None:
        runtime_root = Path(self.temporary.name).resolve() / "sharepoint"
        runtime_root.mkdir()
        (runtime_root / "old.md").write_text("old", encoding="utf-8")
        work = Path(self.temporary.name) / "managed" / "ingest" / "source"
        work.mkdir(parents=True)
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"
        plan = build_fetch_plan(
            source_key=source_key,
            provider="sharepoint",
            settings={"root_env": "RAG_SHAREPOINT_ROOT"},
            logical_root=relative_work,
            work_path=relative_work,
        ).to_dict()
        environment = {"RAG_SHAREPOINT_ROOT": str(runtime_root)}
        with mock.patch(
            "source_manager.execution._is_windows",
            return_value=True,
        ):
            outcome = execute_fetch_plan(
                plan,
                work,
                {},
                environment=environment,
            )
        self.assertEqual(str(runtime_root), outcome["external_add_root"])
        self.assertFalse((work / "old.md").exists())
        self.assertEqual([], list(work.iterdir()))

        if hasattr(os, "symlink"):
            outside = Path(self.temporary.name) / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            (runtime_root / "unsafe.md").symlink_to(outside)
            with mock.patch(
                "source_manager.execution._is_windows",
                return_value=True,
            ):
                with self.assertRaises(SourceManagerError):
                    execute_fetch_plan(
                        plan,
                        work,
                        {},
                        environment=environment,
                    )
            self.assertEqual([], list(work.iterdir()))

    def test_sharepoint_runner_passes_only_external_root_to_add(self) -> None:
        runtime_root = Path(self.temporary.name).resolve() / "sharepoint"
        runtime_root.mkdir()
        (runtime_root / "document.md").write_text(
            "fixture",
            encoding="utf-8",
        )
        add_roots: list[Path] = []

        def command(arguments):
            add_roots.append(Path(arguments[arguments.index("--root") + 1]))
            source_id = arguments[arguments.index("--source-id") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_valid_add_summary(source_id)),
                stderr="",
            )

        with mock.patch(
            "source_manager.execution._is_windows",
            return_value=True,
        ):
            result = register_source(
                self.db_root,
                source_type="sharepoint",
                display_name="External synchronized tree",
                fetch={"root_env": "RAG_SHAREPOINT_ROOT"},
                start=True,
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                command_runner=command,
                environment={"RAG_SHAREPOINT_ROOT": str(runtime_root)},
                metadata_publisher=lambda *_: None,
            )
        self.assertEqual("updated", result["status"])
        self.assertEqual([runtime_root], add_roots)
        stored = SourceStore(self.db_root).read_source(
            result["local_source_key"]
        )
        state = SourceStore(self.db_root).read_state(
            result["local_source_key"]
        )
        self.assertNotIn(
            str(runtime_root),
            stored.path.read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            str(runtime_root),
            state.path.read_text(encoding="utf-8"),
        )
        managed_work = self.db_root.joinpath(
            *result["paths"]["work_directory"].split("/")
        )
        self.assertEqual([], list(managed_work.iterdir()))

    def test_github_default_branch_creates_link_and_ssh_stays_pending(self) -> None:
        https_source = register_source(
            self.db_root,
            source_type="github",
            display_name="HTTPS",
            fetch={"repository_url": "https://github.com/example/r.git"},
        )

        def https_executor(plan, work, state):
            self.assertTrue(Path(work).is_dir())
            self.assertEqual("git_fetch", plan["steps"][0]["operation"])
            return {"status": "ok", "default_branch": "trunk", "documents": 1}

        fetched = update_source(
            self.db_root,
            https_source["local_source_key"],
            executor=https_executor,
        )
        self.assertTrue(fetched["link_configured"])
        payload = SourceStore(self.db_root).read_source(
            https_source["local_source_key"]
        ).payload
        self.assertEqual(
            "trunk",
            payload["pending_metadata"]["link"]["settings"]["ref"],
        )
        self.assertEqual(
            "https://github.com/example/r",
            payload["pending_metadata"]["link"]["settings"]["repository_url"],
        )

        ssh_source = register_source(
            self.db_root,
            source_type="github",
            display_name="SSH",
            fetch={"repository_url": "git@git.example.invalid:o/r.git"},
        )
        pending = update_source(
            self.db_root,
            ssh_source["local_source_key"],
            executor=lambda *_: {
                "status": "ok",
                "default_branch": "main",
                "documents": 1,
            },
        )
        self.assertFalse(pending["link_configured"])
        state = SourceStore(self.db_root).read_state(
            ssh_source["local_source_key"]
        ).payload
        self.assertTrue(state["link_configuration_pending"])

        enterprise_source = register_source(
            self.db_root,
            source_type="github",
            display_name="Enterprise HTTPS",
            fetch={
                "repository_url": "https://git.example.invalid/o/r.git"
            },
        )
        enterprise = update_source(
            self.db_root,
            enterprise_source["local_source_key"],
            executor=lambda *_: {
                "status": "ok",
                "default_branch": "main",
                "documents": 1,
            },
        )
        self.assertFalse(enterprise["link_configured"])
        enterprise_state = SourceStore(self.db_root).read_state(
            enterprise_source["local_source_key"]
        ).payload
        self.assertTrue(enterprise_state["link_configuration_pending"])

    def test_github_refresh_does_not_replace_human_link_metadata(self) -> None:
        source = register_source(
            self.db_root,
            source_type="github",
            display_name="HTTPS",
            fetch={"repository_url": "https://github.com/example/r.git"},
        )
        key = source["local_source_key"]
        first = update_source(
            self.db_root,
            key,
            executor=lambda *_: {
                "status": "ok",
                "default_branch": "initial",
            },
        )
        store = SourceStore(self.db_root)
        loaded = store.read_source(key)
        confirmed = store.confirm_source_id(
            key,
            key,
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )
        store.mark_metadata_synced(
            key,
            expected_revision=confirmed.revision,
            expected_etag=confirmed.etag,
        )
        refreshed = update_source(
            self.db_root,
            key,
            executor=lambda *_: {
                "status": "ok",
                "default_branch": "new-default",
            },
        )
        self.assertEqual("fetched", refreshed["status"])
        payload = store.read_source(key).payload
        self.assertNotIn("pending_metadata", payload)
        self.assertFalse(payload["metadata_sync_pending"])

    def test_metadata_without_pending_link_preserves_canonical_link(self) -> None:
        current = {
            "source_id": "indexed-source",
            "display_name": "Human name",
            "source_type": "github",
            "link": {
                "enabled": True,
                "strategy": "github-blob",
                "settings": {
                    "repository_url": "https://git.example.invalid/o/r",
                    "ref": "human-selected",
                    "permalink_enabled": False,
                },
            },
        }
        source = {
            "source_id": "indexed-source",
            "display_name": "Updated display",
            "source_type": "github",
        }
        value = _canonical_source(source, current_source=current)
        self.assertEqual(current["link"], value["link"])

    def test_metadata_repairs_legacy_generated_redmine_canonical_link(
        self,
    ) -> None:
        current = {
            "source_id": "indexed-source",
            "display_name": "Issue tracker",
            "source_type": "redmine",
            "link": {
                "enabled": True,
                "strategy": "regex-template",
                "settings": {
                    "path_pattern": (
                        r"^issues/(?P<issue_id>[0-9]+)\.md$"
                    ),
                    "url_template": (
                        "https://issues.example.invalid/issues/{issue_id}"
                    ),
                },
            },
        }
        source = {
            "source_id": "indexed-source",
            "display_name": "Mounted issue tracker",
            "source_type": "redmine",
            "fetch": {
                "project_url": (
                    "https://issues.example.invalid/redmine/"
                    "projects/fixture"
                )
            },
        }
        value = _canonical_source(source, current_source=current)
        self.assertEqual(
            (
                "https://issues.example.invalid/redmine/"
                "issues/{issue_id}"
            ),
            value["link"]["settings"]["url_template"],
        )

    def test_metadata_generates_gitlab_issue_link_without_secrets(
        self,
    ) -> None:
        source = {
            "source_id": "indexed-source",
            "display_name": "GitLab tickets",
            "source_type": "gitlab_issues",
            "fetch": {
                "project_url": (
                    "https://gitlab.example.invalid/group/project"
                ),
                "gitlab_url": "https://gitlab.example.invalid",
                "token_env": "LOCAL_RAG_GITLAB_TOKEN_SECRET",
            },
            "pending_metadata": {
                "source_type": "gitlab_issues",
                "link": {
                    "enabled": True,
                    "strategy": "regex-template",
                    "settings": {
                        "path_pattern": (
                            r"^issues/(?P<issue_iid>[0-9]+)\.md$"
                        ),
                        "url_template": (
                            "https://gitlab.example.invalid/group/project/"
                            "-/issues/{issue_iid}"
                        ),
                    },
                },
            },
        }

        value = _canonical_source(source)

        self.assertEqual(
            {
                "enabled": True,
                "strategy": "regex-template",
                "settings": {
                    "path_pattern": (
                        r"^issues/(?P<issue_iid>[0-9]+)\.md$"
                    ),
                    "url_template": (
                        "https://gitlab.example.invalid/group/project/"
                        "-/issues/{issue_iid}"
                    ),
                },
            },
            value["link"],
        )
        encoded = json.dumps(value, ensure_ascii=False)
        self.assertNotIn("api/v4", encoded)
        self.assertNotIn("TOKEN_SECRET", encoded)
        self.assertNotIn("project_id", encoded)

    def test_metadata_does_not_restore_a_removed_gitlab_issue_link(
        self,
    ) -> None:
        source = {
            "source_id": "indexed-source",
            "display_name": "GitLab tickets",
            "source_type": "gitlab_issues",
            "fetch": {
                "project_url": (
                    "https://gitlab.example.invalid/group/project"
                ),
                "gitlab_url": "https://gitlab.example.invalid",
                "token_env": "LOCAL_RAG_GITLAB_TOKEN_SECRET",
            },
        }
        current = {
            "source_id": "indexed-source",
            "display_name": "GitLab tickets",
            "source_type": "gitlab_issues",
        }

        value = _canonical_source(source, current_source=current)

        self.assertNotIn("link", value)

    def test_pending_metadata_publishes_to_canonical_sidecar(self) -> None:
        stored = SourceStore(self.db_root).create_source(
            source_type="github",
            display_name="Canonical metadata",
            fetch={"repository_url": "https://github.com/example/r.git"},
            source_id="indexed-source",
            link={
                "enabled": True,
                "strategy": "github-blob",
                "settings": {
                    "repository_url": "https://github.com/example/r",
                    "ref": "main",
                    "permalink_enabled": False,
                },
            },
        )
        catalog = sqlite3.connect(self.db_root / "catalog.sqlite")
        try:
            catalog.execute(
                """
                CREATE TABLE document (
                    doc_pk INTEGER PRIMARY KEY,
                    source_id TEXT,
                    path TEXT NOT NULL,
                    visible_until INTEGER
                )
                """
            )
            catalog.execute(
                "INSERT INTO document VALUES (?, ?, ?, NULL)",
                (
                    1,
                    "indexed-source",
                    f"{stored.payload['local_source_key']}/document.md",
                ),
            )
            catalog.commit()
        finally:
            catalog.close()
        rag_root = Path(__file__).resolve().parents[2]
        publish_source_metadata(
            self.db_root,
            stored.payload,
            rag_root,
        )
        sidecar = json.loads(
            (self.db_root / "source-links.json").read_text(encoding="utf-8")
        )
        self.assertEqual("rag-source-metadata-v1", sidecar["schema_version"])
        self.assertEqual("indexed-source", sidecar["sources"][0]["source_id"])
        self.assertEqual(
            "github-blob",
            sidecar["sources"][0]["link"]["strategy"],
        )

    def test_other_runtime_path_is_redacted_after_success(self) -> None:
        runtime = Path(self.temporary.name).resolve() / "incoming"
        runtime.mkdir()
        source = register_source(
            self.db_root,
            source_type="other",
            display_name="Other",
            fetch={"runtime_path": str(runtime)},
        )
        key = source["local_source_key"]
        initial = SourceStore(self.db_root).read_state(key).payload
        self.assertEqual(str(runtime), initial["runtime"]["input_path"])
        update_source(
            self.db_root,
            key,
            executor=lambda *_: {"status": "ok", "documents": 1},
        )
        final = SourceStore(self.db_root).read_state(key).payload
        self.assertEqual(
            "<REDACTED_AFTER_IMPORT>",
            final["runtime"]["input_path"],
        )
        raw = SourceStore(self.db_root).read_state(key).path.read_text(
            encoding="utf-8"
        )
        self.assertNotIn(str(runtime), raw)

    def test_invalid_runtime_input_leaves_no_partial_registration(self) -> None:
        sources = self.db_root / "sources"
        with self.assertRaisesRegex(
            SourceManagerError,
            "supported only for Other",
        ):
            register_source(
                self.db_root,
                source_type="github",
                display_name="Invalid runtime",
                fetch={
                    "repository_url": "https://git.example.invalid/o/r"
                },
                runtime_input=Path(self.temporary.name).resolve(),
            )
        self.assertFalse(sources.exists())

        with self.assertRaisesRegex(SourceManagerError, "must be absolute"):
            register_source(
                self.db_root,
                source_type="other",
                display_name="Relative runtime",
                fetch={},
                runtime_input=Path("relative-input"),
            )
        self.assertFalse(sources.exists())

    def test_redmine_none_means_no_updated_time_restriction(self) -> None:
        normalized = validate_provider_config(
            "redmine",
            {
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": None,
            },
        )
        self.assertIsNone(normalized["updated_within_days"])

    def test_redmine_api_uses_project_mount_and_absolute_cutoff(self) -> None:
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"
        work = Path(self.temporary.name) / "mounted-redmine"
        work.mkdir()
        plan = build_fetch_plan(
            source_key=source_key,
            provider="redmine",
            settings={
                "project_url": (
                    "https://issues.example.invalid:8443/redmine/"
                    "projects/fixture/"
                ),
                "updated_within_days": 30,
            },
            logical_root=relative_work,
            work_path=relative_work,
        ).to_dict()
        calls: list[str] = []

        def getter(url, _headers, _timeout):
            calls.append(url)
            return (
                200,
                json.dumps({"issues": [], "total_count": 0}).encode(),
            )

        execute_fetch_plan(
            plan,
            work,
            {},
            http_get=getter,
            environment={"RAG_REDMINE_API_KEY": "fixture-key"},
            clock=lambda: datetime(
                2026, 7, 29, 12, 0, tzinfo=timezone.utc
            ),
        )

        self.assertEqual(1, len(calls))
        parsed = urlsplit(calls[0])
        self.assertEqual("/redmine/issues.json", parsed.path)
        self.assertEqual(
            [">=2026-06-29"],
            parse_qs(parsed.query)["updated_on"],
        )

    def test_redmine_inventory_reports_each_completed_page(self) -> None:
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"
        work = Path(self.temporary.name) / "redmine-page-progress"
        work.mkdir()
        plan = build_fetch_plan(
            source_key=source_key,
            provider="redmine",
            settings={
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": None,
                "api_key_env": "LOCAL_RAG_REDMINE_API_KEY",
            },
            logical_root=relative_work,
            work_path=relative_work,
        ).to_dict()
        events: list[dict[str, object]] = []

        def getter(url, _headers, _timeout):
            parsed = urlsplit(url)
            if parsed.path.endswith("/issues.json"):
                offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
                issue_ids = range(1, 6) if offset == 0 else range(6, 7)
                return (
                    200,
                    json.dumps(
                        {
                            "issues": [{"id": value} for value in issue_ids],
                            "total_count": 6,
                        }
                    ).encode(),
                )
            issue_id = int(parsed.path.rsplit("/", 1)[-1].split(".", 1)[0])
            return (
                200,
                json.dumps(
                    {"issue": {"id": issue_id, "subject": f"Issue {issue_id}"}}
                ).encode(),
            )

        execute_fetch_plan(
            plan,
            work,
            {},
            http_get=getter,
            environment={"LOCAL_RAG_REDMINE_API_KEY": "fixture-key"},
            progress_callback=events.append,
        )
        inventory = [
            event
            for event in events
            if event.get("event") == "provider.page"
        ]
        self.assertEqual([5, 6], [event["completed"] for event in inventory])
        self.assertTrue(
            all(event["total"] == 6 for event in inventory)
        )
        self.assertTrue(
            all(event["total_kind"] == "exact" for event in inventory)
        )

    def test_large_file_copy_reports_exact_byte_progress(self) -> None:
        source = Path(self.temporary.name).resolve() / "large-source.bin"
        source.write_bytes(b"x" * (2 * 1024 * 1024 + 17))
        destination = Path(self.temporary.name) / "copy-destination"
        destination.mkdir()
        events: list[dict[str, object]] = []

        result = execution_module._copy_tree(
            source,
            destination,
            progress_callback=events.append,
            provider="other",
        )

        copied = destination / source.name
        self.assertEqual(source.read_bytes(), copied.read_bytes())
        self.assertEqual(1, result["documents"])
        byte_events = [
            event
            for event in events
            if event.get("event") == "provider.bytes"
        ]
        self.assertGreaterEqual(len(byte_events), 3)
        self.assertEqual(source.stat().st_size, byte_events[-1]["completed"])
        self.assertEqual(source.stat().st_size, byte_events[-1]["total"])
        self.assertTrue(
            all(event["total_kind"] == "exact" for event in byte_events)
        )

    def test_redmine_fetches_stable_detailed_markdown_serially(self) -> None:
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"
        work = Path(self.temporary.name) / "managed" / "ingest" / "source"
        work.mkdir(parents=True)
        plan = build_fetch_plan(
            source_key=source_key,
            provider="redmine",
            settings={
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": None,
                "api_key_env": "LOCAL_RAG_REDMINE_API_KEY",
            },
            logical_root=relative_work,
            work_path=relative_work,
        ).to_dict()
        calls: list[str] = []

        def getter(url, headers, _timeout):
            calls.append(url)
            self.assertEqual(
                "fixture-key",
                headers["X-Redmine-API-Key"],
            )
            if "/issues.json?" in url:
                self.assertIn("status_id=%2A", url)
                self.assertIn("sort=updated_on%3Aasc%2Cid%3Aasc", url)
                self.assertNotIn("updated_on=%", url)
                return (
                    200,
                    json.dumps(
                        {
                            "issues": [{"id": 1}, {"id": 2}],
                            "total_count": 2,
                        }
                    ).encode(),
                )
            issue_id = int(url.split("/issues/", 1)[1].split(".", 1)[0])
            return (
                200,
                json.dumps(
                    {
                        "issue": {
                            "id": issue_id,
                            "subject": f"Subject {issue_id}",
                            "description": "Detail",
                            "journals": [{"id": issue_id * 10}],
                            "relations": [],
                            "attachments": [
                                {"id": issue_id, "filename": "fixture.txt"}
                            ],
                            "custom_fields": [],
                        }
                    }
                ).encode(),
            )

        outcome = execute_fetch_plan(
            plan,
            work,
            {},
            http_get=getter,
            environment={"LOCAL_RAG_REDMINE_API_KEY": "fixture-key"},
        )
        self.assertEqual(2, outcome["documents"])
        self.assertEqual(
            ["/issues/1.json", "/issues/2.json"],
            [
                url.split("?", 1)[0].removeprefix(
                    "https://issues.example.invalid"
                )
                for url in calls[1:]
            ],
        )
        for issue_id in (1, 2):
            text = (work / "issues" / f"{issue_id}.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"# Issue {issue_id}", text)
            self.assertIn('"journals"', text)
            self.assertIn('"attachments"', text)

    def test_redmine_reflects_five_and_resumes_only_unconfirmed_batch(
        self,
    ) -> None:
        issue_ids = list(range(1, 13))
        detail_calls: list[int] = []
        list_calls = 0
        add_calls = 0
        fail_second_add = True

        def getter(url, _headers, _timeout):
            nonlocal list_calls
            split = urlsplit(url)
            if split.path == "/issues.json":
                list_calls += 1
                query = parse_qs(split.query)
                offset = int(query["offset"][0])
                page = issue_ids[offset : offset + 5]
                return (
                    200,
                    json.dumps(
                        {
                            "issues": [{"id": value} for value in page],
                            "total_count": len(issue_ids),
                        }
                    ).encode(),
                )
            issue_id = int(split.path.split("/")[-1].split(".")[0])
            detail_calls.append(issue_id)
            return (
                200,
                json.dumps(
                    {
                        "issue": {
                            "id": issue_id,
                            "subject": f"Issue {issue_id}",
                            "description": "Detail",
                        }
                    }
                ).encode(),
            )

        def add(arguments):
            nonlocal add_calls, fail_second_add
            add_calls += 1
            if fail_second_add and add_calls == 2:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="fixture failure",
                )
            source_id = arguments[arguments.index("--source-id") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_valid_add_summary(source_id)),
                stderr="",
            )

        with self.assertRaisesRegex(
            SourceManagerError,
            "ADD failed",
        ) as captured:
            register_source(
                self.db_root,
                source_type="redmine",
                display_name="Issue tracker",
                fetch={
                    "project_url": (
                        "https://issues.example.invalid/projects/fixture"
                    ),
                    "updated_within_days": 30,
                    "api_key_env": "LOCAL_RAG_REDMINE_API_KEY",
                },
                start=True,
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                command_runner=add,
                http_get=getter,
                environment={"LOCAL_RAG_REDMINE_API_KEY": "fixture-key"},
                metadata_publisher=lambda *_: None,
                clock=lambda: datetime(
                    2025, 3, 10, 12, 0, tzinfo=timezone.utc
                ),
            )
        self.assertIn("終了コード: 1", str(captured.exception))
        self.assertIn("fixture failure", str(captured.exception))
        self.assertEqual("reflect.add", captured.exception.stage)
        self.assertTrue(captured.exception.source_saved)
        self.assertTrue(captured.exception.events_jsonl.endswith("events.jsonl"))
        registered = list_sources(self.db_root)[0]
        interrupted = SourceStore(self.db_root).read_state(
            registered["local_source_key"]
        ).payload
        self.assertEqual("reflect", interrupted["phase"])
        self.assertEqual(10, interrupted["fetched_count"])
        self.assertEqual(5, interrupted["indexed_confirmed_count"])
        self.assertEqual(5, interrupted["pending_count"])
        self.assertEqual(issue_ids, interrupted["redmine_issue_ids"])
        self.assertEqual(
            "2025-02-08",
            interrupted["redmine_updated_on_cutoff"],
        )
        self.assertIn("fixture failure", interrupted["last_error"])
        self.assertEqual(list(range(1, 11)), detail_calls)
        self.assertEqual(3, list_calls)

        fail_second_add = False
        issue_ids[:] = [0, *range(1, 13)]
        result = update_source(
            self.db_root,
            registered["local_source_key"],
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            command_runner=add,
            http_get=getter,
            environment={"LOCAL_RAG_REDMINE_API_KEY": "fixture-key"},
            metadata_publisher=lambda *_: None,
            clock=lambda: datetime(
                2027, 1, 1, 12, 0, tzinfo=timezone.utc
            ),
        )
        self.assertEqual("updated", result["status"])
        self.assertEqual(12, result["indexed_confirmed_count"])
        self.assertEqual(list(range(1, 13)), detail_calls)
        self.assertEqual(3, list_calls)
        self.assertEqual(4, add_calls)
        final = SourceStore(self.db_root).read_state(
            registered["local_source_key"]
        ).payload
        self.assertEqual("complete", final["phase"])
        self.assertFalse(final["can_resume"])
        self.assertEqual(
            "2025-02-08",
            final["redmine_updated_on_cutoff"],
        )

    def test_diagnostic_text_redacts_credentials_but_keeps_failure(self) -> None:
        detail = sanitize_diagnostic(
            "fatal: connection refused "
            "https://user:password@example.invalid/repository\n"
            "X-Redmine-API-Key=secret-value\n"
            'Authorization: Bearer bearer-secret\n'
            '"password": "json-secret"'
        )
        self.assertIn("connection refused", detail)
        self.assertNotIn("user:password", detail)
        self.assertNotIn("secret-value", detail)
        self.assertNotIn("bearer-secret", detail)
        self.assertNotIn("json-secret", detail)
        self.assertIn("<REDACTED>", detail)

    def test_redmine_shorter_window_retains_previously_fetched_issues(
        self,
    ) -> None:
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"
        work = Path(self.temporary.name) / "managed" / "ingest" / "source"
        (work / "issues").mkdir(parents=True)
        retained = work / "issues" / "99.md"
        retained.write_text("previously fetched", encoding="utf-8")
        plan = build_fetch_plan(
            source_key=source_key,
            provider="redmine",
            settings={
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": 30,
            },
            logical_root=relative_work,
            work_path=relative_work,
        ).to_dict()

        def getter(url, _headers, _timeout):
            split = urlsplit(url)
            if split.path == "/issues.json":
                return (
                    200,
                    json.dumps(
                        {
                            "issues": [{"id": 1}],
                            "total_count": 1,
                        }
                    ).encode(),
                )
            return (
                200,
                json.dumps(
                    {
                        "issue": {
                            "id": 1,
                            "subject": "Current",
                            "description": "Current window",
                        }
                    }
                ).encode(),
            )

        execute_fetch_plan(
            plan,
            work,
            {},
            http_get=getter,
            environment={"RAG_REDMINE_API_KEY": "fixture-key"},
        )
        self.assertEqual(
            "previously fetched",
            retained.read_text(encoding="utf-8"),
        )

    def test_redmine_inventory_mutation_stops_before_detail_fetch(self) -> None:
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"
        work = Path(self.temporary.name) / "managed" / "ingest" / "source"
        work.mkdir(parents=True)
        plan = build_fetch_plan(
            source_key=source_key,
            provider="redmine",
            settings={
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": None,
            },
            logical_root=relative_work,
            work_path=relative_work,
        ).to_dict()
        detail_calls = 0

        def getter(url, _headers, _timeout):
            nonlocal detail_calls
            split = urlsplit(url)
            if split.path != "/issues.json":
                detail_calls += 1
                raise AssertionError("detail retrieval must not start")
            offset = int(parse_qs(split.query)["offset"][0])
            page = [1, 2, 3, 4, 5] if offset == 0 else [5]
            return (
                200,
                json.dumps(
                    {
                        "issues": [{"id": issue_id} for issue_id in page],
                        "total_count": 6,
                    }
                ).encode(),
            )

        with self.assertRaisesRegex(
            SourceManagerError,
            "redmine_inventory_changed",
        ):
            execute_fetch_plan(
                plan,
                work,
                {},
                http_get=getter,
                environment={"RAG_REDMINE_API_KEY": "fixture-key"},
            )
        self.assertEqual(0, detail_calls)
        self.assertEqual([], list((work / "issues").iterdir()))

    def test_list_and_update_all_return_manager_dtos(self) -> None:
        for name in ("One", "Two"):
            register_source(
                self.db_root,
                source_type="other",
                display_name=name,
                fetch={},
            )
        listed = list_sources(self.db_root)
        self.assertEqual(2, len(listed))
        result = update_all_sources(self.db_root)
        self.assertEqual("ok", result["status"])
        self.assertEqual(2, result["source_count"])
        self.assertTrue(
            all(item["status"] == "planned" for item in result["results"])
        )
        for item in result["results"]:
            arguments = item["add_request"]["arguments"]
            self.assertNotIn("--source-type", arguments)
            self.assertIn("--root", arguments)
            self.assertIn("--source-id", arguments)

    def test_update_all_skip_policy_and_snapshot_marker(self) -> None:
        other = register_source(
            self.db_root,
            source_type="other",
            display_name="Completed one shot",
            fetch={"one_shot": True},
        )
        store = SourceStore(self.db_root)
        loaded = store.read_source(other["local_source_key"])
        confirmed = store.confirm_source_id(
            other["local_source_key"],
            "completed-other",
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )
        store.mark_metadata_synced(
            other["local_source_key"],
            expected_revision=confirmed.revision,
            expected_etag=confirmed.etag,
        )
        register_source(
            self.db_root,
            source_type="sharepoint",
            display_name="Windows sync",
            fetch={"root_env": "RAG_SHAREPOINT_ROOT"},
        )
        with mock.patch(
            "source_manager.runner._is_windows",
            return_value=False,
        ):
            result = update_all_sources(self.db_root)
        self.assertEqual("ok", result["status"])
        reasons = {
            item.get("skip_reason")
            for item in result["results"]
        }
        self.assertEqual(
            {
                "one_shot_source_complete",
                "sharepoint_update_requires_windows",
            },
            reasons,
        )
        self.assertFalse(result["snapshot_marker_eligible"])

    def test_start_true_fetches_then_runs_trusted_add(self) -> None:
        runtime = Path(self.temporary.name).resolve() / "incoming"
        runtime.mkdir()
        observed: list[list[str]] = []

        def add_runner(arguments):
            observed.append(list(arguments))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    _valid_add_summary(
                        arguments[arguments.index("--source-id") + 1]
                    )
                ),
                stderr="",
            )

        result = register_source(
            self.db_root,
            source_type="other",
            display_name="Immediate import",
            fetch={},
            runtime_input=runtime,
            start=True,
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            executor=lambda *_: {"status": "ok", "documents": 1},
            command_runner=add_runner,
            metadata_publisher=lambda *_: None,
        )
        self.assertEqual("updated", result["status"])
        self.assertEqual(result["local_source_key"], result["source_id"])
        self.assertEqual(1, len(observed))
        arguments = observed[0]
        self.assertNotIn("--source-type", arguments)
        self.assertEqual(
            result["local_source_key"],
            arguments[arguments.index("--source-id") + 1],
        )
        self.assertEqual(
            result["paths"]["logical_root_name"],
            Path(arguments[arguments.index("--root") + 1]).name,
        )
        marker = json.loads(
            (self.db_root / "rag-wrapper.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            "local-rag.wrapper.v1",
            marker["schema_version"],
        )
        self.assertEqual(
            "initial_database_reflection",
            marker["reason"],
        )
        self.assertTrue(marker["content_snapshot_at"].endswith("Z"))
        marker_before = (
            self.db_root / "rag-wrapper.json"
        ).read_bytes()
        second_runtime = (
            Path(self.temporary.name).resolve() / "incoming-second"
        )
        second_runtime.mkdir()
        register_source(
            self.db_root,
            source_type="other",
            display_name="Second import",
            fetch={},
            runtime_input=second_runtime,
            start=True,
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            executor=lambda *_: {"status": "ok", "documents": 1},
            command_runner=add_runner,
            metadata_publisher=lambda *_: None,
        )
        self.assertEqual(
            marker_before,
            (self.db_root / "rag-wrapper.json").read_bytes(),
        )

    def test_untrusted_add_output_does_not_assign_source_id(self) -> None:
        runtime = Path(self.temporary.name).resolve() / "incoming"
        runtime.mkdir()

        with self.assertRaisesRegex(
            SourceManagerError,
            "trusted JSON",
        ):
            register_source(
                self.db_root,
                source_type="other",
                display_name="Failed import",
                fetch={},
                runtime_input=runtime,
                start=True,
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                executor=lambda *_: {"status": "ok", "documents": 1},
                command_runner=lambda _: SimpleNamespace(
                    returncode=0,
                    stdout="not-json",
                    stderr="",
                ),
            )
        listed = list_sources(self.db_root)
        self.assertEqual(1, len(listed))
        self.assertIsNone(listed[0]["source_id"])
        self.assertFalse((self.db_root / "rag-wrapper.json").exists())

    def test_metadata_sync_failure_resumes_without_fetch_or_add(self) -> None:
        runtime = Path(self.temporary.name).resolve() / "incoming"
        runtime.mkdir()
        calls = {"fetch": 0, "add": 0, "metadata": 0}

        def fetch(*_):
            calls["fetch"] += 1
            return {"status": "ok", "documents": 1}

        def add(_):
            calls["add"] += 1
            key = list_sources(self.db_root)[0]["local_source_key"]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "operation": "add",
                        "source_id": key,
                        "file_count": 1,
                        "indexed_files": 1,
                        "skipped_files": 0,
                        "error_files": 0,
                        "upserted_records": 1,
                        "deleted_records": 0,
                    }
                ),
                stderr="",
            )

        def fail_metadata(*_):
            calls["metadata"] += 1
            raise SourceManagerError("sidecar unavailable")

        first = register_source(
            self.db_root,
            source_type="other",
            display_name="Pending metadata",
            fetch={},
            runtime_input=runtime,
            start=True,
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            executor=fetch,
            command_runner=add,
            metadata_publisher=fail_metadata,
        )
        self.assertEqual("metadata_sync_pending", first["status"])
        self.assertEqual({"fetch": 1, "add": 1, "metadata": 1}, calls)

        resumed = update_source(
            self.db_root,
            first["local_source_key"],
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            executor=fetch,
            command_runner=add,
            metadata_publisher=lambda *_: calls.__setitem__(
                "metadata", calls["metadata"] + 1
            ),
        )
        self.assertEqual("updated", resumed["status"])
        self.assertEqual("metadata_sync", resumed["resumed_operation"])
        self.assertEqual({"fetch": 1, "add": 1, "metadata": 2}, calls)
        stored = SourceStore(self.db_root).read_source(
            first["local_source_key"]
        ).payload
        self.assertFalse(stored["metadata_sync_pending"])
        self.assertNotIn("pending_metadata", stored)

    def test_fetched_symlink_is_rejected_before_add(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        outside = Path(self.temporary.name).resolve() / "external.md"
        outside.write_text("not part of the Source", encoding="utf-8")
        add_calls = 0

        def fetch(_plan, work, _state):
            (Path(work) / "linked.md").symlink_to(outside)
            return {"status": "ok", "documents": 1}

        def add(_arguments):
            nonlocal add_calls
            add_calls += 1
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with self.assertRaisesRegex(SourceManagerError, "links"):
            register_source(
                self.db_root,
                source_type="other",
                display_name="Unsafe fetched tree",
                fetch={},
                runtime_input=Path(self.temporary.name).resolve(),
                start=True,
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                executor=fetch,
                command_runner=add,
            )
        self.assertEqual(0, add_calls)

    def test_other_single_file_is_materialized_under_fixed_root(self) -> None:
        incoming = Path(self.temporary.name).resolve() / "single file.md"
        incoming.write_text("fixture", encoding="utf-8")
        add_roots: list[Path] = []

        def command(arguments):
            add_roots.append(Path(arguments[arguments.index("--root") + 1]))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    _valid_add_summary(
                        arguments[arguments.index("--source-id") + 1]
                    )
                ),
                stderr="",
            )

        result = register_source(
            self.db_root,
            source_type="other",
            display_name="Single file",
            fetch={"one_shot": True},
            runtime_input=incoming,
            start=True,
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            command_runner=command,
            metadata_publisher=lambda *_: None,
        )
        self.assertEqual("updated", result["status"])
        self.assertEqual(1, len(add_roots))
        self.assertTrue((add_roots[0] / incoming.name).is_file())
        self.assertEqual(result["local_source_key"], add_roots[0].name)

    def test_completed_other_accepts_new_one_shot_runtime_input(self) -> None:
        first_input = Path(self.temporary.name).resolve() / "first.md"
        second_input = Path(self.temporary.name).resolve() / "second.md"
        first_input.write_text("first", encoding="utf-8")
        second_input.write_text("second", encoding="utf-8")

        def command(arguments):
            source_id = arguments[arguments.index("--source-id") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_valid_add_summary(source_id)),
                stderr="",
            )

        registered = register_source(
            self.db_root,
            source_type="other",
            display_name="Repeatable one shot",
            fetch={"one_shot": True},
            runtime_input=first_input,
            start=True,
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            command_runner=command,
            metadata_publisher=lambda *_: None,
        )
        updated = update_source(
            self.db_root,
            registered["local_source_key"],
            runtime_input=second_input,
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            command_runner=command,
            metadata_publisher=lambda *_: None,
        )
        self.assertEqual("updated", updated["status"])
        work = self.db_root.joinpath(
            *updated["paths"]["work_directory"].split("/")
        )
        self.assertTrue((work / second_input.name).is_file())
        raw = SourceStore(self.db_root).read_state(
            updated["local_source_key"]
        ).path.read_text(encoding="utf-8")
        self.assertNotIn(str(second_input), raw)

    def test_configuration_update_preserves_identity_and_rejects_active_run(
        self,
    ) -> None:
        registered = register_source(
            self.db_root,
            source_type="github",
            display_name="Original",
            fetch={"repository_url": "https://git.example.invalid/o/one"},
        )
        updated = update_source_configuration(
            self.db_root,
            registered["local_source_key"],
            fetch={"repository_url": "https://git.example.invalid/o/two"},
            display_name="Edited",
        )
        self.assertEqual(
            registered["local_source_key"],
            updated["local_source_key"],
        )
        self.assertIsNone(updated["source_id"])
        update_source(self.db_root, registered["local_source_key"])
        state = SourceStore(self.db_root).read_state(
            registered["local_source_key"]
        )
        active = dict(state.payload)
        active["status"] = "running"
        active["phase"] = "fetch"
        SourceStore(self.db_root).save_state(
            registered["local_source_key"],
            active,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
        with self.assertRaisesRegex(SourceManagerError, "resumed"):
            update_source_configuration(
                self.db_root,
                registered["local_source_key"],
                fetch={
                    "repository_url": "https://git.example.invalid/o/three"
                },
            )

    def test_indexed_sharepoint_ingestion_root_cannot_be_changed(self) -> None:
        registered = register_source(
            self.db_root,
            source_type="sharepoint",
            display_name="Synchronized Source",
            fetch={
                "root_env": "RAG_SHAREPOINT_ROOT",
                "relative_path": "Documents/One",
            },
        )
        store = SourceStore(self.db_root)
        loaded = store.read_source(registered["local_source_key"])
        confirmed = store.confirm_source_id(
            registered["local_source_key"],
            "indexed-sharepoint",
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )
        store.mark_metadata_synced(
            registered["local_source_key"],
            expected_revision=confirmed.revision,
            expected_etag=confirmed.etag,
        )
        with self.assertRaisesRegex(
            SourceManagerError,
            "add_new_source",
        ):
            update_source_configuration(
                self.db_root,
                registered["local_source_key"],
                fetch={
                    "root_env": "RAG_SHAREPOINT_ROOT",
                    "relative_path": "Documents/Two",
                },
            )
        unchanged = store.read_source(
            registered["local_source_key"]
        ).payload
        self.assertEqual(
            "Documents/One",
            unchanged["fetch"]["relative_path"],
        )

    def test_add_json_without_source_id_is_not_trusted(self) -> None:
        runtime = Path(self.temporary.name).resolve() / "incoming"
        runtime.mkdir()
        with self.assertRaisesRegex(SourceManagerError, "trusted source_id"):
            register_source(
                self.db_root,
                source_type="other",
                display_name="Missing identity",
                fetch={},
                runtime_input=runtime,
                start=True,
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                executor=lambda *_: {"status": "ok"},
                command_runner=lambda _: SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"status": "ok"}),
                    stderr="",
                ),
            )
        self.assertIsNone(list_sources(self.db_root)[0]["source_id"])

    def test_add_failure_resumes_reflection_without_refetch(self) -> None:
        runtime = Path(self.temporary.name).resolve() / "incoming"
        runtime.mkdir()
        calls = {"fetch": 0, "add": 0}

        def fetch(*_):
            calls["fetch"] += 1
            return {"status": "ok", "documents": 3}

        def add(arguments):
            calls["add"] += 1
            source_id = arguments[arguments.index("--source-id") + 1]
            payload = (
                {"status": "ok"}
                if calls["add"] == 1
                else _valid_add_summary(source_id, file_count=3)
            )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )

        with self.assertRaises(SourceManagerError):
            register_source(
                self.db_root,
                source_type="other",
                display_name="Resume ADD",
                fetch={},
                runtime_input=runtime,
                start=True,
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                executor=fetch,
                command_runner=add,
            )
        registered = list_sources(self.db_root)[0]
        state = SourceStore(self.db_root).read_state(
            registered["local_source_key"]
        ).payload
        required = {
            "operation",
            "phase",
            "started_at",
            "updated_at",
            "last_completed_item",
            "fetched_count",
            "indexed_confirmed_count",
            "pending_count",
            "can_resume",
            "metadata_sync_pending",
            "last_error",
        }
        self.assertTrue(required.issubset(state))
        self.assertEqual("reflect", state["phase"])
        self.assertEqual(3, state["pending_count"])
        self.assertTrue(state["can_resume"])

        result = update_source(
            self.db_root,
            registered["local_source_key"],
            python_executable=Path(self.temporary.name) / "venv-python",
            rag_root=Path(self.temporary.name) / "rag-runtime",
            executor=fetch,
            command_runner=add,
            metadata_publisher=lambda *_: None,
        )
        self.assertEqual("updated", result["status"])
        self.assertEqual("add", result["resumed_operation"])
        self.assertEqual({"fetch": 1, "add": 2}, calls)
        final = SourceStore(self.db_root).read_state(
            registered["local_source_key"]
        ).payload
        self.assertEqual(3, final["indexed_confirmed_count"])
        self.assertEqual(0, final["pending_count"])
        self.assertFalse(final["can_resume"])

    def test_git_fetch_updates_worktree_with_external_control_dir(self) -> None:
        work = Path(self.temporary.name) / "managed" / "ingest" / "source"
        work.mkdir(parents=True)
        plan = build_fetch_plan(
            source_key="src_fixture-0123456789ab",
            provider="github",
            settings={
                "repository_url": "https://git.example.invalid/o/r.git"
            },
            logical_root="sources/src_fixture-0123456789ab/work/ingest/"
            "src_fixture-0123456789ab",
            work_path="sources/src_fixture-0123456789ab/work/ingest/"
            "src_fixture-0123456789ab",
        ).to_dict()
        commands: list[list[str]] = []

        def runner(arguments):
            values = list(arguments)
            commands.append(values)
            if "clone" in values:
                control_value = next(
                    value.split("=", 1)[1]
                    for value in values
                    if value.startswith("--separate-git-dir=")
                )
                Path(control_value).mkdir(parents=True)
                (work / ".git").write_text(
                    f"gitdir: {control_value}\n",
                    encoding="utf-8",
                )
            stdout = (
                "origin/trunk\n"
                if "symbolic-ref" in values
                else ""
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        outcome = execute_fetch_plan(
            plan,
            work,
            {},
            command_runner=runner,
        )
        self.assertEqual("trunk", outcome["default_branch"])
        self.assertFalse((work / ".git").exists())
        self.assertTrue(
            any("reset" in command and "--hard" in command for command in commands)
        )
        self.assertTrue(any("clean" in command for command in commands))
        self.assertTrue((work.parent.parent / "provider" / ".git").is_dir())

    def test_external_source_operation_resolves_canonical_route_once(
        self,
    ) -> None:
        source = register_source(
            self.db_root,
            source_type="github",
            display_name="Network route",
            fetch={"repository_url": "https://github.com/example/r.git"},
        )
        route_runner = object()
        route_http = object()
        route = SimpleNamespace(
            command_runner=route_runner,
            http_get=route_http,
            environment={"ROUTE": "selected"},
        )
        captured: dict[str, object] = {}

        def fetch_plan(_plan, _work, _state, **kwargs):
            captured.update(kwargs)
            return {
                "status": "ok",
                "default_branch": "main",
                "documents": 1,
            }

        with (
            mock.patch(
                "source_manager.runner.resolve_source_network_route",
                return_value=route,
            ) as resolve,
            mock.patch(
                "source_manager.runner.execute_fetch_plan",
                side_effect=fetch_plan,
            ),
            mock.patch(
                "source_manager.runner._execute_add",
                return_value={
                    "source_id": source["local_source_key"],
                    "summary": {
                        "status": "ok",
                        "source_id": source["local_source_key"],
                    },
                },
            ),
        ):
            result = update_source(
                self.db_root,
                source["local_source_key"],
                python_executable=Path(self.temporary.name) / "venv-python",
                rag_root=Path(self.temporary.name) / "rag-runtime",
                metadata_publisher=lambda *_: None,
                environment={},
            )
        self.assertEqual("updated", result["status"])
        resolve.assert_called_once()
        self.assertIs(route_runner, captured["command_runner"])
        self.assertIs(route_http, captured["http_get"])
        self.assertEqual({"ROUTE": "selected"}, captured["environment"])

    def test_svn_direct_refresh_preserves_prior_child_documents(self) -> None:
        work = (
            Path(self.temporary.name).resolve()
            / "managed"
            / "ingest"
            / "source"
        )
        work.mkdir(parents=True)
        source_key = "src_fixture-0123456789ab"
        relative_work = f"sources/{source_key}/work/ingest/{source_key}"

        def plan(recursive):
            return build_fetch_plan(
                source_key=source_key,
                provider="svn",
                settings={
                    "repository_url": "https://svn.example.invalid/project",
                    "recursive": recursive,
                },
                logical_root=relative_work,
                work_path=relative_work,
            ).to_dict()

        revision = 0

        def runner(arguments):
            nonlocal revision
            values = list(arguments)
            checkout = work.parent.parent / "provider" / ".svn-worktree"
            if "checkout" in values:
                (checkout / ".svn").mkdir(parents=True)
                (checkout / "root.md").write_text("first", encoding="utf-8")
                (checkout / "child").mkdir()
                (checkout / "child" / "keep.md").write_text(
                    "keep",
                    encoding="utf-8",
                )
                revision = 1
            elif "update" in values:
                (checkout / "root.md").write_text("second", encoding="utf-8")
                revision = 2
            return SimpleNamespace(
                returncode=0,
                stdout=f"{revision}\n" if "info" in values else "",
                stderr="",
            )

        execute_fetch_plan(plan(True), work, {}, command_runner=runner)
        self.assertTrue((work / "child" / "keep.md").is_file())
        execute_fetch_plan(plan(False), work, {}, command_runner=runner)
        self.assertEqual("second", (work / "root.md").read_text(encoding="utf-8"))
        self.assertEqual("keep", (work / "child" / "keep.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
