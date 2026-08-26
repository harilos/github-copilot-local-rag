from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from source_manager import manager_connections


RAG_ROOT = Path(__file__).resolve().parents[2]
MANAGER_PATH = RAG_ROOT / "manage.py"
SPEC = importlib.util.spec_from_file_location(
    "local_rag_manage_confluence_connections",
    MANAGER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


def _registration(**overrides):
    values = {
        "connection_id": "ba739a53-92e0-41a8-9a2f-86807a93a01c",
        "display_name": "Engineering wiki",
        "deployment": "cloud",
        "base_url": "https://tenant.atlassian.net",
        "token_kind": "unscoped",
        "cloud_id": None,
        "api_root": "https://tenant.atlassian.net/wiki/api/v2",
        "registered": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _confirmation(**overrides):
    values = {
        "deployment": "cloud",
        "base_url": "https://tenant.atlassian.net",
        "token_kind": "unscoped",
        "cloud_id": None,
        "api_root": "https://tenant.atlassian.net/wiki/api/v2",
        "account_email": "owner@example.test",
        "token": "CONFLUENCE-SECRET",
        "principal": "private-principal",
        "security_identity": "private-security-identity",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ConfluenceManagerConnectionUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-confluence-manager-connections-"
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

    def test_required_confluence_route_opens_registration(self) -> None:
        manager = self.manager()
        calls: list[bool] = []
        manager._register_confluence_connection_setting = (
            lambda: calls.append(True) or True
        )

        self.assertTrue(
            manager._source_connection_settings_screen(required="confluence")
        )

        self.assertEqual([True], calls)
        self.assertIn("Confluence", "\n".join(self.output))

    def test_general_menu_routes_confluence_and_lists_all_operations(self) -> None:
        manager = self.manager(["8", "0"])
        calls: list[bool] = []
        manager._register_confluence_connection_setting = (
            lambda: calls.append(True) or True
        )

        self.assertTrue(manager._source_connection_settings_screen())

        output = "\n".join(self.output)
        self.assertEqual([True], calls)
        self.assertIn("Confluence接続を登録・更新する", output)
        self.assertIn("Confluence接続の登録状況を見る", output)
        self.assertIn("Confluence接続を削除する", output)
        self.assertIn("Confluence接続をID指定で復旧する", output)

    def test_general_menu_routes_explicit_connection_id_recovery(self) -> None:
        manager = self.manager(["11", "0"])
        calls: list[bool] = []
        manager._recover_confluence_connection_setting = (
            lambda: calls.append(True) or True
        )

        self.assertTrue(manager._source_connection_settings_screen())

        self.assertEqual([True], calls)

    def test_cloud_unscoped_checks_before_saving_and_never_echoes_private_data(
        self,
    ) -> None:
        email = "owner@example.test"
        token = "CONFLUENCE-SECRET"
        principal = "private-principal"
        confirmation = _confirmation(
            account_email=email,
            token=token,
            principal=principal,
        )
        registration = _registration()
        manager = self.manager(
            [
                "1",
                "Engineering wiki",
                "https://tenant.atlassian.net",
                "1",
            ]
        )
        route_get = mock.sentinel.confluence_http_get

        with (
            mock.patch.object(
                manager_connections,
                "list_confluence_registrations",
                return_value=(),
            ),
            mock.patch.object(
                manager_connections,
                "check_confluence_credentials",
                return_value=confirmation,
            ) as check,
            mock.patch.object(
                manager_connections,
                "register_confluence_connection",
                return_value=registration,
            ) as register,
            mock.patch.object(
                manager_connections.getpass,
                "getpass",
                side_effect=(email, token),
            ) as hidden,
            mock.patch.object(
                manager_connections,
                "resolve_source_network_route",
                return_value=SimpleNamespace(http_get=route_get),
                create=True,
            ) as route,
        ):
            self.assertTrue(manager._register_confluence_connection_setting())

        check.assert_called_once()
        kwargs = check.call_args.kwargs
        self.assertEqual("cloud", kwargs["deployment"])
        self.assertEqual("unscoped", kwargs["token_kind"])
        self.assertEqual(email, kwargs["account_email"])
        self.assertEqual(token, kwargs["token"])
        self.assertIsNone(kwargs["cloud_id"])
        self.assertIs(route_get, kwargs["http_get"])
        route.assert_called_once_with(self.rag_root, environment=None)
        self.assertEqual(2, hidden.call_count)
        self.assertIn("email", hidden.call_args_list[0].args[0])
        self.assertIn("非表示", hidden.call_args_list[0].args[0])
        register.assert_called_once_with(
            self.rag_root,
            display_name="Engineering wiki",
            confirmation=confirmation,
            expected_connection_id=None,
        )
        output = "\n".join(self.output)
        for private in (email, token, principal, "private-security-identity"):
            self.assertNotIn(private, output)
        self.assertIn("接続確認に成功", output)
        self.assertIn("登録しました", output)

    def test_cloud_scoped_accepts_optional_cloud_id(self) -> None:
        email = "owner@example.test"
        confirmation = _confirmation(
            token_kind="scoped",
            cloud_id="3db25510-c13d-49fd-84dd-785b2a30f111",
            api_root=(
                "https://api.atlassian.com/ex/confluence/"
                "3db25510-c13d-49fd-84dd-785b2a30f111/wiki/api/v2"
            ),
        )
        manager = self.manager(
            [
                "1",
                "Scoped wiki",
                "https://tenant.atlassian.net",
                "2",
                "",
            ]
        )

        with (
            mock.patch.object(
                manager_connections,
                "list_confluence_registrations",
                return_value=(),
            ),
            mock.patch.object(
                manager_connections,
                "check_confluence_credentials",
                return_value=confirmation,
            ) as check,
            mock.patch.object(
                manager_connections,
                "register_confluence_connection",
                return_value=_registration(token_kind="scoped"),
            ),
            mock.patch.object(
                manager_connections.getpass,
                "getpass",
                side_effect=(email, "SCOPED-SECRET"),
            ),
            mock.patch.object(
                manager_connections,
                "resolve_source_network_route",
                return_value=SimpleNamespace(
                    http_get=mock.sentinel.scoped_http_get
                ),
                create=True,
            ),
        ):
            self.assertTrue(manager._register_confluence_connection_setting())

        kwargs = check.call_args.kwargs
        self.assertEqual("scoped", kwargs["token_kind"])
        self.assertIsNone(kwargs["cloud_id"])
        self.assertEqual(email, kwargs["account_email"])

    def test_data_center_uses_explicit_deployment_and_pat(self) -> None:
        confirmation = _confirmation(
            deployment="data_center",
            base_url="https://wiki.example.test/confluence",
            token_kind="pat",
            account_email="",
        )
        manager = self.manager(
            [
                "2",
                "Internal wiki",
                "https://wiki.example.test/confluence",
            ]
        )

        with (
            mock.patch.object(
                manager_connections,
                "list_confluence_registrations",
                return_value=(),
            ),
            mock.patch.object(
                manager_connections,
                "check_confluence_credentials",
                return_value=confirmation,
            ) as check,
            mock.patch.object(
                manager_connections,
                "register_confluence_connection",
                return_value=_registration(deployment="data_center"),
            ),
            mock.patch.object(
                manager_connections.getpass,
                "getpass",
                return_value="DATA-CENTER-PAT",
            ),
            mock.patch.object(
                manager_connections,
                "resolve_source_network_route",
                return_value=SimpleNamespace(
                    http_get=mock.sentinel.data_center_http_get
                ),
                create=True,
            ),
        ):
            self.assertTrue(manager._register_confluence_connection_setting())

        kwargs = check.call_args.kwargs
        self.assertEqual("data_center", kwargs["deployment"])
        self.assertEqual("pat", kwargs["token_kind"])
        self.assertIsNone(kwargs["account_email"])
        self.assertIsNone(kwargs["cloud_id"])

    def test_failed_check_is_specific_sanitized_and_never_saved(self) -> None:
        cases = (
            (RuntimeError("HTTP 401 owner@example.test CONFLUENCE-SECRET"), "認証情報"),
            (RuntimeError("HTTP 403 owner@example.test CONFLUENCE-SECRET"), "権限"),
            (RuntimeError("HTTP 404 owner@example.test CONFLUENCE-SECRET"), "見つかりません"),
            (TimeoutError("owner@example.test CONFLUENCE-SECRET"), "通信"),
        )
        for failure, expected in cases:
            with self.subTest(expected=expected):
                self.output.clear()
                manager = self.manager(
                    [
                        "1",
                        "Engineering wiki",
                        "https://tenant.atlassian.net",
                        "1",
                    ]
                )
                with (
                    mock.patch.object(
                        manager_connections,
                        "list_confluence_registrations",
                        return_value=(),
                    ),
                    mock.patch.object(
                        manager_connections,
                        "check_confluence_credentials",
                        side_effect=failure,
                    ),
                    mock.patch.object(
                        manager_connections,
                        "register_confluence_connection",
                    ) as register,
                    mock.patch.object(
                        manager_connections.getpass,
                        "getpass",
                        side_effect=(
                            "owner@example.test",
                            "CONFLUENCE-SECRET",
                        ),
                    ),
                    mock.patch.object(
                        manager_connections,
                        "resolve_source_network_route",
                        return_value=SimpleNamespace(
                            http_get=mock.sentinel.failure_http_get
                        ),
                        create=True,
                    ),
                ):
                    self.assertFalse(
                        manager._register_confluence_connection_setting()
                    )
                register.assert_not_called()
                output = "\n".join(self.output)
                self.assertIn(expected, output)
                self.assertNotIn("owner@example.test", output)
                self.assertNotIn("CONFLUENCE-SECRET", output)

    def test_update_uses_selected_connection_id_only_after_check(self) -> None:
        existing = _registration()
        confirmation = _confirmation()
        manager = self.manager(
            [
                "2",
                "",
                "",
                "",
                "",
            ]
        )

        with (
            mock.patch.object(
                manager_connections,
                "list_confluence_registrations",
                return_value=(existing,),
            ),
            mock.patch.object(
                manager_connections,
                "check_confluence_credentials",
                return_value=confirmation,
            ),
            mock.patch.object(
                manager_connections,
                "register_confluence_connection",
                return_value=existing,
            ) as register,
            mock.patch.object(
                manager_connections.getpass,
                "getpass",
                side_effect=(
                    "replacement@example.test",
                    "REPLACEMENT-SECRET",
                ),
            ),
            mock.patch.object(
                manager_connections,
                "resolve_source_network_route",
                return_value=SimpleNamespace(
                    http_get=mock.sentinel.update_http_get
                ),
                create=True,
            ),
        ):
            self.assertTrue(manager._register_confluence_connection_setting())

        register.assert_called_once_with(
            self.rag_root,
            display_name=existing.display_name,
            confirmation=confirmation,
            expected_connection_id=existing.connection_id,
        )

    def test_recovery_prompts_for_explicit_uuid_and_preserves_it(self) -> None:
        expected = "0b955ed0-4f61-4e11-8d07-d1c35f839968"
        manager = self.manager([expected])
        manager._register_confluence_connection_setting = mock.Mock(
            return_value=True
        )

        self.assertTrue(manager._recover_confluence_connection_setting())

        manager._register_confluence_connection_setting.assert_called_once_with(
            expected_connection_id=expected
        )

    def test_list_and_delete_show_only_public_registration_data(self) -> None:
        registration = _registration(
            account_email="private@example.test",
            principal="private-principal",
            token="PRIVATE-TOKEN",
        )
        manager = self.manager(["1", "y"])

        with mock.patch.object(
            manager_connections,
            "list_confluence_registrations",
            return_value=(registration,),
        ):
            manager._show_confluence_registrations()
            with mock.patch.object(
                manager_connections,
                "delete_confluence_connection",
                return_value=True,
            ) as delete:
                self.assertTrue(manager._delete_confluence_connection_setting())

        delete.assert_called_once_with(
            self.rag_root,
            registration.connection_id,
        )
        output = "\n".join(self.output)
        self.assertIn(registration.display_name, output)
        self.assertIn(registration.base_url, output)
        for private in (
            "private@example.test",
            "private-principal",
            "PRIVATE-TOKEN",
        ):
            self.assertNotIn(private, output)


if __name__ == "__main__":
    unittest.main()
