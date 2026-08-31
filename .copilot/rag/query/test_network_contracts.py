from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
REPO_ROOT = QUERY_ROOT.parents[2]
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.network import (
    CA_ENV_KEYS,
    PROXY_ENV_KEYS,
    ROUTE_TOKEN,
    NetworkConfigError,
    redact_proxy_url,
    redact_text,
    resolve_network_configuration,
)


class NetworkResolutionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.default_config = self.root / "network.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_no_network_json_preserves_direct_behavior_without_probe(self) -> None:
        probe = mock.Mock(return_value=True)
        result = self._resolve(probe=probe)
        self.assertEqual("direct", result.selected_route)
        probe.assert_not_called()

    def test_example_file_is_not_active_configuration(self) -> None:
        (self.root / "network.example.json").write_text(
            json.dumps(self._config()),
            encoding="utf-8",
        )
        result = self._resolve()
        self.assertFalse(result.details["config_file_found"])
        self.assertEqual("direct", result.selected_route)

    def test_mode_off_ignores_tool_proxy_and_ca(self) -> None:
        self._write_config(
            mode="off",
            proxy_url={"ignored": True},
            ca_bundle=["ignored"],
            no_proxy={"ignored": True},
        )
        result = self._resolve(probe=mock.Mock(side_effect=AssertionError))
        self.assertEqual("direct", result.selected_route)
        self.assertEqual("none", result.details["proxy_source"])
        self.assertFalse(result.details["ca_bundle_applied"])

    def test_auto_reachable_selects_proxy_once(self) -> None:
        self._write_config(mode="auto")
        probe = mock.Mock(return_value=True)
        result = self._resolve(probe=probe)
        self.assertEqual("proxy", result.selected_route)
        probe.assert_called_once_with("proxy.example", 8080, 1.0)

    def test_auto_reachable_with_local_mock_proxy_socket(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = int(listener.getsockname()[1])
            self._write_config(
                mode="auto",
                proxy_url=f"http://127.0.0.1:{port}",
            )
            result = self._resolve()
        self.assertEqual("proxy", result.selected_route)
        self.assertTrue(result.details["proxy_reachable"])

    def test_auto_unreachable_selects_direct_before_operation(self) -> None:
        self._write_config(mode="auto")
        events: list[str] = []

        def probe(host: str, port: int, timeout: float) -> bool:
            del host, port, timeout
            events.append("probe")
            return False

        result = self._resolve(probe=probe)
        events.append(f"operation:{result.selected_route}")
        self.assertEqual(["probe", "operation:direct"], events)
        self.assertIn(
            "proxy_config_unavailable_using_direct",
            result.warnings,
        )
        for key in PROXY_ENV_KEYS:
            self.assertNotIn(key, result.environment)

    def test_required_unreachable_fails_before_operation(self) -> None:
        self._write_config(mode="required")
        with self.assertRaises(NetworkConfigError) as raised:
            self._resolve(probe=mock.Mock(return_value=False))
        self.assertEqual("proxy_unavailable", raised.exception.kind)

    def test_tool_ca_is_route_scoped(self) -> None:
        ca = self.root / "company.pem"
        ca.write_text("not parsed during pure resolution\n", encoding="utf-8")
        self._write_config(mode="auto", ca_bundle=str(ca))
        direct = self._resolve(probe=mock.Mock(return_value=False))
        self.assertFalse(direct.details["ca_bundle_applied"])
        for key in CA_ENV_KEYS:
            self.assertNotEqual(str(ca), direct.environment.get(key))

        proxied = self._resolve(probe=mock.Mock(return_value=True))
        self.assertTrue(proxied.details["ca_bundle_applied"])
        for key in CA_ENV_KEYS:
            self.assertEqual(str(ca), proxied.environment[key])

    def test_cli_overrides_environment_and_tool_config(self) -> None:
        ca = self.root / "cli.pem"
        ca.write_text("cli\n", encoding="utf-8")
        self._write_config(proxy_url="http://config.example:9000")
        result = self._resolve(
            cli_proxy="http://cli.example:7000",
            cli_ca_bundle=str(ca),
            environ={"HTTPS_PROXY": "http://env.example:8000"},
        )
        self.assertEqual("cli", result.details["proxy_source"])
        self.assertEqual("http://cli.example:7000", result.environment["HTTPS_PROXY"])
        self.assertEqual("cli", result.details["ca_source"])

    def test_environment_overrides_tool_config_without_probe(self) -> None:
        self._write_config(proxy_url="http://config.example:9000")
        probe = mock.Mock(side_effect=AssertionError)
        result = self._resolve(
            environ={"HTTPS_PROXY": "http://env.example:8000"},
            probe=probe,
        )
        self.assertEqual("environment", result.details["proxy_source"])
        self.assertEqual("proxy", result.selected_route)
        probe.assert_not_called()

    def test_ignore_network_config_does_not_read_invalid_default(self) -> None:
        self.default_config.write_text("{", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError):
            result = self._resolve(ignore_network_config=True)
        self.assertEqual("direct", result.selected_route)

    def test_explicit_invalid_network_config_is_error(self) -> None:
        invalid = self.root / "explicit.json"
        invalid.write_text("{", encoding="utf-8")
        with self.assertRaises(NetworkConfigError) as raised:
            resolve_network_configuration(
                network_config=invalid,
                environ={},
            )
        self.assertEqual("invalid_network_config", raised.exception.kind)

    def test_rag_network_config_environment_selects_alternative_file(
        self,
    ) -> None:
        alternative = self.root / "alternative.json"
        alternative.write_text(
            json.dumps(self._config()),
            encoding="utf-8",
        )
        result = resolve_network_configuration(
            environ={"RAG_NETWORK_CONFIG": str(alternative)},
            probe=mock.Mock(return_value=True),
        )
        self.assertTrue(result.details["config_file_found"])
        self.assertEqual("tool_config", result.details["proxy_source"])

    def test_invalid_optional_default_warns_and_uses_direct(self) -> None:
        self.default_config.write_text("{", encoding="utf-8")
        result = self._resolve()
        self.assertEqual("direct", result.selected_route)
        self.assertEqual(
            ["invalid_optional_network_config"],
            result.warnings,
        )

    def test_invalid_required_default_configuration_fails(self) -> None:
        self.default_config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "mode": "required",
                    "proxy_url": "ftp://proxy.example",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(NetworkConfigError) as raised:
            self._resolve()
        self.assertEqual("invalid_network_config", raised.exception.kind)

    def test_no_proxy_merges_existing_cli_config_and_localhost(self) -> None:
        self._write_config(no_proxy=[".internal.example"])
        result = self._resolve(
            cli_no_proxy="api.example",
            environ={"NO_PROXY": "existing.example"},
            probe=mock.Mock(return_value=True),
        )
        values = set(result.environment["NO_PROXY"].split(","))
        self.assertTrue(
            {
                "existing.example",
                "api.example",
                ".internal.example",
                "localhost",
                "127.0.0.1",
                "::1",
            }
            <= values
        )

    def test_offline_resolution_does_not_probe_tool_config(self) -> None:
        self._write_config(mode="required")
        result = self._resolve(
            external_operation=False,
            probe=mock.Mock(side_effect=AssertionError),
        )
        self.assertEqual("not_required", result.selected_route)

    def test_resolved_child_marker_prevents_second_probe(self) -> None:
        self._write_config(mode="auto")
        first = self._resolve(probe=mock.Mock(return_value=True))
        route_token = "parent-child-route-token"
        first.environment[ROUTE_TOKEN] = route_token
        second_probe = mock.Mock(side_effect=AssertionError)
        second = resolve_network_configuration(
            environ=first.environment,
            probe=second_probe,
            inherited_route_token=route_token,
        )
        self.assertEqual("proxy", second.selected_route)
        second_probe.assert_not_called()

    def test_marker_cannot_override_explicit_cli_configuration(self) -> None:
        first = self._resolve(
            cli_proxy="http://old.example:8000",
        )
        first.environment[ROUTE_TOKEN] = "route-token"
        second = resolve_network_configuration(
            cli_proxy="http://new.example:9000",
            environ=first.environment,
            inherited_route_token="route-token",
            default_config_path=self.default_config,
        )
        self.assertEqual("cli", second.details["proxy_source"])
        self.assertEqual(
            "http://new.example:9000",
            second.environment["HTTPS_PROXY"],
        )

    def test_marker_requires_matching_parent_child_token(self) -> None:
        first = self._resolve(
            cli_proxy="http://proxy.example:8000",
        )
        first.environment[ROUTE_TOKEN] = "correct-token"
        with self.assertRaises(NetworkConfigError) as raised:
            resolve_network_configuration(
                environ=first.environment,
                inherited_route_token="wrong-token",
                default_config_path=self.default_config,
            )
        self.assertEqual(
            "invalid_resolved_network_marker",
            raised.exception.kind,
        )

    def test_proxy_credentials_are_redacted_and_rejected(self) -> None:
        raw = "http://user:secret@proxy.example:8080"
        self.assertEqual(
            "http://***:***@proxy.example:8080",
            redact_proxy_url(raw),
        )
        rendered = redact_text(
            f"failed {raw} password=secret token=abcd"
        )
        self.assertNotIn("user", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("abcd", rendered)

        with self.assertRaises(NetworkConfigError) as raised:
            self._resolve(cli_proxy=raw)
        self.assertEqual("invalid_proxy_config", raised.exception.kind)
        self.assertNotIn("user", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_invalid_proxy_scheme_is_rejected_without_echoing_credentials(
        self,
    ) -> None:
        raw = "ftp://user:secret@proxy.example:21"
        with self.assertRaises(NetworkConfigError) as raised:
            self._resolve(cli_proxy=raw)
        self.assertNotIn("secret", str(raised.exception))

    def _resolve(self, **kwargs: object):
        kwargs.setdefault("environ", {})
        kwargs.setdefault("default_config_path", self.default_config)
        return resolve_network_configuration(**kwargs)

    def _write_config(self, **overrides: object) -> None:
        payload = self._config()
        payload.update(overrides)
        self.default_config.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    @staticmethod
    def _config() -> dict[str, object]:
        return {
            "version": 1,
            "mode": "auto",
            "proxy_url": "http://proxy.example:8080",
            "ca_bundle": None,
            "no_proxy": [],
            "proxy_probe_timeout_seconds": 1.0,
        }


class InstallerAndRoutingContractTests(unittest.TestCase):
    def test_real_network_config_is_ignored_and_example_is_committed_source(
        self,
    ) -> None:
        ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".copilot/rag/config/network.json", ignore)
        self.assertTrue(
            (
                REPO_ROOT
                / ".copilot"
                / "rag"
                / "config"
                / "network.example.json"
            ).is_file()
        )

    def test_posix_installer_preserves_existing_network_json(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX installer test")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            payload_config = source / ".copilot" / "rag" / "config"
            payload_config.mkdir(parents=True)
            (payload_config / "network.example.json").write_text(
                '{"version":1}\n',
                encoding="utf-8",
            )
            (payload_config / "network.json").write_text(
                "payload-secret\n",
                encoding="utf-8",
            )
            target_config = target / "rag" / "config"
            target_config.mkdir(parents=True)
            actual = target_config / "network.json"
            actual.write_text("existing-secret\n", encoding="utf-8")
            installer = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
            script = source / "install.sh"
            script.write_text(installer, encoding="utf-8")
            script.chmod(0o755)
            completed = subprocess.run(
                [str(script)],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "COPILOT_HOME": str(target)},
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                "existing-secret\n",
                actual.read_text(encoding="utf-8"),
            )
            self.assertTrue(
                (target_config / "network.example.json").is_file()
            )

    def test_installer_sources_explicitly_exclude_real_network_json(self) -> None:
        shell = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("rag/config/network.json", shell)
        self.assertIn(r"rag\config\network.json", powershell)

    def test_copilot_slash_skill_is_manual_only(self) -> None:
        skill = (
            REPO_ROOT / ".copilot" / "skills" / "local-rag" / "SKILL.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(skill.split())
        self.assertIn("user-invocable: true", skill)
        self.assertIn("disable-model-invocation: true", skill)
        self.assertIn("explicitly invokes `/local-rag`", normalized)
        self.assertFalse(
            (
                REPO_ROOT
                / ".copilot"
                / "instructions"
                / "rag.instructions.md"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
