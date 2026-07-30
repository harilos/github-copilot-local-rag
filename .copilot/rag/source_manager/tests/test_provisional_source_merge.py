from __future__ import annotations

import unittest
from typing import Any

from source_manager.provisional_source_merge import (
    _install_manager_merge,
    merge_provisional_source_records,
)


LOCAL_KEY = "src_project-alpha-0123456789ab"


def _catalog_record(source_id: str = LOCAL_KEY) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "display_name": source_id,
        "source_type": "",
        "document_count": 5,
        "chunk_count": 12,
        "observed_stored_roots": [f"{source_id}/"],
        "link_status": "not_configured",
        "_catalog_present": True,
    }


def _provisional_record(local_key: str = LOCAL_KEY) -> dict[str, Any]:
    return {
        "schema_version": "local-rag-source-manager-v1",
        "local_source_key": local_key,
        "source_id": None,
        "source_type": "redmine",
        "display_name": "Project Alpha",
        "fetch": {"project_url": "https://redmine.example/projects/alpha"},
        "_local_source_key": local_key,
        "_state": {
            "status": "interrupted",
            "can_resume": True,
            "last_error": "KeyboardInterrupt",
        },
        "_catalog_present": False,
    }


class ProvisionalSourceMergeTests(unittest.TestCase):
    def test_partial_catalog_source_is_collapsed_into_human_named_source(self) -> None:
        values = merge_provisional_source_records(
            [_catalog_record(), _provisional_record()]
        )

        self.assertEqual(1, len(values))
        source = values[0]
        self.assertEqual("Project Alpha", source["display_name"])
        self.assertEqual("redmine", source["source_type"])
        self.assertEqual(LOCAL_KEY, source["source_id"])
        self.assertEqual(LOCAL_KEY, source["_local_source_key"])
        self.assertEqual(5, source["document_count"])
        self.assertEqual(12, source["chunk_count"])
        self.assertTrue(source["_catalog_present"])
        self.assertTrue(source["_provisional_catalog_identity"])
        self.assertTrue(source["_state"]["can_resume"])

    def test_unrelated_catalog_and_provisional_sources_remain_separate(self) -> None:
        values = merge_provisional_source_records(
            [_catalog_record("existing-source"), _provisional_record()]
        )

        self.assertEqual(2, len(values))
        self.assertFalse(
            any(value.get("_provisional_catalog_identity") for value in values)
        )

    def test_manager_wrapper_keeps_initial_resume_status(self) -> None:
        records = [_catalog_record(), _provisional_record()]

        class FakeManager:
            def _combined_source_records(
                self,
                _db_name: str,
                _catalog_sources: list[dict[str, Any]],
            ) -> list[dict[str, Any]]:
                return list(records)

            def _source_manager_status(self, _source: dict[str, Any]) -> str:
                return "base-status"

        _install_manager_merge(FakeManager)
        manager = FakeManager()
        values = manager._combined_source_records("example-rag", [])

        self.assertEqual(1, len(values))
        self.assertEqual(
            "初回取得途中・再開可能",
            manager._source_manager_status(values[0]),
        )

    def test_manager_wrapper_forwards_classification_keyword(self) -> None:
        records = [_catalog_record("secret-source")]
        received: dict[str, str] | None = None

        class FakeManager:
            def _combined_source_records(
                self,
                _db_name: str,
                _catalog_sources: list[dict[str, Any]],
                *,
                classifications: dict[str, str] | None = None,
            ) -> list[dict[str, Any]]:
                nonlocal received
                received = classifications
                return list(records)

            def _source_manager_status(self, _source: dict[str, Any]) -> str:
                return "base-status"

        _install_manager_merge(FakeManager)
        values = FakeManager()._combined_source_records(
            "example-rag",
            [],
            classifications={"secret-source": "secret"},
        )

        self.assertEqual(
            {"secret-source": "secret"},
            received,
        )
        self.assertEqual(records, values)

    def test_patch_is_idempotent(self) -> None:
        class FakeManager:
            def _combined_source_records(
                self,
                _db_name: str,
                _catalog_sources: list[dict[str, Any]],
            ) -> list[dict[str, Any]]:
                return [_catalog_record(), _provisional_record()]

            def _source_manager_status(self, _source: dict[str, Any]) -> str:
                return "base-status"

        _install_manager_merge(FakeManager)
        first = FakeManager._combined_source_records
        _install_manager_merge(FakeManager)

        self.assertIs(first, FakeManager._combined_source_records)
        self.assertEqual(
            1,
            len(FakeManager()._combined_source_records("example-rag", [])),
        )


if __name__ == "__main__":
    unittest.main()
