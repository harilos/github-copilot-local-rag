from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from software_rag_tool import source_links
from software_rag_tool.confluence_links import install_confluence_link_runtime


class ConfluenceLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        install_confluence_link_runtime(source_links)

    @staticmethod
    def _link(page_urls: object) -> dict[str, object]:
        return {
            "enabled": True,
            "provider": "confluence",
            "strategy": "confluence-page-map",
            "settings": {"page_urls": page_urls},
        }

    def test_runtime_install_is_idempotent(self) -> None:
        validator = source_links._validate_provider_settings
        generator = source_links._generate_provider_urls
        install_confluence_link_runtime(source_links)
        install_confluence_link_runtime(source_links)
        self.assertIs(validator, source_links._validate_provider_settings)
        self.assertIs(generator, source_links._generate_provider_urls)

    def test_exact_page_url_is_preserved_and_resolved_by_decimal_id(self) -> None:
        exact = (
            "https://docs.example.test/wiki/spaces/ENG/pages/12345/"
            "Design+Notes?focusedCommentId=678#comment-678"
        )
        validated = source_links.validate_source_link(
            self._link({"12345": exact, "9": "https://docs.example.test/x/9"})
        )

        self.assertEqual(exact, validated["settings"]["page_urls"]["12345"])
        self.assertEqual(
            {
                "source_provider": "confluence",
                "source_url": exact,
            },
            source_links._generate_provider_urls(validated, "pages/12345.md"),
        )

    def test_only_canonical_positive_decimal_page_ids_are_accepted(self) -> None:
        for page_id in ("", "0", "01", "+1", "-1", "1.0", "１２", 1, True):
            with self.subTest(page_id=page_id):
                with self.assertRaises(source_links.SourceLinkError):
                    source_links.validate_source_link(
                        self._link({page_id: "https://docs.example.test/pages/1"})
                    )

    def test_only_exact_page_map_settings_are_accepted(self) -> None:
        invalid_settings = (
            {},
            {"page_urls": []},
            {
                "page_urls": {"1": "https://docs.example.test/pages/1"},
                "root": "x",
            },
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                link = self._link({})
                link["settings"] = settings
                with self.assertRaises(source_links.SourceLinkError):
                    source_links.validate_source_link(link)

    def test_page_urls_reject_credentials_unsafe_schemes_and_mixed_origins(self) -> None:
        invalid_maps = (
            {"1": "https://user:password@docs.example.test/pages/1"},
            {"1": "javascript:alert(1)"},
            {"1": "https://docs.example.test/pages/1?access_token=secret"},
            {"1": "https://docs.example.test/%2e%2e/admin"},
            {
                "1": "https://docs.example.test/pages/1",
                "2": "https://outside.example.test/pages/2",
            },
        )
        for page_urls in invalid_maps:
            with self.subTest(page_urls=page_urls):
                with self.assertRaises(source_links.SourceLinkError):
                    source_links.validate_source_link(self._link(page_urls))

    def test_non_page_paths_and_unmapped_ids_fail_closed(self) -> None:
        validated = source_links.validate_source_link(
            self._link({"12": "https://docs.example.test/pages/12"})
        )
        for path in (
            "pages/13.md",
            "pages/012.md",
            "pages/12.txt",
            "pages/nested/12.md",
            "other/12.md",
        ):
            with self.subTest(path=path):
                with self.assertRaises(source_links.SourceLinkError):
                    source_links._generate_provider_urls(validated, path)

    def test_page_url_map_cannot_bypass_one_mib_sidecar_limit(self) -> None:
        page_urls = {
            str(index): (
                "https://docs.example.test/pages/"
                + str(index)
                + "/"
                + ("x" * 20)
            )
            for index in range(1, 20_001)
        }
        payload = {
            "schema_version": source_links.SCHEMA_VERSION,
            "revision": 1,
            "sources": [
                {
                    "source_id": "src_confluence-0123456789ab",
                    "source_type": "confluence",
                    "link": {
                        "enabled": True,
                        "strategy": "confluence-page-map",
                        "settings": {"page_urls": page_urls},
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(source_links.SourceLinkError):
                source_links.save_source_links(
                    Path(directory),
                    payload,
                    db_name="example",
                    allow_unmatched_sources=True,
                    expected_revision=0,
                    expected_etag="missing",
                )


if __name__ == "__main__":
    unittest.main()
