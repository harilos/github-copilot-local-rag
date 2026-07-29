from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
from manage import LocalRagManager  # noqa: E402
from source_manager import manage_custom  # noqa: E402


class ManageCustomContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-manage-custom-"
        )
        self.root = Path(self.temporary.name)
        self.example = self.root / "manage-custom.example.json"
        self.custom = self.root / "manage-custom.json"
        self.environment_config = self.root / "environment.json"
        self._write(
            self.example,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {
                    "database_name": ["example-rag"],
                    "search_question": ["Example question"],
                    "generic_home_url": [
                        "https://example.invalid/project"
                    ],
                },
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load(
        self,
        *,
        environ: dict[str, str] | None = None,
    ) -> manage_custom.ManageCustom:
        return manage_custom.load_manage_custom(
            self.root,
            environ=environ or {},
            example_path=self.example,
            custom_path=self.custom,
        )

    def test_environment_overrides_custom_then_example_per_key(self) -> None:
        self._write(
            self.custom,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {
                    "database_name": ["custom-rag"],
                    "search_question": ["Custom question"],
                },
            },
        )
        self._write(
            self.environment_config,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {"database_name": ["environment-rag"]},
            },
        )
        loaded = self._load(
            environ={
                manage_custom.ENV_CONFIG_PATH:
                str(self.environment_config)
            }
        )
        self.assertEqual(
            ("environment-rag",),
            loaded.values("database_name"),
        )
        self.assertEqual(
            ("Custom question",),
            loaded.values("search_question"),
        )
        self.assertEqual(
            ("https://example.invalid/project",),
            loaded.values("generic_home_url"),
        )
        self.assertEqual((), loaded.warnings)

    def test_invalid_json_reports_position_and_falls_back(self) -> None:
        self.custom.write_text(
            '{\n  "schema_version": "local-rag.manage-custom.v1",\n'
            '  "examples": ]\n}',
            encoding="utf-8",
        )
        loaded = self._load()
        self.assertEqual(("example-rag",), loaded.values("database_name"))
        warning = next(
            value
            for value in loaded.warnings
            if value.code == "manage_custom_invalid_json"
        )
        self.assertEqual("custom", warning.source)
        self.assertIsInstance(warning.line, int)
        self.assertIsInstance(warning.column, int)
        self.assertIsInstance(warning.offset, int)
        rendered = warning.render()
        self.assertIn("line=", rendered)
        self.assertIn("column=", rendered)
        self.assertIn("offset=", rendered)

    def test_unknown_and_invalid_fields_fall_back_independently(self) -> None:
        self._write(
            self.custom,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {
                    "database_name": 7,
                    "search_question": ["Valid custom question"],
                    "unknown_example": ["ignored"],
                },
                "unknown_top_level": True,
            },
        )
        loaded = self._load()
        self.assertEqual(("example-rag",), loaded.values("database_name"))
        self.assertEqual(
            ("Valid custom question",),
            loaded.values("search_question"),
        )
        codes = [value.code for value in loaded.warnings]
        self.assertIn("manage_custom_invalid_type", codes)
        self.assertEqual(2, codes.count("manage_custom_unknown_key"))

    def test_secret_values_are_rejected_without_value_disclosure(self) -> None:
        secret = "do-not-print-this-value"
        self._write(
            self.custom,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {
                    "database_name": [f"password={secret}"],
                    "search_question": ["Safe custom question"],
                },
            },
        )
        self._write(
            self.environment_config,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {
                    "generic_home_url": [
                        f"https://user:{secret}@example.invalid/project"
                    ]
                },
            },
        )
        loaded = self._load(
            environ={
                manage_custom.ENV_CONFIG_PATH:
                str(self.environment_config)
            }
        )
        self.assertEqual(("example-rag",), loaded.values("database_name"))
        self.assertEqual(
            ("https://example.invalid/project",),
            loaded.values("generic_home_url"),
        )
        self.assertEqual(
            ("Safe custom question",),
            loaded.values("search_question"),
        )
        rendered = "\n".join(
            warning.render() for warning in loaded.warnings
        )
        self.assertEqual(
            2,
            sum(
                warning.code == "manage_custom_secret_rejected"
                for warning in loaded.warnings
            ),
        )
        self.assertNotIn(secret, rendered)

    def test_invalid_environment_path_falls_back_to_custom(self) -> None:
        self._write(
            self.custom,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {"database_name": ["custom-rag"]},
            },
        )
        loaded = self._load(
            environ={
                manage_custom.ENV_CONFIG_PATH: "relative/config.json",
            }
        )
        self.assertEqual(("custom-rag",), loaded.values("database_name"))
        codes = [value.code for value in loaded.warnings]
        self.assertIn("manage_custom_environment_path_invalid", codes)

    def test_tracked_example_covers_every_manager_example_key(self) -> None:
        bundled = (
            RAG_ROOT / "config" / manage_custom.EXAMPLE_FILE_NAME
        )
        payload = json.loads(bundled.read_text(encoding="utf-8"))
        self.assertEqual(
            manage_custom.SCHEMA_VERSION,
            payload["schema_version"],
        )
        self.assertEqual(
            manage_custom.EXAMPLE_KEYS,
            frozenset(payload["examples"]),
        )
        loaded = manage_custom.load_manage_custom(
            RAG_ROOT,
            environ={},
        )
        self.assertEqual((), loaded.warnings)
        self.assertTrue(
            all(loaded.values(key) for key in manage_custom.EXAMPLE_KEYS)
        )

    def test_manage_source_contains_no_literal_prompt_examples(self) -> None:
        source = (RAG_ROOT / "manage.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "examples":
                    continue
                self.assertFalse(
                    isinstance(
                        keyword.value,
                        (ast.Constant, ast.List, ast.Tuple, ast.Set),
                    ),
                    f"literal examples remain at line {node.lineno}",
                )
        self.assertNotRegex(source, r"例:\s*https?://")
        self.assertNotIn("project-rag", source)

    def test_manager_reads_untracked_partial_custom_examples(self) -> None:
        rag_root = self.root / "rag"
        config = rag_root / "config"
        config.mkdir(parents=True)
        self._write(
            config / manage_custom.CUSTOM_FILE_NAME,
            {
                "schema_version": manage_custom.SCHEMA_VERSION,
                "examples": {"database_name": ["organization-rag"]},
            },
        )
        output: list[str] = []
        with mock.patch.dict(
            "os.environ",
            {manage_custom.ENV_CONFIG_PATH: ""},
            clear=False,
        ):
            manager = LocalRagManager(
                rag_root=rag_root,
                dbs_root=rag_root / "dbs",
                runtime_python=rag_root / "query/.venv/bin/python",
                input_fn=lambda _prompt: ":q",
                output_fn=output.append,
                color=False,
            )
        manager._create_database()
        rendered = "\n".join(output)
        self.assertIn("organization-rag", rendered)
        self.assertNotIn("project-rag", rendered)

    def test_real_custom_file_is_ignored_by_git(self) -> None:
        ignore = (RAG_ROOT.parents[1] / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            ".copilot/rag/config/manage-custom.json",
            ignore,
        )


if __name__ == "__main__":
    unittest.main()
