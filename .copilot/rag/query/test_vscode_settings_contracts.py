from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vscode_settings
from vscode_settings import (
    GLOBAL_AUTO_APPROVE_KEY,
    _dedupe_settings_paths,
    candidate_settings,
    configure_vscode,
    patch_settings,
)


class VSCodeSettingsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(r"C:\Profile Root\Example User\.copilot")

    def _settings(self, appdata: Path) -> Path:
        path = appdata / "Code" / "User" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_empty_settings_adds_global_auto_approve_once(self) -> None:
        first = patch_settings("{}\n")
        self.assertEqual(1, first.count(f'"{GLOBAL_AUTO_APPROVE_KEY}"'))
        self.assertIn(f'"{GLOBAL_AUTO_APPROVE_KEY}": true', first)
        self.assertEqual(first, patch_settings(first))

    def test_comments_crlf_unrelated_value_and_trailing_comma_are_preserved(self) -> None:
        original = (
            "{\r\n"
            "  // keep this comment\r\n"
            '  "editor.fontSize": 15,\r\n'
            "}\r\n"
        )
        patched = patch_settings(original)
        self.assertIn("// keep this comment\r\n", patched)
        self.assertIn('"editor.fontSize": 15,\r\n', patched)
        self.assertIn(f'"{GLOBAL_AUTO_APPROVE_KEY}": true,\r\n', patched)
        self.assertNotIn("\n", patched.replace("\r\n", ""))
        self.assertEqual(patched, patch_settings(patched))

    def test_utf8_bom_and_existing_bytes_survive_configure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = self._settings(appdata)
            original = (
                b"\xef\xbb\xbf{\r\n"
                b"  // preserved\r\n"
                b'  "workbench.colorTheme": "Dark",\r\n'
                b"}\r\n"
            )
            path.write_bytes(original)
            result = configure_vscode(self.home, appdata)
            rendered = path.read_bytes()
            self.assertEqual("configured_on_disk", result["status"])
            self.assertEqual([None], result["previous_values"])
            self.assertTrue(rendered.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"// preserved\r\n", rendered)
            self.assertIn(b'"workbench.colorTheme": "Dark",\r\n', rendered)
            self.assertIn(
                f'"{GLOBAL_AUTO_APPROVE_KEY}": true,\r\n'.encode(),
                rendered,
            )
            before_second = rendered
            backups = list(path.parent.glob("*.local-rag-backup-*"))
            second = configure_vscode(self.home, appdata)
            self.assertEqual("already_configured", second["status"])
            self.assertEqual(before_second, path.read_bytes())
            self.assertEqual(backups, list(path.parent.glob("*.local-rag-backup-*")))

    def test_false_becomes_true_after_exact_backup_and_reports_previous_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = self._settings(appdata)
            original = (
                f'{{"{GLOBAL_AUTO_APPROVE_KEY}":false,"unrelated":17}}\n'
            ).encode()
            path.write_bytes(original)
            result = configure_vscode(self.home, appdata)
            self.assertEqual("configured_on_disk", result["status"])
            self.assertEqual([False], result["previous_values"])
            self.assertIn(
                f'"{GLOBAL_AUTO_APPROVE_KEY}":true'.encode(),
                path.read_bytes(),
            )
            backups = list(path.parent.glob("*.local-rag-backup-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(original, backups[0].read_bytes())

    def test_opt_out_is_byte_identical_for_existing_and_absent_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = self._settings(appdata)
            original = b'{"editor.fontSize":15}\n'
            path.write_bytes(original)
            before_mtime = path.stat().st_mtime_ns
            result = configure_vscode(self.home, appdata, enabled=False)
            self.assertEqual("skipped_by_user", result["status"])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(before_mtime, path.stat().st_mtime_ns)
            path.unlink()
            result = configure_vscode(self.home, appdata, enabled=False)
            self.assertEqual("skipped_by_user", result["status"])
            self.assertFalse(path.exists())

    def test_stable_insiders_and_profiles_are_discovered_and_case_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            stable = appdata / "Code" / "User"
            insiders = appdata / "Code - Insiders" / "User"
            (stable / "profiles" / "Alpha").mkdir(parents=True)
            (stable / "profiles" / "beta").mkdir(parents=True)
            insiders.mkdir(parents=True)
            paths = candidate_settings(appdata)
            self.assertEqual(4, len(paths))
            self.assertIn(stable / "settings.json", paths)
            self.assertIn(insiders / "settings.json", paths)
            deduped = _dedupe_settings_paths(
                [Path(r"C:\Users\Me\settings.json"), Path(r"c:\users\me\SETTINGS.JSON")]
            )
            self.assertEqual(1, len(deduped))

    def test_all_stable_insiders_and_profile_targets_are_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            targets = [
                appdata / "Code" / "User" / "settings.json",
                appdata / "Code" / "User" / "profiles" / "one" / "settings.json",
                appdata / "Code" / "User" / "profiles" / "two" / "settings.json",
                appdata / "Code - Insiders" / "User" / "settings.json",
            ]
            for path in targets:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            result = configure_vscode(self.home, appdata)
            self.assertEqual("configured_on_disk", result["status"])
            self.assertEqual(4, result["targets_checked"])
            self.assertEqual(4, result["targets_changed"])
            for path in targets:
                self.assertIn(
                    f'"{GLOBAL_AUTO_APPROVE_KEY}": true',
                    path.read_text(encoding="utf-8"),
                )

    def test_malformed_duplicate_and_utf16_are_non_destructive_errors(self) -> None:
        values = (
            b'{"editor.fontSize":15 "files.autoSave":"off"}\n',
            (
                f'{{"{GLOBAL_AUTO_APPROVE_KEY}":false,'
                f'"{GLOBAL_AUTO_APPROVE_KEY}":true}}\n'
            ).encode(),
            b"\xff\xfe{\x00}\x00",
        )
        for original in values:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                appdata = Path(directory)
                path = self._settings(appdata)
                path.write_bytes(original)
                before_mtime = path.stat().st_mtime_ns
                result = configure_vscode(self.home, appdata)
                self.assertEqual("error", result["status"])
                self.assertEqual(0, result["targets_changed"])
                self.assertEqual(original, path.read_bytes())
                self.assertEqual(before_mtime, path.stat().st_mtime_ns)
                self.assertEqual([], list(path.parent.glob("*.local-rag-backup-*")))

    def test_reparse_and_io_failure_leave_original_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = self._settings(appdata)
            original = b'{}\n'
            path.write_bytes(original)
            with mock.patch.object(vscode_settings, "_path_has_reparse", return_value=True):
                result = configure_vscode(self.home, appdata)
            self.assertEqual("error", result["status"])
            self.assertEqual(original, path.read_bytes())
            with mock.patch.object(
                vscode_settings, "_atomic_write_bytes", side_effect=OSError("denied")
            ):
                result = configure_vscode(self.home, appdata)
            self.assertEqual("error", result["status"])
            self.assertEqual(original, path.read_bytes())

    def test_read_only_target_is_rejected_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            path = self._settings(appdata)
            original = b'{}\n'
            path.write_bytes(original)
            path.chmod(stat.S_IREAD)
            try:
                result = configure_vscode(self.home, appdata)
                self.assertEqual("error", result["status"])
                self.assertEqual(original, path.read_bytes())
                self.assertEqual([], list(path.parent.glob("*.local-rag-backup-*")))
            finally:
                path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_legacy_terminal_rules_and_unrelated_denies_are_preserved(self) -> None:
        original = (
            '{\n  "chat.tools.terminal.enableAutoApprove": false,\n'
            '  "chat.tools.terminal.autoApprove": {\n'
            '    "/legacy-local-rag/": {"approve":true,"matchCommandLine":true},\n'
            '    "Remove-Item": false\n'
            "  }\n}\n"
        )
        patched = patch_settings(original)
        self.assertIn('"chat.tools.terminal.enableAutoApprove": false', patched)
        self.assertIn('"/legacy-local-rag/": {"approve":true', patched)
        self.assertIn('"Remove-Item": false', patched)
        self.assertIn(f'"{GLOBAL_AUTO_APPROVE_KEY}": true', patched)

    def test_non_boolean_global_value_fails_closed(self) -> None:
        for value in ('"true"', "null", "{}", "0"):
            original = f'{{"{GLOBAL_AUTO_APPROVE_KEY}":{value}}}\n'
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be a boolean"):
                    patch_settings(original)

    def test_absent_vscode_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            result = configure_vscode(self.home, appdata)
            self.assertEqual("not_detected", result["status"])
            self.assertEqual(0, result["targets_checked"])
            self.assertFalse((appdata / "Code").exists())

    def test_cli_reports_disk_configuration_and_unknown_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            self._settings(appdata).write_text("{}\n", encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"APPDATA": str(appdata)}),
                mock.patch.object(
                    sys,
                    "argv",
                    ["vscode_settings.py", "--copilot-home", str(self.home)],
                ),
                contextlib.redirect_stdout(output),
            ):
                return_code = vscode_settings.main()
            payload = json.loads(output.getvalue())
            self.assertEqual(0, return_code)
            self.assertEqual("configured_on_disk", payload["status"])
            self.assertEqual("unknown", payload["policy_effectiveness"])
            self.assertEqual([None], payload["previous_values"])


if __name__ == "__main__":
    unittest.main()
