from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_links
from software_rag_tool.source_metadata_migration import (
    migrate_source_metadata,
)


MIGRATION_CLI = RAG_ROOT / "gen_db" / "migrate_source_metadata.py"
ADD_CLI = RAG_ROOT / "gen_db" / "add_data.py"


def _write_catalog(
    db_root: Path,
    sources: dict[str, list[str]],
) -> None:
    connection = sqlite3.connect(db_root / "catalog.sqlite")
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
        row_id = 0
        for source_id, paths in sources.items():
            for stored_path in paths:
                row_id += 1
                connection.execute(
                    """
                    INSERT INTO document(
                        doc_pk, source_id, path, visible_until
                    ) VALUES (?, ?, ?, NULL)
                    """,
                    (row_id, source_id, stored_path),
                )
        connection.commit()
    finally:
        connection.close()


def _legacy_v2_source(
    source_id: str,
    provider: str,
    strategy: str,
    settings: dict[str, Any],
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "display_name": f"{source_id} display",
        "provider": provider,
        "enabled": enabled,
        "strategy": strategy,
        "settings": settings,
    }


class SourceMetadataContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-metadata-"
        )
        self.dbs_root = Path(self.temporary.name) / "dbs"
        self.dbs_root.mkdir()
        self.db_root = self.dbs_root / "example-rag"
        self.db_root.mkdir()
        _write_catalog(
            self.db_root,
            {
                "source-a": ["Example Root/docs/a.md"],
                "source-b": ["Example Root/docs/b.md"],
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_type_and_link_are_independently_optional(self) -> None:
        payload = {
            "schema_version": source_links.SCHEMA_VERSION,
            "revision": 1,
            "sources": [
                {"source_id": "source-a"},
                {
                    "source_id": "source-b",
                    "source_type": "git",
                },
            ],
        }
        normalized = source_links.validate_source_links(
            payload,
            existing_sources={"source-a", "source-b"},
        )
        self.assertNotIn("source_type", normalized["sources"][0])
        self.assertNotIn("link", normalized["sources"][0])
        self.assertEqual(
            "git",
            normalized["sources"][1]["source_type"],
        )
        self.assertNotIn("link", normalized["sources"][1])
        self.assertNotEqual(
            "folder",
            normalized["sources"][0].get("source_type"),
        )

    def test_link_without_type_and_nested_provider_are_rejected(self) -> None:
        base = {
            "schema_version": source_links.SCHEMA_VERSION,
            "revision": 1,
            "sources": [
                {
                    "source_id": "source-a",
                    "link": {
                        "enabled": True,
                        "strategy": "home-only",
                        "settings": {
                            "source_home_url": (
                                "https://fixture.example.invalid"
                            )
                        },
                    },
                }
            ],
        }
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "requires source_type",
        ):
            source_links.validate_source_links(base)
        base["sources"][0]["source_type"] = "other"
        base["sources"][0]["link"]["provider"] = "github"
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "unsupported fields",
        ):
            source_links.validate_source_links(base)
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "unsupported fields",
        ):
            source_links.validate_source_link(base["sources"][0])

    def test_all_source_types_are_accepted_without_links(self) -> None:
        sources = [
            {"source_id": f"source-{index}", "source_type": source_type}
            for index, source_type in enumerate(
                sorted(source_links.ALLOWED_SOURCE_TYPES),
                start=1,
            )
        ]
        normalized = source_links.validate_source_links(
            {
                "schema_version": source_links.SCHEMA_VERSION,
                "revision": 1,
                "sources": sources,
            }
        )
        self.assertEqual(
            sorted(source_links.ALLOWED_SOURCE_TYPES),
            sorted(
                source["source_type"]
                for source in normalized["sources"]
            ),
        )

    def test_v2_migration_is_atomic_idempotent_and_preserves_url(self) -> None:
        legacy = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 4,
            "sources": [
                _legacy_v2_source(
                    "source-a",
                    "github",
                    "github-blob",
                    {
                        "repository_url": (
                            "https://git.example.invalid/group/repository"
                        ),
                        "ref": "main",
                        "permalink_enabled": False,
                    },
                    enabled=True,
                ),
                {"source_id": "source-b", "display_name": "Type unset"},
            ],
        }
        path = self.db_root / source_links.SIDECAR_NAME
        original = (
            json.dumps(
                legacy,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        path.write_bytes(original)

        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertTrue(loaded.migration_required)
        self.assertEqual(
            "github",
            loaded.payload["sources"][0]["source_type"],
        )
        self.assertTrue(
            loaded.payload["sources"][0]["link"]["enabled"]
        )
        self.assertNotIn(
            "provider",
            loaded.payload["sources"][0]["link"],
        )
        self.assertEqual(original, path.read_bytes())
        before = source_links.enrich_search_payload(
            {
                "status": "ok",
                "answerability": "full",
                "evidence": [
                    {
                        "id": "E1",
                        "_source_id": "source-a",
                        "path": "Example Root/docs/a.md",
                    }
                ],
            },
            self.db_root,
            "example-rag",
        )

        preview = migrate_source_metadata(
            self.dbs_root,
            "example-rag",
        )
        self.assertEqual("migration_available", preview["status"])
        self.assertEqual(original, path.read_bytes())

        applied = migrate_source_metadata(
            self.dbs_root,
            "example-rag",
            apply=True,
        )
        self.assertEqual("migrated", applied["status"])
        current = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(source_links.SCHEMA_VERSION, current["schema_version"])
        self.assertEqual(5, current["revision"])
        self.assertEqual(
            original,
            (self.db_root / source_links.BACKUP_NAME).read_bytes(),
        )
        after = source_links.enrich_search_payload(
            {
                "status": "ok",
                "answerability": "full",
                "evidence": [
                    {
                        "id": "E1",
                        "_source_id": "source-a",
                        "path": "Example Root/docs/a.md",
                    }
                ],
            },
            self.db_root,
            "example-rag",
        )
        self.assertEqual(
            before["evidence"][0]["source_url"],
            after["evidence"][0]["source_url"],
        )
        self.assertEqual(
            "already_current",
            migrate_source_metadata(
                self.dbs_root,
                "example-rag",
                apply=True,
            )["status"],
        )

    def test_v2_migration_converts_every_link_provider(self) -> None:
        provider_root = self.dbs_root / "providers-rag"
        provider_root.mkdir()
        definitions = [
            (
                "github",
                "github-blob",
                {
                    "repository_url": (
                        "https://git.example.invalid/group/repository"
                    ),
                    "ref": "main",
                    "permalink_enabled": False,
                },
            ),
            (
                "gitlab",
                "gitlab-blob",
                {
                    "repository_url": (
                        "https://gitlab.example.invalid/group/repository"
                    ),
                    "ref": "release/v1",
                    "permalink_enabled": False,
                },
            ),
            (
                "azure_devops",
                "azure-devops-item",
                {
                    "repository_url": (
                        "https://dev.azure.com/example/project/"
                        "_git/repository"
                    ),
                    "ref": "main",
                    "permalink_enabled": False,
                },
            ),
            (
                "svn",
                "svn-web-root",
                {
                    "repository_url": (
                        "https://svn.example.invalid/web/project"
                    )
                },
            ),
            (
                "sharepoint",
                "append-relative-path",
                {
                    "source_web_root": (
                        "https://sharepoint.example.invalid/sites/"
                        "example/Library"
                    )
                },
            ),
            (
                "redmine",
                "home-only",
                {
                    "source_home_url": (
                        "https://redmine.example.invalid/projects/example"
                    )
                },
            ),
            (
                "other",
                "home-only",
                {
                    "source_home_url": (
                        "https://docs.example.invalid/example"
                    )
                },
            ),
        ]
        source_paths = {
            f"source-{provider}": [
                f"Root {index}/docs/example.md"
            ]
            for index, (provider, _strategy, _settings) in enumerate(
                definitions,
                start=1,
            )
        }
        _write_catalog(provider_root, source_paths)
        legacy = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 7,
            "sources": [
                _legacy_v2_source(
                    f"source-{provider}",
                    provider,
                    strategy,
                    settings,
                    enabled=(provider != "redmine"),
                )
                for provider, strategy, settings in definitions
            ],
        }
        (provider_root / source_links.SIDECAR_NAME).write_text(
            json.dumps(legacy, ensure_ascii=False),
            encoding="utf-8",
        )
        result = migrate_source_metadata(
            self.dbs_root,
            "providers-rag",
            apply=True,
        )
        self.assertEqual("migrated", result["status"])
        saved = json.loads(
            (provider_root / source_links.SIDECAR_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [provider for provider, _strategy, _settings in definitions],
            [source["source_type"] for source in saved["sources"]],
        )
        self.assertFalse(saved["sources"][5]["link"]["enabled"])
        for source in saved["sources"]:
            self.assertEqual(
                {"enabled", "strategy", "settings"},
                set(source["link"]),
            )
            self.assertNotIn("provider", source["link"])

    def test_v2_migration_preserves_unmatched_and_multi_root_links(self) -> None:
        multi_root = self.dbs_root / "multi-root-rag"
        multi_root.mkdir()
        _write_catalog(
            multi_root,
            {
                "mixed-source": [
                    "First Root/a.md",
                    "Second Root/b.md",
                ]
            },
        )
        legacy = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 3,
            "sources": [
                _legacy_v2_source(
                    "mixed-source",
                    "other",
                    "append-relative-path",
                    {
                        "source_web_root": (
                            "https://docs.example.invalid/mixed"
                        )
                    },
                    enabled=False,
                ),
                _legacy_v2_source(
                    "unmatched-source",
                    "other",
                    "home-only",
                    {
                        "source_home_url": (
                            "https://docs.example.invalid/unmatched"
                        )
                    },
                ),
            ],
        }
        (multi_root / source_links.SIDECAR_NAME).write_text(
            json.dumps(legacy),
            encoding="utf-8",
        )
        result = migrate_source_metadata(
            self.dbs_root,
            "multi-root-rag",
            apply=True,
        )
        self.assertEqual("migrated", result["status"])
        saved = json.loads(
            (multi_root / source_links.SIDECAR_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            ["mixed-source", "unmatched-source"],
            [source["source_id"] for source in saved["sources"]],
        )
        self.assertFalse(saved["sources"][0]["link"]["enabled"])

    def test_migrated_multi_root_links_can_be_kept_and_removed(self) -> None:
        root = self.dbs_root / "two-multi-root-rag"
        root.mkdir()
        _write_catalog(
            root,
            {
                "source-a": [
                    "First Root/a.md",
                    "Second Root/a.md",
                ],
                "source-b": [
                    "Third Root/b.md",
                    "Fourth Root/b.md",
                ],
                "source-c": ["Stable Root/c.md"],
            },
        )
        settings = {
            "source_web_root": "https://docs.example.invalid/root",
        }
        legacy = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 2,
            "sources": [
                _legacy_v2_source(
                    "source-a",
                    "other",
                    "append-relative-path",
                    settings,
                ),
                _legacy_v2_source(
                    "source-b",
                    "other",
                    "append-relative-path",
                    settings,
                ),
                {"source_id": "source-c"},
            ],
        }
        (root / source_links.SIDECAR_NAME).write_text(
            json.dumps(legacy),
            encoding="utf-8",
        )
        self.assertEqual(
            "migrated",
            migrate_source_metadata(
                self.dbs_root,
                root.name,
                apply=True,
            )["status"],
        )

        def save_edit(
            edit: Any,
        ) -> dict[str, Any]:
            loaded = source_links.load_source_links(root, root.name)
            self.assertIsNotNone(loaded.payload)
            payload = json.loads(json.dumps(loaded.payload))
            edit(payload)
            payload["revision"] = loaded.revision + 1
            return source_links.save_source_links(
                root,
                payload,
                db_name=root.name,
                existing_sources={"source-a", "source-b", "source-c"},
                expected_revision=loaded.revision,
                expected_etag=loaded.etag,
            )

        saved = save_edit(
            lambda payload: payload["sources"][2].update(
                {"display_name": "Stable source"}
            )
        )
        self.assertEqual("Stable source", saved["sources"][2]["display_name"])

        saved = save_edit(
            lambda payload: payload["sources"][0].pop("link")
        )
        self.assertNotIn("link", saved["sources"][0])
        self.assertIn("link", saved["sources"][1])

        saved = save_edit(
            lambda payload: payload["sources"][1].pop("link")
        )
        self.assertNotIn("link", saved["sources"][1])

        def add_invalid_link(payload: dict[str, Any]) -> None:
            payload["sources"][0]["link"] = {
                "enabled": True,
                "strategy": "append-relative-path",
                "settings": settings,
            }

        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "multiple_observed_roots",
        ):
            save_edit(add_invalid_link)

    def test_v1_and_v2_sharepoint_home_only_migrate_without_output_url(
        self,
    ) -> None:
        for schema in (
            source_links.LEGACY_SCHEMA_VERSION,
            source_links.LEGACY_V2_SCHEMA_VERSION,
        ):
            with self.subTest(schema=schema):
                root = self.dbs_root / (
                    "legacy-v1-rag"
                    if schema == source_links.LEGACY_SCHEMA_VERSION
                    else "legacy-v2-rag"
                )
                root.mkdir()
                _write_catalog(
                    root,
                    {"source-a": ["Example Root/docs/a.md"]},
                )
                flat = _legacy_v2_source(
                    "source-a",
                    "sharepoint",
                    "home-only",
                    {
                        "source_home_url": (
                            "https://sharepoint.example.invalid/"
                            "sites/example"
                        )
                    },
                )
                if schema == source_links.LEGACY_SCHEMA_VERSION:
                    source: dict[str, Any] = {
                        "source_id": "source-a",
                        "mappings": [
                            {
                                "mapping_id": "legacy",
                                "path_prefix": "Example Root/",
                                **{
                                    key: flat[key]
                                    for key in (
                                        "provider",
                                        "enabled",
                                        "strategy",
                                        "settings",
                                    )
                                },
                            }
                        ],
                    }
                    legacy = {
                        "schema_version": schema,
                        "database": root.name,
                        "revision": 1,
                        "sources": [source],
                    }
                else:
                    legacy = {
                        "schema_version": schema,
                        "revision": 1,
                        "sources": [flat],
                    }
                current = root / source_links.SIDECAR_NAME
                original = json.dumps(legacy).encode("utf-8")
                current.write_bytes(original)
                self.assertEqual(
                    "migration_available",
                    migrate_source_metadata(
                        self.dbs_root,
                        root.name,
                    )["status"],
                )
                applied = migrate_source_metadata(
                    self.dbs_root,
                    root.name,
                    apply=True,
                )
                self.assertEqual("migrated", applied["status"])
                saved = json.loads(current.read_text(encoding="utf-8"))
                self.assertEqual(
                    "home-only",
                    saved["sources"][0]["link"]["strategy"],
                )
                self.assertEqual(
                    original,
                    (root / source_links.BACKUP_NAME).read_bytes(),
                )
                enriched = source_links.enrich_search_payload(
                    {
                        "status": "ok",
                        "answerability": "full",
                        "evidence": [
                            {
                                "id": "E1",
                                "_source_id": "source-a",
                                "path": "Example Root/docs/a.md",
                            }
                        ],
                    },
                    root,
                    root.name,
                )
                self.assertNotIn(
                    "source_url",
                    enriched["evidence"][0],
                )
                self.assertEqual("ok", enriched["status"])

    def test_v1_ambiguous_source_blocks_whole_db_without_write(self) -> None:
        legacy = {
            "schema_version": source_links.LEGACY_SCHEMA_VERSION,
            "database": "example-rag",
            "revision": 2,
            "sources": [
                {
                    "source_id": "source-a",
                    "mappings": [
                        {
                            "mapping_id": "first",
                            "enabled": True,
                            "path_prefix": "Example Root/",
                            "provider": "other",
                            "strategy": "home-only",
                            "settings": {
                                "source_home_url": (
                                    "https://fixture.example.invalid/one"
                                )
                            },
                        },
                        {
                            "mapping_id": "second",
                            "enabled": True,
                            "path_prefix": "Example Root/",
                            "provider": "other",
                            "strategy": "home-only",
                            "settings": {
                                "source_home_url": (
                                    "https://fixture.example.invalid/two"
                                )
                            },
                        },
                    ],
                },
                {
                    "source_id": "source-b",
                    "mappings": [],
                },
            ],
        }
        path = self.db_root / source_links.SIDECAR_NAME
        original = json.dumps(legacy).encode("utf-8")
        path.write_bytes(original)
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("manual_required", loaded.status)
        self.assertIsNone(loaded.payload)
        result = migrate_source_metadata(
            self.dbs_root,
            "example-rag",
            apply=True,
        )
        self.assertEqual("manual_required", result["status"])
        self.assertEqual(original, path.read_bytes())
        self.assertFalse(
            (self.db_root / source_links.BACKUP_NAME).exists()
        )

    def test_migration_reports_compare_and_swap_conflict(self) -> None:
        legacy = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 1,
            "sources": [],
        }
        path = self.db_root / source_links.SIDECAR_NAME
        original = json.dumps(legacy).encode("utf-8")
        path.write_bytes(original)
        with mock.patch(
            "software_rag_tool.source_metadata_migration.save_source_links",
            side_effect=source_links.SourceLinkError(
                "source_link_configuration_changed"
            ),
        ):
            result = migrate_source_metadata(
                self.dbs_root,
                "example-rag",
                apply=True,
            )
        self.assertEqual("conflict", result["status"])
        self.assertEqual(original, path.read_bytes())

    def test_cli_scans_all_dbs_or_one_selected_db(self) -> None:
        second = self.dbs_root / "second-rag"
        second.mkdir()
        environment = {
            **os.environ,
            "RAG_DBS_ROOT": str(self.dbs_root),
        }
        all_result = subprocess.run(
            [
                sys.executable,
                str(MIGRATION_CLI),
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
            timeout=15,
        )
        self.assertEqual(0, all_result.returncode, all_result.stderr)
        all_payload = json.loads(all_result.stdout)
        self.assertEqual(
            ["example-rag", "second-rag"],
            [item["db"] for item in all_payload["results"]],
        )
        selected_result = subprocess.run(
            [
                sys.executable,
                str(MIGRATION_CLI),
                "--db",
                "second-rag",
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
            timeout=15,
        )
        self.assertEqual(0, selected_result.returncode, selected_result.stderr)
        selected_payload = json.loads(selected_result.stdout)
        self.assertEqual(
            ["second-rag"],
            [item["db"] for item in selected_payload["results"]],
        )

    def test_cli_never_follows_a_linked_database_directory(self) -> None:
        outside = Path(self.temporary.name) / "outside-rag"
        outside.mkdir()
        _write_catalog(
            outside,
            {"source-a": ["Outside Root/docs/a.md"]},
        )
        legacy = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 1,
            "sources": [],
        }
        active = outside / source_links.SIDECAR_NAME
        original = json.dumps(legacy).encode("utf-8")
        active.write_bytes(original)
        linked = self.dbs_root / "linked-rag"
        try:
            os.symlink(
                outside,
                linked,
                target_is_directory=True,
            )
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"directory links unavailable: {type(exc).__name__}")
        environment = {
            **os.environ,
            "RAG_DBS_ROOT": str(self.dbs_root),
        }
        for arguments in (
            ["--db", "linked-rag", "--apply", "--format", "json"],
            ["--apply", "--format", "json"],
        ):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(MIGRATION_CLI),
                        *arguments,
                    ],
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                    timeout=15,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                result = next(
                    item
                    for item in json.loads(completed.stdout)["results"]
                    if item["db"] == "linked-rag"
                )
                self.assertEqual("invalid", result["status"])
                self.assertEqual(
                    "unsafe_database_root",
                    result["error"],
                )
                self.assertEqual(original, active.read_bytes())
                self.assertFalse(
                    (outside / source_links.BACKUP_NAME).exists()
                )

    def test_add_cli_does_not_gain_source_type_metadata_options(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ADD_CLI), "--help"],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("--source-type", completed.stdout)


if __name__ == "__main__":
    unittest.main()
