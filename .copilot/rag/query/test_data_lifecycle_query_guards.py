from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(QUERY_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

import result_bundle  # noqa: E402
from software_rag_tool.data_lifecycle import (  # noqa: E402
    READY,
    empty_config_digest,
    new_reset_lifecycle,
    transition_lifecycle,
    write_lifecycle,
)
from test_result_bundle_contracts import synthetic_payload  # noqa: E402


class DataLifecycleQueryGuardTests(unittest.TestCase):
    def test_old_detail_bundle_is_rejected_after_new_reset_epoch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="query-generation-") as temporary:
            base = Path(temporary)
            dbs = base / "dbs"
            db = dbs / "fixture-rag"
            spool = base / "spool"
            db.mkdir(parents=True)
            marker = new_reset_lifecycle(
                db, source_config_digest=empty_config_digest()
            )
            write_lifecycle(db, marker)
            ready = transition_lifecycle(db, marker, status=READY)
            payload = synthetic_payload()
            payload["selected_db"] = db.name
            payload["_data_epoch"] = ready.epoch
            pointer = result_bundle.publish_result_bundle(
                payload,
                spool_root=spool,
                now=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
            packet, _ = result_bundle.load_expanded_result(
                pointer["result_set_id"],
                ["E1"],
                detail_level="expanded",
                spool_root=spool,
                now=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
            self.assertTrue(result_bundle.validate_expanded_lifecycle(packet, dbs))

            next_marker = new_reset_lifecycle(
                db, source_config_digest=empty_config_digest()
            )
            write_lifecycle(db, next_marker)
            packet, _ = result_bundle.load_expanded_result(
                pointer["result_set_id"],
                ["E1"],
                detail_level="expanded",
                spool_root=spool,
                now=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
            self.assertFalse(result_bundle.validate_expanded_lifecycle(packet, dbs))


if __name__ == "__main__":
    unittest.main()
