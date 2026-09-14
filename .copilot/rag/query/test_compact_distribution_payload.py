from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import compact_payload  # noqa: E402


class CompactDistributionPayloadTests(unittest.TestCase):
    def _package(self, root: Path) -> Path:
        package = root / "package"
        runtime = package / ".copilot/rag/query/.venv"
        model = package / ".copilot/rag/models/ruri-v3-30m-onnx-int8"
        database = package / ".copilot/rag/dbs/fixture-rag"
        (runtime / "Scripts").mkdir(parents=True)
        model.mkdir(parents=True)
        (database / "index").mkdir(parents=True)
        (runtime / "Scripts/python.exe").write_bytes(b"runtime")
        (model / "model.onnx").write_bytes(b"model")
        (database / "index/data.bin").write_bytes(b"database")
        manifest = {
            "files": [
                {"path": ".copilot/rag/query/.venv/Scripts/python.exe", "size": 7, "sha256": "0" * 64},
                {"path": ".copilot/rag/models/ruri-v3-30m-onnx-int8/model.onnx", "size": 5, "sha256": "0" * 64},
                {"path": ".copilot/rag/dbs/fixture-rag/index/data.bin", "size": 8, "sha256": "0" * 64},
            ],
            "total": {"files": 3, "bytes": 20},
        }
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return package

    def test_compacts_each_heavy_tree_and_rewrites_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(Path(directory))
            archived = compact_payload.compact_heavy_payloads(
                package, ("fixture-rag",), manifest_name="manifest.json"
            )
            self.assertEqual(3, len(archived))
            for relative in archived:
                payload = package / Path(relative)
                self.assertEqual({payload.name}, {item.name for item in payload.parent.iterdir()})
                self.assertTrue(compact_payload.inner_archive_names(payload))
            manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(archived), {item["path"] for item in manifest["files"]})
            self.assertEqual(3, manifest["total"]["files"])

    def test_rejects_traversal_and_case_colliding_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, entries in (
                ("traversal.zip", (("../escape", b"x"),)),
                ("duplicate.zip", (("A.txt", b"x"), ("a.txt", b"y"))),
            ):
                path = root / name
                with zipfile.ZipFile(path, "w") as archive:
                    for entry, value in entries:
                        archive.writestr(entry, value)
                with self.assertRaises(compact_payload.CompactPayloadError):
                    compact_payload.inner_archive_names(path)


if __name__ == "__main__":
    unittest.main()
