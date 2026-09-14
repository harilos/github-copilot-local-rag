from __future__ import annotations

import base64
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = (
    ROOT / ".copilot/rag/source_manager/windows-install-template.ps1",
    ROOT / "tools/windows_portable/install-template.ps1",
)
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def progress_helper(text: str) -> str:
    return text[text.index("$InstallProgressClock ="):text.index("function Start-InstallTranscript")]


class WindowsInstallProgressTests(unittest.TestCase):
    def test_both_routes_use_the_same_progress_helper_and_valid_labels(self) -> None:
        helpers = [progress_helper(path.read_text(encoding="utf-8")) for path in TEMPLATES]
        self.assertEqual(helpers[0], helpers[1])
        labels = dict(re.findall(r'^    (\w+) = "([A-Za-z0-9+/=]+)"$', helpers[0], re.M))
        decoded = {key: base64.b64decode(value, validate=True).decode("utf-8") for key, value in labels.items()}
        self.assertIn("概算", decoded["title"])
        self.assertIn("経過", decoded["format"])
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            for stage in re.findall(r'-Stage "(\w+)"', text):
                self.assertIn(stage, decoded)
            self.assertTrue(progress_helper(text).isascii())

    def test_large_copies_refresh_without_a_counting_pass(self) -> None:
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            helper = progress_helper(text)
            self.assertIn('$Worker.AddCommand("Copy-Item")', helper)
            self.assertIn('$Worker.AddParameter("ErrorAction", "Stop")', helper)
            self.assertRegex(helper, r"while \(-not \$Pending.IsCompleted\) \{\s+Write-InstallProgress")
            self.assertIn("AsyncWaitHandle.WaitOne(1000)", helper)
            self.assertIn("$Worker.EndInvoke($Pending)", helper)
            self.assertIn("$Worker.HadErrors", helper)
            self.assertIn("$Worker.Stop()", helper)
            self.assertIn("$Worker.Dispose()", helper)
            self.assertNotIn("Get-ChildItem", helper)
            self.assertNotIn("Start-Job", helper)
            self.assertEqual(3, text.count("Copy-InstallPayload -LiteralPath"))

    def test_success_only_reaches_100_and_prompt_is_not_obscured(self) -> None:
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r'if \(\$Succeeded\) \{\s+Write-InstallProgress -Percent 100')
            self.assertEqual(1, text.count("-Percent 100"))
            self.assertIn('$Seconds - $script:InstallProgressLogSecond -ge 5', text)
            self.assertIn('Write-Progress -Id 1 -Activity "Local RAG" -Completed\nif ($ConfigureVSCode', text)
            self.assertIn('Write-InstallProgress -Stage "rollback" -Force', text)
            self.assertLess(text.index('$DatabaseOrdinal++'), text.index('$DatabaseStatus = "READY"'))

    @unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
    def test_parse_and_small_copy_success_failure_and_heartbeat(self) -> None:
        # Helper-only execution: no installation, user profile, runtime or DB.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source [literal]"
            source.mkdir()
            (source / "nested").mkdir()
            (source / "nested" / "data.bin").write_bytes(b"local-rag" * 1024)
            script = root / "check.ps1"
            helper = progress_helper(TEMPLATES[0].read_text(encoding="utf-8"))
            body = r"""
param([string]$Fixture, [string]$FirstTemplate, [string]$SecondTemplate)
$ErrorActionPreference = "Stop"
foreach ($Path in @($FirstTemplate, $SecondTemplate)) {
    $Tokens = $null
    $ParseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($Path, [ref]$Tokens, [ref]$ParseErrors)
    if ($ParseErrors.Count) { throw ($ParseErrors | Out-String) }
}
"""
            body += helper
            body += r"""
$Source = Join-Path $Fixture "source [literal]"
$Destination = Join-Path $Fixture "copied"
Write-InstallProgress -Percent 10 -Stage "copy_runtime" -Force
Copy-InstallPayload -LiteralPath $Source -Destination $Destination -Recurse
if (-not (Test-Path -LiteralPath (Join-Path $Destination "nested/data.bin"))) { throw "copy missing" }
$Failed = $false
try { Copy-InstallPayload -LiteralPath (Join-Path $Fixture "absent") -Destination $Destination -Recurse }
catch { $Failed = $true }
if (-not $Failed) { throw "copy error was swallowed" }
"""
            # Delay just this fixture's Copy-Item, not the product or installer.
            delayed = helper.replace('$Worker.AddCommand("Copy-Item")',
                '$Worker.AddScript("param($LiteralPath, $Destination, $Recurse, $ErrorAction) '
                'Start-Sleep -Milliseconds 1200; Copy-Item -LiteralPath $LiteralPath '
                '-Destination $Destination -Recurse:$Recurse -ErrorAction Stop")')
            body += delayed
            body += r"""
Copy-InstallPayload -LiteralPath $Source -Destination (Join-Path $Fixture "delayed") -Recurse
if ($script:InstallProgressSecond -lt 1) { throw "heartbeat did not update" }
Write-Output "PROGRESS_HELPER_PASS"
"""
            script.write_text(body, encoding="utf-8")
            completed = subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script),
                 str(root), *(str(path) for path in TEMPLATES)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("PROGRESS_HELPER_PASS", completed.stdout)
            self.assertEqual((source / "nested/data.bin").read_bytes(), (root / "copied/nested/data.bin").read_bytes())


if __name__ == "__main__":
    unittest.main()
