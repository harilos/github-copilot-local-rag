from __future__ import annotations

import base64
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = (
    ROOT / ".copilot/rag/source_manager/windows-install-template.ps1",
    ROOT / "tools/windows_portable/install-template.ps1",
)
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def progress_helper(text: str) -> str:
    return text[text.index("$InstallProgressClock ="):text.index("function Start-InstallTranscript")]


def archive_helpers(text: str) -> str:
    return text[text.index("function Assert-ChildPath"):text.index("function Remove-Tree")]


def expand_helper(text: str) -> str:
    return text[text.index("function Expand-SafeArchive"):text.index("function Remove-Tree")]


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

    def test_large_archive_extraction_refreshes_without_a_counting_pass(self) -> None:
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            helper = expand_helper(text)
            self.assertIn("[System.IO.Compression.ZipFile]::ExtractToDirectory(", helper)
            self.assertIn('$Worker.AddParameter("ArchivePath", $ArchivePath)', helper)
            self.assertIn('$Worker.AddParameter("Destination", $Destination)', helper)
            self.assertRegex(helper, r"while \(-not \$Pending.IsCompleted\) \{\s+Write-InstallProgress")
            self.assertIn("AsyncWaitHandle.WaitOne(1000)", helper)
            self.assertIn("$Worker.EndInvoke($Pending)", helper)
            self.assertIn("$Worker.HadErrors", helper)
            self.assertIn("$Worker.Stop()", helper)
            self.assertIn("$Worker.Dispose()", helper)
            self.assertNotIn("Get-ChildItem", helper)
            self.assertNotIn("Start-Job", helper)
            self.assertEqual(3, text.count("Expand-SafeArchive `"))

    def test_success_only_reaches_100_and_prompt_is_not_obscured(self) -> None:
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r'if \(\$Succeeded\) \{\s+Write-InstallProgress -Percent 100')
            self.assertEqual(1, text.count("-Percent 100"))
            self.assertIn('$Seconds - $script:InstallProgressLogSecond -ge 5', text)
            self.assertIn('Write-Progress -Id 1 -Activity "Local RAG" -Completed\nif ($ConfigureVSCode', text)
            self.assertIn('Write-InstallProgress -Stage "rollback" -Force', text)
            self.assertLess(text.index('$DatabaseOrdinal++'), text.index('$DatabaseStatus = "READY"'))

    def test_only_database_archives_enable_compression_before_extraction(self) -> None:
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("-CompressDatabase"))
            self.assertIn("-Destination $Existing -CompressDatabase", text)
            helper = expand_helper(text)
            self.assertLess(helper.index("New-Item"), helper.index("Enable-DatabaseCompression"))
            self.assertLess(helper.index("Enable-DatabaseCompression"), helper.index("$Worker.BeginInvoke()"))

    @unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
    def test_parse_and_small_archive_success_failure_and_heartbeat(self) -> None:
        # Helper-only execution: no installation, user profile, runtime or DB.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source [literal].zip"
            with zipfile.ZipFile(archive, "w") as fixture:
                fixture.writestr("nested/data.bin", b"local-rag" * 1024)
            script = root / "check.ps1"
            template = TEMPLATES[0].read_text(encoding="utf-8")
            progress = progress_helper(template)
            archive = archive_helpers(template)
            body = r"""
param([string]$Fixture, [string]$FirstTemplate, [string]$SecondTemplate)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($Path in @($FirstTemplate, $SecondTemplate)) {
    $Tokens = $null
    $ParseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($Path, [ref]$Tokens, [ref]$ParseErrors)
    if ($ParseErrors.Count) { throw ($ParseErrors | Out-String) }
}
"""
            body += progress
            body += archive
            body += r"""
$Source = Join-Path $Fixture "source [literal].zip"
$Destination = Join-Path $Fixture "copied"
Write-InstallProgress -Percent 10 -Stage "copy_runtime" -Force
Expand-SafeArchive -ArchivePath $Source -Destination $Destination
if (-not (Test-Path -LiteralPath (Join-Path $Destination "nested/data.bin"))) { throw "copy missing" }
$Failed = $false
try { Expand-SafeArchive -ArchivePath (Join-Path $Fixture "absent.zip") -Destination $Destination }
catch { $Failed = $true }
if (-not $Failed) { throw "copy error was swallowed" }

# An unavailable compression command must not prevent archive installation.
$SavedSystemRoot = $env:SystemRoot
try {
    $env:SystemRoot = Join-Path $Fixture "no-windows"
    Expand-SafeArchive -ArchivePath $Source -Destination (Join-Path $Fixture "fallback") -CompressDatabase
    if (-not (Test-Path -LiteralPath (Join-Path $Fixture "fallback/nested/data.bin"))) {
        throw "compression fallback did not extract the archive"
    }
} finally { $env:SystemRoot = $SavedSystemRoot }

# On Windows/NTFS, exercise real inheritance through ZIP extraction.
if ($env:OS -eq "Windows_NT" -and ([System.IO.DriveInfo]::new($Fixture)).DriveFormat -eq "NTFS") {
    $Compressed = Join-Path $Fixture "compressed [literal]"
    Expand-SafeArchive -ArchivePath $Source -Destination $Compressed -CompressDatabase
    foreach ($Relative in @("", "nested", "nested/data.bin")) {
        $Attributes = [System.IO.File]::GetAttributes((Join-Path $Compressed $Relative))
        if (($Attributes -band [System.IO.FileAttributes]::Compressed) -eq 0) {
            throw "ZIP entry did not inherit compression: $Relative"
        }
    }
}
"""
            # Delay just this fixture's worker, not the product or installer.
            delayed = archive.replace(
                "'param($ArchivePath, $Destination) ' +",
                "'param($ArchivePath, $Destination) Start-Sleep -Milliseconds 1200; ' +",
            )
            body += delayed
            body += r"""
Expand-SafeArchive -ArchivePath $Source -Destination (Join-Path $Fixture "delayed")
if ($script:InstallProgressSecond -lt 1) { throw "heartbeat did not update" }
Write-Output "PROGRESS_HELPER_PASS"
"""
            script.write_text(body, encoding="utf-8")
            completed = subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script),
                 str(root), *(str(path) for path in TEMPLATES)],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("PROGRESS_HELPER_PASS", completed.stdout)
            self.assertEqual(b"local-rag" * 1024, (root / "copied/nested/data.bin").read_bytes())


if __name__ == "__main__":
    unittest.main()
