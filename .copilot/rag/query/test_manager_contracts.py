from __future__ import annotations

import importlib.util
from contextlib import contextmanager
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
    SCHEMA_VERSION = "rag-source-links-v2"

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


class FakeDaemonControl:
    def __init__(self, status: str = "no_daemon") -> None:
        self.status = status
        self.release_calls: list[str] = []
        self.resume_calls: list[str] = []

    def release_db_before_mutation(
        self,
        db_name: str,
        **_: Any,
    ) -> dict[str, str]:
        self.release_calls.append(db_name)
        if self.status == "db_released":
            return {
                "status": self.status,
                "db": db_name,
                "lease_id": "synthetic-lease",
            }
        return {"status": self.status, "db": db_name}

    def resume_db_after_mutation(
        self,
        db_name: str,
        **_: Any,
    ) -> dict[str, str]:
        self.resume_calls.append(db_name)
        return {"status": "resumed"}

    @contextmanager
    def database_mutation_guard(
        self,
        db_name: str,
        **kwargs: Any,
    ):
        release = self.release_db_before_mutation(db_name, **kwargs)
        if release.get("status") not in {"no_daemon", "db_released"}:
            raise RuntimeError("daemon did not release the database")
        try:
            yield release
        finally:
            lease_id = str(release.get("lease_id") or "")
            if release.get("status") == "db_released" and lease_id:
                self.resume_db_after_mutation(
                    db_name,
                    lease_id=lease_id,
                )


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
                "Initial setup and setup verification",
                "List and select a database",
                "Create a new database",
                "Exit",
            ],
        )
        self.assertEqual(
            [label for _, label in manage.DATABASE_MENU],
            [
                "Search or run a search test",
                "List Sources",
                "Build / resume",
                "Add or update documents",
                "Show detailed status",
                "Repair or recreate search indexes",
                "Delete this database",
                "Back",
            ],
        )
        self.assertEqual(
            [label for _, label in manage.SOURCE_LINK_MENU],
            [
                "Show configuration",
                "Configure or replace",
                "Enable / disable",
                "Remove configuration",
                "Preview generated URLs",
                "Back",
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
        self.assertIn("Setup complete: yes", self.output)
        self.assertIn("Lookup ready: yes", self.output)

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

    def test_database_selection_shows_lightweight_readiness_counts(self) -> None:
        self.runner.respond(
            "list_dbs.py",
            stdout=json.dumps(
                {"databases": [{"name": "example-rag", "title": "Example"}]}
            ),
        )
        self.runner.respond(
            "status.py",
            stdout=json.dumps(
                {
                    "status": "completed",
                    "document_count": 2,
                    "chunk_count": 5,
                }
            ),
        )
        manager = self.manager(["0"])
        manager._load_source_inventory = lambda _name: FakeInventory()
        self.assertIsNone(manager._select_database())
        text = "\n".join(self.output)
        self.assertIn("documents=2, chunks=5", text)
        self.assertIn("ready", text)

    def test_create_rejects_path_shaped_database_name(self) -> None:
        self.manager(["outside/example-rag"])._create_database()
        self.assertEqual(self.runner.calls, [])
        self.assertIn("Invalid database name.", self.output)

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

    def test_repair_components_are_strictly_bounded(self) -> None:
        self.assertEqual(
            set(manage.REPAIR_COMPONENTS.values()),
            {"lexical", "vector", "all"},
        )

    def test_source_list_leads_to_read_only_source_detail(self) -> None:
        self.make_db()
        manager = self.manager(["1", "1", "0", "0"])
        manager._load_source_inventory = lambda _name: FakeInventory()
        manager._sources_screen("example-rag")
        text = "\n".join(self.output)
        self.assertIn("Read-only Source inventory", text)
        self.assertIn("cannot create, rename, or delete", text)
        self.assertIn("Source detail", text)

    def test_source_link_add_uses_sidecar_api_only(self) -> None:
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
        self.assertEqual(source["provider"], "other")
        self.assertEqual(source["strategy"], "home-only")
        self.assertNotIn("mappings", source)
        self.assertNotIn("path_prefix", source)
        self.assertIn(
            "Representative stored paths and generated URLs",
            "\n".join(self.output),
        )

    def test_per_file_link_requires_one_observed_root(self) -> None:
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

    def test_legacy_sidecar_requires_explicit_migration_confirmation(
        self,
    ) -> None:
        links = FakeSourceLinks()
        links.payload = {
            "schema_version": links.SCHEMA_VERSION,
            "revision": 1,
            "sources": [
                {
                    "source_id": "source-a",
                    "provider": "other",
                    "enabled": True,
                    "strategy": "home-only",
                    "settings": {
                        "source_home_url": "https://docs.example.invalid"
                    },
                }
            ],
        }

        def legacy_load(*_: Any) -> SimpleNamespace:
            return SimpleNamespace(
                status="configured",
                payload=links.payload,
                error_kind=None,
                revision=1,
                etag="legacy-etag",
                migration_required=True,
                source_statuses=(
                    ("source-a", "legacy_migration_available"),
                ),
            )

        links.load_source_links = legacy_load
        manager = self.manager(["y", "n"])
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
            FakeInventory(),
            "source-a",
        )
        self.assertEqual([], links.saved)
        text = "\n".join(self.output)
        self.assertIn("explicitly migrate", text)
        self.assertIn("migration cancelled", text)

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
        manager = self.manager(["", "", "", "-", ""])
        value = manager._prompt_source_link(existing=existing)
        self.assertIsNotNone(value)
        assert value is not None
        self.assertNotIn("source_home_url", value["settings"])
        self.assertEqual(
            value["settings"]["source_web_root"],
            "https://files.example.invalid",
        )

    def test_active_status_refuses_build_add_and_repair(self) -> None:
        self.make_db()
        self.runner.respond(
            "status.py",
            stdout=json.dumps(
                {"status": "running", "appears_active": True}
            ),
        )
        manager = self.manager()
        manager._build_or_resume("example-rag")
        manager._add_or_update("example-rag")
        manager._repair_index("example-rag")
        scripts = [Path(call[0][1]).name for call in self.runner.calls]
        self.assertEqual(scripts, ["status.py", "status.py", "status.py"])
        self.assertEqual(
            sum("Operation refused" in value for value in self.output), 3
        )

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

    def test_mutation_refuses_when_status_cannot_be_verified(self) -> None:
        self.make_db()
        manager = self.manager()
        manager._build_or_resume("example-rag")
        manager._add_or_update("example-rag")
        manager._repair_index("example-rag")
        scripts = [Path(call[0][1]).name for call in self.runner.calls]
        self.assertEqual(scripts, ["status.py", "status.py", "status.py"])
        self.assertEqual(
            sum("could not be verified" in value for value in self.output),
            3,
        )

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
            sum("Operation refused" in value for value in self.output),
            3,
        )

    def test_delete_requires_exact_typed_name(self) -> None:
        root = self.make_db()
        manager = self.manager()
        manager._import_daemon_control = lambda: FakeDaemonControl()
        with self.assertRaises(manage.ManagerError):
            manager._delete_database("example-rag", "wrong-rag")
        self.assertTrue(root.exists())

    def test_interactive_delete_refuses_unverified_status(self) -> None:
        root = self.make_db()
        manager = self.manager()
        self.assertFalse(
            manager._delete_database_interactive("example-rag")
        )
        self.assertTrue(root.exists())
        self.assertIn(
            "could not be verified",
            "\n".join(self.output),
        )

    def test_safe_delete_coordinates_daemon_and_removes_only_database(self) -> None:
        root = self.make_db()
        (root / "catalog.sqlite").write_bytes(b"synthetic")
        sibling = self.make_db("sibling-rag")
        daemon = FakeDaemonControl("db_released")
        manager = self.manager()
        manager._import_daemon_control = lambda: daemon
        manager._delete_database("example-rag", "example-rag")
        self.assertFalse(root.exists())
        self.assertTrue(sibling.exists())
        self.assertEqual(daemon.release_calls, ["example-rag"])
        self.assertEqual(daemon.resume_calls, ["example-rag"])

    def test_delete_rejects_active_mutation(self) -> None:
        root = self.make_db()
        logs = root / "logs"
        logs.mkdir()
        (logs / "progress.json").write_text(
            json.dumps({"status": "running"}), encoding="utf-8"
        )
        manager = self.manager()
        manager._import_daemon_control = lambda: FakeDaemonControl()
        with self.assertRaises(manage.ManagerError):
            manager._delete_database("example-rag", "example-rag")
        self.assertTrue(root.exists())

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
        manager._import_daemon_control = lambda: FakeDaemonControl()
        manager._delete_database("example-rag", "example-rag")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_delete_stops_when_daemon_release_fails(self) -> None:
        root = self.make_db()
        manager = self.manager()
        manager._import_daemon_control = lambda: FakeDaemonControl("busy")
        with self.assertRaises(manage.ManagerError):
            manager._delete_database("example-rag", "example-rag")
        self.assertTrue(root.exists())


if __name__ == "__main__":
    unittest.main()
