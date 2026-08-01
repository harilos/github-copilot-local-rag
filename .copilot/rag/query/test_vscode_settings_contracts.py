from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vscode_settings import (
    configure_vscode,
    candidate_settings,
    patch_settings,
    scoped_command_rules,
)


class VSCodeSettingsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(r"C:\Profile Root\Example User\.copilot")
        self.rules = scoped_command_rules(self.home)

    def test_rules_are_scoped_to_public_read_only_scripts(self) -> None:
        rendered = "\n".join(self.rules)
        self.assertIn(r"list_dbs\.py", rendered)
        self.assertIn(r"search\.py", rendered)
        self.assertTrue(all(rule.startswith("/^") and rule.endswith("$/") for rule in self.rules))
        for forbidden in (
            "setup.py",
            "manage.py",
            " -c ",
            " -m ",
            "cmd /c",
            "powershell",
            "pwsh",
        ):
            self.assertNotIn(forbidden, rendered.casefold())
        self.assertNotIn(r"query\\list_dbs\.py", rendered)
        self.assertNotIn(r"query\\search\.py", rendered)

    def test_jsonc_comments_crlf_and_unrelated_values_are_preserved(self) -> None:
        original = (
            "{\r\n"
            "  // keep this comment\r\n"
            "  \"editor.fontSize\": 15,\r\n"
            "}\r\n"
        )
        patched = patch_settings(original, self.rules)
        self.assertIn("// keep this comment\r\n", patched)
        self.assertIn('"editor.fontSize": 15', patched)
        self.assertIn(r"list_dbs\\.py", patched)
        self.assertIn(
            '{"approve":true,"matchCommandLine":true}',
            patched,
        )
        self.assertEqual(patched, patch_settings(patched, self.rules))

    def test_inline_comment_receives_comma_after_value_token(self) -> None:
        original = (
            "{\n"
            '  "editor.fontSize": 15 // keep\n'
            "}\n"
        )
        patched = patch_settings(original, self.rules)
        self.assertIn('"editor.fontSize": 15, // keep\n', patched)
        self.assertNotIn("// keep,", patched)
        self.assertEqual(patched, patch_settings(patched, self.rules))

    def test_explicit_false_is_preserved(self) -> None:
        original = (
            '{\n  "chat.tools.terminal.enableAutoApprove": false,\n'
            '  "chat.tools.terminal.autoApprove": {}\n}\n'
        )
        patched = patch_settings(original, self.rules)
        self.assertIn('"chat.tools.terminal.enableAutoApprove": false', patched)
        self.assertNotIn("list_dbs.py", patched)
        self.assertEqual(original, patched)

    def test_duplicate_target_nonobject_and_malformed_fail_closed(self) -> None:
        duplicate = (
            '{"chat.tools.terminal.autoApprove": {}, '
            '"chat.tools.terminal.autoApprove": {}}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            patch_settings(duplicate, self.rules)
        with self.assertRaisesRegex(ValueError, "not an object"):
            patch_settings(
                '{"chat.tools.terminal.autoApprove": true}', self.rules
            )
        with self.assertRaises(ValueError):
            patch_settings("{/* unterminated", self.rules)

    def test_candidate_paths_do_not_create_absent_insiders_or_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            self.assertEqual([], candidate_settings(appdata))
            stable = appdata / "Code" / "User"
            profile = stable / "profiles" / "one"
            profile.mkdir(parents=True)
            (profile / "settings.json").write_text("{}", encoding="utf-8")
            paths = candidate_settings(appdata)
            self.assertIn(stable / "settings.json", paths)
            self.assertIn(profile / "settings.json", paths)
            self.assertFalse(any("Insiders" in str(path) for path in paths))

    def test_configuration_does_not_create_vscode_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            result = configure_vscode(self.home, appdata)
            self.assertEqual("not_detected", result["status"])
            self.assertEqual(0, result["targets_checked"])
            self.assertFalse((appdata / "Code").exists())
            self.assertFalse((appdata / "Code - Insiders").exists())

    def test_existing_command_deny_is_preserved(self) -> None:
        denied = self.rules[0].replace("\\", "\\\\").replace('"', '\\"')
        original = (
            '{\n  "chat.tools.terminal.enableAutoApprove": true,\n'
            '  "chat.tools.terminal.autoApprove": {\n'
            f'    "{denied}": false\n'
            "  }\n}\n"
        )
        patched = patch_settings(original, self.rules)
        self.assertIn(f'"{denied}": false', patched)
        self.assertNotIn(f'"{denied}": true', patched)
        self.assertIn(r"search\\.py", patched)


    def test_global_false_is_manual_and_second_run_is_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = appdata / "Code" / "User" / "settings.json"
            path.parent.mkdir(parents=True)
            original = b'{"chat.tools.terminal.enableAutoApprove":false}\n'
            path.write_bytes(original)
            first = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", first["status"])
            self.assertEqual(0, first["targets_changed"])
            before_mtime = path.stat().st_mtime_ns
            before_backups = list(path.parent.glob("*.local-rag-backup-*"))
            second = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", second["status"])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(before_mtime, path.stat().st_mtime_ns)
            self.assertEqual(before_backups, list(path.parent.glob("*.local-rag-backup-*")))

    def test_one_denied_rule_and_one_added_rule_reports_manual_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = appdata / "Code" / "User" / "settings.json"
            path.parent.mkdir(parents=True)
            denied = self.rules[0].replace("\\", "\\\\").replace('"', '\\"')
            path.write_text(
                '{"chat.tools.terminal.autoApprove":{"' + denied + '":false}}\n',
                encoding="utf-8",
            )
            first = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", first["status"])
            self.assertEqual(1, first["targets_changed"])
            rendered = path.read_text(encoding="utf-8")
            self.assertIn(f'"{denied}":false', rendered)
            self.assertIn(r"search\\.py", rendered)
            before = path.read_bytes()
            before_mtime = path.stat().st_mtime_ns
            backups = list(path.parent.glob("*.local-rag-backup-*"))
            second = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", second["status"])
            self.assertEqual(0, second["targets_changed"])
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(before_mtime, path.stat().st_mtime_ns)
            self.assertEqual(backups, list(path.parent.glob("*.local-rag-backup-*")))

    def test_incomplete_allow_object_is_manual_but_unrelated_false_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = appdata / "Code" / "User" / "settings.json"
            path.parent.mkdir(parents=True)
            first_rule = self.rules[0].replace("\\", "\\\\").replace('"', '\\"')
            second_rule = self.rules[1].replace("\\", "\\\\").replace('"', '\\"')
            path.write_text(
                '{"unrelated":false,"chat.tools.terminal.autoApprove":{'
                f'"{first_rule}":{{"approve":true}},'
                f'"{second_rule}":{{"approve":true,"matchCommandLine":true}}'
                '}}\n',
                encoding="utf-8",
            )
            result = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", result["status"])
            self.assertEqual(0, result["targets_changed"])
            path.write_text('{"unrelated":false}\n', encoding="utf-8")
            result = configure_vscode(self.home, appdata)
            self.assertEqual("configured_on_disk", result["status"])
            self.assertEqual(1, result["targets_changed"])

    def test_duplicate_target_rule_is_manual_and_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = appdata / "Code" / "User" / "settings.json"
            path.parent.mkdir(parents=True)
            denied = self.rules[0].replace("\\", "\\\\").replace('"', '\\"')
            original = (
                '{"chat.tools.terminal.autoApprove":{'
                f'"{denied}":false,"{denied}":true'
                '}}\n'
            ).encode()
            path.write_bytes(original)
            result = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", result["status"])
            self.assertEqual(0, result["targets_changed"])
            self.assertEqual(original, path.read_bytes())


    def test_unknown_global_types_and_approve_false_fail_closed_manual(self) -> None:
        for value in ('"false"', "null", "{}", "0", "TRUE"):
            with self.subTest(global_value=value), tempfile.TemporaryDirectory() as directory:
                appdata = Path(directory)
                path = appdata / "Code" / "User" / "settings.json"
                path.parent.mkdir(parents=True)
                original = (
                    '{"chat.tools.terminal.enableAutoApprove":' + value + '}\n'
                ).encode()
                path.write_bytes(original)
                result = configure_vscode(self.home, appdata)
                self.assertEqual("manual_action_required", result["status"])
                self.assertEqual(0, result["targets_changed"])
                self.assertEqual(original, path.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = appdata / "Code" / "User" / "settings.json"
            path.parent.mkdir(parents=True)
            first_rule = self.rules[0].replace("\\", "\\\\").replace('"', '\\"')
            second_rule = self.rules[1].replace("\\", "\\\\").replace('"', '\\"')
            original = (
                '{"chat.tools.terminal.autoApprove":{'
                f'"{first_rule}":{{"approve":false,"matchCommandLine":true}},'
                f'"{second_rule}":{{"approve":true,"matchCommandLine":true,"unknown":1}}'
                '}}\n'
            ).encode()
            path.write_bytes(original)
            result = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", result["status"])
            self.assertEqual(0, result["targets_changed"])
            self.assertEqual(original, path.read_bytes())
            uppercase = original.replace(b"false", b"TRUE", 1)
            path.write_bytes(uppercase)
            result = configure_vscode(self.home, appdata)
            self.assertEqual("manual_action_required", result["status"])
            self.assertEqual(uppercase, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
