from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from source_manager.machine_connections import (
    SHAREPOINT_ROOT_ENV,
    clear_sharepoint_root,
    connection_config_path,
    connection_secret_path,
    list_redmine_registrations,
    redmine_api_key_env,
    register_redmine_api_key,
    resolve_redmine_api_key,
    set_sharepoint_root,
    sharepoint_root_status,
    source_runtime_environment,
)
from source_manager import manager_connections


RAG_ROOT = Path(__file__).resolve().parents[2]
MANAGER_PATH = RAG_ROOT / "manage.py"
SPEC = importlib.util.spec_from_file_location(
    "local_rag_manage_machine_connections",
    MANAGER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


class MachineConnectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-machine-connections-"
        )
        self.rag_root = Path(self.temporary.name) / "rag"
        (self.rag_root / "config").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_redmine_keys_are_per_server_and_not_stored_in_plaintext(self) -> None:
        first_url = "https://redmine-a.example/redmine/projects/alpha"
        second_url = "https://redmine-b.example/projects/beta"
        first_secret = "FIRST-SECRET-012345"
        second_secret = "SECOND-SECRET-987654"

        first = register_redmine_api_key(
            self.rag_root,
            first_url,
            first_secret,
        )
        second = register_redmine_api_key(
            self.rag_root,
            second_url,
            second_secret,
        )

        self.assertNotEqual(first.connection_id, second.connection_id)
        self.assertNotEqual(first.api_key_env, second.api_key_env)
        self.assertEqual(
            first_secret,
            resolve_redmine_api_key(
                self.rag_root,
                project_url=first_url,
                api_key_env=first.api_key_env,
                environ={},
            ),
        )
        self.assertEqual(
            second_secret,
            resolve_redmine_api_key(
                self.rag_root,
                project_url=second_url,
                api_key_env=second.api_key_env,
                environ={},
            ),
        )

        persisted = (
            connection_config_path(self.rag_root).read_text(encoding="utf-8")
            + connection_secret_path(self.rag_root).read_text(encoding="utf-8")
        )
        self.assertNotIn(first_secret, persisted)
        self.assertNotIn(second_secret, persisted)
        registrations = list_redmine_registrations(self.rag_root)
        self.assertEqual(2, len(registrations))
        self.assertTrue(all(item.registered for item in registrations))
        self.assertNotIn(first_secret, repr(registrations))
        self.assertNotIn(second_secret, repr(registrations))

    def test_sharepoint_manager_setting_precedes_environment_fallback(self) -> None:
        manager_root = self.rag_root / "sharepoint-manager"
        environment_root = self.rag_root / "sharepoint-environment"
        manager_root.mkdir()
        environment_root.mkdir()

        saved = set_sharepoint_root(self.rag_root, manager_root)
        self.assertEqual(manager_root.resolve(), saved)
        status = sharepoint_root_status(
            self.rag_root,
            environ={SHAREPOINT_ROOT_ENV: str(environment_root)},
        )
        self.assertEqual("manager", status.source)
        self.assertEqual(str(manager_root.resolve()), status.root)

        injected = source_runtime_environment(
            self.rag_root,
            {
                "source_type": "sharepoint",
                "fetch": {"root_env": SHAREPOINT_ROOT_ENV},
            },
            environ={SHAREPOINT_ROOT_ENV: str(environment_root)},
        )
        self.assertEqual(
            str(manager_root.resolve()),
            injected[SHAREPOINT_ROOT_ENV],
        )

        clear_sharepoint_root(self.rag_root)
        inherited = sharepoint_root_status(
            self.rag_root,
            environ={SHAREPOINT_ROOT_ENV: str(environment_root)},
        )
        self.assertEqual("environment", inherited.source)
        self.assertEqual(str(environment_root.resolve()), inherited.root)

    def test_registered_key_overrides_legacy_shared_environment_for_existing_source(self) -> None:
        project_url = "https://redmine.example/redmine/projects/alpha"
        register_redmine_api_key(self.rag_root, project_url, "PER-SERVER")
        resolved = resolve_redmine_api_key(
            self.rag_root,
            project_url=project_url,
            api_key_env="LOCAL_RAG_REDMINE_API_KEY",
            environ={"LOCAL_RAG_REDMINE_API_KEY": "LEGACY-SHARED"},
        )
        self.assertEqual("PER-SERVER", resolved)

        runtime = source_runtime_environment(
            self.rag_root,
            {
                "source_type": "redmine",
                "fetch": {
                    "project_url": project_url,
                    "api_key_env": "LOCAL_RAG_REDMINE_API_KEY",
                },
            },
            environ={"LOCAL_RAG_REDMINE_API_KEY": "LEGACY-SHARED"},
        )
        self.assertEqual("PER-SERVER", runtime["LOCAL_RAG_REDMINE_API_KEY"])

    def test_runtime_environment_injects_machine_values(self) -> None:
        sharepoint_root = self.rag_root / "sharepoint"
        sharepoint_root.mkdir()
        set_sharepoint_root(self.rag_root, sharepoint_root)
        project_url = "https://redmine.example/tools/projects/alpha"
        secret = "MACHINE-ONLY-KEY"
        register_redmine_api_key(self.rag_root, project_url, secret)

        redmine_environment = source_runtime_environment(
            self.rag_root,
            {
                "source_type": "redmine",
                "fetch": {
                    "project_url": project_url,
                    "api_key_env": redmine_api_key_env(project_url),
                },
            },
            environ={},
        )
        self.assertEqual(
            secret,
            redmine_environment[redmine_api_key_env(project_url)],
        )

        sharepoint_environment = source_runtime_environment(
            self.rag_root,
            {
                "source_type": "sharepoint",
                "fetch": {"root_env": SHAREPOINT_ROOT_ENV},
            },
            environ={},
        )
        self.assertEqual(
            str(sharepoint_root.resolve()),
            sharepoint_environment[SHAREPOINT_ROOT_ENV],
        )


class ManagerMachineConnectionUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-manager-machine-connections-"
        )
        self.rag_root = Path(self.temporary.name) / "rag"
        (self.rag_root / "config").mkdir(parents=True)
        (self.rag_root / "dbs").mkdir()
        self.runtime = self.rag_root / "query" / ".venv" / "bin" / "python"
        self.runtime.parent.mkdir(parents=True)
        self.runtime.touch()
        self.output: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(self, answers: list[str] | None = None):
        values = iter(answers or [])
        return manage.LocalRagManager(
            rag_root=self.rag_root,
            dbs_root=self.rag_root / "dbs",
            runtime_python=self.runtime,
            input_fn=lambda _prompt: next(values),
            output_fn=self.output.append,
            color=False,
        )

    def test_missing_sharepoint_root_opens_common_settings_screen(self) -> None:
        manager = self.manager()
        calls: list[dict[str, object]] = []

        def common_screen(**kwargs):
            calls.append(dict(kwargs))
            return False

        manager._source_connection_settings_screen = common_screen
        with mock.patch.object(manager_connections.os, "name", "nt"):
            value = manager._prompt_new_sharepoint_source()
        self.assertIsNone(value)
        self.assertEqual([{"required": "sharepoint"}], calls)
        self.assertIn("共通のSource接続設定", "\n".join(self.output))

    def test_redmine_source_uses_registered_per_server_key_reference(self) -> None:
        project_url = "https://redmine.example/redmine/projects/alpha"
        manager = self.manager([project_url, "Project Alpha", ""])

        def common_screen(**kwargs):
            register_redmine_api_key(
                self.rag_root,
                str(kwargs["redmine_project_url"]),
                "WRITE-ONLY-SECRET",
            )
            return True

        manager._source_connection_settings_screen = common_screen
        value = manager._prompt_new_redmine_source()
        assert value is not None
        self.assertEqual("redmine", value["source_type"])
        self.assertEqual(
            redmine_api_key_env(project_url),
            value["fetch"]["api_key_env"],
        )
        self.assertNotIn("WRITE-ONLY-SECRET", "\n".join(self.output))

    def test_api_key_registration_never_echoes_secret(self) -> None:
        project_url = "https://redmine.example/projects/alpha"
        manager = self.manager()
        with mock.patch.object(
            manager_connections.getpass,
            "getpass",
            return_value="NEVER-ECHO-THIS",
        ):
            self.assertTrue(
                manager._register_redmine_api_key_setting(
                    project_url=project_url,
                )
            )
        output = "\n".join(self.output)
        self.assertNotIn("NEVER-ECHO-THIS", output)
        self.assertIn("再表示できません", output)
        secret_file = json.loads(
            connection_secret_path(self.rag_root).read_text(encoding="utf-8")
        )
        self.assertNotIn("NEVER-ECHO-THIS", json.dumps(secret_file))


if __name__ == "__main__":
    unittest.main()
