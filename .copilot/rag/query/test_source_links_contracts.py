from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_links


def mapping(
    *,
    prefix: str = "Source Root/",
    provider: str = "sharepoint",
    strategy: str = "append-relative-path",
    settings: dict | None = None,
    enabled: bool = True,
) -> dict:
    return {
        "mapping_id": str(uuid.uuid4()),
        "enabled": enabled,
        "path_prefix": prefix,
        "provider": provider,
        "strategy": strategy,
        "settings": settings
        or {
            "source_home_url": "https://tenant.example.invalid/sites/example",
            "source_web_root": (
                "https://tenant.example.invalid/sites/example/Library/Folder"
            ),
        },
    }


def sidecar(*mappings: dict, revision: int = 1) -> dict:
    return {
        "schema_version": "rag-source-links-v1",
        "database": "example-rag",
        "revision": revision,
        "sources": [
            {
                "source_id": "source-a",
                "display_name": "Example Source",
                "mappings": list(mappings),
            }
        ],
    }


def search_payload(path: str, source_id: str = "source-a") -> dict:
    return {
        "status": "ok",
        "answerability": "full",
        "evidence": [
            {
                "id": "R1",
                "_source_id": source_id,
                "source": {"path": path},
                "text": "Evidence text.",
            }
        ],
        "background_context": [],
        "related_context": [],
        "document_results": [
            {
                "_source_id": source_id,
                "path": path,
                "title": "Example",
            }
        ],
        "_result_detail_items": [
            {
                "_source_id": source_id,
                "path": path,
                "matched_excerpt": "Evidence text.",
            }
        ],
    }


class SourceLinksContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-links-test-"
        )
        self.db_root = Path(self.temporary.name) / "example-rag"
        self.db_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def save(self, payload: dict) -> dict:
        return source_links.save_source_links(
            self.db_root,
            payload,
            db_name="example-rag",
            existing_sources={"source-a"},
            observed_paths={
                "source-a": [
                    "Source Root/docs/設計 #1 (final).pdf",
                    "Source Root/special/item.txt",
                    "Source Root/docs/secret/item.txt",
                ]
            },
        )

    def test_absent_sidecar_is_unconfigured_and_path_only(self) -> None:
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("unconfigured", loaded.status)
        payload = search_payload("Source Root/docs/example.pdf")
        enriched = source_links.enrich_search_payload(
            payload,
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", enriched["evidence"][0])
        self.assertNotIn("_source_id", enriched["evidence"][0])
        self.assertEqual("ok", enriched["status"])

    def test_absent_sidecar_preserves_public_result_fields_and_order(self) -> None:
        payload = search_payload("Source Root/docs/example.pdf")
        for key in (
            "evidence",
            "document_results",
            "_result_detail_items",
        ):
            for item in payload[key]:
                item.pop("_source_id", None)
        before = json.loads(json.dumps(payload))
        enriched = source_links.enrich_search_payload(
            payload,
            self.db_root,
            "example-rag",
        )
        self.assertEqual(before, enriched)

    def test_first_save_is_utf8_atomic_and_second_save_keeps_backup(
        self,
    ) -> None:
        first = sidecar(mapping())
        self.save(first)
        current = self.db_root / "source-links.json"
        self.assertTrue(current.exists())
        self.assertFalse((self.db_root / "source-links.json.tmp").exists())
        self.assertEqual(
            "Example Source",
            json.loads(current.read_text(encoding="utf-8"))["sources"][0][
                "display_name"
            ],
        )
        second = sidecar(mapping(enabled=False), revision=2)
        self.save(second)
        backup = json.loads(
            (self.db_root / "source-links.json.bak").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, backup["revision"])
        self.assertEqual(2, json.loads(current.read_text())["revision"])
        if os.name != "nt":
            self.assertEqual(0, current.stat().st_mode & 0o077)

    def test_sharepoint_encodes_each_relative_path_segment(self) -> None:
        self.save(sidecar(mapping()))
        path = "Source Root/docs/設計 #1 (final).pdf"
        enriched = source_links.enrich_search_payload(
            search_payload(path),
            self.db_root,
            "example-rag",
        )
        url = enriched["evidence"][0]["source_url"]
        self.assertIn(
            "%E8%A8%AD%E8%A8%88%20%231%20%28final%29.pdf",
            url,
        )
        self.assertEqual(
            "sharepoint",
            enriched["evidence"][0]["source_provider"],
        )
        self.assertEqual(
            url,
            enriched["document_results"][0]["source_url"],
        )
        self.assertEqual(
            url,
            enriched["_result_detail_items"][0]["source_url"],
        )

    def test_longest_prefix_wins_and_disabled_mapping_is_ignored(self) -> None:
        broad = mapping(
            settings={
                "source_web_root": "https://example.invalid/general"
            }
        )
        narrow = mapping(
            prefix="Source Root/docs/",
            provider="other",
            settings={
                "source_web_root": "https://example.invalid/specific"
            },
        )
        disabled = mapping(
            prefix="Source Root/docs/secret/",
            provider="other",
            settings={
                "source_web_root": "https://example.invalid/disabled"
            },
            enabled=False,
        )
        self.save(sidecar(broad, narrow, disabled))
        enriched = source_links.enrich_search_payload(
            search_payload("Source Root/docs/設計 #1 (final).pdf"),
            self.db_root,
            "example-rag",
        )
        self.assertTrue(
            enriched["evidence"][0]["source_url"].startswith(
                "https://example.invalid/specific/"
            )
        )

    def test_equal_priority_is_ambiguous_and_fails_open(self) -> None:
        payload = sidecar(mapping())
        duplicate = mapping()
        payload["sources"][0]["mappings"].append(duplicate)
        # Simulate a hand-edited sidecar. Save validation correctly rejects it.
        (self.db_root / "source-links.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        enriched = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
            explain=True,
        )
        self.assertNotIn("source_url", enriched["evidence"][0])
        self.assertEqual(
            "ambiguous",
            enriched["evidence"][0]["source_link_status"],
        )

    def test_mapping_never_crosses_source_id(self) -> None:
        self.save(sidecar(mapping()))
        enriched = source_links.enrich_search_payload(
            search_payload(
                "Source Root/docs/example.pdf",
                source_id="source-b",
            ),
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", enriched["evidence"][0])

    def test_invalid_changed_or_deleted_sidecar_drops_cached_link(self) -> None:
        self.save(sidecar(mapping()))
        initial = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
        )
        self.assertIn("source_url", initial["evidence"][0])
        path = self.db_root / "source-links.json"
        path.write_text("{invalid", encoding="utf-8")
        invalid = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", invalid["evidence"][0])
        path.unlink()
        missing = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
        )
        self.assertNotIn("source_url", missing["evidence"][0])

    def test_valid_sidecar_update_is_visible_without_process_restart(
        self,
    ) -> None:
        self.save(sidecar(mapping()))
        first = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
        )
        updated = mapping(
            settings={
                "source_home_url": "https://tenant.example.invalid/sites/example",
                "source_web_root": (
                    "https://tenant.example.invalid/sites/example/Library/New"
                ),
            }
        )
        self.save(sidecar(updated, revision=2))
        second = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
        )
        self.assertNotEqual(
            first["evidence"][0]["source_url"],
            second["evidence"][0]["source_url"],
        )
        self.assertIn(
            "/Library/New/",
            second["evidence"][0]["source_url"],
        )

    def test_cache_is_scoped_by_expected_database_identity(self) -> None:
        self.save(sidecar(mapping()))
        permissive = source_links.load_source_links(self.db_root, None)
        mismatched = source_links.load_source_links(
            self.db_root,
            "different-rag",
        )
        matching = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("configured", permissive.status)
        self.assertEqual("invalid", mismatched.status)
        self.assertEqual("configured", matching.status)

    def test_cached_payload_is_not_mutable_by_a_caller(self) -> None:
        self.save(sidecar(mapping()))
        first = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        assert first.payload is not None
        first.payload["sources"][0]["display_name"] = "Unsaved mutation"
        second = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        assert second.payload is not None
        self.assertEqual(
            "Example Source",
            second.payload["sources"][0]["display_name"],
        )

    def test_backup_is_never_used_as_active_configuration(self) -> None:
        (self.db_root / "source-links.json.bak").write_text(
            json.dumps(sidecar(mapping())),
            encoding="utf-8",
        )
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("unconfigured", loaded.status)

    def test_sharepoint_browser_url_is_normalized(self) -> None:
        item = mapping(
            settings={
                "source_home_url": (
                    "https://tenant.example.invalid/sites/example"
                ),
                "source_web_root": (
                    "https://tenant.example.invalid/sites/example/"
                    "Library/Forms/AllItems.aspx"
                    "?id=%2Fsites%2Fexample%2FLibrary%2FFolder"
                ),
            }
        )
        normalized = source_links.validate_mapping(item)
        self.assertEqual(
            "https://tenant.example.invalid/sites/example/Library/Folder",
            normalized["settings"]["source_web_root"],
        )

    def test_sharepoint_opaque_sharing_url_is_not_a_path_root(self) -> None:
        item = mapping(
            settings={
                "source_web_root": (
                    "https://tenant.example.invalid/:f:/r/sites/example/item"
                )
            }
        )
        with self.assertRaises(source_links.SourceLinkError):
            source_links.validate_mapping(item)

    def test_github_is_manual_and_permalink_requires_explicit_commit(
        self,
    ) -> None:
        item = mapping(
            provider="github",
            strategy="github-blob",
            settings={
                "repository_url": "https://git.example.invalid/team/repo.git",
                "ref": "release/v1",
                "repository_path_prefix": "docs",
                "commit": "0123456789abcdef",
                "permalink_enabled": True,
            },
        )
        self.save(sidecar(item))
        enriched = source_links.enrich_search_payload(
            search_payload("Source Root/docs/設計 #1 (final).pdf"),
            self.db_root,
            "example-rag",
        )
        evidence = enriched["evidence"][0]
        self.assertIn("/blob/release/v1/", evidence["source_url"])
        self.assertIn(
            "/blob/0123456789abcdef/",
            evidence["source_permalink"],
        )
        self.assertNotIn(".git/", evidence["source_url"])

    def test_github_permalink_is_absent_until_explicitly_enabled(self) -> None:
        item = mapping(
            provider="github",
            strategy="github-blob",
            settings={
                "repository_url": "https://git.example.invalid/team/repo",
                "ref": "release/v1",
                "permalink_enabled": False,
            },
        )
        self.save(sidecar(item))
        enriched = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
        )
        self.assertIn("source_url", enriched["evidence"][0])
        self.assertNotIn("source_permalink", enriched["evidence"][0])

    def test_home_only_never_exposes_manager_home_url(self) -> None:
        item = mapping(
            provider="other",
            strategy="home-only",
            settings={"source_home_url": "https://example.invalid/home"},
        )
        self.save(sidecar(item))
        enriched = source_links.enrich_search_payload(
            search_payload("Source Root/docs/example.pdf"),
            self.db_root,
            "example-rag",
        )
        serialized = json.dumps(enriched)
        self.assertNotIn("https://example.invalid/home", serialized)
        self.assertNotIn("source_url", enriched["evidence"][0])

    def test_regex_template_and_unmatched_path(self) -> None:
        item = mapping(
            provider="redmine",
            strategy="regex-template",
            settings={
                "path_pattern": r"(?P<id>[0-9]+)",
                "url_template": "https://tracker.example.invalid/items/{id}",
            },
        )
        normalized = source_links.validate_mapping(item)
        matched = source_links.resolve_mapping_preview(
            normalized,
            ["Source Root/docs/123.txt"],
        )
        self.assertEqual(
            "https://tracker.example.invalid/items/123",
            matched[0]["source_url"],
        )
        unmatched = source_links.resolve_mapping_preview(
            normalized,
            ["Source Root/docs/no-id.txt"],
        )
        self.assertEqual("unconfigured", unmatched[0]["status"])

    def test_regex_template_rejects_quantified_groups(self) -> None:
        item = mapping(
            provider="redmine",
            strategy="regex-template",
            settings={
                "path_pattern": r"(?P<id>(a+)+$)",
                "url_template": "https://tracker.example.invalid/items/{id}",
            },
        )
        with self.assertRaises(source_links.SourceLinkError):
            source_links.validate_mapping(item)

        repeated = mapping(
            provider="redmine",
            strategy="regex-template",
            settings={
                "path_pattern": r"(?P<id>a*a*a*a*b)",
                "url_template": "https://tracker.example.invalid/items/{id}",
            },
        )
        with self.assertRaises(source_links.SourceLinkError):
            source_links.validate_mapping(repeated)

        many_groups = mapping(
            provider="redmine",
            strategy="regex-template",
            settings={
                "path_pattern": (
                    r"(?P<a>a*)(?P<b>a*)(?P<c>a*)"
                    r"(?P<d>a*)(?P<e>a*)(?P<f>a*)b"
                ),
                "url_template": "https://tracker.example.invalid/items/{a}",
            },
        )
        with self.assertRaises(source_links.SourceLinkError):
            source_links.validate_mapping(many_groups)

        many_repetitions = mapping(
            provider="redmine",
            strategy="regex-template",
            settings={
                "path_pattern": (
                    r"(?P<a>a{0,100}a{0,100}a{0,100}"
                    r"a{0,100}a{0,100}b)"
                ),
                "url_template": "https://tracker.example.invalid/items/{a}",
            },
        )
        with self.assertRaises(source_links.SourceLinkError):
            source_links.validate_mapping(many_repetitions)

    def test_malformed_template_sidecar_fails_open(self) -> None:
        payload = sidecar(
            mapping(
                provider="redmine",
                strategy="regex-template",
                settings={
                    "path_pattern": r"(?P<id>[0-9]+)",
                    "url_template": "https://example.invalid/items/{id",
                },
            )
        )
        (self.db_root / "source-links.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        enriched = source_links.enrich_search_payload(
            search_payload("Source Root/docs/123.txt"),
            self.db_root,
            "example-rag",
        )

        self.assertEqual("invalid", loaded.status)
        self.assertNotIn("source_url", enriched["evidence"][0])
        self.assertEqual("ok", enriched["status"])

    def test_deeply_nested_optional_sidecar_fails_open(self) -> None:
        nested = "{}"
        for _index in range(1_100):
            nested = '{"nested":' + nested + "}"
        raw = (
            '{"schema_version":"rag-source-links-v1",'
            '"database":"example-rag","revision":1,'
            '"sources":[{"source_id":"source-a","mappings":['
            '{"mapping_id":"'
            + str(uuid.uuid4())
            + '","enabled":true,"path_prefix":"Source Root/",'
            '"provider":"other","strategy":"home-only","settings":'
            + nested
            + "}]}]}"
        )
        (self.db_root / "source-links.json").write_text(
            raw,
            encoding="utf-8",
        )

        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )

        self.assertEqual("invalid", loaded.status)

    def test_regex_template_rejects_format_specs_and_sensitive_fragments(
        self,
    ) -> None:
        values = [
            mapping(
                provider="redmine",
                strategy="regex-template",
                settings={
                    "path_pattern": r"(?P<id>[0-9]+)",
                    "url_template": (
                        "https://tracker.example.invalid/items/{id:1000000000}"
                    ),
                },
            ),
            mapping(
                provider="redmine",
                strategy="regex-template",
                settings={
                    "path_pattern": r"(?P<id>[0-9]+)",
                    "url_template": (
                        "https://tracker.example.invalid/items/{id}"
                        "#access_token=not-allowed"
                    ),
                },
            ),
        ]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(source_links.SourceLinkError):
                    source_links.validate_mapping(value)

    def test_credentials_absolute_paths_and_traversal_are_rejected(
        self,
    ) -> None:
        bad_values = [
            mapping(
                settings={
                    "source_web_root": (
                        "https://user:password@example.invalid/root"
                    )
                }
            ),
            mapping(prefix="../outside/"),
            mapping(prefix="/absolute/source/root"),
            mapping(prefix=r"C:\source\root"),
            mapping(
                provider="other",
                settings={
                    "source_web_root": "https://example.invalid/root",
                    "access_token": "not-allowed",
                },
            ),
            mapping(
                provider="other",
                settings={
                    "source_web_root": (
                        "https://example.invalid/root?access_token=not-allowed"
                    ),
                },
            ),
            mapping(
                provider="other",
                settings={
                    "source_web_root": (
                        "https://example.invalid/root?view=browser"
                    ),
                },
            ),
            mapping(
                provider="other",
                settings={
                    "source_web_root": "https://example.invalid/root folder",
                },
            ),
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(source_links.SourceLinkError):
                    source_links.validate_mapping(value)

    def test_save_can_preserve_unmatched_existing_settings(self) -> None:
        payload = sidecar(mapping())
        payload["sources"].append(
            {
                "source_id": "not-indexed",
                "mappings": [
                    mapping(
                        prefix="Another Root/",
                        settings={
                            "source_web_root": (
                                "https://example.invalid/another"
                            )
                        },
                    )
                ],
            }
        )
        source_links.save_source_links(
            self.db_root,
            payload,
            db_name="example-rag",
            existing_sources=["source-a"],
            observed_paths={
                "source-a": ["Source Root/docs/example.pdf"],
            },
            allow_unmatched_sources=True,
        )
        loaded = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        self.assertEqual("configured", loaded.status)
        self.assertEqual(
            {"source-a", "not-indexed"},
            {
                item["source_id"]
                for item in (loaded.payload or {}).get("sources") or []
            },
        )

    def test_expected_revision_prevents_lost_updates(self) -> None:
        self.save(sidecar(mapping(), revision=1))
        first_editor = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        second_editor = source_links.load_source_links(
            self.db_root,
            "example-rag",
        )
        assert first_editor.payload is not None
        assert second_editor.payload is not None
        first_editor.payload["revision"] = 2
        source_links.save_source_links(
            self.db_root,
            first_editor.payload,
            db_name="example-rag",
            existing_sources=["source-a"],
            expected_revision=1,
        )
        second_editor.payload["revision"] = 2
        with self.assertRaises(source_links.SourceLinkError):
            source_links.save_source_links(
                self.db_root,
                second_editor.payload,
                db_name="example-rag",
                existing_sources=["source-a"],
                expected_revision=1,
            )

    def test_invalid_path_affects_only_its_own_result(self) -> None:
        self.save(sidecar(mapping()))
        payload = search_payload("Source Root/docs/example.pdf")
        payload["evidence"].insert(
            0,
            {
                "id": "R0",
                "_source_id": "source-a",
                "source": {"path": "../invalid.txt"},
                "text": "Invalid path.",
            },
        )
        enriched = source_links.enrich_search_payload(
            payload,
            self.db_root,
            "example-rag",
            explain=True,
        )
        self.assertNotIn("source_url", enriched["evidence"][0])
        self.assertEqual(
            "resolution_failed",
            enriched["evidence"][0]["source_link_status"],
        )
        self.assertIn("source_url", enriched["evidence"][1])


if __name__ == "__main__":
    unittest.main()
