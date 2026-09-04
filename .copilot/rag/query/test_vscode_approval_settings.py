from __future__ import annotations

import json
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest.mock import patch as mock
from unittest.mock import Mock

import vscode_approval_settings as approval
from mcp_config import _JsoncLexer


class ApprovalTests(unittest.TestCase):
    def test_interactive_default_is_runner_and_other_choices_are_explicit(self):
        for response, expected in (('', 'runner'), ('1', 'runner'), (' 1 ', 'runner'),
                                   ('2', None), ('3', 'global'), ('garbage', None)):
            with self.subTest(response=response), mock('sys.stdin.isatty', return_value=True), \
                    mock('builtins.input', return_value=response), mock('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(approval.choose_mode(), expected)

    def test_no_input_never_means_consent(self):
        with mock('sys.stdin.isatty', return_value=False), mock('builtins.input') as read, \
                mock('sys.stdout', new_callable=io.StringIO):
            self.assertIsNone(approval.choose_mode())
            read.assert_not_called()
        for error in (EOFError, OSError):
            with mock('sys.stdin.isatty', return_value=True), mock('builtins.input', side_effect=error), \
                    mock('sys.stdout', new_callable=io.StringIO):
                self.assertIsNone(approval.choose_mode())

    def test_choose_skip_does_not_access_policy_or_settings(self):
        with mock.object(sys, 'argv', ['approval', '--mode', 'choose', '--install-root', '.']), \
                mock('sys.stdin.isatty', return_value=False), mock.object(approval, 'policy_allows') as policy, \
                mock.object(approval, 'configure') as configure, mock('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(approval.main(), 0)
            policy.assert_not_called()
            configure.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows settings main')
    def test_choose_default_configures_only_runner(self):
        with mock.object(sys, 'argv', ['approval', '--mode', 'choose', '--install-root', '.']), \
                mock('sys.stdin.isatty', return_value=True), mock('builtins.input', return_value=''), \
                mock.dict(os.environ, {'APPDATA': 'C:/Synthetic/AppData'}), \
                mock.object(approval, 'policy_allows', return_value=True) as policy, \
                mock.object(approval, 'configure', return_value='unchanged') as configure, \
                mock('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(approval.main(), 0)
            policy.assert_called_once_with('runner')
            self.assertEqual(configure.call_args.args[-1], 'runner')

    def test_approval_scope_is_explained_before_selection(self):
        with mock('sys.stdin.isatty', return_value=False), mock('sys.stdout', new_callable=io.StringIO) as output:
            approval.choose_mode()
        for phrase in ('[default]', 'VS Code only', 'NOT Copilot CLI', 'DANGER / NOT RECOMMENDED',
                       'does NOT affect standalone Copilot CLI'):
            self.assertIn(phrase, output.getvalue())

    def test_pipe_cannot_implicitly_select_default(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'APPDATA': directory}
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(approval.__file__)),
                '--mode', 'choose', '--install-root', directory], input='\n', capture_output=True,
                encoding='utf-8', env=env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('No interactive input', result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_jsonc_bom_comments_crlf_preserved_and_idempotent(self):
        text = '\ufeff{\r\n  // keep\r\n  "editor.fontSize": 17,\r\n}\r\n'
        result = approval.patch(text, Path('C:/Example/.copilot'), 'runner')
        self.assertIn('// keep\r\n  "editor.fontSize": 17,\r\n', result)
        self.assertTrue(result.startswith('\ufeff'))
        self.assertEqual(result, approval.patch(result, Path('C:/Example/.copilot'), 'runner'))
        values, _ = _JsoncLexer(result[1:]).document()
        self.assertNotIn(approval.GLOBAL, values)
        self.assertNotIn(approval.ENABLE, values)

    def test_global_only_changes_requested_key(self):
        text = '{"chat.tools.global.autoApprove":false, "other": [1,2]}'
        self.assertEqual(approval.patch(text, Path('.'), 'global'), text.replace(':false', ':true'))

    def test_foreign_allow_and_deny_rules_preserved(self):
        text = '{"chat.tools.terminal.autoApprove":{ /* user */ "git":false,"echo":true}}'
        result = approval.patch(text, Path('C:/Example/.copilot'), 'runner')
        self.assertIn('/* user */ "git":false,"echo":true', result)

    def test_bad_jsonc_and_disabled_settings_fail_closed(self):
        for text in ('{broken}', '{"a":1,"a":2}', '[]',
                     '{"chat.tools.terminal.autoApprove":false}',
                     '{"chat.tools.terminal.enableAutoApprove":false}',
                     '{"chat.tools.global.autoApprove":true}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                approval.patch(text, Path('.'), 'runner')

    def test_conflicting_rule_is_not_overwritten(self):
        root = Path('C:/Example/.copilot')
        with self.assertRaises(ValueError):
            approval.patch(json.dumps({approval.TERMINAL: {approval.runner_rule(root): False}}), root, 'runner')

    def test_full_command_boundary(self):
        root = Path('C:/Example/.copilot').absolute()
        prefix = str(root).replace('/', '\\')
        command = f'& "{prefix}\\rag\\query\\.venv\\Scripts\\python.exe" -I -X utf8 -B "{prefix}\\rag\\query\\skill_runner.py"'
        pattern = approval.runner_rule(root)[1:-1]
        for verb in ('list', 'search', 'detail', 'setup'):
            self.assertIsNotNone(re.fullmatch(pattern, command + ' ' + verb))
        self.assertIsNotNone(re.fullmatch(pattern, command + " search --db db --question '日本語 — 🍀 O''Brien; | $(literal)'"))
        for bad in (
            command + ' search --question "$(whoami)"',
            command + ' list; git push', command + ' list | powershell',
            command + ' list > output', command + ' list\ngit push',
            command + ' list\n', command + ' list && echo x',
            command.replace('-I -X utf8 -B', '-c') + ' list',
            command.replace('skill_runner.py', 'other.py') + ' list',
            command.replace('python.exe', 'python2.exe') + ' list',
            command + ' delete', 'powershell -Command ' + command + ' list',
            command + ' search --question $env:SECRET',
        ):
            with self.subTest(command=bad):
                self.assertIsNone(re.fullmatch(pattern, bad))

    def test_shipped_environment_form_only_for_default_install(self):
        with mock.dict(os.environ, {'USERPROFILE': str(Path('C:/Example').absolute())}):
            root = Path(os.environ['USERPROFILE']) / '.copilot'
            command = '& "$env:USERPROFILE\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe" -I -X utf8 -B "$env:USERPROFILE\\.copilot\\rag\\query\\skill_runner.py" list'
            self.assertIsNotNone(re.fullmatch(approval.runner_rule(root)[1:-1], command))
            self.assertIsNone(re.fullmatch(approval.runner_rule(root / 'other')[1:-1], command))

    def test_atomic_settings_write_and_unchanged_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '.copilot'
            for name in ('rag/query/skill_runner.py', 'rag/query/.venv/Scripts/python.exe'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            settings = Path(directory) / 'Code/User/settings.json'
            self.assertEqual(approval.configure(settings, root, 'runner'), 'settings_written_not_effective_permission_verified')
            first = settings.read_bytes()
            self.assertEqual(approval.configure(settings, root, 'runner'), 'unchanged')
            self.assertEqual(settings.read_bytes(), first)
            settings.write_bytes(b'{broken}')
            with self.assertRaises(ValueError):
                approval.configure(settings, root, 'global')
            self.assertEqual(settings.read_bytes(), b'{broken}')

    def test_policy_deny_and_absence(self):
        class Key:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        import types
        registry = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2,
            KEY_WOW64_64KEY=4, KEY_WOW64_32KEY=8, KEY_READ=16,
            OpenKey=lambda *args: Key(), QueryValueEx=lambda *args: (0, 4))
        with mock.dict(sys.modules, {'winreg': registry}):
            self.assertFalse(approval.policy_allows('global'))
            registry.QueryValueEx = lambda *args: (1, 4)
            self.assertTrue(approval.policy_allows('runner'))
            registry.OpenKey = Mock(side_effect=FileNotFoundError)
            self.assertTrue(approval.policy_allows('global'))
            registry.OpenKey = Mock(side_effect=PermissionError)
            with self.assertRaises(PermissionError):
                approval.policy_allows('global')

    def test_rule_runs_in_javascript_not_only_python(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node required to validate VS Code regex semantics')
        root = Path('C:/Example/.copilot').absolute()
        prefix = str(root).replace('/', '\\')
        command = f'& "{prefix}\\rag\\query\\.venv\\Scripts\\python.exe" -I -X utf8 -B "{prefix}\\rag\\query\\skill_runner.py" list'
        payload = json.dumps([approval.runner_rule(root), command])
        result = subprocess.run([node, '-e',
            "const [key,c]=JSON.parse(require('fs').readFileSync(0,'utf8'));"
            "const r=new RegExp(key.slice(1,-1));"
            "if(!r.test(c)||r.test(c+'; git push')||r.test(c+'\\n'))process.exit(1);"],
            input=payload, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reparse_settings_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            destination = base / 'foreign'
            destination.mkdir()
            link = base / 'link'
            if os.name == 'nt':
                subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(destination)],
                               check=True, capture_output=True, timeout=20)
            else:
                link.symlink_to(destination, target_is_directory=True)
            try:
                with self.assertRaises(ValueError):
                    approval.configure(link / 'settings.json', base, 'global')
                self.assertEqual(list(destination.iterdir()), [])
            finally:
                if os.name == 'nt':
                    os.rmdir(link)
                else:
                    link.unlink()

    def test_both_installers_forward_and_guard_options(self):
        repository = Path(__file__).resolve().parents[3]
        source = (repository / 'install.ps1').read_text(encoding='utf-8')
        portable = (repository / 'tools/windows_portable/install-template.ps1').read_text(encoding='utf-8')
        mirror = (repository / '.copilot/rag/source_manager/windows-install-template.ps1').read_text(encoding='utf-8')
        self.assertEqual(portable, mirror)
        for text in (source, portable):
            self.assertIn('[switch]$SkipVSCodeAutoApprove', text)
            self.assertIn('$ConfigureVSCodeAutoApprove -and $ConfigureVSCodeRunnerApproval', text)
            self.assertIn('vscode_approval_settings.py', text)
            self.assertIn('$ApprovalMode = "choose"', text)
            self.assertIn('if ($ConfigureVSCodeRunnerApproval)', text)
            self.assertIn('if ($ConfigureVSCodeAutoApprove)', text)
        for path in ('tools/windows_portable/windows_package_builder.py',
                     '.copilot/rag/source_manager/windows_distribution.py'):
            generator = (repository / path).read_text(encoding='utf-8')
            for option in ('global', 'runner', 'skip'):
                self.assertIn(f'%local_rag_{option}%', generator)

    def test_powershell_51_parses_installers(self):
        if os.name != 'nt':
            self.skipTest('Windows PowerShell parse is Windows-only')
        repository = Path(__file__).resolve().parents[3]
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = "$bad=0; foreach($f in @('install.ps1','tools/windows_portable/install-template.ps1','.copilot/rag/source_manager/windows-install-template.ps1')) { $t=$null; $e=$null; [System.Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) $f),[ref]$t,[ref]$e) | Out-Null; $bad += $e.Count }; exit $bad"
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script], cwd=repository,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'install.cmd requires Windows')
    def test_generated_launchers_forward_only_explicit_options(self):
        repository = Path(__file__).resolve().parents[3]
        sys.path.insert(0, str(repository / 'tools/windows_portable'))
        sys.path.insert(0, str(repository / '.copilot/rag'))
        import windows_package_builder
        from source_manager import windows_distribution
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows_distribution._generated_installer_entries(root)
            generated = (root / 'generated/install.cmd').read_text(encoding='utf-8')
            for launcher in (windows_package_builder._install_cmd(), generated):
                internal = root / 'internal'
                internal.mkdir(exist_ok=True)
                (root / 'install.cmd').write_text(launcher, encoding='utf-8', newline='\r\n')
                (internal / 'install.ps1').write_text(
                    'param([switch]$ConfigureVSCodeAutoApprove,[switch]$ConfigureVSCodeRunnerApproval,'
                    '[switch]$SkipVSCodeAutoApprove,[switch]$ReplaceExistingDatabases,[switch]$LauncherArgumentError)\n'
                    'Write-Output ("FLAGS:" + [int][bool]$ConfigureVSCodeAutoApprove + [int][bool]$ConfigureVSCodeRunnerApproval + [int][bool]$SkipVSCodeAutoApprove)\n',
                    encoding='ascii')
                for flags, expected in (([], '000'), (['-ConfigureVSCodeAutoApprove'], '100'),
                    (['-ConfigureVSCodeRunnerApproval'], '010'),
                    (['-ConfigureVSCodeAutoApprove', '-ConfigureVSCodeRunnerApproval'], '110'),
                    (['-ConfigureVSCodeAutoApprove', '-SkipVSCodeAutoApprove'], '101')):
                    with self.subTest(flags=flags):
                        result = subprocess.run(['cmd', '/d', '/c', 'install.cmd', '-NoPause', *flags],
                            cwd=root, capture_output=True, encoding='utf-8', timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('FLAGS:' + expected, result.stdout)


if __name__ == '__main__':
    unittest.main()
