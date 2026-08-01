from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_links


def link(
    *,
    provider: str = "sharepoint",
    settings: dict | None = None,
    enabled: bool = True,
    strategy: str = "append-relative-path",
) -> dict:
    return {
        "source_id": "source-a",
        "provider": provider,
        "enabled": enabled,
        "strategy": strategy,
        "settings": settings
        or {
            "source_home_url": "https://tenant.example.invalid/sites/example",
            "source_web_root": (
                "https://tenant.example.invalid/sites/example/Library/Folder"
            ),
        },
    }


def sidecar(
    source: dict | None = None,
    *,
    revision: int = 1,
    schema_version: str = source_links.SCHEMA_VERSION,
) -> dict:
    value = metadata_source(source or link())
    return {
        "schema_version": schema_version,
        "revision": revision,
        "sources": [value],
    }


def metadata_source(source: dict) -> dict:
    value = dict(source)
    if "provider" in value:
        provider = value.pop("provider")
        value = {
            "source_id": value.pop("source_id"),
            **(
                {"display_name": value.pop("display_name")}
                if "display_name" in value
                else {}
            ),
            "source_type": provider,
            "link": {
                "enabled": value.pop("enabled"),
                "strategy": value.pop("strategy"),
                "settings": value.pop("settings"),
            },
            **value,
        }
    return value


def legacy_sidecar(
    *,
    prefix: str = "Former Root/",
    mappings: list[dict] | None = None,
    revision: int = 1,
) -> dict:
    values = mappings or [
        {
            "mapping_id": "00000000-0000-0000-0000-000000000001",
            "enabled": True,
            "path_prefix": prefix,
            "provider": "sharepoint",
            "strategy": "append-relative-path",
            "settings": {
                "source_web_root": (
                    "https://tenant.example.invalid/sites/example/Library"
                )
            },
        }
    ]
    return {
        "schema_version": source_links.LEGACY_SCHEMA_VERSION,
        "database": "example-rag",
        "revision": revision,
        "sources": [
            {
                "source_id": "source-a",
                "display_name": "Legacy display",
                "mappings": values,
            }
        ],
    }


def search_payload(path: str, source_id: str = "source-a") -> dict:
    return {
        "status": "ok",
        "answerability": "full",
        "evidence": [
            {
                "id": "E1",
                "_source_id": source_id,
                "source": {"path": path},
                "text": "Evidence.",
            }
        ],
        "background_context": [],
        "related_context": [],
        "document_results": [
            {
                "id": "D1",
                "_source_id": source_id,
                "path": path,
            }
        ],
        "_result_detail_items": [
            {
                "item_id": "E1",
                "_source_id": source_id,
                "path": path,
            }
        ],
    }


