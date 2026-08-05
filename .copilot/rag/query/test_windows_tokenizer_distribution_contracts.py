from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RAG_ROOT.parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import windows_distribution  # noqa: E402
from source_manager.windows_tokenizer_contract import (  # noqa: E402
    DatabaseTokenizerCompatibilityError,
    load_tokenizer_contract,
    normalize_distribution_name,
    validate_distribution_database,
    validate_runtime_tokenizer_packages,
)


def _write_database(root: Path, name: str = "fixture-rag") -> Path:
    contract = load_tokenizer_contract(windows_distribution.LOCK_PATH)
    database = root / name
    (database / "index").mkdir(parents=True)
    (database / "VERSION.json").write_text(
        json.dumps(
            {
                "schema": "local-rag.db-version.v1",
                "db_name": name,
                "tokenizer": contract.fingerprint,
                "tokenizer_config": contract.descriptor,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (database / "index" / "manifest.json").write_text(
        json.dumps(
            {
                "catalog_schema_version": contract.catalog_schema_version,
                "tokenizer": contract.fingerprint,
                "tokenizer_config": contract.descriptor,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (database / "db.json").write_text(
        json.dumps({"db_name": name, "collection": name.replace("-", "_")})
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(database / "catalog.sqlite")
    try:
        connection.execute("CREATE TABLE database_meta (key TEXT, value TEXT)")
        connection.executemany(
            "INSERT INTO database_meta VALUES (?, ?)",
            (
                ("schema_version", str(contract.catalog_schema_version)),
                ("tokenizer", contract.fingerprint),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class WindowsTokenizerDistributionContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_tokenizer_contract(windows_distribution.LOCK_PATH)

    def test_lock_is_single_source_for_all_tokenizer_constraints(self) -> None:
        repository_lock = json.loads(
            (REPOSITORY_ROOT / "tools/windows_portable/runtime-lock.json").read_text(
                encoding="utf-8"
            )
        )
        product_lock = json.loads(
            windows_distribution.LOCK_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(product_lock, repository_lock)
        self.assertEqual(
            "sudachi-a-v3-tf:sudachipy-0.6.10:sudachidict-core-20250515",
            self.contract.fingerprint,
        )
        requirements = (
            RAG_ROOT / "gen_db/software_rag_tool/requirements.txt"
        ).read_text(encoding="utf-8").splitlines()
        query_requirements = windows_distribution.SEARCH_REQUIREMENTS.read_text(
            encoding="utf-8"
        ).splitlines()
        portable_requirements = (
            REPOSITORY_ROOT / "tools/windows_portable/requirements-search.lock"
        ).read_text(encoding="utf-8").splitlines()
        expected = {"SudachiPy==0.6.10", "SudachiDict-core==20250515"}
        for lines in (requirements, query_requirements, portable_requirements):
            self.assertTrue(expected.issubset(set(lines)))
            self.assertFalse(any(line.startswith("SudachiPy>=") for line in lines))
            self.assertFalse(
                any(line.startswith("SudachiDict-core>=") for line in lines)
            )
        project = tomllib.loads(
            (RAG_ROOT / "gen_db/software_rag_tool/pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(
            expected.issubset(set(project["project"]["dependencies"]))
        )
        self.assertEqual("sudachidict-core", normalize_distribution_name("SudachiDict_core"))

    def test_runtime_versions_must_match_lock_exactly(self) -> None:
        versions = {
            "SudachiPy": "0.6.10",
            "SudachiDict-core": "20250515",
        }
        self.assertEqual(
            {
                "sudachipy": "0.6.10",
                "sudachidict-core": "20250515",
            },
            validate_runtime_tokenizer_packages(
                self.contract,
                version_provider=versions.__getitem__,
            ),
        )
        versions["SudachiDict-core"] = "20240716"
        with self.assertRaisesRegex(
            RuntimeError, "windows_runtime_tokenizer_dependency_mismatch"
        ):
            validate_runtime_tokenizer_packages(
                self.contract,
                version_provider=versions.__getitem__,
            )

    def test_compatible_version_manifest_and_catalog_pass_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = _write_database(Path(directory))
            before = _tree_digest(database)
            result = validate_distribution_database(
                database,
                database.name,
                contract=self.contract,
            )
            self.assertEqual("pass", result["status"])
            self.assertEqual(before, _tree_digest(database))

    def test_missing_old_fallback_corrupt_and_wal_fixtures_fail_closed(self) -> None:
        cases = (
            "version_missing",
            "version_old_v2",
            "manifest_missing",
            "manifest_old_v2",
            "manifest_fallback",
            "catalog_other_version",
            "catalog_duplicate",
            "catalog_corrupt",
            "catalog_wal",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                database = _write_database(Path(directory))
                version_path = database / "VERSION.json"
                manifest_path = database / "index/manifest.json"
                catalog_path = database / "catalog.sqlite"
                if case == "version_missing":
                    payload = _json(version_path)
                    payload.pop("tokenizer")
                    _write_json(version_path, payload)
                elif case == "version_old_v2":
                    payload = _json(version_path)
                    payload["tokenizer"] = "sudachi-a-v2"
                    _write_json(version_path, payload)
                elif case == "manifest_missing":
                    manifest_path.unlink()
                elif case == "manifest_old_v2":
                    payload = _json(manifest_path)
                    payload["tokenizer"] = "sudachi-a-v2"
                    _write_json(manifest_path, payload)
                elif case == "manifest_fallback":
                    payload = _json(manifest_path)
                    payload["tokenizer"] = "fallback-cjk-ngram-v3-tf-explicit"
                    _write_json(manifest_path, payload)
                elif case == "catalog_other_version":
                    connection = sqlite3.connect(catalog_path)
                    connection.execute(
                        "UPDATE database_meta SET value = ? WHERE key = 'tokenizer'",
                        ("sudachi-a-v3-tf:sudachipy-0.6.10:sudachidict-core-20240716",),
                    )
                    connection.commit()
                    connection.close()
                elif case == "catalog_duplicate":
                    connection = sqlite3.connect(catalog_path)
                    connection.execute(
                        "INSERT INTO database_meta VALUES ('tokenizer', ?)",
                        (self.contract.fingerprint,),
                    )
                    connection.commit()
                    connection.close()
                elif case == "catalog_corrupt":
                    catalog_path.write_bytes(b"not sqlite")
                elif case == "catalog_wal":
                    Path(str(catalog_path) + "-wal").write_bytes(b"pending")
                before = _tree_digest(database)
                with self.assertRaises(DatabaseTokenizerCompatibilityError):
                    validate_distribution_database(
                        database,
                        database.name,
                        contract=self.contract,
                    )
                self.assertEqual(before, _tree_digest(database))

    def test_error_is_bounded_and_does_not_echo_arbitrary_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = _write_database(Path(directory))
            payload = _json(database / "index/manifest.json")
            payload["tokenizer"] = "secret/source/path token"
            _write_json(database / "index/manifest.json", payload)
            with self.assertRaises(DatabaseTokenizerCompatibilityError) as caught:
                validate_distribution_database(
                    database,
                    database.name,
                    contract=self.contract,
                )
            value = str(caught.exception)
            self.assertIn("actual=invalid", value)
            self.assertNotIn("secret", value)
            self.assertNotIn("source/path", value)

    def test_builder_rejects_one_bad_database_without_writing_zip_or_db(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".copilot"
            dbs_root = home / "rag/dbs"
            good = _write_database(dbs_root, "good-rag")
            bad = _write_database(dbs_root, "bad-rag")
            payload = _json(bad / "VERSION.json")
            payload["tokenizer"] = "sudachi-a-v2"
            _write_json(bad / "VERSION.json", payload)
            before = {_db.name: _tree_digest(_db) for _db in (good, bad)}
            output = root / "offline.zip"
            databases = [{"name": "good-rag"}, {"name": "bad-rag"}]
            with (
                mock.patch.object(windows_distribution.sys, "platform", "win32"),
                mock.patch.object(windows_distribution, "_validate_model"),
                mock.patch.object(windows_distribution, "_prepare_runtime"),
                mock.patch.object(windows_distribution, "_runtime_entries", return_value=[]),
                mock.patch.object(windows_distribution, "_generated_installer_entries", return_value=[]),
                mock.patch.object(windows_distribution.packages, "_product_entries", return_value=[]),
                mock.patch.object(
                    windows_distribution.packages,
                    "_database_entries",
                    return_value=([], databases),
                ),
            ):
                with self.assertRaises(DatabaseTokenizerCompatibilityError):
                    windows_distribution.create_windows_distribution_package(
                        home,
                        output,
                        db_names=("good-rag", "bad-rag"),
                    )
            self.assertFalse(output.exists())
            self.assertEqual(
                before,
                {_db.name: _tree_digest(_db) for _db in (good, bad)},
            )

    def test_windows_builder_copies_checkpointed_catalog_without_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbs_root = root / "dbs"
            database = _write_database(dbs_root)
            entries, databases = windows_distribution.packages._database_entries(
                dbs_root,
                db_names=(database.name,),
                distribution=True,
            )
            entries = [
                windows_distribution.replace(entry, mode="copy")
                if entry.mode == "sqlite"
                else entry
                for entry in entries
            ]
            before = _tree_digest(database)
            stage = root / "stage"
            windows_distribution.packages._stage_package(
                stage,
                entries,
                kind=windows_distribution.packages._DISTRIBUTION_KIND,
                databases=databases,
                created="2026-08-05T00:00:00Z",
                tool_version="test",
            )
            self.assertEqual(before, _tree_digest(database))
            self.assertFalse(Path(str(database / "catalog.sqlite") + "-wal").exists())
            self.assertFalse(Path(str(database / "catalog.sqlite") + "-shm").exists())
            copied = stage / ".copilot/rag/dbs/fixture-rag/catalog.sqlite"
            connection = sqlite3.connect(
                copied.resolve().as_uri() + "?mode=ro&immutable=1",
                uri=True,
            )
            try:
                self.assertEqual(
                    self.contract.fingerprint,
                    connection.execute(
                        "SELECT value FROM database_meta WHERE key='tokenizer'"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_wal_created_during_staging_aborts_before_zip_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".copilot"
            database = _write_database(home / "rag/dbs")
            output = root / "offline.zip"
            databases = [{"name": database.name}]

            def stage_with_racing_wal(*_args: object, **_kwargs: object) -> dict:
                Path(str(database / "catalog.sqlite") + "-wal").write_bytes(
                    b"pending"
                )
                return {}

            with (
                mock.patch.object(windows_distribution.sys, "platform", "win32"),
                mock.patch.object(windows_distribution, "_validate_model"),
                mock.patch.object(windows_distribution, "_prepare_runtime"),
                mock.patch.object(windows_distribution, "_runtime_entries", return_value=[]),
                mock.patch.object(windows_distribution, "_generated_installer_entries", return_value=[]),
                mock.patch.object(windows_distribution, "_tokenizer_contract_entries", return_value=[]),
                mock.patch.object(windows_distribution.packages, "_product_entries", return_value=[]),
                mock.patch.object(
                    windows_distribution.packages,
                    "_database_entries",
                    return_value=([], databases),
                ),
                mock.patch.object(
                    windows_distribution.packages,
                    "_stage_package",
                    side_effect=stage_with_racing_wal,
                ),
                mock.patch.object(
                    windows_distribution.packages,
                    "validate_package_tree",
                ),
            ):
                with self.assertRaises(DatabaseTokenizerCompatibilityError):
                    windows_distribution.create_windows_distribution_package(
                        home,
                        output,
                        db_names=(database.name,),
                    )
            self.assertFalse(output.exists())

    def test_staged_gate_and_installer_run_before_publish_without_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            lock = stage / ".copilot/rag/query/windows-runtime-lock.json"
            lock.parent.mkdir(parents=True)
            lock.write_bytes(windows_distribution.LOCK_PATH.read_bytes())
            database = _write_database(stage / ".copilot/rag/dbs")
            with (
                mock.patch.object(windows_distribution, "_validate_runtime"),
                mock.patch.object(windows_distribution, "_validate_model"),
            ):
                result = windows_distribution._verify_staged_structure(
                    stage,
                    [{"name": database.name}],
                )
            self.assertEqual("pass", result["database_tokenizer_compatibility"])
            self.assertFalse(result["list_dbs_executed"])
            self.assertFalse(result["real_search_executed"])
            self.assertFalse(result["dense_inference_executed"])

        template = windows_distribution.INSTALL_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("verify_windows_distribution_databases.py", template)
        self.assertLess(
            template.index("$VerificationText ="),
            template.index('$InstallStage = "stage_payload"'),
        )
        verification_block = template[
            template.index("$VerificationText =") : template.index(
                '$InstallStage = "stage_payload"'
            )
        ]
        self.assertNotIn("list_dbs.py", verification_block)
        self.assertNotIn("search.py", verification_block)


if __name__ == "__main__":
    unittest.main()
