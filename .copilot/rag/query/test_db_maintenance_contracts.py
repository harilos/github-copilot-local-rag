from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import db_maintenance as maintenance  # noqa: E402
from software_rag_tool import daemon_control  # noqa: E402


class DatabaseMaintenanceContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-db-maintenance-"
        )
        self.rag_root = Path(self.temporary.name) / "rag"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_paths_are_db_scoped_hashed_and_outside_the_database(self) -> None:
        state, lock = maintenance.maintenance_paths(
            "example-rag",
            rag_root=self.rag_root,
        )
        expected = hashlib.sha256(b"example-rag").hexdigest()
        self.assertEqual(
            self.rag_root.resolve()
            / "query"
            / "run"
            / "db-maintenance",
            state.parent,
        )
        self.assertEqual(f"{expected}.json", state.name)
        self.assertEqual(f"{expected}.lock", lock.name)
        self.assertNotIn("example-rag", state.name)

    def test_active_lease_is_atomic_valid_json_and_search_busy(self) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="add",
            rag_root=self.rag_root,
        )
        try:
            state = json.loads(lease.state_path.read_text(encoding="utf-8"))
            self.assertEqual(maintenance.SCHEMA_VERSION, state["schema"])
            self.assertEqual("active", state["state"])
            self.assertEqual("example-rag", state["db"])
            self.assertEqual("add", state["operation"])
            self.assertEqual(lease.lease_id, state["lease_id"])
            self.assertTrue(lease.active)
            self.assertEqual([], list(lease.state_path.parent.glob("*.tmp")))

            payload = maintenance.maintenance_search_payload(
                "example-rag",
                rag_root=self.rag_root,
            )
            self.assertEqual(
                {
                    "schema": "local-rag.search.v1",
                    "status": "busy",
                    "error": "db_maintenance_in_progress",
                    "db": "example-rag",
                    "operation": "add",
                },
                payload,
            )
        finally:
            maintenance.finish_maintenance(lease)

    def test_other_database_is_unaffected(self) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "target-rag",
            operation="build",
            rag_root=self.rag_root,
        )
        try:
            self.assertIsNotNone(
                maintenance.maintenance_search_payload(
                    "target-rag",
                    rag_root=self.rag_root,
                )
            )
            self.assertIsNone(
                maintenance.maintenance_search_payload(
                    "other-rag",
                    rag_root=self.rag_root,
                )
            )
        finally:
            maintenance.finish_maintenance(lease)

    def test_search_guard_blocks_maintenance_and_carries_payload(
        self,
    ) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="add",
            rag_root=self.rag_root,
        )
        try:
            with self.assertRaises(maintenance.MaintenanceError) as raised:
                with maintenance.database_search_guard(
                    "example-rag",
                    rag_root=self.rag_root,
                ):
                    self.fail("maintenance target must not enter search")
            self.assertEqual(
                "db_maintenance_in_progress",
                raised.exception.error_kind,
            )
            self.assertEqual(
                "busy",
                raised.exception.search_payload["status"],
            )
            self.assertEqual(
                "add",
                raised.exception.search_payload["operation"],
            )
        finally:
            maintenance.finish_maintenance(lease)

    def test_search_guard_holds_lock_until_search_exits(self) -> None:
        with maintenance.database_search_guard(
            "example-rag",
            rag_root=self.rag_root,
        ):
            with self.assertRaises(maintenance.MaintenanceError) as raised:
                maintenance.acquire_maintenance_lease(
                    "example-rag",
                    operation="build",
                    rag_root=self.rag_root,
                    lock_timeout_seconds=0.0,
                )
            self.assertEqual(
                "db_writer_already_active",
                raised.exception.error_kind,
            )
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="build",
            rag_root=self.rag_root,
        )
        maintenance.finish_maintenance(lease)

    def test_same_database_writer_conflicts_without_replacing_state(
        self,
    ) -> None:
        first = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="add",
            rag_root=self.rag_root,
        )
        before = first.state_path.read_bytes()
        try:
            with self.assertRaises(maintenance.MaintenanceError) as raised:
                maintenance.acquire_maintenance_lease(
                    "example-rag",
                    operation="build",
                    rag_root=self.rag_root,
                    lock_timeout_seconds=0.0,
                )
            self.assertEqual(
                "db_writer_already_active",
                raised.exception.error_kind,
            )
            self.assertEqual("example-rag", raised.exception.db_name)
            self.assertEqual("add", raised.exception.operation)
            self.assertEqual(before, first.state_path.read_bytes())
        finally:
            maintenance.finish_maintenance(first)

    def test_lease_id_mismatch_cannot_release_the_database(self) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="add",
            rag_root=self.rag_root,
        )
        with self.assertRaises(maintenance.MaintenanceError) as raised:
            maintenance.finish_maintenance(
                lease,
                lease_id="different-lease-id",
            )
        self.assertEqual(
            "maintenance_lease_mismatch",
            raised.exception.error_kind,
        )
        self.assertTrue(lease.active)
        self.assertTrue(lease.state_path.exists())
        maintenance.finish_maintenance(lease)
        self.assertFalse(lease.state_path.exists())

    def test_dead_active_owner_is_recovered_automatically(self) -> None:
        state_path, _lock_path = maintenance.maintenance_paths(
            "example-rag",
            rag_root=self.rag_root,
        )
        self._write_state(
            state_path,
            owner_pid=999_999_999,
            owner_start_identity="proc:old",
        )
        with (
            mock.patch.object(
                maintenance,
                "_pid_is_alive",
                return_value=False,
            ),
            mock.patch.object(
                maintenance,
                "database_integrity_ok",
                return_value=True,
            ),
        ):
            status = maintenance.maintenance_status(
                "example-rag",
                rag_root=self.rag_root,
            )
        self.assertEqual("available", status["status"])
        self.assertTrue(status["stale_recovered"])
        self.assertFalse(state_path.exists())

    def test_pid_reuse_identity_mismatch_is_recovered(self) -> None:
        state_path, _lock_path = maintenance.maintenance_paths(
            "example-rag",
            rag_root=self.rag_root,
        )
        self._write_state(
            state_path,
            owner_pid=1234,
            owner_start_identity="proc:old",
        )
        with (
            mock.patch.object(
                maintenance,
                "_pid_is_alive",
                return_value=True,
            ),
            mock.patch.object(
                maintenance,
                "_process_start_identity",
                return_value="proc:new",
            ),
            mock.patch.object(
                maintenance,
                "database_integrity_ok",
                return_value=True,
            ),
        ):
            status = maintenance.maintenance_status(
                "example-rag",
                rag_root=self.rag_root,
            )
        self.assertEqual("available", status["status"])
        self.assertTrue(status["stale_recovered"])

    def test_dead_writer_with_failed_integrity_requires_repair(
        self,
    ) -> None:
        state_path, _lock_path = maintenance.maintenance_paths(
            "example-rag",
            rag_root=self.rag_root,
        )
        self._write_state(
            state_path,
            owner_pid=999_999_999,
            owner_start_identity="proc:old",
        )
        with (
            mock.patch.object(
                maintenance,
                "_pid_is_alive",
                return_value=False,
            ),
            mock.patch.object(
                maintenance,
                "database_integrity_ok",
                return_value=False,
            ),
        ):
            status = maintenance.maintenance_status(
                "example-rag",
                rag_root=self.rag_root,
            )
        self.assertEqual("requires_repair", status["status"])
        self.assertEqual("db_requires_repair", status["error"])

    def test_live_owner_remains_fail_closed(self) -> None:
        state_path, _lock_path = maintenance.maintenance_paths(
            "example-rag",
            rag_root=self.rag_root,
        )
        self._write_state(
            state_path,
            owner_pid=1234,
            owner_start_identity="proc:same",
        )
        with (
            mock.patch.object(
                maintenance,
                "_pid_is_alive",
                return_value=True,
            ),
            mock.patch.object(
                maintenance,
                "_process_start_identity",
                return_value="proc:same",
            ),
        ):
            status = maintenance.maintenance_status(
                "example-rag",
                rag_root=self.rag_root,
            )
        self.assertEqual("active", status["status"])
        self.assertTrue(status["blocks_search"])
        self.assertTrue(state_path.exists())

    def test_failed_operation_with_verified_integrity_reopens_database(
        self,
    ) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="add",
            rag_root=self.rag_root,
        )
        result = maintenance.finish_maintenance(
            lease,
            operation_succeeded=False,
            integrity_ok=True,
            error_kind="synthetic_failure",
        )
        self.assertEqual(
            "maintenance_failed_integrity_verified",
            result["status"],
        )
        self.assertIsNone(
            maintenance.maintenance_search_payload(
                "example-rag",
                rag_root=self.rag_root,
            )
        )

    def test_integrity_failure_persists_requires_repair_fail_closed(
        self,
    ) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "target-rag",
            operation="vector_rebuild",
            rag_root=self.rag_root,
        )
        result = maintenance.finish_maintenance(
            lease,
            operation_succeeded=False,
            integrity_ok=False,
            error_kind="catalog_vector_mismatch",
        )
        self.assertEqual("db_requires_repair", result["status"])
        payload = maintenance.maintenance_search_payload(
            "target-rag",
            rag_root=self.rag_root,
        )
        self.assertEqual("error", payload["status"])
        self.assertEqual("db_requires_repair", payload["error"])
        self.assertIsNone(
            maintenance.maintenance_search_payload(
                "other-rag",
                rag_root=self.rag_root,
            )
        )
        with mock.patch.object(
            maintenance,
            "_pid_is_alive",
            return_value=False,
        ):
            still_blocked = maintenance.maintenance_status(
                "target-rag",
                rag_root=self.rag_root,
            )
        self.assertEqual("requires_repair", still_blocked["status"])
        self.assertTrue(still_blocked["blocks_search"])

    def test_integrity_failure_overrides_nominal_operation_success(
        self,
    ) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="build",
            rag_root=self.rag_root,
        )
        result = maintenance.finish_maintenance(
            lease,
            operation_succeeded=True,
            integrity_ok=False,
            error_kind="post_build_health_failed",
        )
        self.assertEqual("db_requires_repair", result["status"])
        self.assertEqual(
            "db_requires_repair",
            maintenance.maintenance_search_payload(
                "example-rag",
                rag_root=self.rag_root,
            )["error"],
        )

    def test_unknown_integrity_persists_failed_fail_closed(self) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="catalog_rebuild",
            rag_root=self.rag_root,
        )
        maintenance.finish_maintenance(
            lease,
            operation_succeeded=False,
            integrity_ok=None,
            error_kind="verification_unavailable",
        )
        payload = maintenance.maintenance_search_payload(
            "example-rag",
            rag_root=self.rag_root,
        )
        self.assertEqual("error", payload["status"])
        self.assertEqual("db_maintenance_failed", payload["error"])

    def test_explicit_repair_lease_can_replace_failed_state(self) -> None:
        lease = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="add",
            rag_root=self.rag_root,
        )
        maintenance.finish_maintenance(
            lease,
            operation_succeeded=False,
            integrity_ok=None,
            error_kind="synthetic_failure",
        )
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.acquire_maintenance_lease(
                "example-rag",
                operation="add",
                rag_root=self.rag_root,
            )
        repair = maintenance.acquire_maintenance_lease(
            "example-rag",
            operation="rebuild_all",
            rag_root=self.rag_root,
            recover_failed=True,
        )
        maintenance.finish_maintenance(repair)
        self.assertIsNone(
            maintenance.maintenance_search_payload(
                "example-rag",
                rag_root=self.rag_root,
            )
        )

    def test_mutation_guard_blocks_only_target_and_reopens_on_success(
        self,
    ) -> None:
        with mock.patch.object(
            daemon_control,
            "release_db_before_mutation",
            return_value={"status": "no_daemon", "db": "target-rag"},
        ), mock.patch.object(
            daemon_control,
            "database_integrity_ok",
            return_value=True,
        ):
            with daemon_control.database_mutation_guard(
                "target-rag",
                operation="add",
                rag_root=self.rag_root,
            ):
                self.assertEqual(
                    "db_maintenance_in_progress",
                    maintenance.maintenance_search_payload(
                        "target-rag",
                        rag_root=self.rag_root,
                    )["error"],
                )
                self.assertIsNone(
                    maintenance.maintenance_search_payload(
                        "other-rag",
                        rag_root=self.rag_root,
                    )
                )
        self.assertIsNone(
            maintenance.maintenance_search_payload(
                "target-rag",
                rag_root=self.rag_root,
            )
        )

    def test_mutation_exception_fails_closed_for_target_only(self) -> None:
        with mock.patch.object(
            daemon_control,
            "release_db_before_mutation",
            return_value={"status": "no_daemon", "db": "target-rag"},
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with daemon_control.database_mutation_guard(
                    "target-rag",
                    operation="add",
                    rag_root=self.rag_root,
                ):
                    raise RuntimeError("synthetic")
        target = maintenance.maintenance_search_payload(
            "target-rag",
            rag_root=self.rag_root,
        )
        self.assertEqual("db_requires_repair", target["error"])
        self.assertIsNone(
            maintenance.maintenance_search_payload(
                "other-rag",
                rag_root=self.rag_root,
            )
        )

    def test_invalid_state_is_fail_closed_and_never_deleted(self) -> None:
        state_path, _lock_path = maintenance.maintenance_paths(
            "example-rag",
            rag_root=self.rag_root,
        )
        state_path.parent.mkdir(parents=True)
        raw = b'{"schema":"wrong","state":"active"}'
        state_path.write_bytes(raw)
        payload = maintenance.maintenance_search_payload(
            "example-rag",
            rag_root=self.rag_root,
        )
        self.assertEqual("error", payload["status"])
        self.assertEqual(
            "db_maintenance_state_invalid",
            payload["error"],
        )
        self.assertEqual(raw, state_path.read_bytes())
        with self.assertRaises(maintenance.MaintenanceError) as raised:
            maintenance.acquire_maintenance_lease(
                "example-rag",
                operation="add",
                rag_root=self.rag_root,
            )
        self.assertEqual(
            "db_maintenance_state_invalid",
            raised.exception.error_kind,
        )

    def test_names_reject_traversal_without_creating_state(self) -> None:
        for value in ("../example-rag", r"C:\example-rag", "a/b-rag", ""):
            with self.subTest(value=value):
                with self.assertRaises(maintenance.MaintenanceError):
                    maintenance.maintenance_paths(
                        value,
                        rag_root=self.rag_root,
                    )
        self.assertFalse(
            (
                self.rag_root
                / "query"
                / "run"
                / "db-maintenance"
            ).exists()
        )

    def test_integrity_check_honors_explicit_database_root(self) -> None:
        dbs_root = Path(self.temporary.name) / "alternate-dbs"
        db_root = dbs_root / "example-rag"
        (db_root / "index" / "chroma").mkdir(parents=True)
        collection = "example_collection"
        for name, payload in (
            ("db.json", {"collection": collection}),
            (
                "VERSION.json",
                {
                    "schema": "local-rag.db-version.v1",
                    "collection": collection,
                },
            ),
        ):
            (db_root / name).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
        (db_root / "index" / "manifest.json").write_text(
            json.dumps(
                {"collection": collection, "record_count": 1}
            ),
            encoding="utf-8",
        )
        catalog = sqlite3.connect(db_root / "catalog.sqlite")
        catalog.execute("CREATE TABLE chunk (chunk_pk INTEGER PRIMARY KEY)")
        catalog.execute("INSERT INTO chunk VALUES (1)")
        catalog.commit()
        catalog.close()
        chroma = sqlite3.connect(
            db_root / "index" / "chroma" / "chroma.sqlite3"
        )
        chroma.execute("CREATE TABLE collections (id TEXT, name TEXT)")
        chroma.execute("CREATE TABLE segments (id TEXT, collection TEXT)")
        chroma.execute("CREATE TABLE embeddings (segment_id TEXT)")
        chroma.execute(
            "INSERT INTO collections VALUES ('c1', ?)",
            (collection,),
        )
        chroma.execute("INSERT INTO segments VALUES ('s1', 'c1')")
        chroma.execute("INSERT INTO embeddings VALUES ('s1')")
        chroma.commit()
        chroma.close()
        self.assertTrue(
            maintenance.database_integrity_ok(
                "example-rag",
                rag_root=self.rag_root,
                dbs_root=dbs_root,
            )
        )
        (db_root / "index" / "manifest.json").write_text(
            json.dumps(
                {"collection": collection, "record_count": 0}
            ),
            encoding="utf-8",
        )
        self.assertFalse(
            maintenance.database_integrity_ok(
                "example-rag",
                rag_root=self.rag_root,
                dbs_root=dbs_root,
            )
        )

    def _write_state(
        self,
        path: Path,
        *,
        owner_pid: int,
        owner_start_identity: str,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": maintenance.SCHEMA_VERSION,
                    "state": "active",
                    "db": "example-rag",
                    "operation": "add",
                    "lease_id": "lease-12345678",
                    "operation_token": "token-12345678",
                    "owner_pid": owner_pid,
                    "owner_start_identity": owner_start_identity,
                    "started_at": "2030-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
