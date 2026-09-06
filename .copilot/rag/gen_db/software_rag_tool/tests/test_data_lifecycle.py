from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from software_rag_tool.data_lifecycle import (
    DataLifecycleError,
    LEGACY_EPOCH,
    READY,
    assert_ready_epoch,
    capture_ready_epoch,
    empty_config_digest,
    new_reset_lifecycle,
    read_lifecycle,
    transition_lifecycle,
    write_lifecycle,
)
from software_rag_tool.db_runtime import DbRegistry


class DataLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lifecycle-")
        self.db = Path(self.temporary.name) / "fixture-rag"
        self.db.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_markerless_database_remains_legacy_compatible(self) -> None:
        self.assertEqual(LEGACY_EPOCH, capture_ready_epoch(self.db))
        self.assertEqual(LEGACY_EPOCH, assert_ready_epoch(self.db, LEGACY_EPOCH))

    def test_nonready_marker_blocks_read_and_ready_epoch_is_bound(self) -> None:
        marker = new_reset_lifecycle(
            self.db, source_config_digest=empty_config_digest()
        )
        write_lifecycle(self.db, marker)
        with self.assertRaisesRegex(DataLifecycleError, "refresh is required"):
            capture_ready_epoch(self.db)
        ready = transition_lifecycle(self.db, marker, status=READY)
        self.assertEqual(ready.epoch, capture_ready_epoch(self.db))
        assert_ready_epoch(self.db, ready.epoch)
        next_marker = new_reset_lifecycle(
            self.db, source_config_digest=empty_config_digest()
        )
        write_lifecycle(self.db, next_marker)
        with self.assertRaises(DataLifecycleError):
            assert_ready_epoch(self.db, ready.epoch)

    def test_tampered_or_wrong_database_marker_fails_closed(self) -> None:
        marker = new_reset_lifecycle(
            self.db, source_config_digest=empty_config_digest()
        )
        write_lifecycle(self.db, marker)
        path = self.db / "data-lifecycle.json"
        text = path.read_text(encoding="utf-8").replace(
            '"db_name": "fixture-rag"', '"db_name": "other-rag"'
        )
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(DataLifecycleError, "identity mismatch"):
            read_lifecycle(self.db)

    def test_registry_rejects_nonready_before_opening_old_catalog(self) -> None:
        marker = new_reset_lifecycle(
            self.db, source_config_digest=empty_config_digest()
        )
        write_lifecycle(self.db, marker)
        registry = DbRegistry(self.db.parent)
        with self.assertRaisesRegex(DataLifecycleError, "refresh is required"):
            registry.get(self.db.name)
        self.assertEqual(0, registry.cached_count)


if __name__ == "__main__":
    unittest.main()
