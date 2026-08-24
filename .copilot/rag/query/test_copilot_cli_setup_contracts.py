from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
TEMPLATE_ROOT = RAG_ROOT / "copilot-cli"
if str(QUERY_ROOT) not in sys.path:
    sys.path.insert(0, str(QUERY_ROOT))

SPEC = importlib.util.spec_from_file_location(
    "copilot_cli_setup_under_test", QUERY_ROOT / "copilot_cli_setup.py"
)
assert SPEC is not None and SPEC.loader is not None
setup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = setup
SPEC.loader.exec_module(setup)


class CopilotCliSetupContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home" / ".copilot"
        self.product = self.root / "product" / ".copilot"
        self.profile = (
            self.root
            / "Documents"
            / "PowerShell"
            / "Microsoft.PowerShell_profile.ps1"
        )
        self.home.mkdir(parents=True)
        python = (
            self.product
            / "rag"
            / "query"
            / ".venv"
            / "Scripts"
            / "python.exe"
        )
        python.parent.mkdir(parents=True)
        python.write_bytes(b"test-python")
        (self.product / "rag" / "query" / "mcp_server.py").write_text(
            "# test server\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _frontmatter(path: Path) -> dict[str, object]:
        text = path.read_text(encoding="utf-8")
        parts = text.split("---\n", 2)
        if len(parts) != 3 or parts[0]:
            raise AssertionError(f"missing frontmatter: {path}")
        result: dict[str, object] = {}
        for line in parts[1].splitlines():
            key, separator, raw = line.partition(":")
            if not separator:
                raise AssertionError(f"invalid frontmatter line: {line}")
            raw = raw.strip()
            if raw.startswith("["):
                result[key] = [
                    item.strip().strip("'").strip('"')
                    for item in raw[1:-1].split(",")
                ]
            elif raw in {"true", "false"}:
                result[key] = raw == "true"
            else:
                result[key] = raw
        return result

    def _owned_paths(self) -> list[Path]:
        return list(setup._artifact_paths(self.home, self.product).values()) + [
            setup._manifest_path(self.product)
        ]

    def test_agent_templates_have_exact_cli_identity_tools_models_and_caps(self) -> None:
        expected = {
            "local-rag-agent003-savings.agent.md": (
                "local-rag-agent003-savings",
                "claude-haiku-4.5",
                "at most two search calls",
                "at most one Evidence-detail call",
            ),
            "local-rag-agent003-standard.agent.md": (
                "local-rag-agent003-standard",
                "auto",
                "five total tool calls",
                "up to three needed returned Evidence IDs",
            ),
            "local-rag-agent003-thorough.agent.md": (
                "local-rag-agent003-thorough",
                "gpt-5.3-codex",
                "seven total tool calls",
                "groups of at most three",
            ),
        }
        exact_tools = [
            "localragagent003/local_rag_search",
            "localragagent003/local_rag_get_evidence",
        ]
        for filename, (agent_id, model, call_cap, evidence_cap) in expected.items():
            path = TEMPLATE_ROOT / filename
            header = self._frontmatter(path)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(filename.isascii())
            self.assertTrue(agent_id.isascii())
            self.assertEqual(agent_id, header["name"])
            self.assertEqual("github-copilot", header["target"])
            self.assertEqual(model, header["model"])
            self.assertEqual(exact_tools, header["tools"])
            self.assertNotIn("agents", header)
            self.assertIn(call_cap, text)
            self.assertIn(evidence_cap, text)
            self.assertIn("Never promote notices, related material", text)
            self.assertIn("unconfirmed Evidence", text)
            self.assertIn(
                "routing metadata explicitly labels them as `decoy`",
                text,
            )
            self.assertIn("if one candidate remains, treat it as the clear match", text)
            self.assertIn(
                "same turn with the unchanged question and exact returned database name",
                text,
            )
            self.assertIn("Routing metadata is never answer evidence", text)
            self.assertIn("strictly one at a time", text)
            self.assertIn("Wait for each tool result", text)
            self.assertIn("never issue tool calls in parallel", text)

    def test_install_pins_absolute_cli_config_and_preserves_foreign_jsonc(self) -> None:
        config = self.home / "mcp-config.json"
        original = (
            "{\n"
            "  // retained user setting\n"
            '  "theme": "dark",\n'
            '  "mcpServers": {\n'
            '    "foreign": {"type":"http","url":"https://example.invalid"},\n'
            "  },\n"
            "}\n"
        )
        config.write_text(original, encoding="utf-8")
        sentinels = {
            self.home / "config.json": b"config-sentinel",
            self.home / "permissions.json": b"permissions-sentinel",
            self.home / "session-state.json": b"session-sentinel",
            self.product / "rag" / "dbs" / "keep" / "db.json": b"db-sentinel",
        }
        for path, content in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        result = setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )

        self.assertEqual("installed", result["status"])
        current = config.read_text(encoding="utf-8")
        self.assertIn("// retained user setting", current)
        self.assertIn('"theme": "dark"', current)
        document, _ = setup.mcp_config._JsoncLexer(current).document()
        self.assertIn("foreign", document["mcpServers"])
        owned = document["mcpServers"][setup.mcp_config.SERVER_NAME]
        expected = setup.mcp_config.owned_cli_server_config(self.product)
        self.assertEqual(expected, owned)
        self.assertEqual("local", owned["type"])
        self.assertEqual(180000, owned["timeout"])
        self.assertEqual(
            ["local_rag_search", "local_rag_get_evidence"], owned["tools"]
        )
        self.assertTrue(Path(owned["command"]).is_absolute())
        self.assertTrue(Path(owned["args"][1]).is_absolute())
        self.assertTrue(Path(owned["args"][3]).is_absolute())

        pinned = json.loads(
            (self.product / setup.BUNDLE_NAME / setup.PINNED_CONFIG_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            {"mcpServers": {setup.mcp_config.SERVER_NAME: expected}}, pinned
        )
        manifest = json.loads(
            setup._manifest_path(self.product).read_text(encoding="utf-8")
        )
        self.assertTrue(manifest["config_existed_before_install"])
        self.assertEqual(5, len(manifest["artifacts"]))
        self.assertTrue(setup._temporary_path(self.product).is_dir())
        for path, content in sentinels.items():
            self.assertEqual(content, path.read_bytes())

    def test_repair_restores_owned_file_then_uninstall_preserves_foreign_state(self) -> None:
        config = self.home / "mcp-config.json"
        config.write_text(
            '{"unknown":{"keep":true},"mcpServers":{"foreign":{"type":"stdio","command":"x"}}}\n',
            encoding="utf-8",
        )
        sentinel = self.home / "config.json"
        sentinel.write_bytes(b"untouched")
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        agent = self.home / "agents" / "local-rag-agent003-standard.agent.md"
        agent.write_text("tampered", encoding="utf-8")

        result = setup.install_or_repair(
            "repair",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        self.assertEqual("repaired", result["status"])
        self.assertEqual(
            (TEMPLATE_ROOT / agent.name).read_bytes(), agent.read_bytes()
        )

        result = setup.uninstall(
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        self.assertEqual("uninstalled", result["status"])
        self.assertTrue(config.is_file())
        document, _ = setup.mcp_config._JsoncLexer(
            config.read_text(encoding="utf-8")
        ).document()
        self.assertEqual({"keep": True}, document["unknown"])
        self.assertEqual(
            {"foreign": {"type": "stdio", "command": "x"}},
            document["mcpServers"],
        )
        self.assertEqual(b"untouched", sentinel.read_bytes())
        for path in self._owned_paths():
            self.assertFalse(path.exists(), path)

    def test_round_trip_removes_config_created_by_setup(self) -> None:
        config = self.home / "mcp-config.json"
        self.assertFalse(config.exists())
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        self.assertTrue(config.is_file())
        setup.uninstall(
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        self.assertFalse(config.exists())

    def test_same_name_foreign_collision_and_malformed_jsonc_are_noops(self) -> None:
        cases = (
            '{"mcpServers":{"localragagent003":{"type":"http","url":"https://foreign.invalid"}}}\n',
            '{"mcpServers": /* unterminated\n',
        )
        for index, original in enumerate(cases):
            with self.subTest(index=index):
                home = self.root / f"collision-{index}"
                home.mkdir()
                config = home / "mcp-config.json"
                config.write_text(original, encoding="utf-8")
                profile = self.root / f"profile-{index}.ps1"
                with self.assertRaises(ValueError):
                    setup.install_or_repair(
                        "install",
                        home,
                        install_root=self.product,
                        profile_path=profile,
                    )
                self.assertEqual(original, config.read_text(encoding="utf-8"))
                self.assertFalse(setup._manifest_path(self.product).exists())
                for path in setup._artifact_paths(home, self.product).values():
                    self.assertFalse(path.exists(), path)

    def test_unowned_artifact_collision_does_not_claim_path(self) -> None:
        target = self.home / "agents" / "local-rag-agent003-savings.agent.md"
        target.parent.mkdir()
        target.write_bytes(b"foreign")
        with self.assertRaises(setup.OwnedArtifactCollisionError):
            setup.install_or_repair(
                "install",
                self.home,
                install_root=self.product,
                profile_path=self.profile,
            )
        self.assertEqual(b"foreign", target.read_bytes())
        self.assertFalse((self.home / "mcp-config.json").exists())

    def test_artifact_write_failure_rolls_back_config_and_files(self) -> None:
        config = self.home / "mcp-config.json"
        original = b'{"foreign":true}\n'
        config.write_bytes(original)
        original_writer = setup._atomic_write_owned_bytes
        failed = False

        def fail_once(path: Path, content: bytes, *, boundary: Path) -> None:
            nonlocal failed
            if path.name == "local-rag-agent003-standard.agent.md" and not failed:
                failed = True
                raise OSError("injected artifact failure")
            original_writer(path, content, boundary=boundary)

        with mock.patch.object(
            setup, "_atomic_write_owned_bytes", side_effect=fail_once
        ):
            with self.assertRaises(OSError):
                setup.install_or_repair(
                    "install",
                    self.home,
                    install_root=self.product,
                    profile_path=self.profile,
                )
        self.assertTrue(failed)
        self.assertEqual(original, config.read_bytes())
        for path in self._owned_paths():
            self.assertFalse(path.exists(), path)

    def test_install_and_repair_safely_create_expected_temporary_directory(self) -> None:
        temporary = setup._temporary_path(self.product)
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        self.assertTrue(temporary.is_dir())
        temporary.rmdir()

        result = setup.install_or_repair(
            "repair",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )

        self.assertEqual("repaired", result["status"])
        self.assertTrue(temporary.is_dir())

    def test_existing_temporary_directory_and_foreign_content_are_preserved(self) -> None:
        temporary = setup._temporary_path(self.product)
        temporary.mkdir(parents=True)
        sentinel = temporary / "foreign.keep"
        sentinel.write_bytes(b"foreign-temporary-content")

        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )

        self.assertEqual(b"foreign-temporary-content", sentinel.read_bytes())

    def test_temporary_file_or_reparse_chain_is_rejected_before_writes(self) -> None:
        temporary = setup._temporary_path(self.product)
        temporary.parent.mkdir(parents=True)
        temporary.write_bytes(b"foreign-file")
        with self.assertRaisesRegex(
            setup.OwnedArtifactCollisionError,
            "expected temporary path is not a regular directory",
        ):
            setup.install_or_repair(
                "install",
                self.home,
                install_root=self.product,
                profile_path=self.profile,
            )
        self.assertEqual(b"foreign-file", temporary.read_bytes())
        self.assertFalse((self.home / "mcp-config.json").exists())
        for path in self._owned_paths():
            self.assertFalse(path.exists(), path)
        temporary.unlink()

        original_reparse_check = setup.mcp_config._path_has_reparse

        def report_temporary_reparse(path: Path, boundary: Path) -> bool:
            if setup._absolute(path) == temporary:
                return True
            return original_reparse_check(path, boundary)

        with mock.patch.object(
            setup.mcp_config,
            "_path_has_reparse",
            side_effect=report_temporary_reparse,
        ):
            with self.assertRaisesRegex(
                setup.OwnedArtifactCollisionError,
                "expected temporary directory crosses a reparse point",
            ):
                setup.install_or_repair(
                    "install",
                    self.home,
                    install_root=self.product,
                    profile_path=self.profile,
                )
        self.assertFalse((self.home / "mcp-config.json").exists())
        for path in self._owned_paths():
            self.assertFalse(path.exists(), path)

    def test_temporary_directories_created_by_failed_transaction_are_removed(self) -> None:
        config = self.home / "mcp-config.json"
        original = b'{"foreign":true}\n'
        config.write_bytes(original)
        temporary = setup._temporary_path(self.product)
        original_writer = setup._write_and_readback

        def fail_manifest(path: Path, content: bytes, *, boundary: Path) -> None:
            if path == setup._manifest_path(self.product):
                raise OSError("injected manifest failure after temporary creation")
            original_writer(path, content, boundary=boundary)

        with mock.patch.object(
            setup,
            "_write_and_readback",
            side_effect=fail_manifest,
        ):
            with self.assertRaises(OSError):
                setup.install_or_repair(
                    "install",
                    self.home,
                    install_root=self.product,
                    profile_path=self.profile,
                )

        self.assertEqual(original, config.read_bytes())
        self.assertFalse(temporary.exists())
        self.assertFalse(temporary.parent.exists())
        for path in self._owned_paths():
            self.assertFalse(path.exists(), path)

    def test_tampered_manifest_or_artifact_blocks_install_and_uninstall(self) -> None:
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        launcher = self.product / setup.BUNDLE_NAME / setup.LAUNCHER_NAME
        launcher.write_bytes(launcher.read_bytes() + b"\n# tampered\n")
        with self.assertRaises(setup.OwnedArtifactCollisionError):
            setup.install_or_repair(
                "install",
                self.home,
                install_root=self.product,
                profile_path=self.profile,
            )
        with self.assertRaises(setup.OwnedArtifactCollisionError):
            setup.uninstall(
                self.home,
                install_root=self.product,
                profile_path=self.profile,
            )
        self.assertTrue(launcher.is_file())
        self.assertTrue(setup._manifest_path(self.product).is_file())

    def test_default_copilot_home_uses_env_once_without_double_nesting(self) -> None:
        explicit = self.root / "explicit" / ".copilot"
        user = self.root / "user"
        self.assertEqual(
            explicit,
            setup.default_copilot_home(
                {"COPILOT_HOME": str(explicit), "USERPROFILE": str(user)}
            ),
        )
        self.assertEqual(
            user / ".copilot",
            setup.default_copilot_home(
                {"COPILOT_HOME": "  ", "USERPROFILE": str(user)}
            ),
        )
        with self.assertRaises(setup.CopilotCliSetupError):
            setup.default_copilot_home(
                {"COPILOT_HOME": "relative", "USERPROFILE": str(user)}
            )

    def test_profile_marker_round_trip_preserves_bom_newline_and_foreign_body(self) -> None:
        original = b"\xef\xbb\xbf# foreign\r\n$global:Keep = 'yes'"
        self.profile.parent.mkdir(parents=True)
        self.profile.write_bytes(original)
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        installed = self.profile.read_bytes()
        self.assertTrue(installed.startswith(original + b"\r\n"))
        text = installed.decode("utf-8-sig")
        self.assertEqual(1, text.count(setup.PROFILE_START))
        self.assertEqual(1, text.count(setup.PROFILE_END))
        self.assertIn("function global:local-rag-copilot", text)
        self.assertIn(str(setup._launcher_path(self.product)), text)
        self.assertIn("[string]$Tier = 'standard'", text)

        setup.uninstall(
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        self.assertEqual(original, self.profile.read_bytes())

    def test_foreign_profile_marker_collision_is_a_full_noop(self) -> None:
        original = (
            setup.PROFILE_START
            + "\nfunction global:foreign-owner {}\n"
            + setup.PROFILE_END
            + "\n"
        ).encode("utf-8")
        self.profile.parent.mkdir(parents=True)
        self.profile.write_bytes(original)
        with self.assertRaises(setup.OwnedArtifactCollisionError):
            setup.install_or_repair(
                "install",
                self.home,
                install_root=self.product,
                profile_path=self.profile,
            )
        self.assertEqual(original, self.profile.read_bytes())
        self.assertFalse((self.home / "mcp-config.json").exists())
        for path in self._owned_paths():
            self.assertFalse(path.exists(), path)

    def test_preflight_collision_does_not_create_missing_copilot_home(self) -> None:
        absent_home = self.root / "fresh-home" / ".copilot"
        original = (
            setup.PROFILE_START
            + "\nfunction global:foreign-owner {}\n"
            + setup.PROFILE_END
            + "\n"
        ).encode("utf-8")
        self.profile.parent.mkdir(parents=True)
        self.profile.write_bytes(original)
        with self.assertRaises(setup.OwnedArtifactCollisionError):
            setup.install_or_repair(
                "install",
                absent_home,
                install_root=self.product,
                profile_path=self.profile,
            )
        self.assertFalse(absent_home.exists())
        self.assertEqual(original, self.profile.read_bytes())

    def test_existing_owned_install_updates_desired_version_after_old_hash_check(self) -> None:
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        original_reader = setup._template_bytes
        target_name = "local-rag-agent003-standard.agent.md"
        updated = original_reader(target_name) + b"\n# setup-version-2\n"

        def version_two(name: str) -> bytes:
            return updated if name == target_name else original_reader(name)

        with mock.patch.object(setup, "_template_bytes", side_effect=version_two):
            result = setup.install_or_repair(
                "install",
                self.home,
                install_root=self.product,
                profile_path=self.profile,
            )
        self.assertEqual("updated", result["status"])
        self.assertEqual(
            updated, (self.home / "agents" / target_name).read_bytes()
        )
        manifest = json.loads(
            setup._manifest_path(self.product).read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in manifest["artifacts"]
            if item["path"] == f"agents/{target_name}"
        )
        self.assertEqual(setup._sha256(updated), entry["sha256"])

    def test_repair_restores_missing_agent_and_modified_owned_profile_block(self) -> None:
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        agent = self.home / "agents" / "local-rag-agent003-savings.agent.md"
        agent.unlink()
        profile_text = self.profile.read_text(encoding="utf-8")
        self.profile.write_text(
            profile_text.replace("function global:local-rag-copilot", "function global:tampered"),
            encoding="utf-8",
        )
        result = setup.install_or_repair(
            "repair",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        self.assertEqual("repaired", result["status"])
        self.assertEqual((TEMPLATE_ROOT / agent.name).read_bytes(), agent.read_bytes())
        repaired = self.profile.read_text(encoding="utf-8")
        self.assertIn("function global:local-rag-copilot", repaired)
        self.assertNotIn("function global:tampered", repaired)

    def test_optional_vscode_target_round_trip_preserves_both_foreign_configs(self) -> None:
        cli = self.home / "mcp-config.json"
        cli.write_text(
            '{"mcpServers":{"foreignCli":{"type":"local","command":"x"}}}\n',
            encoding="utf-8",
        )
        vscode = self.root / "Code" / "User" / "mcp.json"
        vscode.parent.mkdir(parents=True)
        vscode.write_text(
            '{\n // vscode foreign\n "servers":{"foreignVs":{"type":"stdio","command":"y"},},\n "keep":7,\n}\n',
            encoding="utf-8",
        )
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
            vscode_mcp_config=vscode,
        )
        cli_doc, _ = setup.mcp_config._JsoncLexer(
            cli.read_text(encoding="utf-8")
        ).document()
        vscode_doc, _ = setup.mcp_config._JsoncLexer(
            vscode.read_text(encoding="utf-8")
        ).document()
        self.assertIn("foreignCli", cli_doc["mcpServers"])
        self.assertIn(setup.mcp_config.SERVER_NAME, cli_doc["mcpServers"])
        self.assertIn("foreignVs", vscode_doc["servers"])
        self.assertIn(setup.mcp_config.SERVER_NAME, vscode_doc["servers"])
        self.assertEqual(7, vscode_doc["keep"])

        setup.uninstall(
            self.home,
            install_root=self.product,
            profile_path=self.profile,
            vscode_mcp_config=vscode,
        )
        cli_doc, _ = setup.mcp_config._JsoncLexer(
            cli.read_text(encoding="utf-8")
        ).document()
        vscode_text = vscode.read_text(encoding="utf-8")
        vscode_doc, _ = setup.mcp_config._JsoncLexer(vscode_text).document()
        self.assertEqual({"foreignCli"}, set(cli_doc["mcpServers"]))
        self.assertEqual({"foreignVs"}, set(vscode_doc["servers"]))
        self.assertIn("// vscode foreign", vscode_text)
        self.assertEqual(7, vscode_doc["keep"])

    def test_vscode_collision_rolls_back_cli_profile_and_artifacts(self) -> None:
        cli = self.home / "mcp-config.json"
        cli_original = b'{"foreign":true}\n'
        cli.write_bytes(cli_original)
        vscode = self.root / "vscode-mcp.json"
        vscode_original = (
            '{"servers":{"localragagent003":{"type":"http","url":"https://foreign.invalid"}}}\n'
        ).encode("utf-8")
        vscode.write_bytes(vscode_original)
        with self.assertRaises(setup.mcp_config.McpConfigCollisionError):
            setup.install_or_repair(
                "install",
                self.home,
                install_root=self.product,
                profile_path=self.profile,
                vscode_mcp_config=vscode,
            )
        self.assertEqual(cli_original, cli.read_bytes())
        self.assertEqual(vscode_original, vscode.read_bytes())
        self.assertFalse(self.profile.exists())
        for path in self._owned_paths():
            self.assertFalse(path.exists(), path)

    def test_launcher_executes_fixed_default_contract_and_rejects_nested_shadow(self) -> None:
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("PowerShell is unavailable")
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        launcher = self.product / setup.BUNDLE_NAME / setup.LAUNCHER_NAME
        temporary = setup._temporary_path(self.product)
        self.assertTrue(temporary.is_dir())
        temporary.rmdir()
        project = self.root / "project"
        nested = project / "one" / "two"
        nested.mkdir(parents=True)
        (project / ".git").mkdir()
        binary_dir = self.root / "global-bin"
        binary_dir.mkdir()
        capture = self.root / "copilot-capture.txt"
        fake = binary_dir / "copilot.cmd"
        fake.write_text(
            "@echo off\r\n"
            "> \"%LRR_CAPTURE%\" echo ARGS=%*\r\n"
            ">> \"%LRR_CAPTURE%\" echo HOME=%COPILOT_HOME%\r\n"
            ">> \"%LRR_CAPTURE%\" echo LARGE=%COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES%\r\n"
            ">> \"%LRR_CAPTURE%\" echo CACHE=%COPILOT_MCP_TOOL_CACHE%\r\n"
            ">> \"%LRR_CAPTURE%\" echo CWD=%CD%\r\n"
            "exit /b 0\r\n",
            encoding="ascii",
        )
        environment = os.environ.copy()
        environment["PATH"] = str(binary_dir) + os.pathsep + environment["PATH"]
        environment["LRR_CAPTURE"] = str(capture)
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "--max-ai-credits",
                "30",
            ],
            cwd=nested,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        captured = capture.read_text(encoding="utf-8")
        self.assertIn("--agent=local-rag-agent003-standard", captured)
        self.assertIn("--model=auto", captured)
        self.assertIn("--additional-mcp-config=@", captured)
        self.assertIn("--available-tools=localragagent003-local_rag_search,localragagent003-local_rag_get_evidence", captured)
        self.assertIn("--allow-tool=localragagent003(local_rag_search),localragagent003(local_rag_get_evidence)", captured)
        self.assertIn("--no-custom-instructions", captured)
        self.assertIn("--max-ai-credits 30", captured)
        self.assertIn(f"HOME={self.home}", captured)
        self.assertIn("LARGE=1310720", captured)
        self.assertIn("CACHE=false", captured)
        self.assertIn(f"CWD={nested}", captured)
        self.assertTrue(temporary.is_dir())

        sentinel = temporary / "foreign.keep"
        sentinel.write_bytes(b"preserve-on-launch")
        capture.unlink()
        quoted_launcher = str(launcher).replace("'", "''")
        diagnostic_command = (
            "$ErrorActionPreference='Stop';"
            f"try {{ & '{quoted_launcher}' }} catch {{ "
            "[Console]::Error.WriteLine($_.Exception.Message);"
            "[Console]::Error.WriteLine($_.ScriptStackTrace);exit 1 }"
        )
        reused = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                diagnostic_command,
            ],
            cwd=nested,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, reused.returncode, reused.stdout + reused.stderr)
        self.assertEqual(b"preserve-on-launch", sentinel.read_bytes())
        capture.unlink()
        for reserved_argument in ("--session-id=foreign", "-rforeign"):
            rejected_reserved = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(launcher),
                    reserved_argument,
                ],
                cwd=nested,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(0, rejected_reserved.returncode)
            self.assertIn(
                "Reserved Copilot CLI",
                rejected_reserved.stdout + rejected_reserved.stderr,
            )
            self.assertFalse(capture.exists())

        shadow = (
            project
            / "one"
            / ".github"
            / "agents"
            / "local-rag-agent003-standard.md"
        )
        shadow.parent.mkdir(parents=True)
        shadow.write_text("foreign shadow", encoding="utf-8")
        quoted_launcher = str(launcher).replace("'", "''")
        shadow_command = (
            "$ErrorActionPreference='Stop';"
            f"try {{ & '{quoted_launcher}' }} catch {{ "
            "[Console]::Error.WriteLine($_.Exception.Message);"
            "[Console]::Error.WriteLine($_.ScriptStackTrace);exit 1 }"
        )
        rejected = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                shadow_command,
            ],
            cwd=nested,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn(
            "project-shadowed Agent definition", rejected.stdout + rejected.stderr
        )
        self.assertFalse(capture.exists())

        shadow.unlink()
        sentinel.unlink()
        temporary.rmdir()
        temporary.write_bytes(b"foreign-file")
        rejected_file = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                diagnostic_command,
            ],
            cwd=nested,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(0, rejected_file.returncode)
        self.assertIn(
            "Expected temporary path is not a directory",
            rejected_file.stdout + rejected_file.stderr,
        )
        self.assertEqual(b"foreign-file", temporary.read_bytes())
        self.assertFalse(capture.exists())
        temporary.unlink()

        if os.name == "nt":
            reparse_target = self.root / "foreign-reparse-target"
            reparse_target.mkdir()
            junction = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(temporary),
                    str(reparse_target),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(0, junction.returncode, junction.stdout + junction.stderr)
            rejected_reparse = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    diagnostic_command,
                ],
                cwd=nested,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(0, rejected_reparse.returncode)
            self.assertIn(
                "Expected temporary path crosses a reparse point",
                rejected_reparse.stdout + rejected_reparse.stderr,
            )
            self.assertFalse(capture.exists())
            os.rmdir(temporary)

    def test_launcher_fails_closed_for_project_jsonc_shadows_and_uses_git_boundary(
        self,
    ) -> None:
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("PowerShell is unavailable")
        setup.install_or_repair(
            "install",
            self.home,
            install_root=self.product,
            profile_path=self.profile,
        )
        launcher = self.product / setup.BUNDLE_NAME / setup.LAUNCHER_NAME
        binary_dir = self.root / "global-bin"
        binary_dir.mkdir()
        capture = self.root / "semantic-shadow-capture.txt"
        fake = binary_dir / "copilot.cmd"
        fake.write_text(
            "@echo off\r\n"
            "> \"%LRR_CAPTURE%\" echo ARGS=%*\r\n"
            "exit /b 0\r\n",
            encoding="ascii",
        )
        environment = os.environ.copy()
        environment["PATH"] = str(binary_dir) + os.pathsep + environment["PATH"]
        environment["LRR_CAPTURE"] = str(capture)

        def invoke(directory: Path, *, custom_environment: dict[str, str] | None = None):
            capture.unlink(missing_ok=True)
            return subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(launcher),
                ],
                cwd=directory,
                env=custom_environment or environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        non_git = self.root / "non-git"
        non_git.mkdir()
        agent_shadow = (
            non_git
            / ".github"
            / "agents"
            / "local-rag-agent003-standard.md"
        )
        agent_shadow.parent.mkdir(parents=True)
        agent_shadow.write_text("foreign", encoding="utf-8")
        rejected_agent = invoke(non_git)
        self.assertNotEqual(0, rejected_agent.returncode)
        self.assertIn(
            "project-shadowed Agent definition",
            rejected_agent.stdout + rejected_agent.stderr,
        )
        self.assertFalse(capture.exists())
        agent_shadow.unlink()

        local_binary_dir = non_git / "bin"
        local_binary_dir.mkdir()
        shutil.copyfile(fake, local_binary_dir / "copilot.cmd")
        local_environment = environment.copy()
        local_environment["PATH"] = (
            str(local_binary_dir) + os.pathsep + environment["PATH"]
        )
        rejected_executable = invoke(
            non_git,
            custom_environment=local_environment,
        )
        self.assertNotEqual(0, rejected_executable.returncode)
        self.assertIn(
            "project-shadowed Copilot CLI executable",
            rejected_executable.stdout + rejected_executable.stderr,
        )
        self.assertFalse(capture.exists())

        escaped_server = "LOCALRAGAGENT\\u0030\\u0030\\u0033"
        shadow_documents = (
            (
                ".copilot/mcp-config.json",
                f'{{ // comment\n "mcpServers": {{ "{escaped_server}": {{"command":"x"}}, }}, }}',
            ),
            (
                ".vscode/mcp.json",
                f'{{ /* comment */ "servers": {{ "{escaped_server}": {{"command":"x"}}, }}, }}',
            ),
            (
                ".mcp.json",
                f'{{ "{escaped_server}": {{"command":"x"}}, }}',
            ),
            (
                ".github/mcp.json",
                f'{{ "servers": {{ "{escaped_server}": {{"command":"x"}}, }}, }}',
            ),
        )
        for relative, document in shadow_documents:
            with self.subTest(relative=relative):
                config = non_git / Path(relative)
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(document, encoding="utf-8")
                rejected_config = invoke(non_git)
                self.assertNotEqual(0, rejected_config.returncode)
                self.assertIn(
                    "project-shadowed MCP server",
                    rejected_config.stdout + rejected_config.stderr,
                )
                self.assertFalse(capture.exists())
                config.unlink()

        unrelated = non_git / ".mcp.json"
        unrelated.write_text(
            "{\n"
            "  // Markers and a server-looking fragment inside a string are data.\n"
            '  "foreign": {"command": "literal // /* \\"localragagent003\\": text"},\n'
            "}\n",
            encoding="utf-8",
        )
        accepted_unrelated = invoke(non_git)
        self.assertEqual(
            0,
            accepted_unrelated.returncode,
            accepted_unrelated.stdout + accepted_unrelated.stderr,
        )
        self.assertTrue(capture.exists())
        unrelated.unlink()

        malformed = non_git / ".mcp.json"
        malformed.write_text('{"foreign": /* unterminated', encoding="utf-8")
        rejected_malformed = invoke(non_git)
        self.assertNotEqual(0, rejected_malformed.returncode)
        self.assertIn(
            "Invalid project MCP config",
            rejected_malformed.stdout + rejected_malformed.stderr,
        )
        self.assertFalse(capture.exists())
        malformed.unlink()

        wrong_schema = non_git / ".vscode" / "mcp.json"
        wrong_schema.parent.mkdir(parents=True, exist_ok=True)
        wrong_schema.write_text('{"servers":[]}', encoding="utf-8")
        rejected_schema = invoke(non_git)
        self.assertNotEqual(0, rejected_schema.returncode)
        self.assertIn(
            "Invalid project MCP config",
            rejected_schema.stdout + rejected_schema.stderr,
        )
        self.assertFalse(capture.exists())
        wrong_schema.unlink()

        outer = self.root / "outer"
        repository = outer / "repository"
        nested = repository / "one" / "two"
        nested.mkdir(parents=True)
        (repository / ".git").mkdir()
        outer_shadow = outer / ".mcp.json"
        outer_shadow.write_text(
            f'{{"{escaped_server}":{{"command":"x"}}}}',
            encoding="utf-8",
        )
        accepted_above_boundary = invoke(nested)
        self.assertEqual(
            0,
            accepted_above_boundary.returncode,
            accepted_above_boundary.stdout + accepted_above_boundary.stderr,
        )
        self.assertTrue(capture.exists())

        ancestor_shadow = repository / ".github" / "mcp.json"
        ancestor_shadow.parent.mkdir(parents=True)
        ancestor_shadow.write_text(
            f'{{"mcpServers":{{"{escaped_server}":{{"command":"x"}}}}}}',
            encoding="utf-8",
        )
        rejected_ancestor = invoke(nested)
        self.assertNotEqual(0, rejected_ancestor.returncode)
        self.assertIn(
            "project-shadowed MCP server",
            rejected_ancestor.stdout + rejected_ancestor.stderr,
        )
        self.assertFalse(capture.exists())

    def test_launcher_contract_and_powershell_syntax(self) -> None:
        launcher = TEMPLATE_ROOT / setup.LAUNCHER_NAME
        text = launcher.read_text(encoding="utf-8")
        for tier, agent, model in (
            ("savings", "local-rag-agent003-savings", "claude-haiku-4.5"),
            ("standard", "local-rag-agent003-standard", "auto"),
            ("thorough", "local-rag-agent003-thorough", "gpt-5.3-codex"),
        ):
            self.assertIn(tier, text)
            self.assertIn(agent, text)
            self.assertIn(model, text)
        self.assertIn('"--additional-mcp-config=@$PinnedConfig"', text)
        self.assertIn('"--available-tools=$ToolList"', text)
        self.assertIn('"--allow-tool=$AllowList"', text)
        self.assertIn('"--no-custom-instructions"', text)
        self.assertIn("localragagent003-local_rag_search", text)
        self.assertIn("localragagent003-local_rag_get_evidence", text)
        self.assertIn("localragagent003(local_rag_search)", text)
        self.assertIn("localragagent003(local_rag_get_evidence)", text)
        self.assertIn('"COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES", "1310720"', text)
        self.assertIn('"COPILOT_MCP_TOOL_CACHE", "false"', text)
        self.assertIn('Get-Command "copilot" -CommandType Application', text)
        self.assertIn("Refusing a project-shadowed Copilot CLI executable", text)
        self.assertIn("Refusing a project-shadowed Agent definition", text)
        self.assertIn("Refusing a project-shadowed MCP server", text)
        self.assertIn("Invalid project MCP config", text)
        self.assertIn("Project shadow path crosses a reparse point", text)
        self.assertIn('".github/agents/$AgentId.md"', text)
        self.assertIn('".claude/agents/$AgentId.md"', text)
        self.assertIn('".github/mcp.json"', text)
        self.assertIn("function ConvertFrom-ProjectJsonc", text)
        self.assertIn("[System.Text.UTF8Encoding]::new($false, $true)", text)
        self.assertNotIn("[System.Text.RegularExpressions.Regex]::IsMatch", text)
        self.assertIn("-StartDirectory ([System.Environment]::CurrentDirectory)", text)
        self.assertIn('"--python", $ExpectedPython', text)
        self.assertIn('"--spool-root", $ExpectedSpool', text)
        self.assertIn("$server.env.TEMP", text)
        self.assertIn("$server.env.TMP", text)
        self.assertIn("function Ensure-SafeDirectory", text)
        self.assertIn("[System.IO.Directory]::CreateDirectory", text)
        self.assertIn("Expected temporary path crosses a reparse point", text)
        self.assertIn(
            "Ensure-SafeDirectory -Path $ExpectedTemporary -Boundary $InstallRoot",
            text,
        )
        self.assertIn("Reserved Copilot CLI flag", text)
        self.assertIn('"--resume", "-r"', text)
        self.assertIn('"--continue"', text)
        for reserved in (
            '"--session-id"',
            '"--yolo"',
            '"--acp"',
            '"--attachment"',
            '"--autopilot"',
            '"--enable-memory"',
            '"--remote", "--remote-export"',
            '"--share", "--share-gist"',
            '"--connect"',
            '"--plugin-dir"',
            '"--extension-sdk-path"',
            '"--add-github-mcp-tool"',
            '"--allow-all-mcp-server-instructions"',
            '"--disable-builtin-mcps"',
            '"--worktree", "-w"',
        ):
            self.assertIn(reserved, text)
        self.assertIn("Reserved Copilot CLI short flag", text)
        self.assertIn("Get-Sha256", text)
        self.assertIn("Owned artifact hash mismatch", text)
        self.assertIn("@CopilotArguments", text)

        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("PowerShell parser is unavailable")
        quoted = str(launcher).replace("'", "''")
        command = (
            "$e=$null;$t=$null;"
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{quoted}',[ref]$t,[ref]$e);"
            "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
