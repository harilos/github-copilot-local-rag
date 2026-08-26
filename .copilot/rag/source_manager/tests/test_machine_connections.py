from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from source_manager.errors import SourceManagerError
from source_manager.machine_connections import (
    SHAREPOINT_ROOT_ENV,
    check_confluence_credentials,
    check_gitlab_project,
    clear_sharepoint_root,
    confluence_connection_id,
    connection_config_path,
    connection_secret_path,
    delete_confluence_connection,
    gitlab_project_location,
    gitlab_token_env,
    list_confluence_registrations,
    list_gitlab_registrations,
    list_redmine_registrations,
    redmine_api_key_env,
    register_confluence_connection,
    register_gitlab_token,
    register_redmine_api_key,
    resolve_confluence_credentials,
    resolve_gitlab_token,
    resolve_redmine_api_key,
    set_sharepoint_root,
    sharepoint_root_status,
    source_runtime_environment,
)
from source_manager import machine_connections, manager_connections


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

    def test_gitlab_project_path_is_encoded_below_self_managed_root(
        self,
    ) -> None:
        location = gitlab_project_location(
            "https://git.example.test/tools/gitlab/",
            "https://git.example.test/tools/gitlab/"
            "group/sub%20group/project/",
        )
        self.assertEqual(
            "https://git.example.test/tools/gitlab",
            location.gitlab_url,
        )
        self.assertEqual("group/sub group/project", location.project_path)
        self.assertEqual(
            "group%2Fsub%20group%2Fproject",
            location.encoded_project_path,
        )
        self.assertEqual(
            "https://git.example.test/tools/gitlab/api/v4/projects/"
            "group%2Fsub%20group%2Fproject",
            location.project_api_url,
        )

    def test_gitlab_token_is_machine_local_and_injected_by_instance(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test/tools/gitlab"
        secret = "GITLAB-WRITE-ONLY-SECRET"
        registration = register_gitlab_token(
            self.rag_root,
            gitlab_url,
            secret,
        )
        self.assertEqual(
            secret,
            resolve_gitlab_token(
                self.rag_root,
                gitlab_url=gitlab_url,
                token_env=registration.token_env,
                environ={},
            ),
        )
        environment = source_runtime_environment(
            self.rag_root,
            {
                "source_type": "gitlab_issues",
                "fetch": {
                    "gitlab_url": gitlab_url,
                    "project_url": (
                        f"{gitlab_url}/group/subgroup/project"
                    ),
                    "updated_within_days": 30,
                    "token_env": registration.token_env,
                },
            },
            environ={},
        )
        self.assertEqual(secret, environment[registration.token_env])
        persisted = (
            connection_config_path(self.rag_root).read_text(encoding="utf-8")
            + connection_secret_path(self.rag_root).read_text(encoding="utf-8")
        )
        self.assertNotIn(secret, persisted)
        registrations = list_gitlab_registrations(self.rag_root)
        self.assertEqual(1, len(registrations))
        self.assertNotIn(secret, repr(registrations))

    def test_gitlab_token_environment_is_bound_to_the_instance(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test/tools/gitlab"
        expected_env = gitlab_token_env(gitlab_url)
        self.assertEqual(
            "EXPECTED-GITLAB-TOKEN",
            resolve_gitlab_token(
                self.rag_root,
                gitlab_url=gitlab_url,
                token_env=expected_env,
                environ={expected_env: "EXPECTED-GITLAB-TOKEN"},
            ),
        )
        with self.assertRaises(SourceManagerError):
            source_runtime_environment(
                self.rag_root,
                {
                    "source_type": "gitlab_issues",
                    "fetch": {
                        "gitlab_url": gitlab_url,
                        "project_url": f"{gitlab_url}/group/project",
                        "updated_within_days": 30,
                        "token_env": "AWS_SECRET_ACCESS_KEY",
                    },
                },
                environ={
                    "AWS_SECRET_ACCESS_KEY": "UNRELATED-SECRET",
                },
            )

    def test_gitlab_connection_check_uses_encoded_project_path(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test/tools/gitlab"
        project_url = (
            f"{gitlab_url}/group/subgroup/project"
        )
        token = "CHECK-ONLY-SECRET"
        registration = register_gitlab_token(
            self.rag_root,
            gitlab_url,
            token,
        )
        calls: list[tuple[str, dict[str, str], float]] = []

        def getter(url, headers, timeout):
            calls.append((url, dict(headers), float(timeout)))
            if "/projects/42/issues?" in url:
                return 200, b"[]", {}
            return (
                200,
                json.dumps(
                    {
                        "id": 42,
                        "name_with_namespace": (
                            "Group / Subgroup / Project"
                        ),
                        "web_url": project_url,
                        "path_with_namespace": (
                            "group/subgroup/project"
                        ),
                    }
                ).encode(),
                {},
            )

        checked = check_gitlab_project(
            self.rag_root,
            gitlab_url=gitlab_url,
            project_url=project_url,
            token_env=registration.token_env,
            environ={},
            http_get=getter,
        )
        self.assertEqual(42, checked.project_id)
        self.assertEqual("Group / Subgroup / Project", checked.name)
        self.assertEqual(2, len(calls))
        self.assertTrue(
            calls[0][0].endswith(
                "/api/v4/projects/group%2Fsubgroup%2Fproject"
            )
        )
        self.assertTrue(
            calls[1][0].endswith(
                "/api/v4/projects/42/issues"
                "?scope=all&state=all&per_page=1&page=1"
            )
        )
        self.assertEqual(token, calls[0][1]["PRIVATE-TOKEN"])
        self.assertEqual(token, calls[1][1]["PRIVATE-TOKEN"])
        self.assertNotIn(token, repr(checked))

    def test_gitlab_connection_check_accepts_dual_hostname_metadata(
        self,
    ) -> None:
        cases = (
            (
                "P1-same-host",
                "https://git-e.example/gitlab",
                "group/project",
                "https://git-e.example/gitlab/group/project",
            ),
            (
                "P2-dual-host",
                "https://git-e.example/gitlab",
                "group/project",
                "https://git-p.example/group/project",
            ),
            (
                "P3-dual-host-subpath",
                "https://git-e.example/internal-gitlab",
                "group/project",
                "https://git-p.example/external-gitlab/group/project",
            ),
            (
                "P4-nested-namespace",
                "https://git-e.example/gitlab",
                "group/subgroup/project",
                "https://git-p.example/group/subgroup/project",
            ),
        )
        for label, gitlab_url, project_path, returned_web_url in cases:
            with self.subTest(case=label):
                project_url = f"{gitlab_url}/{project_path}"
                token = f"CHECK-ONLY-{label}"
                registration = register_gitlab_token(
                    self.rag_root,
                    gitlab_url,
                    token,
                )
                calls: list[tuple[str, dict[str, str], float]] = []

                def getter(url, headers, timeout):
                    calls.append((url, dict(headers), float(timeout)))
                    if "/projects/42/issues?" in url:
                        return 200, b"[]", {}
                    return (
                        200,
                        json.dumps(
                            {
                                "id": 42,
                                "name": "Project",
                                "web_url": returned_web_url,
                                "path_with_namespace": project_path,
                            }
                        ).encode(),
                        {},
                    )

                checked = check_gitlab_project(
                    self.rag_root,
                    gitlab_url=gitlab_url,
                    project_url=project_url,
                    token_env=registration.token_env,
                    environ={},
                    http_get=getter,
                )

                expected = gitlab_project_location(
                    gitlab_url,
                    project_url,
                )
                self.assertEqual(expected, checked.location)
                self.assertEqual(2, len(calls))
                for url, headers, _timeout in calls:
                    self.assertTrue(
                        url.startswith(f"{expected.api_base_url}/")
                    )
                    self.assertEqual(token, headers["PRIVATE-TOKEN"])
                    self.assertNotIn("git-p.example", url)

    def test_gitlab_connection_check_requires_exact_response_path_identity(
        self,
    ) -> None:
        gitlab_url = "https://git-e.example/gitlab"
        project_url = f"{gitlab_url}/group/project"
        registration = register_gitlab_token(
            self.rag_root,
            gitlab_url,
            "CHECK-ONLY-SECRET",
        )
        invalid_paths = (
            None,
            "",
            "   ",
            123,
            " group/project",
            "group/project ",
            "other/group/project",
            "group/project-extra",
            "group/project/child",
            "group%2Fproject",
            "group\\project",
            "group/../project",
            "group∕project",
        )
        for response_path in invalid_paths:
            with self.subTest(path=response_path):
                calls: list[str] = []

                def getter(url, _headers, _timeout):
                    calls.append(url)
                    return (
                        200,
                        json.dumps(
                            {
                                "id": 42,
                                "name": "Project",
                                "web_url": (
                                    "https://git-p.example/group/project"
                                ),
                                "path_with_namespace": response_path,
                            }
                        ).encode(),
                        {},
                    )

                with self.assertRaisesRegex(
                    SourceManagerError,
                    "different project",
                ):
                    check_gitlab_project(
                        self.rag_root,
                        gitlab_url=gitlab_url,
                        project_url=project_url,
                        token_env=registration.token_env,
                        environ={},
                        http_get=getter,
                    )
                self.assertEqual(1, len(calls))
                self.assertTrue(
                    calls[0].startswith(f"{gitlab_url}/api/v4/")
                )

    def test_gitlab_connection_check_requires_issues_api_access(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test"
        project_url = f"{gitlab_url}/group/project"
        token = "CHECK-ONLY-SECRET"
        registration = register_gitlab_token(
            self.rag_root,
            gitlab_url,
            token,
        )

        def getter(url, _headers, _timeout):
            if "/projects/42/issues?" in url:
                return 403, b'{"message":"forbidden"}', {}
            return (
                200,
                json.dumps(
                    {
                        "id": 42,
                        "name": "Project",
                        "web_url": project_url,
                        "path_with_namespace": "group/project",
                    }
                ).encode(),
                {},
            )

        with self.assertRaisesRegex(
            SourceManagerError,
            r"GitLab Issues API connection check failed \(HTTP 403\)",
        ) as raised:
            check_gitlab_project(
                self.rag_root,
                gitlab_url=gitlab_url,
                project_url=project_url,
                token_env=registration.token_env,
                environ={},
                http_get=getter,
            )
        self.assertNotIn(token, str(raised.exception))

    def test_gitlab_connection_check_requires_issues_json_array(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test"
        project_url = f"{gitlab_url}/group/project"
        registration = register_gitlab_token(
            self.rag_root,
            gitlab_url,
            "CHECK-ONLY-SECRET",
        )

        def getter(url, _headers, _timeout):
            if "/projects/42/issues?" in url:
                return 200, b'{"message":"unexpected"}', {}
            return (
                200,
                json.dumps(
                    {
                        "id": 42,
                        "name": "Project",
                        "web_url": project_url,
                        "path_with_namespace": "group/project",
                    }
                ).encode(),
                {},
            )

        with self.assertRaisesRegex(
            SourceManagerError,
            "GitLab Issues API connection check returned an invalid response",
        ):
            check_gitlab_project(
                self.rag_root,
                gitlab_url=gitlab_url,
                project_url=project_url,
                token_env=registration.token_env,
                environ={},
                http_get=getter,
            )

    def test_gitlab_project_url_rejects_other_origin_and_non_top_page(
        self,
    ) -> None:
        for project_url in (
            "https://other.example.test/group/project",
            "https://git.example.test/group/project/-/issues",
        ):
            with self.subTest(project_url=project_url), self.assertRaises(
                SourceManagerError
            ):
                gitlab_project_location(
                    "https://git.example.test",
                    project_url,
                )

    def test_confluence_cloud_registration_keeps_identity_and_secrets_private(
        self,
    ) -> None:
        token = "CLOUD-TOKEN-MUST-STAY-PRIVATE"
        email = "owner@example.test"
        principal = "712020:cloud-account-id"
        calls: list[tuple[str, dict[str, str]]] = []

        def getter(url, headers, _timeout):
            calls.append((url, dict(headers)))
            return (
                200,
                json.dumps({"accountId": principal}).encode("utf-8"),
                {},
            )

        confirmation = check_confluence_credentials(
            deployment="cloud",
            base_url="https://tenant.atlassian.net/",
            account_email=email,
            token=token,
            token_kind="unscoped",
            http_get=getter,
        )
        self.assertFalse(connection_config_path(self.rag_root).exists())
        self.assertFalse(connection_secret_path(self.rag_root).exists())
        registration = register_confluence_connection(
            self.rag_root,
            display_name="Finance wiki",
            confirmation=confirmation,
        )

        parsed_id = uuid.UUID(registration.connection_id)
        self.assertEqual(4, parsed_id.version)
        self.assertEqual(
            registration.connection_id,
            confluence_connection_id(registration.connection_id),
        )
        self.assertEqual(
            "https://tenant.atlassian.net/wiki/rest/api/user/current",
            calls[0][0],
        )
        self.assertTrue(calls[0][1]["Authorization"].startswith("Basic "))

        public_text = connection_config_path(self.rag_root).read_text(
            encoding="utf-8"
        )
        secret_text = connection_secret_path(self.rag_root).read_text(
            encoding="utf-8"
        )
        for private in (token, email, principal, confirmation.security_identity):
            self.assertNotIn(private, public_text)
            self.assertNotIn(private, secret_text)
        public = json.loads(public_text)["confluence"][registration.connection_id]
        self.assertEqual(
            {
                "api_root",
                "base_url",
                "cloud_id",
                "deployment",
                "display_name",
                "token_kind",
                "updated_at",
            },
            set(public),
        )

        listed = list_confluence_registrations(self.rag_root)
        self.assertEqual((registration,), listed)
        resolved = resolve_confluence_credentials(
            self.rag_root,
            registration.connection_id,
        )
        assert resolved is not None
        self.assertEqual(email, resolved.account_email)
        self.assertEqual(token, resolved.token)
        self.assertEqual(principal, resolved.principal)
        for private in (token, email, principal, confirmation.security_identity):
            self.assertNotIn(private, repr(listed))
            self.assertNotIn(private, repr(resolved))
            self.assertNotIn(private, repr(confirmation))

    def test_confluence_data_center_uses_bearer_and_stable_principal(
        self,
    ) -> None:
        token = "DATA-CENTER-PAT"
        principal = "4028a8085f0f7c6f015f0f7d6a1b0001"
        calls: list[tuple[str, dict[str, str]]] = []

        def getter(url, headers, _timeout):
            calls.append((url, dict(headers)))
            return (
                200,
                json.dumps(
                    {"userKey": principal, "username": "renameable-name"}
                ).encode("utf-8"),
                {},
            )

        confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test/tools/confluence/",
            token=token,
            http_get=getter,
        )
        registered = register_confluence_connection(
            self.rag_root,
            display_name="Engineering Confluence",
            confirmation=confirmation,
        )

        self.assertEqual(
            "https://confluence.example.test/tools/confluence/rest/api/user/current",
            calls[0][0],
        )
        self.assertEqual(f"Bearer {token}", calls[0][1]["Authorization"])
        resolved = resolve_confluence_credentials(
            self.rag_root,
            registered.connection_id,
        )
        assert resolved is not None
        self.assertEqual("data_center", resolved.deployment)
        self.assertEqual(principal, resolved.principal)
        self.assertEqual("", resolved.account_email)

    def test_confluence_rotation_changes_display_and_secret_not_identity(
        self,
    ) -> None:
        def confirmation(*, email: str, token: str, principal: str):
            return check_confluence_credentials(
                deployment="cloud",
                base_url="https://tenant.atlassian.net",
                account_email=email,
                token=token,
                token_kind="unscoped",
                http_get=lambda _url, _headers, _timeout: (
                    200,
                    json.dumps({"accountId": principal}).encode("utf-8"),
                    {},
                ),
            )

        first = register_confluence_connection(
            self.rag_root,
            display_name="Old display",
            confirmation=confirmation(
                email="old@example.test",
                token="OLD-TOKEN",
                principal="stable-cloud-account",
            ),
        )
        rotated = register_confluence_connection(
            self.rag_root,
            display_name="New display",
            confirmation=confirmation(
                email="renamed@example.test",
                token="NEW-TOKEN",
                principal="stable-cloud-account",
            ),
            expected_connection_id=first.connection_id,
        )
        self.assertEqual(first.connection_id, rotated.connection_id)
        self.assertEqual("New display", rotated.display_name)
        resolved = resolve_confluence_credentials(
            self.rag_root,
            first.connection_id,
        )
        assert resolved is not None
        self.assertEqual("NEW-TOKEN", resolved.token)
        self.assertEqual("renamed@example.test", resolved.account_email)

        config_before = connection_config_path(self.rag_root).read_bytes()
        secrets_before = connection_secret_path(self.rag_root).read_bytes()
        with self.assertRaisesRegex(
            SourceManagerError,
            "security identity does not match",
        ):
            register_confluence_connection(
                self.rag_root,
                display_name="Attempted takeover",
                confirmation=confirmation(
                    email="other@example.test",
                    token="TAKEOVER-TOKEN",
                    principal="different-cloud-account",
                ),
                expected_connection_id=first.connection_id,
            )
        self.assertEqual(
            config_before,
            connection_config_path(self.rag_root).read_bytes(),
        )
        self.assertEqual(
            secrets_before,
            connection_secret_path(self.rag_root).read_bytes(),
        )

    def test_confluence_deleted_id_recovers_only_same_security_identity(
        self,
    ) -> None:
        first_confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test",
            token="ORIGINAL-PAT",
            http_get=lambda _url, _headers, _timeout: (
                200,
                b'{"userKey":"stable-principal"}',
                {},
            ),
        )
        first = register_confluence_connection(
            self.rag_root,
            display_name="Original connection",
            confirmation=first_confirmation,
        )
        self.assertTrue(
            delete_confluence_connection(
                self.rag_root,
                first.connection_id,
            )
        )
        secret_payload = json.loads(
            connection_secret_path(self.rag_root).read_text(encoding="utf-8")
        )
        tombstone = secret_payload["confluence_tombstones"][
            first.connection_id
        ]
        self.assertEqual(
            {"security_identity", "updated_at"},
            set(tombstone),
        )
        serialized = json.dumps(secret_payload)
        self.assertNotIn("ORIGINAL-PAT", serialized)
        self.assertNotIn("stable-principal", serialized)

        wrong_confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test",
            token="WRONG-PAT",
            http_get=lambda _url, _headers, _timeout: (
                200,
                b'{"userKey":"different-principal"}',
                {},
            ),
        )
        config_before = connection_config_path(self.rag_root).read_bytes()
        secrets_before = connection_secret_path(self.rag_root).read_bytes()
        with self.assertRaisesRegex(
            SourceManagerError,
            "security identity does not match",
        ):
            register_confluence_connection(
                self.rag_root,
                display_name="Wrong recovery",
                confirmation=wrong_confirmation,
                expected_connection_id=first.connection_id,
            )
        self.assertEqual(
            config_before,
            connection_config_path(self.rag_root).read_bytes(),
        )
        self.assertEqual(
            secrets_before,
            connection_secret_path(self.rag_root).read_bytes(),
        )

        recovered_confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test",
            token="RECOVERED-PAT",
            http_get=lambda _url, _headers, _timeout: (
                200,
                b'{"userKey":"stable-principal"}',
                {},
            ),
        )
        recovered = register_confluence_connection(
            self.rag_root,
            display_name="Recovered connection",
            confirmation=recovered_confirmation,
            expected_connection_id=first.connection_id,
        )
        self.assertEqual(first.connection_id, recovered.connection_id)
        resolved = resolve_confluence_credentials(
            self.rag_root,
            recovered.connection_id,
        )
        assert resolved is not None
        self.assertEqual("RECOVERED-PAT", resolved.token)
        secret_payload = json.loads(
            connection_secret_path(self.rag_root).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            recovered.connection_id,
            secret_payload["confluence_tombstones"],
        )

    def test_confluence_new_random_id_never_reuses_tombstone(self) -> None:
        confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test",
            token="FIRST-PAT",
            http_get=lambda _url, _headers, _timeout: (
                200,
                b'{"userKey":"stable-principal"}',
                {},
            ),
        )
        first = register_confluence_connection(
            self.rag_root,
            display_name="First",
            confirmation=confirmation,
        )
        self.assertTrue(
            delete_confluence_connection(self.rag_root, first.connection_id)
        )
        replacement_id = uuid.uuid4()
        with mock.patch.object(
            machine_connections.uuid,
            "uuid4",
            side_effect=(uuid.UUID(first.connection_id), replacement_id),
        ):
            replacement = register_confluence_connection(
                self.rag_root,
                display_name="Replacement",
                confirmation=confirmation,
            )
        self.assertEqual(str(replacement_id), replacement.connection_id)

    def test_confluence_public_registry_rejects_unknown_or_secret_aliases(
        self,
    ) -> None:
        confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test",
            token="PRIVATE-PAT",
            http_get=lambda _url, _headers, _timeout: (
                200,
                b'{"userKey":"stable-principal"}',
                {},
            ),
        )
        registered = register_confluence_connection(
            self.rag_root,
            display_name="Strict public entry",
            confirmation=confirmation,
        )
        original = json.loads(
            connection_config_path(self.rag_root).read_text(encoding="utf-8")
        )
        for field in (
            "api_token",
            "password",
            "access_token",
            "username",
            "unknown_field",
        ):
            with self.subTest(field=field):
                tampered = json.loads(json.dumps(original))
                tampered["confluence"][registered.connection_id][field] = (
                    "MUST-NOT-BE-ACCEPTED"
                )
                connection_config_path(self.rag_root).write_text(
                    json.dumps(tampered),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    SourceManagerError,
                    "public connection data is invalid",
                ):
                    list_confluence_registrations(self.rag_root)
                with self.assertRaisesRegex(
                    SourceManagerError,
                    "public connection data is invalid",
                ):
                    resolve_confluence_credentials(
                        self.rag_root,
                        registered.connection_id,
                    )
        connection_config_path(self.rag_root).write_text(
            json.dumps(original),
            encoding="utf-8",
        )

    def test_confluence_registration_rolls_back_both_registry_files(
        self,
    ) -> None:
        confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test",
            token="ROLLBACK-PAT",
            http_get=lambda _url, _headers, _timeout: (
                200,
                b'{"userKey":"rollback-principal"}',
                {},
            ),
        )
        with mock.patch.object(
            machine_connections,
            "_save_connections",
            side_effect=OSError("simulated public config failure"),
        ), self.assertRaisesRegex(OSError, "simulated public config failure"):
            register_confluence_connection(
                self.rag_root,
                display_name="Must roll back",
                confirmation=confirmation,
            )
        self.assertFalse(connection_config_path(self.rag_root).exists())
        self.assertFalse(connection_secret_path(self.rag_root).exists())

    def test_confluence_delete_is_atomic_and_removes_both_entries(self) -> None:
        confirmation = check_confluence_credentials(
            deployment="data_center",
            base_url="https://confluence.example.test",
            token="DELETE-PAT",
            http_get=lambda _url, _headers, _timeout: (
                200,
                b'{"userKey":"delete-principal"}',
                {},
            ),
        )
        registered = register_confluence_connection(
            self.rag_root,
            display_name="Delete me",
            confirmation=confirmation,
        )
        config_before = connection_config_path(self.rag_root).read_bytes()
        secrets_before = connection_secret_path(self.rag_root).read_bytes()
        with mock.patch.object(
            machine_connections,
            "_save_connections",
            side_effect=OSError("simulated public delete failure"),
        ), self.assertRaisesRegex(OSError, "simulated public delete failure"):
            delete_confluence_connection(
                self.rag_root,
                registered.connection_id,
            )
        self.assertEqual(
            config_before,
            connection_config_path(self.rag_root).read_bytes(),
        )
        self.assertEqual(
            secrets_before,
            connection_secret_path(self.rag_root).read_bytes(),
        )

        self.assertTrue(
            delete_confluence_connection(
                self.rag_root,
                registered.connection_id,
            )
        )
        self.assertIsNone(
            resolve_confluence_credentials(
                self.rag_root,
                registered.connection_id,
            )
        )
        self.assertEqual((), list_confluence_registrations(self.rag_root))
        self.assertFalse(
            delete_confluence_connection(
                self.rag_root,
                registered.connection_id,
            )
        )

    def test_confluence_scoped_token_discovers_or_manually_recovers_cloud_id(
        self,
    ) -> None:
        discovered = "b85d6f92-20fb-4c40-91e2-9f78e4271150"
        principal = "712020:scoped-cloud-account"
        urls: list[str] = []

        def getter(url, headers, _timeout):
            urls.append(url)
            if url.endswith("/_edge/tenant_info"):
                self.assertNotIn("Authorization", headers)
                return 200, json.dumps({"cloudId": discovered}).encode(), {}
            self.assertTrue(headers["Authorization"].startswith("Basic "))
            return 200, json.dumps({"accountId": principal}).encode(), {}

        confirmation = check_confluence_credentials(
            deployment="cloud",
            base_url="https://tenant.atlassian.net",
            account_email="owner@example.test",
            token="SCOPED-TOKEN",
            token_kind="scoped",
            http_get=getter,
        )
        self.assertEqual(discovered, confirmation.cloud_id)
        self.assertEqual(
            "https://api.atlassian.com/ex/confluence/"
            f"{discovered}/wiki/api/v2",
            confirmation.api_root,
        )
        self.assertEqual(
            [
                "https://tenant.atlassian.net/_edge/tenant_info",
                "https://api.atlassian.com/ex/confluence/"
                f"{discovered}/wiki/rest/api/user/current",
            ],
            urls,
        )

        manual = "bb80d19d-7725-409e-9b0b-12fddccfebbf"

        def unavailable(url, _headers, _timeout):
            if url.endswith("/_edge/tenant_info"):
                return 503, b"", {}
            return 200, json.dumps({"accountId": principal}).encode(), {}

        recovered = check_confluence_credentials(
            deployment="cloud",
            base_url="https://tenant.atlassian.net",
            account_email="owner@example.test",
            token="SCOPED-TOKEN",
            token_kind="scoped",
            cloud_id=manual,
            http_get=unavailable,
        )
        self.assertEqual(manual, recovered.cloud_id)
        with self.assertRaisesRegex(SourceManagerError, r"manual .*cloud ID"):
            check_confluence_credentials(
                deployment="cloud",
                base_url="https://tenant.atlassian.net",
                account_email="owner@example.test",
                token="SCOPED-TOKEN",
                token_kind="scoped",
                cloud_id=manual,
                http_get=getter,
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

    def test_gitlab_token_registration_never_echoes_secret(self) -> None:
        manager = self.manager()
        with mock.patch.object(
            manager_connections.getpass,
            "getpass",
            return_value="NEVER-ECHO-GITLAB-TOKEN",
        ):
            self.assertTrue(
                manager._register_gitlab_token_setting(
                    gitlab_url=(
                        "https://git.example.test/tools/gitlab"
                    ),
                )
            )
        output = "\n".join(self.output)
        self.assertNotIn("NEVER-ECHO-GITLAB-TOKEN", output)
        self.assertIn("再表示できません", output)
        secret_file = json.loads(
            connection_secret_path(self.rag_root).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "NEVER-ECHO-GITLAB-TOKEN",
            json.dumps(secret_file),
        )

    def test_common_connection_menu_routes_gitlab_project_check(
        self,
    ) -> None:
        manager = self.manager(["7", "0"])
        calls: list[bool] = []
        manager._check_gitlab_project_setting = (
            lambda: calls.append(True) or True
        )

        self.assertTrue(manager._source_connection_settings_screen())

        self.assertEqual([True], calls)
        self.assertIn(
            "GitLabプロジェクトへの接続を確認する",
            "\n".join(self.output),
        )

    def test_gitlab_source_form_uses_confirmed_canonical_settings(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test/tools/gitlab"
        project_url = (
            f"{gitlab_url}/group/subgroup/project"
        )
        manager = self.manager(
            [
                gitlab_url,
                project_url,
                "Project issues",
                "",
            ]
        )
        location = gitlab_project_location(gitlab_url, project_url)
        manager._confirm_gitlab_project_connection = (
            lambda **_kwargs: SimpleNamespace(
                location=location,
                project_id=42,
                name="Group / Subgroup / Project",
            )
        )

        value = manager._prompt_new_gitlab_issues_source()

        assert value is not None
        self.assertEqual("gitlab_issues", value["source_type"])
        self.assertEqual(
            {
                "gitlab_url": gitlab_url,
                "project_url": project_url,
                "updated_within_days": 365,
                "token_env": gitlab_token_env(gitlab_url),
            },
            value["fetch"],
        )
        self.assertNotIn("project_id", value["fetch"])
        self.assertNotIn("api_base_url", value["fetch"])
        self.assertEqual(
            f"{project_url}/-/issues/{{issue_iid}}",
            value["link"]["settings"]["url_template"],
        )
        self.assertEqual(
            "open／closed両方",
            dict(value["summary"])["Issue状態"],
        )
        self.assertEqual(
            "削除・閲覧不可になった既存Issueも保持",
            dict(value["summary"])["履歴保持"],
        )
        self.assertEqual(
            "初回反映後は不可（別Sourceとして追加）",
            dict(value["summary"])["project変更"],
        )

    def test_manager_gitlab_connection_confirmation_uses_stored_token(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test/tools/gitlab"
        project_url = f"{gitlab_url}/group/project"
        register_gitlab_token(
            self.rag_root,
            gitlab_url,
            "CONNECTION-CHECK-TOKEN",
        )
        manager = self.manager()

        def getter(url, _headers, _timeout):
            if "/projects/7/issues?" in url:
                return 200, b"[]", {}
            return (
                200,
                json.dumps(
                    {
                        "id": 7,
                        "name_with_namespace": "Group / Project",
                        "web_url": project_url,
                        "path_with_namespace": "group/project",
                    }
                ).encode(),
                {},
            )

        route = SimpleNamespace(environment={}, http_get=getter)
        with mock.patch(
            "source_manager.networking.resolve_source_network_route",
            return_value=route,
        ):
            checked = manager._confirm_gitlab_project_connection(
                gitlab_url=gitlab_url,
                project_url=project_url,
            )
        assert checked is not None
        self.assertEqual(7, checked.project_id)
        rendered = "\n".join(self.output)
        self.assertIn("access tokenで接続できました", rendered)
        self.assertNotIn("CONNECTION-CHECK-TOKEN", rendered)

    def test_gitlab_fetch_settings_are_displayed_and_editable(
        self,
    ) -> None:
        gitlab_url = "https://git.example.test/tools/gitlab"
        project_url = f"{gitlab_url}/group/project"
        token_env = gitlab_token_env(gitlab_url)
        source = {
            "source_type": "gitlab_issues",
            "display_name": "Project issues",
            "source_id": "src_project-0123456789ab",
            "_local_source_key": "src_project-0123456789ab",
            "fetch": {
                "gitlab_url": gitlab_url,
                "project_url": project_url,
                "updated_within_days": 365,
                "token_env": token_env,
                "api_base_url": f"{gitlab_url}/api/v4",
                "project_id": 7,
                "legacy_option": "remove-me",
            },
        }
        (self.rag_root / "dbs" / "example-rag").mkdir()
        manager = self.manager(["90", "y"])
        manager._confirm_gitlab_project_connection = mock.Mock(
            side_effect=AssertionError(
                "indexed GitLab project identity must not be reselected"
            )
        )
        with mock.patch(
            "source_manager.runner.update_source_configuration"
        ) as update:
            manager._edit_source_fetch_settings(
                "example-rag",
                source,
            )
        update.assert_called_once()
        kwargs = update.call_args.kwargs
        self.assertEqual(
            {
                "gitlab_url",
                "project_url",
                "updated_within_days",
                "token_env",
            },
            set(kwargs["fetch"]),
        )
        self.assertEqual(gitlab_url, kwargs["fetch"]["gitlab_url"])
        self.assertEqual(project_url, kwargs["fetch"]["project_url"])
        self.assertEqual(90, kwargs["fetch"]["updated_within_days"])
        self.assertEqual(token_env, kwargs["fetch"]["token_env"])
        self.assertNotIn("pending_link", kwargs)
        manager._confirm_gitlab_project_connection.assert_not_called()
        rendered = "\n".join(self.output)
        self.assertIn("プロジェクトを変更できません", rendered)
        self.assertIn(f"GitLab本体URL: {gitlab_url}", rendered)
        self.assertIn(f"プロジェクトURL: {project_url}", rendered)
        self.assertNotIn("token_env", rendered)


if __name__ == "__main__":
    unittest.main()
