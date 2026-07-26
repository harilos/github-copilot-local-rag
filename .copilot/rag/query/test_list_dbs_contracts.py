from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("rag_list_dbs_cli", QUERY_ROOT / "list_dbs.py")
assert SPEC and SPEC.loader
LIST_DBS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LIST_DBS)


class ListDbsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.dbs_root = Path(self.temporary.name)
        self._create_db(
            "ac-rag",
            title="Air Conditioning Knowledge",
            hint="Air conditioning, cooling, efficiency, markets, policy, and regional demand",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_compact_json_contains_only_routing_fields(self) -> None:
        payload = json.loads(self._run_main(["--format", "json"]))
        self.assertEqual({"databases"}, set(payload))
        self.assertEqual(
            {"name", "title", "query_hint"},
            set(payload["databases"][0]),
        )
        self.assertEqual("ac-rag", payload["databases"][0]["name"])
        self.assertNotIn("model", json.dumps(payload))

    def test_no_argument_legacy_json_shape_is_preserved(self) -> None:
        payload = json.loads(self._run_main([]))
        self.assertEqual({"dbs"}, set(payload))
        self.assertEqual("ac-rag", payload["dbs"][0]["db"])
        self.assertIn("config", payload["dbs"][0])
        self.assertIn("hint", payload["dbs"][0])

    def test_human_output_remains_available(self) -> None:
        output = self._run_main(["--format", "text"])
        self.assertIn("ac-rag: Air Conditioning Knowledge", output)
        self.assertIn("Air conditioning, cooling", output)

    def test_empty_database_root_is_valid_json(self) -> None:
        empty_root = self.dbs_root / "empty"
        empty_root.mkdir()
        with mock.patch.object(LIST_DBS, "DBS_ROOT", empty_root):
            output = self._run_main(["--format", "json"], patch_root=False)
        self.assertEqual({"databases": []}, json.loads(output))

    def test_json_errors_are_structured_and_stdout_remains_json(self) -> None:
        (self.dbs_root / "ac-rag" / "db.json").write_text("{", encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            output = self._run_main(["--format", "json"], allow_exit=True)
        self.assertEqual(1, raised.exception.code)
        payload = json.loads(output.getvalue())
        self.assertEqual("error", payload["status"])
        self.assertEqual([], payload["databases"])

    def test_cli_does_not_import_search_or_model_runtime(self) -> None:
        source = (QUERY_ROOT / "list_dbs.py").read_text(encoding="utf-8")
        self.assertNotIn("search_api", source)
        self.assertNotIn("DbRegistry", source)
        self.assertNotIn("chromadb", source)
        self.assertNotIn("get_embedder", source)

    def _run_main(
        self,
        arguments: list[str],
        *,
        patch_root: bool = True,
        allow_exit: bool = False,
    ) -> str | io.StringIO:
        output = io.StringIO()
        root_patch = (
            mock.patch.object(LIST_DBS, "DBS_ROOT", self.dbs_root)
            if patch_root
            else contextlib.nullcontext()
        )
        with root_patch, mock.patch.object(sys, "argv", ["list_dbs.py", *arguments]):
            with contextlib.redirect_stdout(output):
                if allow_exit:
                    try:
                        LIST_DBS.main()
                    except SystemExit:
                        raise
                else:
                    LIST_DBS.main()
        return output if allow_exit else output.getvalue()

    def _create_db(self, name: str, *, title: str, hint: str) -> None:
        root = self.dbs_root / name
        root.mkdir(parents=True)
        (root / "db.json").write_text(
            json.dumps(
                {
                    "db_name": name,
                    "title": title,
                    "model": "legacy-field-must-not-leak",
                }
            ),
            encoding="utf-8",
        )
        (root / "DB_PROFILE.md").write_text(
            f"# {title}\n\n## Query Hint\n\n{hint}\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
