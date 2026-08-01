from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vscode_settings import (
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
        self.assertIn("list_dbs.py", rendered)
        self.assertIn("search.py", rendered)
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
        self.assertNotIn(str(self.home / "rag" / "query" / ".venv" / "Scripts" / "python.exe"), self.rules)

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
        self.assertIn("list_dbs.py", patched)
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
            stable = appdata / "Code" / "User"
            profile = stable / "profiles" / "one"
            profile.mkdir(parents=True)
            (profile / "settings.json").write_text("{}", encoding="utf-8")
            paths = candidate_settings(appdata)
            self.assertIn(stable / "settings.json", paths)
            self.assertIn(profile / "settings.json", paths)
            self.assertFalse(any("Insiders" in str(path) for path in paths))

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
        self.assertIn("search.py", patched)


if __name__ == "__main__":
    unittest.main()
