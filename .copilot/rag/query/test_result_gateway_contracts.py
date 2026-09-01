from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(QUERY_ROOT))

import result_bundle
import result_gateway


def _payload() -> dict:
    return {
        "schema": "local-rag.search.v1",
        "status": "ok",
        "answerability": "full",
        "selected_db": "project-rag",
        "query": "What is the approved value?",
        "evidence": [{
            "id": "R1",
            "text": "The approved value is seven percent.",
            "matched_excerpt": "The approved value is seven percent.",
            "context_before": "A proposal existed earlier.",
            "context_after": "The issue is closed.",
            "context_reason": "same_section_neighbor",
            "source_ranges": [],
            "source": {"path": "docs/issue.md", "title": "issue.md", "revision": "sha256:test"},
            "location": {"section": "Decision"},
            "signals": ["semantic"],
        }],
        "background_context": [],
        "related_context": [],
        "document_results": [],
        "warnings": [],
        "coverage": {"returned_distinct_documents": 1},
    }


class ResultGatewayContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="result-gateway-")
        self.root = Path(self.temporary.name)
        self.spool = self.root / "results"
        self.registry_root = self.root / "bindings"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _publish(self) -> tuple[dict, dict, datetime]:
        pointer = result_bundle.publish_result_bundle(
            _payload(), spool_root=self.spool, now=self.now
        )
        summary, expiry = result_bundle.load_initial_summary(
            pointer["result_set_id"], "project-rag",
            spool_root=self.spool, now=self.now,
        )
        assert summary is not None and expiry is not None
        return pointer, summary, expiry

    def _binding(self) -> tuple[dict, result_gateway.ResultBinding]:
        pointer, summary, expiry = self._publish()
        binding = result_gateway.create_result_binding(
            pointer["result_set_id"], "project-rag", summary, expiry,
            pointer["bytes"], spool_root=self.spool, now=self.now,
        )
        return pointer, binding

    def test_pointer_is_closed_and_its_absolute_path_is_never_used(self) -> None:
        pointer, _summary, _expiry = self._publish()
        pointer["summary_file"] = "C:" + "\\Users\\someone\\secret.txt"
        result_id, size = result_gateway.parse_search_pointer(pointer)
        self.assertEqual(pointer["result_set_id"], result_id)
        self.assertEqual(pointer["bytes"], size)
        for mutation in (
            {**pointer, "extra": True},
            {**pointer, "result_set_id": "../escape"},
            {**pointer, "bytes": True},
        ):
            with self.assertRaises(result_gateway.GatewayError):
                result_gateway.parse_search_pointer(mutation)

    def test_pointer_accepts_only_the_closed_public_freshness_extension(self) -> None:
        pointer, _summary, _expiry = self._publish()
        pointer["database_freshness"] = {
            "status": "stale",
            "content_snapshot_at": "2026-08-01T04:17:07Z",
            "age_days": 31,
            "chat_notice": {
                "code": "local_rag_content_snapshot_older_than_30_days",
                "scope": "conversation",
                "dedupe_key": "local_rag_content_snapshot_stale",
                "message_ja": "内容が古い可能性があります。",
            },
        }
        self.assertEqual(
            (pointer["result_set_id"], pointer["bytes"]),
            result_gateway.parse_search_pointer(pointer),
        )
        for freshness in (
            {"status": "stale", "content_snapshot_at": "invalid", "age_days": 31},
            {"status": "current", "content_snapshot_at": "2026-08-01T04:17:07Z", "age_days": True},
            {"status": "unknown", "content_snapshot_at": None, "age_days": None, "extra": True},
        ):
            with self.assertRaises(result_gateway.GatewayError):
                result_gateway.parse_search_pointer(
                    {**pointer, "database_freshness": freshness}
                )

    def test_binding_revalidates_db_ttl_hash_size_and_bundle_meta(self) -> None:
        pointer, binding = self._binding()
        self.assertGreaterEqual(len(binding.evidence_ids), 1)
        self.assertLessEqual(
            len(binding.evidence_ids),
            result_gateway.MAX_INSPECTABLE_EVIDENCE_IDS,
        )
        result_gateway.revalidate_result_binding(binding, spool_root=self.spool, now=self.now)
        with self.assertRaises(result_gateway.GatewayError):
            result_gateway.create_result_binding(
                pointer["result_set_id"], "wrong-rag", {},
                self.now + timedelta(minutes=1), pointer["bytes"],
                spool_root=self.spool, now=self.now,
            )
        summary_path = self.spool / pointer["result_set_id"] / "summary.json"
        summary_path.write_bytes(summary_path.read_bytes() + b" ")
        with self.assertRaises(result_gateway.GatewayError):
            result_gateway.revalidate_result_binding(binding, spool_root=self.spool, now=self.now)

    def test_expired_binding_and_tampered_record_fail_closed(self) -> None:
        _pointer, binding = self._binding()
        registry = result_gateway.DiskTokenRegistry(self.registry_root)
        token = registry.add(binding, now=self.now)
        self.assertIsNotNone(registry.get(token, now=self.now))
        record = next(self.registry_root.glob("*.json"))
        data = json.loads(record.read_text(encoding="utf-8"))
        data["selected_db"] = "other-rag"
        record.write_text(json.dumps(data), encoding="utf-8")
        tampered = registry.get(token, now=self.now)
        self.assertIsNotNone(tampered)
        assert tampered is not None
        with self.assertRaises(result_gateway.GatewayError):
            result_gateway.revalidate_result_binding(
                tampered, spool_root=self.spool, now=self.now
            )
        expired = result_gateway.ResultBinding(
            binding.result_set_id, binding.selected_db, binding.evidence_ids,
            self.now - timedelta(minutes=2), self.now - timedelta(minutes=1),
            binding.manifest_integrity, binding.summary_integrity,
            binding.bundle_size,
        )
        expired_token = registry.add(expired, now=self.now)
        self.assertIsNone(registry.get(expired_token, now=self.now))

    def test_token_is_192_bit_opaque_and_record_name_is_only_its_digest(self) -> None:
        pointer, binding = self._binding()
        registry = result_gateway.DiskTokenRegistry(self.registry_root)
        tokens = {registry.add(binding, now=self.now) for _ in range(8)}
        self.assertEqual(8, len(tokens))
        for token in tokens:
            self.assertRegex(token, r"^lrt_[A-Za-z0-9_-]{32}$")
            self.assertNotIn(pointer["result_set_id"], token)
            self.assertNotIn("project-rag", token)
        names = [path.name for path in self.registry_root.iterdir()]
        self.assertTrue(all(len(name) == 69 and name.endswith(".json") for name in names))
        listing = "\n".join(names)
        self.assertTrue(all(token not in listing for token in tokens))

    def test_registry_is_atomic_bounded_and_readable_by_another_process(self) -> None:
        _pointer, binding = self._binding()
        registry = result_gateway.DiskTokenRegistry(self.registry_root, maximum=4)
        tokens = [registry.add(binding, now=self.now) for _ in range(7)]
        self.assertLessEqual(len(list(self.registry_root.glob("*.json"))), 4)
        token = tokens[-1]
        script = (
            "import json,sys; from pathlib import Path; "
            "sys.path.insert(0,sys.argv[1]); import result_gateway; "
            "b=result_gateway.DiskTokenRegistry(Path(sys.argv[2])).get(sys.argv[3]); "
            "print(json.dumps({'db':b.selected_db,'ids':list(b.evidence_ids)}))"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(QUERY_ROOT),
             str(self.registry_root), token],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual("project-rag", json.loads(completed.stdout)["db"])

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink unsupported")
    def test_symlinked_spool_result_and_registry_are_rejected(self) -> None:
        pointer, summary, expiry = self._publish()
        real = self.spool / pointer["result_set_id"]
        moved = self.root / "moved-result"
        real.rename(moved)
        try:
            os.symlink(moved, real, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {exc}")
        with self.assertRaises(result_gateway.GatewayError):
            result_gateway.create_result_binding(
                pointer["result_set_id"], "project-rag", summary, expiry,
                pointer["bytes"], spool_root=self.spool, now=self.now,
            )
        registry_target = self.root / "real-bindings"
        registry_target.mkdir()
        registry_link = self.root / "linked-bindings"
        try:
            os.symlink(registry_target, registry_link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {exc}")
        with self.assertRaises(result_gateway.GatewayError):
            result_gateway.DiskTokenRegistry(registry_link).cleanup(now=self.now)


if __name__ == "__main__":
    unittest.main()
