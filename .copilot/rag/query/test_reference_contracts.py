from __future__ import annotations

import io
import json
import types
import unittest
from contextlib import redirect_stdout

from reference_contract import (  # noqa: E402
    add_reference_metadata_to_payload,
    install_result_bundle_reference_contract,
    install_search_command_reference_contract,
    preferred_reference_url,
    reference_metadata,
)


class ReferenceContractTests(unittest.TestCase):
    def test_permalink_is_selected_and_only_one_url_is_rendered(self) -> None:
        item = {
            "id": "E1",
            "path": "project/docs/design.md",
            "source_url": "https://example.invalid/blob/main/docs/design.md",
            "source_permalink": (
                "https://example.invalid/blob/"
                "0123456789abcdef/docs/design.md"
            ),
        }
        url, kind = preferred_reference_url(item)
        self.assertEqual(item["source_permalink"], url)
        self.assertEqual("permalink", kind)
        reference = reference_metadata(item)
        self.assertEqual(item["source_permalink"], reference["url"])
        self.assertEqual("permalink", reference["url_kind"])
        self.assertEqual(1, reference["markdown"].count("https://"))
        self.assertNotIn("/blob/main/", reference["markdown"])

    def test_source_url_is_used_when_permalink_is_missing(self) -> None:
        item = {
            "path": "project/docs/specification.pdf",
            "source_url": "https://example.invalid/docs/specification.pdf",
        }
        reference = reference_metadata(item)
        self.assertEqual(item["source_url"], reference["url"])
        self.assertEqual("source_url", reference["url_kind"])
        self.assertEqual(
            "[specification.pdf](https://example.invalid/docs/specification.pdf)",
            reference["markdown"],
        )

    def test_path_only_reference_has_no_url(self) -> None:
        reference = reference_metadata(
            {"path": "project/docs/local-only.txt"}
        )
        self.assertEqual("", reference["url"])
        self.assertEqual("none", reference["url_kind"])
        self.assertEqual(
            "local-only.txt — `project/docs/local-only.txt`",
            reference["markdown"],
        )

    def test_control_character_url_fails_open_to_path(self) -> None:
        reference = reference_metadata(
            {
                "path": "project/docs/file.txt",
                "source_url": "https://example.invalid/file.txt\nsecret",
            }
        )
        self.assertEqual("", reference["url"])
        self.assertNotIn("https://", reference["markdown"])

    def test_payload_projection_adds_reference_and_response_rules(self) -> None:
        payload = {
            "initial_response": {"response_rules": {}},
            "evidence": [
                {
                    "id": "E1",
                    "source": {"path": "root/docs/evidence.md"},
                    "source_url": "https://example.invalid/evidence.md",
                }
            ],
            "background_context": [],
            "document_results": [
                {"id": "D1", "path": "root/docs/related.md"}
            ],
        }
        projected = add_reference_metadata_to_payload(payload)
        self.assertEqual(
            "evidence.md",
            projected["evidence"][0]["reference"]["filename"],
        )
        self.assertEqual(
            "related.md",
            projected["document_results"][0]["reference"]["filename"],
        )
        rules = projected["initial_response"]["response_rules"]
        self.assertTrue(rules["material_citations_required"])
        self.assertTrue(rules["body_urls_forbidden"])
        self.assertTrue(rules["references_footer_required"])
        self.assertTrue(rules["one_url_per_reference"])
        self.assertEqual(
            "reference.markdown",
            rules["preferred_reference_field"],
        )

    def test_result_bundle_patch_projects_summary_detail_and_expanded_item(
        self,
    ) -> None:
        def build_initial_summary(*_args: object, **_kwargs: object):
            return (
                {
                    "initial_response": {"response_rules": {}},
                    "evidence": [
                        {
                            "id": "E1",
                            "path": "root/docs/evidence.md",
                            "source_url": "https://example.invalid/evidence.md",
                        }
                    ],
                },
                [
                    (
                        "E1",
                        "evidence",
                        {
                            "item_id": "E1",
                            "path": "root/docs/evidence.md",
                            "source_url": "https://example.invalid/evidence.md",
                        },
                    )
                ],
            )

        def expanded_item(detail: dict, *, detail_level: str):
            return {
                "item_id": detail["item_id"],
                "path": detail["path"],
                "source_url": detail.get("source_url"),
                "detail_level": detail_level,
            }

        module = types.SimpleNamespace(
            build_initial_summary=build_initial_summary,
            _expanded_item=expanded_item,
        )
        install_result_bundle_reference_contract(module)
        summary, details = module.build_initial_summary()
        self.assertIn("reference", summary["evidence"][0])
        self.assertIn("reference", details[0][2])
        expanded = module._expanded_item(
            details[0][2],
            detail_level="expanded",
        )
        self.assertEqual(
            details[0][2]["reference"],
            expanded["reference"],
        )
        first = module.build_initial_summary
        install_result_bundle_reference_contract(module)
        self.assertIs(first, module.build_initial_summary)

    def test_search_command_patch_projects_final_json(self) -> None:
        output = io.StringIO()

        def print_json(payload: dict, *, ascii_safe: bool) -> None:
            print(
                json.dumps(
                    {"payload": payload, "ascii_safe": ascii_safe},
                    ensure_ascii=False,
                )
            )

        module = types.SimpleNamespace(_print_json=print_json)
        install_search_command_reference_contract(module)
        with redirect_stdout(output):
            module._print_json(
                {
                    "evidence": [
                        {
                            "id": "E1",
                            "path": "root/docs/evidence.md",
                            "source_url": "https://example.invalid/evidence.md",
                        }
                    ]
                },
                ascii_safe=False,
            )
        rendered = json.loads(output.getvalue())
        reference = rendered["payload"]["evidence"][0]["reference"]
        self.assertEqual("evidence.md", reference["filename"])
        self.assertEqual(1, reference["markdown"].count("https://"))


if __name__ == "__main__":
    unittest.main()