def forbidden_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "mappings",
                "mapping_id",
                "path_prefix",
            }:
                found.add(key)
            found.update(forbidden_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(forbidden_keys(child))
    return found


class SourceLinksContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-metadata-"
        )
        self.db_root = Path(self.temporary.name) / "example-rag"
        self.db_root.mkdir()
        self.set_paths(
            {
                "source-a": ["Root/a.txt"],
                "source-b": ["Root/a.txt"],
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def save(
        self,
        payload: dict,
        *,
        expected_revision: int | None = None,
        expected_etag: str | None = None,
        allow_unmatched_sources: bool = False,
    ) -> dict:
        current = self.db_root / source_links.SIDECAR_NAME
        if expected_revision is None:
            if current.is_file():
                existing = json.loads(current.read_text(encoding="utf-8"))
                expected_revision = int(existing["revision"])
            else:
                expected_revision = 0
        if expected_etag is None:
            expected_etag = (
                source_links._current_etag(current)
                if current.is_file()
                else "missing"
            )
        return source_links.save_source_links(
            self.db_root,
            payload,
            db_name="example-rag",
            existing_sources={"source-a", "source-b"},
            allow_unmatched_sources=allow_unmatched_sources,
            expected_revision=expected_revision,
            expected_etag=expected_etag,
        )

    def set_paths(self, values: dict[str, list[str]]) -> None:
        path = self.db_root / "catalog.sqlite"
        path.unlink(missing_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE document (
                    doc_pk INTEGER PRIMARY KEY,
                    source_id TEXT,
                    path TEXT NOT NULL,
                    visible_until INTEGER
                )
                """
            )
            index = 0
            for source_id, paths in values.items():
                for stored_path in paths:
                    index += 1
                    connection.execute(
                        "INSERT INTO document VALUES (?, ?, ?, NULL)",
                        (index, source_id, stored_path),
                    )
            connection.commit()
        finally:
            connection.close()

    def test_source_metadata_is_exactly_one_link_per_source(self) -> None:
        saved = self.save(sidecar())
        self.assertEqual(source_links.SCHEMA_VERSION, saved["schema_version"])
        self.assertEqual(
            {"source_id", "source_type", "link"},
            set(saved["sources"][0]),
        )
        self.assertEqual(
            {"enabled", "strategy", "settings"},
            set(saved["sources"][0]["link"]),
        )
        self.assertNotIn("database", saved)

    def test_saving_known_source_preserves_unmatched_setting(self) -> None:
        payload = sidecar()
        orphan = link(
            provider="other",
            settings={
                "source_web_root": "https://files.example.invalid/root"
            },
        )
        orphan["source_id"] = "orphan-source"
        payload["sources"].append(metadata_source(orphan))
        saved = self.save(payload, allow_unmatched_sources=True)
        self.assertEqual(
            ["source-a", "orphan-source"],
            [source["source_id"] for source in saved["sources"]],
        )
        self.assertEqual(set(), forbidden_keys(saved))

    def test_v2_rejects_mapping_and_prefix_fields(self) -> None:
        for field, value in (
            ("mappings", []),
            ("mapping_id", "legacy"),
            ("path_prefix", "Legacy/"),
        ):
            payload = sidecar()
            payload["sources"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(payload)
        payload = sidecar()
        payload["database"] = "example-rag"
        with self.assertRaises(source_links.SourceLinkError):
            self.save(payload)

    def test_display_name_only_source_is_valid(
        self,
    ) -> None:
        payload = sidecar(
            {
                "source_id": "source-a",
                "display_name": "Example Source",
            }
        )
        saved = self.save(payload)
        self.assertEqual(
            {"source_id": "source-a", "display_name": "Example Source"},
            saved["sources"][0],
        )

    def test_legacy_requires_one_mapping_and_matching_observed_root(self) -> None:
        valid = source_links.load_source_links(
            self.db_root,
            observed_roots={"source-a": ["Former Root/"]},
        )
        self.assertEqual("unconfigured", valid.status)
        current = self.db_root / source_links.SIDECAR_NAME
        current.write_text(
            json.dumps(legacy_sidecar(), ensure_ascii=False),
            encoding="utf-8",
        )
        matched = source_links.load_source_links(
            self.db_root,
            observed_roots={"source-a": ["Former Root/"]},
        )
        self.assertEqual(
            "legacy_migration_available",
            dict(matched.source_statuses)["source-a"],
        )
        assert matched.payload is not None
        self.assertEqual(
            "sharepoint",
            matched.payload["sources"][0]["source_type"],
        )
        mismatched = source_links.load_source_links(
            self.db_root,
            observed_roots={"source-a": ["Different Root/"]},
        )
        self.assertEqual(
            "legacy_root_mismatch",
            dict(mismatched.source_statuses)["source-a"],
        )
        self.assertFalse(
            any(
                source.get("provider")
                for source in (mismatched.payload or {}).get("sources", [])
            )
        )
        multiple_payload = legacy_sidecar()
        multiple_payload["sources"][0]["mappings"].append(
            dict(multiple_payload["sources"][0]["mappings"][0])
        )
        current.write_text(
            json.dumps(multiple_payload),
            encoding="utf-8",
        )
        multiple = source_links.load_source_links(
            self.db_root,
            observed_roots={"source-a": ["Former Root/"]},
        )
        self.assertEqual(
            "legacy_multiple_mappings",
            dict(multiple.source_statuses)["source-a"],
        )

        empty_payload = legacy_sidecar()
        empty_payload["sources"][0]["mappings"] = []
        current.write_text(
            json.dumps(empty_payload),
            encoding="utf-8",
        )
        empty = source_links.load_source_links(
            self.db_root,
            observed_roots={"source-a": ["Former Root/"]},
        )
        self.assertEqual(
            "not_configured",
            dict(empty.source_statuses)["source-a"],
        )

    def test_legacy_save_publishes_v2_and_keeps_raw_v1_backup(self) -> None:
        legacy = legacy_sidecar()
        current = self.db_root / source_links.SIDECAR_NAME
        current.write_text(
            json.dumps(legacy, ensure_ascii=False),
            encoding="utf-8",
        )
        loaded = source_links.load_source_links(
            self.db_root,
            observed_roots={"source-a": ["Former Root/"]},
        )
        self.assertEqual("configured", loaded.status)
        assert loaded.payload is not None
        loaded.payload["revision"] = 2
        self.save(loaded.payload, expected_revision=1)
        published = json.loads(current.read_text(encoding="utf-8"))
        backup = json.loads(
            (self.db_root / source_links.BACKUP_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(source_links.SCHEMA_VERSION, published["schema_version"])
        self.assertEqual(source_links.LEGACY_SCHEMA_VERSION, backup["schema_version"])
        self.assertEqual(set(), forbidden_keys(published))

    def test_legacy_database_identity_mismatch_fails_open(self) -> None:
        payload = legacy_sidecar(prefix="Root/")
        payload["database"] = "other-rag"
        current = self.db_root / source_links.SIDECAR_NAME
        current.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("invalid", loaded.status)
        self.assertIsNone(loaded.payload)
        enriched = source_links.enrich_search_payload(
            search_payload("Root/a.txt"),
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", enriched["evidence"][0])

    def test_raw_legacy_payload_cannot_be_saved_implicitly(self) -> None:
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "explicit",
        ):
            source_links.save_source_links(
                self.db_root,
                legacy_sidecar(),
                expected_revision=0,
                expected_etag="missing",
            )

    def test_observed_root_is_removed_once_before_url_generation(self) -> None:
        self.set_paths({"source-a": ["Root Name/docs/設計 #1 (final).pdf"]})
        self.save(sidecar())
        path = "Root Name/docs/設計 #1 (final).pdf"
        enriched = source_links.enrich_search_payload(
            search_payload(path),
            self.db_root,
            "example-rag",
        )
        expected_suffix = (
            "/docs/%E8%A8%AD%E8%A8%88%20%231%20%28final%29.pdf"
        )
        self.assertTrue(
            enriched["evidence"][0]["source_url"].endswith(expected_suffix)
        )
        self.assertTrue(
            enriched["document_results"][0]["source_url"].endswith(
                expected_suffix
            )
        )

    def test_source_identity_selects_one_configuration(self) -> None:
        payload = sidecar()
        payload["sources"].append(
            {
                "source_id": "source-b",
                "source_type": "other",
                "link": {
                    "enabled": True,
                    "strategy": "append-relative-path",
                    "settings": {
                        "source_web_root": "https://other.example.invalid/base"
                    },
                },
            }
        )
        self.save(payload)
        first = source_links.enrich_search_payload(
            search_payload("Root/a.txt", "source-a"),
            self.db_root,
            "example-rag",
        )
        second = source_links.enrich_search_payload(
            search_payload("Root/a.txt", "source-b"),
            self.db_root,
            "example-rag",
        )
        self.assertIn("tenant.example.invalid", first["evidence"][0]["source_url"])
        self.assertIn("other.example.invalid", second["evidence"][0]["source_url"])

    def test_disabled_unknown_and_absent_sidecar_fail_open(self) -> None:
        for payload, source_id in (
            (None, "source-a"),
            (sidecar(link(enabled=False)), "source-a"),
            (sidecar(), "unknown"),
        ):
            with self.subTest(payload=payload is not None, source_id=source_id):
                current = self.db_root / source_links.SIDECAR_NAME
                current.unlink(missing_ok=True)
                if payload is not None:
                    self.save(payload)
                original = search_payload("Root/a.txt", source_id)
                enriched = source_links.enrich_search_payload(
                    original,
                    self.db_root,
                    "example-rag",
                )
                self.assertEqual("ok", enriched["status"])
                self.assertNotIn("source_url", enriched["evidence"][0])

    def test_zero_or_multiple_observed_roots_are_path_only(self) -> None:
        for paths, expected_status in (
            ([], "no_observed_root"),
            (
                ["First/a.txt", "Second/b.txt"],
                "multiple_observed_roots",
            ),
        ):
            with self.subTest(status=expected_status):
                self.set_paths({"source-a": paths})
                current = self.db_root / source_links.SIDECAR_NAME
                current.unlink(missing_ok=True)
                with self.assertRaisesRegex(
                    source_links.SourceLinkError,
                    expected_status,
                ):
                    self.save(sidecar())
                current.write_text(
                    json.dumps(sidecar(), ensure_ascii=False),
                    encoding="utf-8",
                )
                enriched = source_links.enrich_search_payload(
                    search_payload(
                        paths[0] if paths else "Root/a.txt",
                    ),
                    self.db_root,
                    "example-rag",
                    explain=True,
                )
                self.assertNotIn("source_url", enriched["evidence"][0])
                self.assertEqual(
                    expected_status,
                    enriched["evidence"][0]["source_link_status"],
                )

    def test_invalid_or_deleted_sidecar_drops_cached_url(self) -> None:
        self.save(sidecar())
        original = search_payload("Root/a.txt")
        self.assertIn(
            "source_url",
            source_links.enrich_search_payload(
                original, self.db_root, "example-rag"
            )["evidence"][0],
        )
        current = self.db_root / source_links.SIDECAR_NAME
        current.write_text("{invalid", encoding="utf-8")
        invalid = source_links.enrich_search_payload(
            original, self.db_root, "example-rag"
        )
        self.assertNotIn("source_url", invalid["evidence"][0])
        current.unlink()
        deleted = source_links.enrich_search_payload(
            original, self.db_root, "example-rag"
        )
        self.assertNotIn("source_url", deleted["evidence"][0])

    def test_same_size_same_mtime_content_change_invalidates_cache(
        self,
    ) -> None:
        first = sidecar()
        first["sources"][0]["link"]["settings"]["source_web_root"] = (
            "https://one.example.invalid/root"
        )
        self.save(first)
        current = self.db_root / source_links.SIDECAR_NAME
        stat = current.stat()
        loaded_first = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        second = sidecar()
        second["sources"][0]["link"]["settings"]["source_web_root"] = (
            "https://two.example.invalid/root"
        )
        second = source_links.validate_source_links(
            second,
            expected_database="example-rag",
            existing_sources={"source-a", "source-b"},
            observed_paths={
                "source-a": ["Root/a.txt"],
                "source-b": ["Root/a.txt"],
            },
        )
        first_bytes = current.read_bytes()
        second_bytes = (
            json.dumps(
                second,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(len(first_bytes), len(second_bytes))
        current.write_bytes(second_bytes)
        os.utime(
            current,
            ns=(stat.st_atime_ns, stat.st_mtime_ns),
        )
        loaded_second = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertNotEqual(loaded_first.etag, loaded_second.etag)
        assert loaded_second.payload is not None
        self.assertIn(
            "two.example.invalid",
            loaded_second.payload["sources"][0]["link"]["settings"][
                "source_web_root"
            ],
        )

    def test_revision_prevents_lost_update(self) -> None:
        self.save(sidecar(revision=1))
        winner = sidecar(revision=2)
        winner["sources"][0]["link"]["settings"]["source_web_root"] = (
            "https://winner.example.invalid/root"
        )
        self.save(winner, expected_revision=1)
        loser = sidecar(revision=2)
        loser["sources"][0]["link"]["settings"]["source_web_root"] = (
            "https://loser.example.invalid/root"
        )
        with self.assertRaises(source_links.SourceLinkError):
            self.save(loser, expected_revision=1)
        loaded = source_links.load_source_links(self.db_root, "example-rag")
        assert loaded.payload is not None
        self.assertIn(
            "winner.example.invalid",
            loaded.payload["sources"][0]["link"]["settings"]["source_web_root"],
        )

    def test_same_revision_external_change_is_rejected_by_etag(self) -> None:
        self.save(sidecar(revision=1))
        loaded = source_links.load_source_links(self.db_root, "example-rag")
        current = self.db_root / source_links.SIDECAR_NAME
        external = sidecar(revision=1)
        external["sources"][0]["link"]["settings"]["source_web_root"] = (
            "https://external.example.invalid/root"
        )
        current.write_text(
            json.dumps(external, ensure_ascii=False),
            encoding="utf-8",
        )
        proposed = sidecar(revision=2)
        proposed["sources"][0]["link"]["settings"]["source_web_root"] = (
            "https://proposed.example.invalid/root"
        )
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "source_link_configuration_changed",
        ):
            self.save(
                proposed,
                expected_revision=1,
                expected_etag=loaded.etag,
            )
        self.assertIn(
            "external.example.invalid",
            current.read_text(encoding="utf-8"),
        )

    def test_save_requires_revision_and_content_hash(self) -> None:
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "revision and content hash",
        ):
            source_links.save_source_links(
                self.db_root,
                sidecar(),
            )

    def test_catalog_root_change_before_publish_aborts_save(self) -> None:
        original_write = source_links._write_bytes
        changed = False

        def write_then_change_catalog(path: Path, value: bytes) -> None:
            nonlocal changed
            original_write(path, value)
            if changed or source_links.SIDECAR_NAME not in path.name:
                return
            changed = True
            connection = sqlite3.connect(self.db_root / "catalog.sqlite")
            try:
                connection.execute(
                    "INSERT INTO document VALUES (?, ?, ?, NULL)",
                    (99, "source-a", "Second Root/new.txt"),
                )
                connection.commit()
            finally:
                connection.close()

        with mock.patch.object(
            source_links,
            "_write_bytes",
            side_effect=write_then_change_catalog,
        ):
            with self.assertRaisesRegex(
                source_links.SourceLinkError,
                "catalog_roots_changed",
            ):
                self.save(sidecar())
        self.assertFalse(
            (self.db_root / source_links.SIDECAR_NAME).exists()
        )

    def test_failed_publish_does_not_replace_existing_backup(self) -> None:
        self.save(sidecar())
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        assert loaded.payload is not None
        backup = self.db_root / source_links.BACKUP_NAME
        backup.write_text(
            '{"schema_version":"synthetic-backup"}\n',
            encoding="utf-8",
        )
        before = backup.read_bytes()
        update = loaded.payload
        update["revision"] = loaded.revision + 1
        with mock.patch.object(
            source_links,
            "read_visible_observed_roots",
            side_effect=[
                {"source-a": ("Root/",)},
                {"source-a": ("Second Root/",)},
            ],
        ):
            with self.assertRaisesRegex(
                source_links.SourceLinkError,
                "catalog_roots_changed",
            ):
                source_links.save_source_links(
                    self.db_root,
                    update,
                    db_name="example-rag",
                    existing_sources={"source-a"},
                    expected_revision=loaded.revision,
                    expected_etag=loaded.etag,
                )
        self.assertEqual(before, backup.read_bytes())

    def test_windows_atomic_replace_retries_transient_sharing_error(
        self,
    ) -> None:
        original = source_links.os.replace
        calls = 0

        def transient(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                error = PermissionError("synthetic sharing violation")
                error.winerror = 5
                raise error
            original(source, target)

        with (
            mock.patch.object(
                source_links,
                "_is_windows",
                return_value=True,
            ),
            mock.patch.object(
                source_links.os,
                "replace",
                side_effect=transient,
            ),
            mock.patch.object(
                source_links,
                "WINDOWS_REPLACE_RETRY_SECONDS",
                0.5,
            ),
        ):
            self.save(sidecar())
        self.assertEqual(3, calls)
        self.assertTrue(
            (self.db_root / source_links.SIDECAR_NAME).is_file()
        )

    def test_persistent_lock_file_contents_are_opaque(self) -> None:
        lock = self.db_root / ".source-links.lock"
        lock.write_bytes(b"opaque legacy bytes")
        identity = lock.stat().st_ino
        started = time.monotonic()
        self.save(sidecar())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(lock.exists())
        self.assertEqual(identity, lock.stat().st_ino)
        self.assertEqual(b"opaque legacy bytes", lock.read_bytes())

    def test_save_never_creates_a_source_link_lock(self) -> None:
        self.save(sidecar())
        self.assertFalse((self.db_root / ".source-links.lock").exists())

    @unittest.skipIf(
        os.name == "nt",
        "creating symlinks is not generally available to Windows test users",
    )
    def test_sidecar_symlink_is_never_read_or_written(self) -> None:
        outside = Path(self.temporary.name) / "outside.json"
        original = json.dumps(sidecar()).encode("utf-8")
        outside.write_bytes(original)
        current = self.db_root / source_links.SIDECAR_NAME
        current.symlink_to(outside)

        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("invalid", loaded.status)
        self.assertIsNone(loaded.payload)
        with self.assertRaises(source_links.SourceLinkError):
            source_links.save_source_links(
                self.db_root,
                sidecar(revision=2),
                db_name="example-rag",
                existing_sources={"source-a", "source-b"},
                expected_revision=1,
                expected_etag=source_links._current_etag(outside),
            )
        self.assertEqual(original, outside.read_bytes())

    @unittest.skipIf(
        os.name == "nt",
        "creating symlinks is not generally available to Windows test users",
    )
    def test_database_root_symlink_fails_open(self) -> None:
        outside = Path(self.temporary.name) / "outside-db"
        outside.mkdir()
        (outside / source_links.SIDECAR_NAME).write_text(
            json.dumps(sidecar()),
            encoding="utf-8",
        )
        alias = Path(self.temporary.name) / "alias-rag"
        alias.symlink_to(outside, target_is_directory=True)
        loaded = source_links.load_source_links(alias, "alias-rag")
        self.assertEqual("invalid", loaded.status)
        self.assertIsNone(loaded.payload)
        with self.assertRaises(source_links.SourceLinkError):
            source_links.save_source_links(
                alias,
                sidecar(),
                db_name="alias-rag",
                existing_sources={"source-a"},
                expected_revision=0,
                expected_etag="missing",
            )

    def test_sharepoint_browser_url_is_normalized(self) -> None:
        payload = sidecar(
            link(
                settings={
                    "source_web_root": (
                        "https://tenant.example.invalid/sites/example/"
                        "Forms/AllItems.aspx?id=%2Fsites%2Fexample%2FLibrary"
                        "%2FFolder"
                    )
                }
            )
        )
        normalized = self.save(payload)
        self.assertEqual(
            "https://tenant.example.invalid/sites/example/Library/Folder",
            normalized["sources"][0]["link"]["settings"]["source_web_root"],
        )

    def test_sharepoint_home_key_is_read_but_removed_on_canonical_save(
        self,
    ) -> None:
        legacy_value = sidecar(
            link(
                settings={
                    "source_home_url": (
                        "https://tenant.example.invalid/sites/example"
                    ),
                    "source_web_root": (
                        "https://tenant.example.invalid/sites/example/Library"
                    ),
                }
            )
        )
        current = self.db_root / source_links.SIDECAR_NAME
        current.write_text(
            json.dumps(legacy_value),
            encoding="utf-8",
        )
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("configured", loaded.status)
        assert loaded.payload is not None
        settings = loaded.payload["sources"][0]["link"]["settings"]
        self.assertNotIn("source_home_url", settings)
        normalized = self.save(loaded.payload)
        self.assertNotIn(
            "source_home_url",
            normalized["sources"][0]["link"]["settings"],
        )

    def test_retired_sharepoint_home_key_still_rejects_credentials(
        self,
    ) -> None:
        for unsafe_home in (
            "https://user:secret@tenant.example.invalid/sites/example",
            "https://tenant.example.invalid/sites/example?token=secret",
        ):
            with self.subTest(unsafe_home=unsafe_home):
                with self.assertRaises(source_links.SourceLinkError):
                    source_links.validate_source_links(
                        sidecar(
                            link(
                                settings={
                                    "source_home_url": unsafe_home,
                                    "source_web_root": (
                                        "https://tenant.example.invalid/"
                                        "sites/example/Library"
                                    ),
                                }
                            )
                        ),
                        expected_database="example-rag",
                        existing_sources={"source-a", "source-b"},
                    )

    def test_sharepoint_home_only_is_preserved_but_remains_path_only(
        self,
    ) -> None:
        legacy_value = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 1,
            "sources": [
                link(
                    strategy="home-only",
                    settings={
                        "source_home_url": (
                            "https://tenant.example.invalid/sites/example"
                        )
                    },
                )
            ],
        }
        current = self.db_root / source_links.SIDECAR_NAME
        current.write_text(json.dumps(legacy_value), encoding="utf-8")
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("configured", loaded.status)
        assert loaded.payload is not None
        normalized = self.save(loaded.payload)
        self.assertEqual(
            "home-only",
            normalized["sources"][0]["link"]["strategy"],
        )
        enriched = source_links.enrich_search_payload(
            search_payload("Root/a.txt"),
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", enriched["evidence"][0])
        self.assertEqual("ok", enriched["status"])

    def test_sharepoint_without_web_root_does_not_save_a_fallback_link(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "source_web_root",
        ):
            self.save(
                sidecar(
                    link(
                        settings={
                            "source_home_url": (
                                "https://tenant.example.invalid/sites/example"
                            )
                        }
                    )
                )
            )

    def test_sharepoint_source_relative_segments_are_encoded_once(self) -> None:
        self.set_paths(
            {
                "source-a": [
                    "Root/日本語 空白 # % + (final).txt",
                ]
            }
        )
        self.save(sidecar())
        enriched = source_links.enrich_search_payload(
            search_payload("Root/日本語 空白 # % + (final).txt"),
            self.db_root,
            "example-rag",
        )
        url = enriched["evidence"][0]["source_url"]
        for encoded in (
            "%E6%97%A5%E6%9C%AC%E8%AA%9E",
            "%20",
            "%23",
            "%25",
            "%2B",
            "%28final%29",
        ):
            self.assertIn(encoded, url)
        self.assertNotIn("%2525", url)

    def test_github_is_manual_and_permalink_is_explicit(self) -> None:
        payload = sidecar(
            link(
                provider="github",
                strategy="github-blob",
                settings={
                    "repository_url": "https://git.example.invalid/o/r.git",
                    "ref": "release/v2",
                    "repository_path_prefix": "docs",
                    "commit": "a" * 40,
                    "permalink_enabled": True,
                },
            )
        )
        self.save(payload)
        enriched = source_links.enrich_search_payload(
            search_payload("Root/file name.md"),
            self.db_root,
            "example-rag",
        )
        item = enriched["evidence"][0]
        self.assertEqual(
            "https://git.example.invalid/o/r/blob/release/v2/docs/file%20name.md",
            item["source_url"],
        )
        self.assertIn("/blob/" + "a" * 40 + "/", item["source_permalink"])

    def test_non_sharepoint_legacy_settings_fail_open(
        self,
    ) -> None:
        legacy_value = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 1,
            "sources": [
                link(
                    provider="github",
                    strategy="append-relative-path",
                    settings={
                        "repository_url": "https://git.example.invalid/o/r",
                        "ref": "main",
                        "permalink_enabled": False,
                    },
                )
            ],
        }
        current = self.db_root / source_links.SIDECAR_NAME
        original = json.dumps(legacy_value).encode("utf-8")
        current.write_bytes(original)
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("manual_required", loaded.status)
        self.assertEqual(
            "legacy_non_sharepoint_manual_required",
            loaded.error_kind,
        )
        self.assertTrue(loaded.migration_required)
        self.assertEqual(original, current.read_bytes())
        self.assertIsNone(loaded.payload)

    def test_github_repository_root_rejects_browse_query_and_fragment(
        self,
    ) -> None:
        for repository_url in (
            "https://git.example.invalid/o/r/blob/main",
            "https://git.example.invalid/o/r/tree/main",
            "https://git.example.invalid/o/r?ref=main",
            "https://git.example.invalid/o/r#readme",
        ):
            with self.subTest(repository_url=repository_url):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider="github",
                                strategy="github-blob",
                                settings={
                                    "repository_url": repository_url,
                                    "ref": "main",
                                    "permalink_enabled": False,
                                },
                            )
                        )
                    )

    def test_github_permalink_requires_full_commit_and_safe_ref(
        self,
    ) -> None:
        invalid_settings = [
            {"ref": "../issues", "commit": "a" * 40},
            {"ref": "feature//x", "commit": "a" * 40},
            {"ref": "feature/../x", "commit": "a" * 40},
            {"ref": "feature.lock", "commit": "a" * 40},
            {"ref": "main", "commit": "main"},
            {"ref": "main", "commit": "a" * 39},
            {"ref": "main", "commit": "g" * 40},
        ]
        for values in invalid_settings:
            with self.subTest(values=values):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider="github",
                                strategy="github-blob",
                                settings={
                                    "repository_url": (
                                        "https://git.example.invalid/o/r"
                                    ),
                                    "ref": values["ref"],
                                    "commit": values["commit"],
                                    "permalink_enabled": True,
                                },
                            )
                        )
                    )

    def test_gitlab_saas_self_hosted_subgroup_and_permalink(self) -> None:
        self.set_paths(
            {"source-a": ["Root/日本語 空白 # % + (final).md"]}
        )
        for repository_url in (
            "https://gitlab.com/group/subgroup/repository.git",
            "https://gitlab.example.invalid/base/group/repository",
        ):
            with self.subTest(repository_url=repository_url):
                current = self.db_root / source_links.SIDECAR_NAME
                current.unlink(missing_ok=True)
                self.save(
                    sidecar(
                        link(
                            provider="gitlab",
                            strategy="gitlab-blob",
                            settings={
                                "repository_url": repository_url,
                                "ref": "release/v2",
                                "repository_path_prefix": "docs",
                                "commit": "b" * 40,
                                "permalink_enabled": True,
                            },
                        )
                    )
                )
                enriched = source_links.enrich_search_payload(
                    search_payload("Root/日本語 空白 # % + (final).md"),
                    self.db_root,
                    "example-rag",
                )
                item = enriched["evidence"][0]
                self.assertIn("/-/blob/release/v2/docs/", item["source_url"])
                self.assertIn(
                    "/-/blob/" + "b" * 40 + "/docs/",
                    item["source_permalink"],
                )
                for encoded in (
                    "%E6%97%A5%E6%9C%AC%E8%AA%9E",
                    "%20",
                    "%23",
                    "%25",
                    "%2B",
                    "%28final%29",
                ):
                    self.assertIn(encoded, item["source_url"])

    def test_azure_devops_modern_legacy_prefix_and_permalink(self) -> None:
        self.set_paths({"source-a": ["Root/日本語 file #1?.md"]})
        repositories = (
            "https://dev.azure.com/organization/project/_git/repository",
            "https://organization.visualstudio.com/project/_git/repository",
            (
                "https://organization.visualstudio.com/"
                "DefaultCollection/project/_git/repository"
            ),
        )
        for repository_url in repositories:
            with self.subTest(repository_url=repository_url):
                current = self.db_root / source_links.SIDECAR_NAME
                current.unlink(missing_ok=True)
                self.save(
                    sidecar(
                        link(
                            provider="azure_devops",
                            strategy="azure-devops-item",
                            settings={
                                "repository_url": repository_url,
                                "ref": "release/v2",
                                "repository_path_prefix": "product docs",
                                "commit": "c" * 40,
                                "permalink_enabled": True,
                            },
                        )
                    )
                )
                item = source_links.enrich_search_payload(
                    search_payload("Root/日本語 file #1?.md"),
                    self.db_root,
                    "example-rag",
                )["evidence"][0]
                self.assertIn(
                    "?path=/product%20docs/"
                    "%E6%97%A5%E6%9C%AC%E8%AA%9E%20file%20%231%3F.md",
                    item["source_url"],
                )
                self.assertIn("&version=GBrelease%2Fv2", item["source_url"])
                self.assertIn("&version=GC" + "c" * 40, item["source_permalink"])

    def test_new_git_provider_repository_roots_are_strict(self) -> None:
        invalid = (
            (
                "gitlab",
                "gitlab-blob",
                "https://gitlab.example.invalid",
            ),
            (
                "gitlab",
                "gitlab-blob",
                "https://gitlab.example.invalid/group",
            ),
            (
                "gitlab",
                "gitlab-blob",
                "https://gitlab.example.invalid/group/repository/-/tree/main",
            ),
            (
                "gitlab",
                "gitlab-blob",
                "https://user:password@gitlab.example.invalid/group/repository",
            ),
            (
                "azure_devops",
                "azure-devops-item",
                "https://dev.azure.com/organization/project/_git",
            ),
            (
                "azure_devops",
                "azure-devops-item",
                (
                    "https://dev.azure.com/organization/project/_git/repository"
                    "?path=/file.md"
                ),
            ),
            (
                "azure_devops",
                "azure-devops-item",
                (
                    "https://dev.azure.com/organization/project/_git/repository"
                    "#fragment"
                ),
            ),
        )
        for provider, strategy, repository_url in invalid:
            with self.subTest(
                provider=provider,
                repository_url=repository_url,
            ):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider=provider,
                                strategy=strategy,
                                settings={
                                    "repository_url": repository_url,
                                    "ref": "main",
                                    "permalink_enabled": False,
                                },
                            )
                        )
                    )

    def test_moved_database_keeps_gitlab_source_links_portable(self) -> None:
        self.save(
            sidecar(
                link(
                    provider="gitlab",
                    strategy="gitlab-blob",
                    settings={
                        "repository_url": (
                            "https://gitlab.example.invalid/group/repository"
                        ),
                        "ref": "main",
                        "permalink_enabled": False,
                    },
                )
            )
        )
        moved = Path(self.temporary.name) / "moved-rag"
        shutil.copytree(self.db_root, moved)
        item = source_links.enrich_search_payload(
            search_payload("Root/a.txt"),
            moved,
            "moved-rag",
        )["evidence"][0]
        self.assertEqual(
            "https://gitlab.example.invalid/group/repository"
            "/-/blob/main/a.txt",
            item["source_url"],
        )

    def test_moved_database_keeps_azure_and_svn_http_links_portable(
        self,
    ) -> None:
        cases = (
            (
                link(
                    provider="azure_devops",
                    strategy="azure-devops-item",
                    settings={
                        "repository_url": (
                            "https://dev.azure.com/organization/project/"
                            "_git/repository"
                        ),
                        "ref": "main",
                        "permalink_enabled": False,
                    },
                ),
                (
                    "https://dev.azure.com/organization/project/"
                    "_git/repository?path=/a.txt&version=GBmain"
                ),
            ),
            (
                link(
                    provider="svn",
                    strategy="svn-http",
                    settings={
                        "repository_url": (
                            "https://svn.example.invalid/repos/project/trunk"
                        ),
                        "permalink_enabled": False,
                    },
                ),
                "https://svn.example.invalid/repos/project/trunk/a.txt",
            ),
        )
        for index, (source, expected_url) in enumerate(cases):
            with self.subTest(provider=source["provider"]):
                self.save(sidecar(source))
                moved = Path(self.temporary.name) / f"moved-{index}-rag"
                shutil.copytree(self.db_root, moved)
                item = source_links.enrich_search_payload(
                    search_payload("Root/a.txt"),
                    moved,
                    f"moved-{index}-rag",
                )["evidence"][0]
                self.assertEqual(expected_url, item["source_url"])

    def test_svn_http_generates_file_and_revision_links(self) -> None:
        self.set_paths(
            {"source-a": ["Root/日本語 空白 @ # % + (final).md"]}
        )
        saved = self.save(
            sidecar(
                link(
                    provider="svn",
                    strategy="svn-http",
                    settings={
                        "repository_url": (
                            "https://svn.example.invalid/repos/project/trunk/"
                        ),
                        "repository_path_prefix": r"docs\ja",
                        "permalink_enabled": True,
                        "revision": "1234",
                    },
                )
            )
        )
        self.assertEqual(
            1234,
            saved["sources"][0]["link"]["settings"]["revision"],
        )
        item = source_links.enrich_search_payload(
            search_payload("Root/日本語 空白 @ # % + (final).md"),
            self.db_root,
            "example-rag",
        )["evidence"][0]
        self.assertEqual(
            "https://svn.example.invalid/repos/project/trunk/docs/ja/"
            "%E6%97%A5%E6%9C%AC%E8%AA%9E%20%E7%A9%BA%E7%99%BD"
            "%20%40%20%23%20%25%20%2B%20%28final%29.md",
            item["source_url"],
        )
        self.assertEqual(
            item["source_url"] + "?p=1234&r=1234",
            item["source_permalink"],
        )

    def test_svn_revision_validation_is_strict(self) -> None:
        invalid_revisions = (0, -1, 1.5, True, False, "0", "-1", "HEAD")
        for revision in invalid_revisions:
            with self.subTest(revision=revision):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider="svn",
                                strategy="svn-http",
                                settings={
                                    "repository_url": (
                                        "https://svn.example.invalid/"
                                        "repos/project/trunk"
                                    ),
                                    "permalink_enabled": True,
                                    "revision": revision,
                                },
                            )
                            )
                        )

    def test_svn_saved_revision_is_ignored_when_permalink_is_disabled(
        self,
    ) -> None:
        saved = self.save(
            sidecar(
                link(
                    provider="svn",
                    strategy="svn-http",
                    settings={
                        "repository_url": (
                            "https://svn.example.invalid/repos/project/trunk"
                        ),
                        "permalink_enabled": False,
                        "revision": 1234,
                    },
                )
            )
        )
        self.assertEqual(
            1234,
            saved["sources"][0]["link"]["settings"]["revision"],
        )
        item = source_links.enrich_search_payload(
            search_payload("Root/a.txt"),
            self.db_root,
            "example-rag",
        )["evidence"][0]
        self.assertEqual(
            "https://svn.example.invalid/repos/project/trunk/a.txt",
            item["source_url"],
        )
        self.assertNotIn("source_permalink", item)

    def test_svn_http_rejects_unsafe_roots_and_prefixes(self) -> None:
        invalid_values = (
            (
                "https://svn.example.invalid/repos/project?view=1",
                "docs",
            ),
            (
                "https://svn.example.invalid/repos/project#fragment",
                "docs",
            ),
            (
                "https://svn.example.invalid:99999/repos/project",
                "docs",
            ),
            (
                "https://[invalid/repos/project",
                "docs",
            ),
            (
                "https://svn.example.invalid/repos/project with spaces",
                "docs",
            ),
            (
                "https://svn.example.invalid/repos/project%ZZ",
                "docs",
            ),
            (
                "https://user:password@svn.example.invalid/repos/project",
                "docs",
            ),
            (
                "svn://svn.example.invalid/repos/project",
                "docs",
            ),
            (
                "https://svn.example.invalid/repos/project",
                "../outside",
            ),
            (
                "https://svn.example.invalid/repos/project",
                r"C:\outside",
            ),
            (
                "https://svn.example.invalid/repos/project",
                r"\\server\share",
            ),
        )
        for repository_url, prefix in invalid_values:
            with self.subTest(
                repository_url=repository_url,
                prefix=prefix,
            ):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider="svn",
                                strategy="svn-http",
                                settings={
                                    "repository_url": repository_url,
                                    "repository_path_prefix": prefix,
                                    "permalink_enabled": False,
                                },
                            )
                        )
                    )

    def test_svn_web_root_is_preserved_for_every_file(self) -> None:
        top_url = (
            "https://svn-web.example.invalid/project/"
            "?view=summary&path=%2Fdocs#files"
        )
        self.set_paths(
            {
                "source-a": [
                    "Root-A/one.md",
                    "Root-B/two.md",
                ]
            }
        )
        self.save(
            sidecar(
                link(
                    provider="svn",
                    strategy="svn-web-root",
                    settings={"repository_url": top_url},
                )
            )
        )
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        assert loaded.payload is not None
        self.assertEqual(
            "svn-web-root",
            loaded.payload["sources"][0]["link"]["strategy"],
        )
        for stored_path in ("Root-A/one.md", "Root-B/two.md"):
            with self.subTest(stored_path=stored_path):
                item = source_links.enrich_search_payload(
                    search_payload(stored_path),
                    self.db_root,
                    "example-rag",
                )["evidence"][0]
                self.assertEqual(top_url, item["source_url"])
                self.assertNotIn("source_permalink", item)
                self.assertNotIn("one.md", item["source_url"])
                self.assertNotIn("two.md", item["source_url"])
                self.assertNotIn("&r=", item["source_url"])
        self.set_paths({"source-b": ["Other/only.md"]})
        no_root_item = source_links.enrich_search_payload(
            search_payload("Unobserved/file.md"),
            self.db_root,
            "example-rag",
        )["evidence"][0]
        self.assertEqual(top_url, no_root_item["source_url"])

    def test_svn_web_root_preserves_query_fragment_and_trailing_slash(
        self,
    ) -> None:
        urls = (
            "https://svn-web.example.invalid/project/?view=summary",
            "https://svn-web.example.invalid/project/#files",
            (
                "https://svn-web.example.invalid/project/"
                "?view=summary#files"
            ),
        )
        for repository_url in urls:
            with self.subTest(repository_url=repository_url):
                saved = self.save(
                    sidecar(
                        link(
                            provider="svn",
                            strategy="svn-web-root",
                            settings={"repository_url": repository_url},
                        )
                    )
                )
                self.assertEqual(
                    repository_url,
                    saved["sources"][0]["link"]["settings"]["repository_url"],
                )
                item = source_links.enrich_search_payload(
                    search_payload("Root/a.txt"),
                    self.db_root,
                    "example-rag",
                )["evidence"][0]
                self.assertEqual(repository_url, item["source_url"])

    def test_svn_web_root_rejects_credentials_and_non_http(self) -> None:
        invalid_urls = (
            "https://user:password@svn-web.example.invalid/project",
            (
                "https://user%3Apassword%40svn-web.example.invalid/"
                "project"
            ),
            "svn://svn.example.invalid/repos/project",
            "svn+ssh://svn.example.invalid/repos/project",
            "file:///repos/project",
            "https://svn-web.example.invalid:99999/project",
            "https://[invalid/project",
            "https://svn-web.example.invalid/project with spaces",
            "https://svn-web.example.invalid/project%ZZ",
            "https://svn-web.example.invalid/project%0d%0aInjected",
        )
        for repository_url in invalid_urls:
            with self.subTest(repository_url=repository_url):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider="svn",
                                strategy="svn-web-root",
                                settings={"repository_url": repository_url},
                            )
                        )
                    )

    def test_moved_database_keeps_svn_web_root_portable(self) -> None:
        top_url = "https://svn-web.example.invalid/project/?view=summary#files"
        self.save(
            sidecar(
                link(
                    provider="svn",
                    strategy="svn-web-root",
                    settings={"repository_url": top_url},
                )
            )
        )
        moved = Path(self.temporary.name) / "moved-svn-rag"
        shutil.copytree(self.db_root, moved)
        item = source_links.enrich_search_payload(
            search_payload("Root/a.txt"),
            moved,
            "moved-svn-rag",
        )["evidence"][0]
        self.assertEqual(top_url, item["source_url"])

    def test_home_only_never_exposes_home_url(self) -> None:
        self.save(
            sidecar(
                link(
                    provider="other",
                    strategy="home-only",
                    settings={
                        "source_home_url": "https://home.example.invalid/"
                    },
                )
            )
        )
        enriched = source_links.enrich_search_payload(
            search_payload("Root/a.txt"),
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", enriched["evidence"][0])

    def test_regex_template_matches_source_relative_path(self) -> None:
        self.save(
            sidecar(
                link(
                    provider="redmine",
                    strategy="regex-template",
                    settings={
                        "path_pattern": (
                            r"^issues/issue-(?P<id>[0-9]+)\.md$"
                        ),
                        "url_template": (
                            "https://tickets.example.invalid/issues/{id}"
                        ),
                    },
                )
            )
        )
        matched = source_links.enrich_search_payload(
            search_payload("Root/issues/issue-123.md"),
            self.db_root,
            "example-rag",
        )
        unmatched = source_links.enrich_search_payload(
            search_payload("issues/issue-123.md"),
            self.db_root,
            "example-rag",
        )
        self.assertEqual(
            "https://tickets.example.invalid/issues/123",
            matched["evidence"][0]["source_url"],
        )
        self.assertNotIn("source_url", unmatched["evidence"][0])

    def test_gitlab_issue_regex_template_resolves_issue_iid(self) -> None:
        self.save(
            sidecar(
                link(
                    provider="gitlab_issues",
                    strategy="regex-template",
                    settings={
                        "path_pattern": (
                            r"^issues/(?P<issue_iid>[0-9]+)\.md$"
                        ),
                        "url_template": (
                            "https://gitlab.example.invalid/group/project/"
                            "-/issues/{issue_iid}"
                        ),
                    },
                )
            )
        )

        matched = source_links.enrich_search_payload(
            search_payload("Root/issues/123.md"),
            self.db_root,
            "example-rag",
        )
        unmatched = source_links.enrich_search_payload(
            search_payload("Root/issues/not-an-iid.md"),
            self.db_root,
            "example-rag",
        )

        self.assertEqual(
            "gitlab_issues",
            matched["evidence"][0]["source_provider"],
        )
        self.assertEqual(
            (
                "https://gitlab.example.invalid/group/project/"
                "-/issues/123"
            ),
            matched["evidence"][0]["source_url"],
        )
        self.assertNotIn("source_url", unmatched["evidence"][0])

    def test_credentials_absolute_paths_and_traversal_are_rejected(self) -> None:
        deeply_encoded = "access_token=synthetic-value"
        for _ in range(12):
            deeply_encoded = quote(deeply_encoded, safe="")
        for url in (
            "https://user:secret@example.invalid/root",
            "https://example.invalid/access_token=secret/root",
            "https://example.invalid/token%3Asecret/root",
            "https://example.invalid/api-key=secret/root",
            "https://example.invalid/credential:secret/root",
            "https://example.invalid/oauth-token%3Asecret/root",
            "https://example.invalid/refresh_token=secret/root",
            "https://example.invalid/session_token=secret/root",
            "https://example.invalid/id-token%3Asecret/root",
            "https://example.invalid/client_secret=secret/root",
            "https://example.invalid/bearer=secret/root",
            "https://example.invalid/jwt%3Asecret/root",
            "https://example.invalid/foo.refresh_token=secret/root",
            "https://example.invalid/foo+access_token=secret/root",
            "https://example.invalid/foo%20session_token%3Dsecret/root",
            "https://example.invalid/x(refresh_token=secret)/root",
            "https://example.invalid/root?bearer=synthetic-value",
            "https://example.invalid/root?jwt=synthetic-value",
            "https://example.invalid/root#bearer=synthetic-value",
            "https://example.invalid/root#jwt=synthetic-value",
            f"https://example.invalid/{deeply_encoded}/root",
            "https://example.invalid/root?b%2565arer=synthetic-value",
            "https://example.invalid/root?j%2577t=synthetic-value",
            "https://example.invalid/root#b%65arer=synthetic-value",
            "https://example.invalid/root#j%2577t=synthetic-value",
            "https://example.invalid/base/../wrong",
            "https://example.invalid/base/%2e%2e/wrong",
            "https://example.invalid/base/%252e%252e/wrong",
            r"https://example.invalid/base\..\wrong",
            "https://example.invalid/base/%5c..%5cwrong",
        ):
            with self.subTest(url=url):
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider="other",
                                settings={"source_web_root": url},
                            )
                        )
                    )
        self.save(sidecar())
        for path in ("../secret.txt", "C:/secret.txt", "/secret.txt"):
            with self.subTest(path=path):
                enriched = source_links.enrich_search_payload(
                    search_payload(path),
                    self.db_root,
                    "example-rag",
                )
                self.assertNotIn("source_url", enriched["evidence"][0])

    def test_encoded_credentials_in_query_values_are_rejected(self) -> None:
        direct = "refresh_token=synthetic-secret"
        double_encoded = quote(quote(direct, safe=""), safe="")
        nested = quote(
            "https://fixture.example.invalid/path?"
            f"next={double_encoded}",
            safe="",
        )
        nested_userinfo = quote(
            "https://user:synthetic-secret@nested.example.invalid/",
            safe="",
        )
        username_only = quote(
            "https://synthetic-token@nested.example.invalid/",
            safe="",
        )
        protocol_relative_userinfo = quote(
            "//user:synthetic-secret@nested.example.invalid/",
            safe="",
        )
        ftp_userinfo = quote(
            "ftp://user:synthetic-secret@nested.example.invalid/",
            safe="",
        )
        credential_name_variants = (
            "APIKEYS",
            "apikeys",
            "ACCESSKEYS",
            "accesskeys",
            "SECRETKEYS",
            "secretkeys",
            "PRIVATEKEYS",
            "privatekeys",
            "SIGNINGKEYS",
            "signingkeys",
            "SSHKEY",
            "sshkey",
            "SUBSCRIPTIONKEY",
            "subscriptionkey",
            "ACCESSKEYID",
            "accesskeyid",
            "AWSACCESSKEYID",
            "awsaccesskeyid",
            "PASSPHRASES",
            "passphrases",
            "AUTHCODE",
            "authcode",
            "AUTHORIZATIONCODE",
            "authorizationcode",
            "OAUTHCODE",
            "oauthcode",
            "XAMZSIGNATURE",
            "xamzsignature",
            "XGOOGSIGNATURE",
            "xgoogsignature",
            "PROXYAUTHORIZATION",
            "proxyauthorization",
            "PROXYAUTH",
            "proxyauth",
            "SECRETACCESSKEY",
            "secretaccesskey",
            "GOOGLEAPIKEY",
            "googleapikey",
        )
        malicious_values = (
            direct,
            double_encoded,
            nested,
            nested_userinfo,
            username_only,
            protocol_relative_userinfo,
            ftp_userinfo,
            quote("credentials=synthetic-secret", safe=""),
            quote("tokens=synthetic-secret", safe=""),
            quote("secrets=synthetic-secret", safe=""),
            quote("passwords=synthetic-secret", safe=""),
            quote("cookies=synthetic-secret", safe=""),
            quote("code=synthetic-secret", safe=""),
            quote("proxy=synthetic-secret", safe=""),
            quote("signature=synthetic-secret", safe=""),
            quote("sig=synthetic-secret", safe=""),
            quote("sas=synthetic-secret", safe=""),
            quote("Basic synthetic-secret", safe=""),
            quote("Bearer synthetic-secret", safe=""),
            quote("foo+Basic synthetic-secret", safe=""),
            quote("foo.Bearer synthetic-secret", safe=""),
            quote("apiKeys=synthetic-secret", safe=""),
            quote(quote("APIKeys=synthetic-secret", safe=""), safe=""),
            quote("accessKeys=synthetic-secret", safe=""),
            quote("subscriptionKey=synthetic-secret", safe=""),
            quote("sshKey=synthetic-secret", safe=""),
            quote("pwd=synthetic-secret", safe=""),
            quote("passphrase=synthetic-secret", safe=""),
            quote(quote("passPhrase=synthetic-secret", safe=""), safe=""),
        ) + tuple(
            encoded
            for name in credential_name_variants
            for encoded in (
                quote(f"{name}=synthetic-secret", safe=""),
                quote(
                    quote(f"{name}=synthetic-secret", safe=""),
                    safe="",
                ),
            )
        )
        for value in malicious_values:
            with self.subTest(value=value):
                for separator in ("?next=", "#next="):
                    home = sidecar(
                        link(
                            provider="other",
                            strategy="home-only",
                            settings={
                                "source_home_url": (
                                    "https://fixture.example.invalid/"
                                    f"{separator}{value}"
                                )
                            },
                        )
                    )
                    with self.assertRaises(source_links.SourceLinkError):
                        self.save(home)

                    regex_link = link(
                        provider="redmine",
                        strategy="regex-template",
                        settings={
                            "path_pattern": (
                                r"^issue-(?P<id>[0-9]+)\.md$"
                            ),
                            "url_template": (
                                "https://fixture.example.invalid/"
                                f"issues/{{id}}{separator}{value}"
                            ),
                        },
                    )
                    with self.assertRaises(source_links.SourceLinkError):
                        self.save(sidecar(regex_link))
                    with self.assertRaises(source_links.SourceLinkError):
                        source_links._generate_provider_urls(
                            regex_link,
                            "issue-123.md",
                        )

        for benign in (
            "tokenization-guide",
            "secret-management",
            "monkeys",
        ):
            with self.subTest(benign=benign):
                normalized = self.save(
                    sidecar(
                        link(
                            provider="other",
                            strategy="home-only",
                            settings={
                                "source_home_url": (
                                    "https://fixture.example.invalid/"
                                    f"?topic={benign}"
                                )
                            },
                        )
                    )
                )
                self.assertEqual(
                    f"https://fixture.example.invalid/?topic={benign}",
                    normalized["sources"][0]["link"]["settings"][
                        "source_home_url"
                    ],
                )

        for key in (
            "pwd",
            "passphrase",
            "passphrases",
            "sas",
            "dbpwd",
            "proxypwd",
            "awssig",
            "requestsig",
            "idjwt",
            "authjwt",
        ):
            with self.subTest(top_level_key=key):
                url = (
                    "https://fixture.example.invalid/"
                    f"?{key}=synthetic-secret"
                )
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(
                        sidecar(
                            link(
                                provider="other",
                                strategy="home-only",
                                settings={"source_home_url": url},
                            )
                        )
                    )
                regex_link = link(
                    provider="redmine",
                    strategy="regex-template",
                    settings={
                        "path_pattern": (
                            r"^issue-(?P<id>[0-9]+)\.md$"
                        ),
                        "url_template": (
                            "https://fixture.example.invalid/"
                            f"issues/{{id}}?{key}=synthetic-secret"
                        ),
                    },
                )
                with self.assertRaises(source_links.SourceLinkError):
                    self.save(sidecar(regex_link))
                with self.assertRaises(source_links.SourceLinkError):
                    source_links._generate_provider_urls(
                        regex_link,
                        "issue-123.md",
                    )

    def test_duplicate_legacy_sources_cannot_bypass_multiple_mapping_gate(
        self,
    ) -> None:
        unsafe = legacy_sidecar()["sources"][0]
        unsafe.pop("display_name", None)
        unsafe["mappings"].append(
            dict(unsafe["mappings"][0], mapping_id="second")
        )
        safe = legacy_sidecar()["sources"][0]
        raw = {
            "schema_version": source_links.LEGACY_SCHEMA_VERSION,
            "database": "example-rag",
            "revision": 1,
            "sources": [unsafe, safe],
        }
        (self.db_root / source_links.SIDECAR_NAME).write_text(
            json.dumps(raw, ensure_ascii=False),
            encoding="utf-8",
        )
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("invalid", loaded.status)
        enriched = source_links.enrich_search_payload(
            search_payload("Root/a.txt"),
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", enriched["evidence"][0])

    def test_search_semantics_and_order_are_unchanged(self) -> None:
        self.save(sidecar())
        original = search_payload("Root/a.txt")
        enriched = source_links.enrich_search_payload(
            original,
            self.db_root,
            "example-rag",
        )
        self.assertEqual(original["status"], enriched["status"])
        self.assertEqual(original["answerability"], enriched["answerability"])
        self.assertEqual(
            [item["id"] for item in original["evidence"]],
            [item["id"] for item in enriched["evidence"]],
        )
        self.assertEqual(
            [item["id"] for item in original["document_results"]],
            [item["id"] for item in enriched["document_results"]],
        )


if __name__ == "__main__":
    unittest.main()
