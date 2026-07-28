from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


MANAGER_PATH = Path(__file__).resolve().parents[1] / "manage.py"
SPEC = importlib.util.spec_from_file_location("local_rag_manage", MANAGER_PATH)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.responses: dict[str, SimpleNamespace] = {}

    def respond(
        self,
        script_name: str,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.responses[script_name] = SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append((list(argv), dict(kwargs)))
        return self.responses.get(
            Path(argv[1]).name,
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        )


class FakeInventory:
    def __init__(self) -> None:
        self.payload = {
            "documents_without_source_id": 1,
            "sources": [
                {
                    "source_id": "source-a",
                    "document_count": 2,
                    "chunk_count": 5,
                    "display_name": "Synthetic Source",
                    "observed_stored_roots": ["Example Root/"],
                    "observed_root_status": "ready",
                    "sample_documents": [
                        "Example Root/docs/first.txt",
                        "Example Root/docs/second.txt",
                    ],
                    "ingestion_scopes": [
                        {"root": "Example Root", "scan_subdir": "docs"}
                    ],
                }
            ]
        }

    def to_dict(self) -> dict[str, Any]:
        return self.payload

    def observed_paths_by_source(self) -> dict[str, list[str]]:
        return {
            "source-a": [
                "Example Root/docs/first.txt",
                "Example Root/docs/second.txt",
            ]
        }


class FakeSourceLinks:
    SCHEMA_VERSION = "rag-source-metadata-v1"

    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []
        self.payload: dict[str, Any] | None = None

    def load_source_links(self, *_: Any) -> SimpleNamespace:
        if self.payload is None:
            return SimpleNamespace(
                status="unconfigured", payload=None, error_kind=None
            )
        return SimpleNamespace(
            status="configured", payload=self.payload, error_kind=None
        )

    @staticmethod
    def validate_source_link(link: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": str(link["provider"]),
            "enabled": bool(link["enabled"]),
            "strategy": str(link["strategy"]),
            "settings": dict(link["settings"]),
        }

    @staticmethod
    def resolve_mapping_preview(
        mapping: dict[str, Any],
        paths: list[str],
    ) -> list[dict[str, str]]:
        return [{"path": path, "mapping": mapping["provider"]} for path in paths]

    def save_source_links(
        self,
        _db_root: Path,
        payload: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.payload = json.loads(json.dumps(payload))
        self.saved.append(self.payload)
        return self.payload



class ManagerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-manager-contract-"
        )
        self.base = Path(self.temporary.name)
        self.rag_root = self.base / "rag"
        self.dbs_root = self.rag_root / "dbs"
        self.runtime = self.rag_root / "query" / ".venv" / "bin" / "python"
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_text("", encoding="utf-8")
        self.dbs_root.mkdir(parents=True)
        self.runner = RecordingRunner()
        self.output: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(self, answers: list[str] | None = None) -> Any:
        values = iter(answers or [])
        return manage.LocalRagManager(
            rag_root=self.rag_root,
            dbs_root=self.dbs_root,
            runtime_python=self.runtime,
            input_fn=lambda _prompt: next(values),
            output_fn=self.output.append,
            runner=self.runner,
        )

    def make_db(self, name: str = "example-rag") -> Path:
        root = self.dbs_root / name
        root.mkdir()
        return root

    def test_fixed_human_menu_contract(self) -> None:
        self.assertEqual(
            [label for _, label in manage.TOP_MENU],
            [
                "新しいDBを作る",
                "DBを選んで管理する",
                "全DBの全Sourceを更新・再開する",
                "配布・管理PCの引っ越し",
                "この端末の設定・動作確認",
                "終了",
            ],
        )
        self.assertEqual(
            [label for _, label in manage.DATABASE_MENU],
            [
                "Sourceを見る・更新する",
                "新しいSourceを追加する",
                "このDBの全Sourceを更新・再開する",
                "DBの名前・説明を変更する",
                "問題があるとき",
                "このDBを削除する【危険】",
                "戻る",
            ],
        )

    def test_source_update_groups_use_runner_result_contract(self) -> None:
        groups = manage.LocalRagManager._source_update_groups(
            {
                "results": [
                    {"status": "updated", "display_name": "A"},
                    {
                        "status": "skipped",
                        "display_name": "B",
                        "skip_reason": "one_shot_source_complete",
                    },
                    {"status": "failed", "display_name": "C"},
                ]
            }
        )
        self.assertEqual(["A"], [
            item["display_name"] for item in groups["completed"]
        ])
        self.assertEqual(["B"], [
            item["display_name"] for item in groups["skipped"]
        ])
        self.assertEqual(["C"], [
            item["display_name"] for item in groups["failed"]
        ])
        self.assertEqual(
            [label for _, label in manage.SOURCE_LINK_MENU],
            [
                "現在の設定を確認",
                "新規設定・設定変更",
                "有効・無効を切り替える",
                "設定を削除する",
                "生成URLを確認する",
                "Source Linkヘルプを開く",
                "戻る",
            ],
        )

    def test_child_commands_are_argv_only_and_shell_false(self) -> None:
        manager = self.manager()
        manager._invoke("query/list_dbs.py", ["--format", "json"])
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(Path(argv[0]), self.runtime.resolve())
        self.assertEqual(Path(argv[1]).name, "list_dbs.py")
        self.assertEqual(argv[2:], ["--format", "json"])
        self.assertIs(kwargs["shell"], False)
        self.assertNotIsInstance(argv, str)
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
        self.assertEqual(
            Path(kwargs["env"]["RAG_DBS_ROOT"]),
            self.dbs_root.resolve(),
        )

    def test_non_allowlisted_script_is_rejected(self) -> None:
        with self.assertRaises(manage.ManagerError):
            self.manager()._invoke("query/untrusted.py", [])
        self.assertEqual(self.runner.calls, [])

    def test_setup_verification_parses_machine_contract(self) -> None:
        self.runner.respond(
            "setup.py",
            stdout=json.dumps(
                {
                    "status": "ready",
                    "setup_complete": True,
                    "lookup_ready": True,
                }
            ),
        )
        self.manager(["1"])._setup_or_verify()
        argv = self.runner.calls[0][0]
        self.assertIn("--verify-only", argv)
        self.assertIn("json", argv)
        self.assertIn("初期設定: 完了", self.output)
        self.assertIn("検索準備: 利用可能", self.output)

    def test_search_normal_is_one_direct_compact_call(self) -> None:
        self.manager(["1", "synthetic question"])._search("example-rag")
        self.assertEqual(len(self.runner.calls), 1)
        argv = self.runner.calls[0][0]
        self.assertEqual(Path(argv[1]).name, "search.py")
        self.assertEqual(
            argv[2:],
            [
                "--db",
                "example-rag",
                "--compact-json",
                "synthetic question",
                "--format",
                "json",
                "--result-delivery",
                "stdout",
            ],
        )

    def test_search_diagnostic_is_one_call_with_explain(self) -> None:
        self.manager(["2", "synthetic question"])._search("example-rag")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIn("--explain", self.runner.calls[0][0])

    def test_search_rendering_prefers_permalink_then_url_then_path(self) -> None:
        manager = self.manager()
        manager._show_search_result(
            json.dumps(
                {
                    "status": "ok",
                    "evidence": [
                        {
                            "source": {"path": "Root/first.txt"},
                            "source_url": "https://example.invalid/current",
                            "source_permalink": (
                                "https://example.invalid/fixed"
                            ),
                        },
                        {
                            "source": {"path": "Root/second.txt"},
                            "source_url": "https://example.invalid/second",
                        },
                        {"source": {"path": "Root/third.txt"}},
                    ],
                }
            )
        )
        text = "\n".join(self.output)
        self.assertIn("https://example.invalid/fixed", text)
        self.assertIn("https://example.invalid/second", text)
        self.assertIn("Root/third.txt", text)

    def test_database_listing_uses_direct_venv_json_command(self) -> None:
        self.runner.respond(
            "list_dbs.py",
            stdout=json.dumps(
                {"databases": [{"name": "example-rag", "title": "Example"}]}
            ),
        )
        values = self.manager()._database_summaries()
        self.assertEqual(values[0]["name"], "example-rag")
        self.assertEqual(
            self.runner.calls[0][0][0], str(self.runtime.resolve())
        )

    def test_database_selection_shows_public_content_summary(self) -> None:
        self.runner.respond(
            "list_dbs.py",
            stdout=json.dumps(
                {
                    "databases": [
                        {
                            "name": "example-rag",
                            "title": "Example",
                            "query_hint": "製品資料",
                            "content_summary": "GitHub「製品コード」",
                        }
                    ]
                }
            ),
        )
        manager = self.manager(["0"])
        self.assertIsNone(manager._select_database())
        text = "\n".join(self.output)
        self.assertIn("内容: GitHub「製品コード」", text)
        self.assertIn("検索向け: 製品資料", text)
        self.assertNotIn("チャンク数", text)

    def test_create_rejects_path_shaped_database_name(self) -> None:
        self.manager(["outside/example-rag"])._create_database()
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(
            any("[エラー] DB名は" in value for value in self.output)
        )

    def test_database_display_name_and_query_hint_can_be_edited(
        self,
    ) -> None:
        root = self.make_db()
        (root / "db.json").write_text(
            json.dumps(
                {
                    "db_name": "example-rag",
                    "title": "Old title",
                    "profile": "DB_PROFILE.md",
                    "collection": "unchanged",
                }
            ),
            encoding="utf-8",
        )
        (root / "DB_PROFILE.md").write_text(
            "# Old title\n\n"
            "## Query Hint\n\nOld hint\n\n"
            "## Indexed Content\n\nPreserve this section.\n",
            encoding="utf-8",
        )
        self.manager(
            ["New title", "New selection hint", "y"]
        )._edit_database_metadata("example-rag")
        config = json.loads(
            (root / "db.json").read_text(encoding="utf-8")
        )
        profile = (root / "DB_PROFILE.md").read_text(encoding="utf-8")
        self.assertEqual("New title", config["title"])
        self.assertEqual("unchanged", config["collection"])
        self.assertIn("# New title", profile)
        self.assertIn("New selection hint", profile)
        self.assertIn("Preserve this section.", profile)
        self.assertEqual([], self.runner.calls)
        self.assertIn(
            "[成功] DBの表示名と検索ヒントを保存しました。",
            self.output,
        )

    def test_database_metadata_edit_cancel_preserves_files(self) -> None:
        root = self.make_db()
        config_path = root / "db.json"
        profile_path = root / "DB_PROFILE.md"
        config_path.write_text(
            json.dumps(
                {
                    "db_name": "example-rag",
                    "title": "Original",
                    "profile": "DB_PROFILE.md",
                }
            ),
            encoding="utf-8",
        )
        profile_path.write_text(
            "# Original\n\n## Query Hint\n\nOriginal hint\n",
            encoding="utf-8",
        )
        before = (config_path.read_bytes(), profile_path.read_bytes())
        self.manager([":q"])._edit_database_metadata("example-rag")
        self.assertEqual(
            before,
            (config_path.read_bytes(), profile_path.read_bytes()),
        )

    def test_resume_reconstructs_allowlisted_argv(self) -> None:
        manager = self.manager()
        manager._resume_saved_operation(
            "example-rag",
            {
                "operation": "add",
                "root": "Example Root",
                "source_id": "source-a",
                "scan_subdir": "docs",
                "resume_command": "untrusted command",
            },
        )
        argv = self.runner.calls[0][0]
        self.assertEqual(Path(argv[1]).name, "add_data.py")
        self.assertIn("--resume", argv)
        self.assertIn("--scan-subdir", argv)
        self.assertNotIn("untrusted command", argv)

    def test_resume_confirmation_shows_saved_scope_before_execution(
        self,
    ) -> None:
        self.make_db()
        status = {
            "status": "interrupted",
            "appears_active": False,
            "can_resume": True,
            "operation": "add",
            "root": "Example Root",
            "source_id": "source-a",
            "scan_subdir": "docs",
        }
        self.runner.respond("status.py", stdout=json.dumps(status))
        manager = self.manager(["2", "n"])
        manager._build_or_resume("example-rag")
        text = "\n".join(self.output)
        self.assertIn("再開する保存済み処理", text)
        self.assertIn("論理ルート: Example Root", text)
        self.assertIn("Source ID: source-a", text)
        self.assertIn("読込範囲: docs", text)
        self.assertEqual(
            [Path(call[0][1]).name for call in self.runner.calls],
            ["status.py"],
        )

    def test_repair_components_are_strictly_bounded(self) -> None:
        self.assertEqual(
            set(manage.REPAIR_COMPONENTS.values()),
            {"lexical", "vector", "all"},
        )

    def test_source_list_leads_to_final_source_detail(self) -> None:
        self.make_db()
        manager = self.manager(["1", "0", "0"])
        manager._load_source_inventory = lambda _name: FakeInventory()
        manager._sources_screen("example-rag")
        text = "\n".join(self.output)
        self.assertIn("同じ取得元と検索結果リンク設定", text)
        self.assertIn("画面: Source詳細", text)
        self.assertIn("1. 更新・再開する", text)
        self.assertIn("5. 技術情報", text)

    def test_source_link_add_uses_sidecar_api_only(self) -> None:
        self.make_db()
        links = FakeSourceLinks()
        manager = self.manager(
            ["", "4", "1", "https://docs.example.invalid", "y"]
        )
        manager._import_source_links = lambda: links
        manager._configure_source_link(
            "example-rag", FakeInventory(), "source-a"
        )
        self.assertEqual(len(links.saved), 1)
        source = links.saved[0]["sources"][0]
        self.assertEqual(1, links.saved[0]["revision"])
        self.assertEqual(source["source_id"], "source-a")
        self.assertEqual(source["source_type"], "other")
        self.assertEqual(source["link"]["strategy"], "home-only")
        self.assertNotIn("provider", source["link"])
        self.assertNotIn("mappings", source)
        self.assertNotIn("path_prefix", source)
        self.assertIn(
            "生成URLの確認",
            "\n".join(self.output),
        )

    def test_per_file_link_requires_one_observed_root(self) -> None:
        self.make_db()
        links = FakeSourceLinks()
        inventory = FakeInventory()
        inventory.payload["sources"][0]["observed_root_status"] = (
            "multiple_observed_roots"
        )
        manager = self.manager()
        manager._import_source_links = lambda: links
        manager._prompt_source_link = lambda **_: {
            "provider": "other",
            "enabled": True,
            "strategy": "append-relative-path",
            "settings": {
                "source_web_root": "https://docs.example.invalid/root"
            },
        }
        manager._configure_source_link(
            "example-rag",
            inventory,
            "source-a",
        )
        self.assertEqual([], links.saved)
        self.assertIn(
            "multiple_observed_roots",
            "\n".join(self.output),
        )

    def test_home_only_link_is_allowed_without_observed_root(self) -> None:
        self.make_db()
        links = FakeSourceLinks()
        inventory = FakeInventory()
        inventory.payload["sources"][0]["observed_root_status"] = (
            "no_observed_root"
        )
        manager = self.manager(["y"])
        manager._import_source_links = lambda: links
        manager._prompt_source_link = lambda **_: {
            "provider": "other",
            "enabled": True,
            "strategy": "home-only",
            "settings": {
                "source_home_url": "https://docs.example.invalid"
            },
        }
        manager._configure_source_link(
            "example-rag",
            inventory,
            "source-a",
        )
        self.assertEqual(1, len(links.saved))

    def test_generic_source_migration_is_not_public(self) -> None:
        labels = [label for _, label in manage.SOURCE_MENU]
        self.assertFalse(any("移行" in label for label in labels))
        self.assertNotIn(
            "gen_db/migrate_source_metadata.py",
            manage.ALLOWED_SCRIPTS,
        )

    def test_unset_source_type_is_displayed_as_unconfigured(self) -> None:
        manager = self.manager(["0"])
        inventory = FakeInventory()
        manager._select_source(inventory)
        text = "\n".join(self.output)
        self.assertIn("Source種別: 未設定", text)
        self.assertNotIn("Source種別: フォルダー", text)

    def test_indexed_sharepoint_fetch_root_cannot_be_retargeted(self) -> None:
        manager = self.manager()
        manager._edit_source_fetch_settings(
            "example-rag",
            {
                "_local_source_key": "src_sharepoint-fixture",
                "source_id": "sharepoint-source",
                "source_type": "sharepoint",
                "fetch": {
                    "root_env": "LOCAL_RAG_SHAREPOINT_ROOT",
                    "relative_path": "Team/Docs",
                },
            },
        )
        rendered = "\n".join(self.output)
        self.assertIn(
            "検索へ反映済みのSharePoint Source",
            rendered,
        )
        self.assertIn("新しいSourceを追加する", rendered)

    def test_source_type_can_be_saved_without_a_link(self) -> None:
        self.make_db()
        links = FakeSourceLinks()
        manager = self.manager(["", "2", "y"])
        manager._import_source_links = lambda: links
        manager._configure_source_metadata(
            "example-rag",
            FakeInventory(),
            "source-a",
        )
        self.assertEqual(1, len(links.saved))
        source = links.saved[0]["sources"][0]
        self.assertEqual("folder", source["source_type"])
        self.assertNotIn("link", source)
        self.assertIn("Source種別【任意】", "\n".join(self.output))

    def test_ingestion_prompt_shows_provider_oriented_source_examples(
        self,
    ) -> None:
        manager = self.manager(
            ["<source-root>", "sharepoint-docs", ""]
        )
        self.assertEqual(
            ("<source-root>", "sharepoint-docs", ""),
            manager._prompt_ingestion_values(),
        )
        text = "\n".join(self.output)
        self.assertIn("sharepoint-docs", text)
        self.assertIn("redmine-issues", text)
        self.assertIn("github-repository", text)
        self.assertIn("gitlab-repository", text)
        self.assertIn("azure-repository", text)
        self.assertIn("svn-repository", text)

    def test_source_link_edit_preserves_enter_and_clears_optional_dash(
        self,
    ) -> None:
        existing = {
            "enabled": True,
            "provider": "sharepoint",
            "settings": {
                "source_home_url": "https://home.example.invalid",
                "source_web_root": "https://files.example.invalid",
            },
        }
        manager = self.manager(["", "", ""])
        value = manager._prompt_source_link(existing=existing)
        self.assertIsNotNone(value)
        assert value is not None
        self.assertNotIn("source_home_url", value["settings"])
        self.assertEqual(
            value["settings"]["source_web_root"],
            "https://files.example.invalid",
        )
        text = "\n".join(self.output)
        self.assertNotIn("SourceトップURL", text)
        self.assertNotIn("リンク方式を選択", text)
        self.assertIn("SharePoint上の基準フォルダURL【必須】", text)

    def test_sharepoint_form_has_one_url_and_fixed_strategy(self) -> None:
        manager = self.manager(
            [
                "",
                "1",
                "https://tenant.example.invalid/sites/example/Library",
            ]
        )
        value = manager._prompt_source_link()
        self.assertIsNotNone(value)
        assert value is not None
        self.assertEqual("sharepoint", value["provider"])
        self.assertEqual("append-relative-path", value["strategy"])
        self.assertEqual(
            {
                "source_web_root": (
                    "https://tenant.example.invalid/sites/example/Library"
                )
            },
            value["settings"],
        )
        text = "\n".join(self.output)
        self.assertNotIn("SourceトップURL", text)
        self.assertNotIn("トップページのみ", text)
        self.assertNotIn("リンク方式を選択", text)
        self.assertIn("リンク方式を自動設定", text)

    def test_sharepoint_uncertain_observed_root_has_actionable_warning(
        self,
    ) -> None:
        self.make_db()
        links = FakeSourceLinks()
        inventory = FakeInventory()
        inventory.payload["sources"][0]["observed_root_status"] = (
            "multiple_observed_roots"
        )
        manager = self.manager()
        manager._import_source_links = lambda: links
        manager._prompt_source_link = lambda **_: {
            "provider": "sharepoint",
            "enabled": True,
            "strategy": "append-relative-path",
            "settings": {
                "source_web_root": "https://files.example.invalid/root"
            },
        }
        manager._configure_source_link(
            "example-rag",
            inventory,
            "source-a",
        )
        self.assertEqual([], links.saved)
        text = "\n".join(self.output)
        self.assertIn("SharePointのファイルURLを生成できません", text)
        self.assertIn("自動検出された保存ルートを確認", text)
        self.assertIn("multiple_observed_roots", text)

    def test_force_rebuild_requires_selected_database_name(self) -> None:
        self.make_db()
        self.runner.respond(
            "status.py",
            stdout=json.dumps(
                {"status": "completed", "appears_active": False}
            ),
        )
        manager = self.manager(
            ["3", "Example Root", "source-a", "docs", "example-rag"]
        )
        manager._build_or_resume("example-rag")
        argv = self.runner.calls[-1][0]
        self.assertEqual(Path(argv[1]).name, "build_db.py")
        self.assertIn("--force-rebuild", argv)

    def test_add_selects_existing_source_and_retry_errors_explicitly(
        self,
    ) -> None:
        self.make_db()
        self.runner.respond(
            "status.py",
            stdout=json.dumps(
                {"status": "completed", "appears_active": False}
            ),
        )
        manager = self.manager(["1", "Example Root", "docs", "y", "y"])
        manager._load_source_inventory = lambda _name: FakeInventory()
        manager._add_or_update("example-rag")
        argv = self.runner.calls[-1][0]
        self.assertEqual(Path(argv[1]).name, "add_data.py")
        self.assertIn("source-a", argv)
        self.assertIn("--retry-errors", argv)

    def test_mutation_rejects_symlink_database_root(self) -> None:
        outside = self.base / "outside-rag"
        outside.mkdir()
        (self.dbs_root / "example-rag").symlink_to(
            outside,
            target_is_directory=True,
        )
        manager = self.manager()
        manager._build_or_resume("example-rag")
        manager._add_or_update("example-rag")
        manager._repair_index("example-rag")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(
            sum("安全な操作対象として確認できません" in value for value in self.output),
            3,
        )

    def test_delete_requires_exact_typed_name(self) -> None:
        root = self.make_db()
        manager = self.manager()
        with self.assertRaises(manage.ManagerError):
            manager._delete_database("example-rag", "wrong-rag")
        self.assertTrue(root.exists())

    def test_delete_confirmation_shows_only_normal_summary_fields(self) -> None:
        root = self.make_db()
        (root / "payload.bin").write_bytes(b"12345")
        manager = self.manager(["example-rag"])
        manager._load_source_inventory = lambda _name: SimpleNamespace(
            to_dict=lambda: {
                "document_count": 3,
                "chunk_count": 99,
                "sources": [
                    {
                        "source_id": "source-a",
                        "document_count": 2,
                        "chunk_count": 98,
                    }
                ],
                "documents_without_source_id": 1,
            }
        )
        manager._read_database_metadata = lambda _name: {
            "title": "Example Database",
            "query_hint": "secret hint",
        }
        deleted: list[tuple[str, str]] = []
        manager._delete_database = (
            lambda name, typed: deleted.append((name, typed))
        )

        self.assertTrue(manager._delete_database_interactive("example-rag"))

        rendered = "\n".join(self.output)
        self.assertIn("DB名: example-rag", rendered)
        self.assertIn("表示名: Example Database", rendered)
        self.assertIn("文書数: 3", rendered)
        self.assertIn("サイズ: 5 bytes", rendered)
        self.assertNotIn("チャンク", rendered)
        self.assertNotIn(str(root.resolve()), rendered)
        self.assertNotIn("secret hint", rendered)
        self.assertEqual(deleted, [("example-rag", "example-rag")])
        self.assertEqual(self.runner.calls, [])

    def test_delete_marks_in_progress_source_interrupted_then_reconfirms(
        self,
    ) -> None:
        root = self.make_db()
        source_dir = root / "sources" / "src_example-123456789abc"
        source_dir.mkdir(parents=True)
        state_path = source_dir / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": "local-rag-source-state-v1",
                    "revision": 4,
                    "operation": "update",
                    "phase": "fetching",
                    "can_resume": False,
                    "last_error": None,
                }
            ),
            encoding="utf-8",
        )
        manager = self.manager(["y", "example-rag"])
        manager._load_source_inventory = lambda _name: None
        manager._read_database_metadata = lambda _name: {
            "title": "Example Database",
            "query_hint": "",
        }
        deleted: list[tuple[str, str]] = []
        manager._delete_database = (
            lambda name, typed: deleted.append((name, typed))
        )

        self.assertTrue(manager._delete_database_interactive("example-rag"))

        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["phase"], "interrupted")
        self.assertTrue(saved["can_resume"])
        self.assertEqual(saved["revision"], 5)
        self.assertIn("updated_at", saved)
        self.assertEqual(deleted, [("example-rag", "example-rag")])
        rendered = "\n".join(self.output)
        self.assertIn("中断状態として記録しました", rendered)
        self.assertIn("続けてDB削除をもう一度確認します", rendered)
        self.assertEqual(self.runner.calls, [])

    def test_delete_declined_interruption_keeps_state_and_database(self) -> None:
        root = self.make_db()
        source_dir = root / "sources" / "src_example-123456789abc"
        source_dir.mkdir(parents=True)
        state_path = source_dir / "state.json"
        original = {
            "schema_version": "local-rag-source-state-v1",
            "revision": 1,
            "operation": "update",
            "phase": "indexing",
            "can_resume": False,
        }
        state_path.write_text(json.dumps(original), encoding="utf-8")
        manager = self.manager([""])
        manager._load_source_inventory = lambda _name: None
        manager._read_database_metadata = lambda _name: {
            "title": "Example Database",
            "query_hint": "",
        }

        self.assertFalse(manager._delete_database_interactive("example-rag"))

        self.assertTrue(root.exists())
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8")),
            original,
        )
        self.assertIn(
            "中断状態へ変更せず、DB削除をキャンセルしました",
            "\n".join(self.output),
        )
        self.assertEqual(self.runner.calls, [])

    def test_in_progress_detection_accepts_current_status_field(self) -> None:
        manager = self.manager()
        self.assertTrue(
            manager._source_state_is_in_progress({"status": "running"})
        )
        self.assertFalse(
            manager._source_state_is_in_progress({"status": "complete"})
        )
        self.assertFalse(
            manager._source_state_is_in_progress(
                {"status": "interrupted", "can_resume": True}
            )
        )

    def test_partial_interruption_is_reported_and_database_is_not_deleted(
        self,
    ) -> None:
        root = self.make_db()
        state_paths: list[Path] = []
        for suffix in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
            source_dir = root / "sources" / f"src_example-{suffix}"
            source_dir.mkdir(parents=True)
            state_path = source_dir / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": "local-rag-source-state-v1",
                        "revision": 1,
                        "operation": "update",
                        "phase": "running",
                        "can_resume": False,
                    }
                ),
                encoding="utf-8",
            )
            state_paths.append(state_path)
        manager = self.manager(["y"])
        manager._load_source_inventory = lambda _name: None
        manager._read_database_metadata = lambda _name: {
            "title": "Example Database",
            "query_hint": "",
        }
        original_write = manager._write_interrupted_source_state
        write_count = 0

        def fail_second(
            db_root: Path,
            state_path: Path,
            payload: dict[str, Any],
        ) -> None:
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                raise OSError("synthetic interruption failure")
            original_write(db_root, state_path, payload)

        manager._write_interrupted_source_state = fail_second
        deleted: list[str] = []
        manager._delete_database = (
            lambda name, _typed: deleted.append(name)
        )

        self.assertFalse(manager._delete_database_interactive("example-rag"))

        first = json.loads(state_paths[0].read_text(encoding="utf-8"))
        second = json.loads(state_paths[1].read_text(encoding="utf-8"))
        self.assertEqual(first["phase"], "interrupted")
        self.assertEqual(second["phase"], "running")
        self.assertEqual(deleted, [])
        rendered = "\n".join(self.output)
        self.assertIn("中断状態を保存済み: 1件 / 未保存: 1件", rendered)
        self.assertIn("DBは削除されませんでした", rendered)

    def test_safe_delete_removes_only_selected_database(self) -> None:
        root = self.make_db()
        (root / "catalog.sqlite").write_bytes(b"synthetic")
        sibling = self.make_db("sibling-rag")
        manager = self.manager()
        manager._delete_database("example-rag", "example-rag")
        self.assertFalse(root.exists())
        self.assertTrue(sibling.exists())

    def test_delete_rejects_symlink_database_root(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        outside = self.base / "outside-rag"
        outside.mkdir()
        link = self.dbs_root / "example-rag"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(manage.ManagerError):
            self.manager()._validated_database_root("example-rag")
        self.assertTrue(outside.exists())

    def test_delete_does_not_follow_interior_symlink(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        root = self.make_db()
        outside = self.base / "external-data"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        try:
            (root / "external-link").symlink_to(
                outside, target_is_directory=True
            )
        except OSError:
            self.skipTest("symlink creation is unavailable")
        manager = self.manager()
        manager._delete_database("example-rag", "example-rag")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_partial_delete_failure_is_not_reported_as_success(self) -> None:
        root = self.make_db()
        removed = root / "removed.txt"
        retained = root / "retained.txt"
        removed.write_text("removed", encoding="utf-8")
        retained.write_text("retained", encoding="utf-8")
        manager = self.manager(["example-rag"])
        manager._load_source_inventory = lambda _name: None
        manager._read_database_metadata = lambda _name: {
            "title": "Example Database",
            "query_hint": "",
        }
        original_rmtree = manage.shutil.rmtree

        def partial_failure(target: Path) -> None:
            (Path(target) / "removed.txt").unlink()
            raise OSError("synthetic partial failure")

        manage.shutil.rmtree = partial_failure
        try:
            self.assertFalse(
                manager._delete_database_interactive("example-rag")
            )
        finally:
            manage.shutil.rmtree = original_rmtree

        self.assertTrue(root.exists())
        self.assertFalse(removed.exists())
        self.assertTrue(retained.exists())
        rendered = "\n".join(self.output)
        self.assertIn("DB削除は完了していません", rendered)
        self.assertNotIn("DB「example-rag」を削除しました", rendered)

if __name__ == "__main__":
    unittest.main()
