from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from source_manager import packages
from source_manager.store import (
    CLASSIFICATION_FILE_NAME,
    SECRET_CLASSIFICATION,
    SourceStore,
)


class SourceClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-classification-"
        )
        self.root = Path(self.temporary.name)
        self.copilot_home = self.root / "copilot"
        self.dbs_root = self.copilot_home / "rag" / "dbs"
        self.database = self._make_database("example-rag")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_database(self, name: str) -> Path:
        database = self.dbs_root / name
        database.mkdir(parents=True)
        (database / "db.json").write_text(
            json.dumps(
                {
                    "db_name": name,
                    "collection": name.replace("-", "_"),
                }
            ),
            encoding="utf-8",
        )
        (database / "VERSION.json").write_text(
            json.dumps(
                {
                    "schema": "local-rag.db-version.v1",
                    "db_name": name,
                    "collection": name.replace("-", "_"),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with sqlite3.connect(database / "catalog.sqlite"):
            pass
        return database

    def test_unset_is_unrestricted_and_secret_can_be_cleared(self) -> None:
        store = SourceStore(self.database)
        missing = store.read_source_classifications()
        self.assertEqual({}, missing.payload)

        secret = store.save_source_classification(
            "source-a",
            SECRET_CLASSIFICATION,
            expected_revision=missing.revision,
            expected_etag=missing.etag,
        )
        self.assertEqual(
            [
                {
                    "source_id": "source-a",
                    "classification": SECRET_CLASSIFICATION,
                }
            ],
            secret.payload["sources"],
        )

        cleared = store.save_source_classification(
            "source-a",
            "",
            expected_revision=secret.revision,
            expected_etag=secret.etag,
        )
        self.assertEqual([], cleared.payload["sources"])

    def test_classification_is_admin_only_package_data(self) -> None:
        store = SourceStore(self.database)
        loaded = store.read_source_classifications()
        store.save_source_classification(
            "source-a",
            SECRET_CLASSIFICATION,
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )
        self._make_database("not-selected-rag")
        archive = self.root / "selected.zip"
        administration_root = self.root / "selected-admin"
        with mock.patch.object(
            packages,
            "_product_entries",
            return_value=[],
        ):
            distribution_result = packages.create_distribution_package(
                self.copilot_home,
                archive,
                db_names=["example-rag"],
            )
            administration_result = packages.create_admin_transfer_package(
                self.copilot_home,
                administration_root,
                db_names=["example-rag"],
            )

        self.assertEqual(
            ["example-rag"],
            [
                item["name"]
                for item in distribution_result["manifest"]["dbs"]
            ],
        )
        self.assertEqual(
            ["example-rag"],
            [
                item["name"]
                for item in administration_result["manifest"]["dbs"]
            ],
        )
        suffix = f"sources/{CLASSIFICATION_FILE_NAME}"
        with zipfile.ZipFile(archive) as package:
            distribution_paths = {
                value.rstrip("/")
                for value in package.namelist()
                if value.rstrip("/")
            }
        self.assertFalse(
            any(path.endswith(suffix) for path in distribution_paths)
        )
        self.assertFalse(
            any("not-selected-rag" in path for path in distribution_paths)
        )
        administration_paths = {
            str(record["path"])
            for record in administration_result["manifest"]["files"]
        }
        self.assertTrue(
            any(path.endswith(suffix) for path in administration_paths)
        )
        self.assertFalse(
            any("not-selected-rag" in path for path in administration_paths)
        )
        packages.validate_package_tree(
            administration_root,
            expected_kind=packages._ADMIN_KIND,
        )


if __name__ == "__main__":
    unittest.main()
