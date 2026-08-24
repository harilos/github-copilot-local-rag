[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [string]$AuthorityPath = (Join-Path $PSScriptRoot "data\lrr-agent003-cli-model-bakeoff-v1.json"),

    [Parameter()]
    [string]$CollectorPath = (Join-Path $PSScriptRoot "collect_lrr_agent003_cli_model_bakeoff.py"),

    [Parameter()]
    [string]$HistoricalHarnessArchiveRoot = (Join-Path $PSScriptRoot "data\r3-historical-harness"),

    [Parameter()]
    [string]$CollectorPython = (Join-Path $env:USERPROFILE ".copilot\rag\query\.venv\Scripts\python.exe"),

    [Parameter()]
    [string]$CandidateRuntimeRoot = (Join-Path $env:USERPROFILE ".copilot"),

    [Parameter()]
    [string]$ProductionLauncherPath = (Join-Path $env:USERPROFILE ".copilot\copilot-cli\local-rag-agent003.ps1"),

    [Parameter()]
    [string]$OutputRoot,

    [Parameter()]
    [string]$ResumeEvidenceRoot,

    [Parameter()]
    [ValidateSet(0, 5)]
    [int]$ResumeCompletedOrdinal = 0,

    [Parameter()]
    [ValidateSet(15)]
    [int]$CaseTimeoutMinutes = 15,

    [Parameter()]
    [switch]$AllowMeteredRun,

    [Parameter()]
    [switch]$ValidateResumeOnly,

    [Parameter()]
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RunSchema = "lrr-agent003-cli-model-bakeoff-run-v1"
$MutationSchema = "lrr-agent003-cli-model-mutation-audit-v1"
$ExpectedCandidates = @(
    "claude-haiku-4.5",
    "gpt-5-mini",
    "gpt-5.4-mini",
    "gpt-5.6-luna",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "mai-code-1-flash-picker"
)
$ExpectedAvailableTools = @(
    "localragagent003-local_rag_search",
    "localragagent003-local_rag_get_evidence"
)
$ExpectedAllowedTools = @(
    "localragagent003(local_rag_search)",
    "localragagent003(local_rag_get_evidence)"
)
$AggregateCreditCap = 50.0
$PerSessionSoftCap = 30
$BoundaryMarker = "LRR-CLI-LARGE-OUTPUT-TAIL-7F3C9A21"
$HistoricalHarnessExpected = [ordered]@{
    runner = [ordered]@{
        relative_path = "run_lrr_agent003_cli_model_bakeoff.ps1"
        sha256 = "f841047aa991b3ac24fa1478754bb166db4441da0421d4e86a6b99049d29b7ea"
    }
    collector = [ordered]@{
        relative_path = "collect_lrr_agent003_cli_model_bakeoff.py"
        sha256 = "2d61824fdb16bda6b458dc78e9d1ba1f1bedcb147e53a9cd14a676232274e708"
    }
    authority = [ordered]@{
        relative_path = "lrr-agent003-cli-model-bakeoff-v1.json"
        sha256 = "c3a3dfa6ef96d1362f7763b8391c9d9d0b94560ab82e33edc5597cda90024517"
    }
}

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-PathInside {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )
    $candidate = (Get-FullPath -Path $Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $boundary = (Get-FullPath -Path $Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    return $candidate.Equals($boundary, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith(
            $boundary + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
}

function Get-Sha256File {
    param([Parameter(Mandatory)][string]$Path)
    $stream = $null
    $sha = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $sha) { $sha.Dispose() }
    }
}

function Get-Sha256Text {
    param([Parameter(Mandatory)][string]$Value)
    $sha = $null
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        return ([System.BitConverter]::ToString(
            $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Value))
        )).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $sha) { $sha.Dispose() }
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value
    )
    [System.IO.File]::WriteAllText(
        $Path,
        $Value,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][object]$Value
    )
    Write-Utf8NoBom -Path $Path -Value (($Value | ConvertTo-Json -Depth 30) + "`n")
}

function Invoke-NativeProcessToFiles {
    param(
        [Parameter(Mandatory)][string]$FileName,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][string]$StdoutPath,
        [Parameter(Mandatory)][string]$StderrPath,
        [Parameter()][hashtable]$Environment = @{},
        [Parameter()][ValidateRange(0, 86400)][int]$TimeoutSeconds = 0
    )
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $FileName
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $start.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    foreach ($argument in $Arguments) { [void]$start.ArgumentList.Add($argument) }
    foreach ($name in $Environment.Keys) {
        $start.Environment[[string]$name] = [string]$Environment[$name]
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    $startedAt = [DateTimeOffset]::UtcNow
    $timedOut = $false
    $treeTerminated = $false
    try {
        if (-not $process.Start()) { throw "Process did not start: $FileName" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($TimeoutSeconds -gt 0 -and -not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $timedOut = $true
            if (-not $process.HasExited) { $process.Kill($true) }
            if (-not $process.WaitForExit(30000)) {
                throw "Timed-out process tree did not terminate: $FileName"
            }
            $treeTerminated = $true
        }
        elseif ($TimeoutSeconds -eq 0) {
            $process.WaitForExit()
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        Write-Utf8NoBom -Path $StdoutPath -Value $stdout
        Write-Utf8NoBom -Path $StderrPath -Value $stderr
        $finishedAt = [DateTimeOffset]::UtcNow
        return [pscustomobject]@{
            ExitCode = if ($timedOut) { $null } else { $process.ExitCode }
            ProcessId = $process.Id
            StartedAt = $startedAt.ToString("o")
            FinishedAt = $finishedAt.ToString("o")
            ElapsedSeconds = ($finishedAt - $startedAt).TotalSeconds
            StdoutBytes = [System.Text.Encoding]::UTF8.GetByteCount($stdout)
            StderrBytes = [System.Text.Encoding]::UTF8.GetByteCount($stderr)
            TimedOut = $timedOut
            ProcessTreeTerminated = $treeTerminated
        }
    }
    finally {
        $process.Dispose()
    }
}

function Get-PowerShellExecutable {
    $candidate = Join-Path $PSHOME "pwsh.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "PowerShell 7 executable was not found."
    }
    return (Get-FullPath -Path $candidate)
}

function Get-LauncherIdentity {
    param([Parameter(Mandatory)][string]$Launcher)
    $launcherPath = Get-FullPath -Path $Launcher
    if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
        throw "Launcher is missing: $launcherPath"
    }
    $bundleRoot = Split-Path -Parent $launcherPath
    $manifestPath = Join-Path $bundleRoot "owned-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Owned manifest is missing: $manifestPath"
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 |
        ConvertFrom-Json -Depth 30
    if ([int]$manifest.schema -ne 1 -or
        [string]::IsNullOrWhiteSpace([string]$manifest.copilot_home) -or
        [string]::IsNullOrWhiteSpace([string]$manifest.install_root)) {
        throw "Owned manifest identity is invalid: $manifestPath"
    }
    $copilotHome = Get-FullPath -Path ([string]$manifest.copilot_home)
    $installRoot = Get-FullPath -Path ([string]$manifest.install_root)
    $expectedLauncher = Get-FullPath -Path (Join-Path $installRoot "copilot-cli\local-rag-agent003.ps1")
    if (-not $launcherPath.Equals($expectedLauncher, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Launcher does not belong to the manifest install root."
    }
    foreach ($artifact in @($manifest.artifacts)) {
        $root = if ([string]$artifact.root -ceq "copilot_home") { $copilotHome } else { $installRoot }
        $path = Get-FullPath -Path (Join-Path $root ([string]$artifact.path).Replace("/", "\"))
        if (-not (Test-PathInside -Path $path -Root $root) -or
            -not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-Sha256File -Path $path) -cne [string]$artifact.sha256 -or
            (Get-Item -LiteralPath $path).Length -ne [long]$artifact.bytes) {
            throw "Owned artifact is missing or does not match its manifest: $($artifact.root):$($artifact.path)"
        }
    }
    return [pscustomobject]@{
        LauncherPath = $launcherPath
        LauncherSha256 = Get-Sha256File -Path $launcherPath
        ManifestPath = Get-FullPath -Path $manifestPath
        ManifestSha256 = Get-Sha256File -Path $manifestPath
        CopilotHome = $copilotHome
        InstallRoot = $installRoot
    }
}

function Read-Authority {
    param([Parameter(Mandatory)][string]$Path)
    $value = Get-Content -Raw -LiteralPath $Path -Encoding UTF8 |
        ConvertFrom-Json -Depth 30
    if ($value.schema_version -cne "lrr-agent003-cli-model-bakeoff-authority-v1" -or
        @($value.candidate_models).Count -ne 7 -or
        [int]$value.fresh_session_repetitions -ne 3 -or
        [double]$value.aggregate_ai_credit_cap -ne $AggregateCreditCap -or
        [int]$value.per_session_cli_soft_cap -ne $PerSessionSoftCap) {
        throw "Model bakeoff authority is not canonical."
    }
    for ($index = 0; $index -lt $ExpectedCandidates.Count; $index++) {
        if ([string]$value.candidate_models[$index] -cne $ExpectedCandidates[$index]) {
            throw "Model bakeoff candidate authority/order differs at ordinal $($index + 1)."
        }
    }
    if ([string]$value.standard_case.requested_model -cne "auto" -or
        [string]$value.thorough_case.requested_model -cne "auto" -or
        [string]$value.boundary_case.requested_model -cne "auto") {
        throw "Standard, thorough, and boundary cases must use auto."
    }
    return $value
}

function Get-CopilotHelpEvidence {
    param(
        [Parameter(Mandatory)][string]$PowerShell,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][string]$OutputRoot
    )
    $commands = @(Get-Command "copilot" -CommandType Application -All -ErrorAction Stop)
    if ($commands.Count -lt 1) { throw "Copilot CLI was not found." }
    $cliPath = Get-FullPath -Path ([string]$commands[0].Source)
    $versionStdout = Join-Path $OutputRoot "copilot-version.stdout.log"
    $versionStderr = Join-Path $OutputRoot "copilot-version.stderr.log"
    $version = Invoke-NativeProcessToFiles `
        -FileName $PowerShell `
        -Arguments @(
            "-NoProfile", "-NonInteractive", "-Command",
            '& ([System.Environment]::GetEnvironmentVariable(''LRR_BAKEOFF_CLI'', ''Process'')) --version'
        ) `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath $versionStdout `
        -StderrPath $versionStderr `
        -Environment @{ LRR_BAKEOFF_CLI = $cliPath } `
        -TimeoutSeconds 30
    if ($version.TimedOut -or $version.ExitCode -ne 0) {
        throw "Copilot CLI version preflight failed."
    }
    $helpStdout = Join-Path $OutputRoot "copilot-help-config.stdout.log"
    $helpStderr = Join-Path $OutputRoot "copilot-help-config.stderr.log"
    $help = Invoke-NativeProcessToFiles `
        -FileName $PowerShell `
        -Arguments @(
            "-NoProfile", "-NonInteractive", "-Command",
            '& ([System.Environment]::GetEnvironmentVariable(''LRR_BAKEOFF_CLI'', ''Process'')) help config'
        ) `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath $helpStdout `
        -StderrPath $helpStderr `
        -Environment @{ LRR_BAKEOFF_CLI = $cliPath } `
        -TimeoutSeconds 30
    if ($help.TimedOut -or $help.ExitCode -ne 0) {
        throw "Copilot CLI help config preflight failed."
    }
    $helpText = [System.IO.File]::ReadAllText($helpStdout, [System.Text.Encoding]::UTF8)
    $listed = @()
    foreach ($candidate in $ExpectedCandidates) {
        $pattern = '(?<![A-Za-z0-9._/-])' + [regex]::Escape($candidate) + '(?![A-Za-z0-9._/-])'
        if ([regex]::IsMatch($helpText, $pattern)) { $listed += $candidate }
    }
    return [pscustomobject]@{
        CliPath = $cliPath
        CliSha256 = Get-Sha256File -Path $cliPath
        VersionEvidencePath = Get-FullPath -Path $versionStdout
        VersionEvidenceSha256 = Get-Sha256File -Path $versionStdout
        HelpEvidencePath = Get-FullPath -Path $helpStdout
        HelpEvidenceSha256 = Get-Sha256File -Path $helpStdout
        ListedCandidates = @($listed)
    }
}

function New-BoundaryFixtureServerSource {
    return @'
from __future__ import annotations
import json
import sys

MARKER = "LRR-CLI-LARGE-OUTPUT-TAIL-7F3C9A21"
FILLER = "境界検証データ" * 5000
TOOLS = [
    {"name": "local_rag_search", "description": "Read-only boundary search.", "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}, "database": {"type": "string"}}, "required": ["question"], "additionalProperties": False}},
    {"name": "local_rag_get_evidence", "description": "Read-only boundary evidence.", "inputSchema": {"type": "object", "properties": {"result_token": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["result_token", "evidence_ids"], "additionalProperties": False}}
]

def emit(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def packet():
    return {"schema_version": "lrr-agent003-cli-prod-large-output-fixture-v1", "status": "ok", "answerability": "full", "database": "lrr-agent003-cli-prod-boundary-rag", "next_action": "answer_now", "result_token": "fixture-token", "notices": [], "evidence": [{"id": "E1", "title": "Deterministic large-output boundary fixture", "text": FILLER + MARKER}], "tail_marker": MARKER}

for raw in sys.stdin:
    try:
        request = json.loads(raw)
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            emit(request_id, {"protocolVersion": (request.get("params") or {}).get("protocolVersion") or "2024-11-05", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "localragagent003", "version": "fixture-v1"}})
        elif method == "tools/list":
            emit(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "local_rag_search" and not arguments.get("database"):
                value = {"status": "database_required", "candidates": [{"name": "lrr-agent003-cli-prod-boundary-rag", "description": "Deterministic boundary result"}], "instruction": "Routing only; retrieval has not run."}
            else:
                value = packet()
            emit(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": name not in {"local_rag_search", "local_rag_get_evidence"}})
        elif request_id is not None:
            emit(request_id, {})
    except Exception as exc:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": type(exc).__name__}}) + "\n")
        sys.stdout.flush()
'@
}

function New-TemporaryBakeoffInstall {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$SourceRuntimeRoot,
        [Parameter(Mandatory)][string]$NeutralWorkspace,
        [Parameter(Mandatory)][string]$Python,
        [Parameter()][switch]$BoundaryFixture
    )
    $rootPath = Get-FullPath -Path $Root
    if (Test-Path -LiteralPath $rootPath) {
        throw "Temporary install root already exists: $rootPath"
    }
    [void](New-Item -ItemType Directory -Path $rootPath)
    $sourceRoot = Get-FullPath -Path $SourceRuntimeRoot
    $sourceRag = Join-Path $sourceRoot "rag"
    $sourceSetup = Join-Path $sourceRag "query\copilot_cli_setup.py"
    foreach ($source in @($sourceRag, $sourceSetup, (Join-Path $sourceRag "query\.venv\Scripts\python.exe"))) {
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Candidate runtime prerequisite is missing: $source"
        }
    }
    $installRoot = Join-Path $rootPath "install"
    [void](New-Item -ItemType Directory -Path $installRoot)
    # The accepted runtime can contain live, ACL-restricted state under
    # query\run.  It is runtime-managed output, not an install input, and a
    # test snapshot must neither depend on nor copy it.  Copy the immutable
    # runtime tree while omitting only that directory; setup below creates a
    # fresh run tree for this temporary install.
    $installRag = Join-Path $installRoot "rag"
    [void](New-Item -ItemType Directory -Path $installRag)
    foreach ($sourceEntry in Get-ChildItem -LiteralPath $sourceRag -Force) {
        if ($sourceEntry.PSIsContainer -and $sourceEntry.Name -eq "query") {
            $installQuery = Join-Path $installRag "query"
            [void](New-Item -ItemType Directory -Path $installQuery)
            foreach ($queryEntry in Get-ChildItem -LiteralPath $sourceEntry.FullName -Force) {
                if ($queryEntry.PSIsContainer -and $queryEntry.Name -eq "run") {
                    continue
                }
                Copy-Item -LiteralPath $queryEntry.FullName -Destination $installQuery -Recurse
            }
            continue
        }
        Copy-Item -LiteralPath $sourceEntry.FullName -Destination $installRag -Recurse
    }
    $copilotHome = Join-Path $rootPath "copilot-home"
    $profile = Join-Path $rootPath "profile.ps1"
    if ($BoundaryFixture) {
        $server = Join-Path $installRoot "rag\query\mcp_server.py"
        Write-Utf8NoBom -Path $server -Value (New-BoundaryFixtureServerSource)
    }
    $setupStdout = Join-Path $rootPath "setup.stdout.log"
    $setupStderr = Join-Path $rootPath "setup.stderr.log"
    $setup = Invoke-NativeProcessToFiles `
        -FileName $Python `
        -Arguments @(
            "-B", $sourceSetup, "install",
            "--copilot-home", $copilotHome,
            "--install-root", $installRoot,
            "--profile-path", $profile
        ) `
        -WorkingDirectory $NeutralWorkspace `
        -StdoutPath $setupStdout `
        -StderrPath $setupStderr
    if ($setup.ExitCode -ne 0) {
        throw "Temporary bakeoff install failed; inspect $setupStderr"
    }
    $launcher = Join-Path $installRoot "copilot-cli\local-rag-agent003.ps1"
    $identity = Get-LauncherIdentity -Launcher $launcher
    return [pscustomobject]@{
        Launcher = $identity.LauncherPath
        CopilotHome = $identity.CopilotHome
        InstallRoot = $identity.InstallRoot
        BaselineLauncherSha256 = $identity.LauncherSha256
        BaselineManifestSha256 = $identity.ManifestSha256
        BoundaryFixture = [bool]$BoundaryFixture
    }
}

function Set-TemporaryTierModel {
    param(
        [Parameter(Mandatory)][object]$Install,
        [Parameter(Mandatory)][ValidateSet("savings", "standard", "thorough")][string]$Tier,
        [Parameter(Mandatory)][string]$Model,
        [Parameter(Mandatory)][object]$ProductionIdentity,
        [Parameter(Mandatory)][string]$AuditPath
    )
    if (-not (Test-PathInside -Path ([string]$Install.Launcher) -Root ([string]$Install.InstallRoot)) -or
        [string]$Install.InstallRoot -eq [string]$ProductionIdentity.InstallRoot) {
        throw "Test-only model mutation escaped its temporary install."
    }
    $agentName = "local-rag-agent003-$Tier.agent.md"
    $agentPath = Join-Path ([string]$Install.CopilotHome) "agents\$agentName"
    $launcherPath = [string]$Install.Launcher
    $manifestPath = Join-Path ([string]$Install.InstallRoot) "copilot-cli\owned-manifest.json"
    $before = [ordered]@{
        launcher_sha256 = Get-Sha256File -Path $launcherPath
        agent_sha256 = Get-Sha256File -Path $agentPath
        manifest_sha256 = Get-Sha256File -Path $manifestPath
    }
    $launcherText = [System.IO.File]::ReadAllText($launcherPath, [System.Text.Encoding]::UTF8)
    $tierPattern = '(?m)^    ' + [regex]::Escape($Tier) + ' = @\{ Agent = "local-rag-agent003-' + [regex]::Escape($Tier) + '"; Model = "[^"]+" \}$'
    $tierReplacement = '    ' + $Tier + ' = @{ Agent = "local-rag-agent003-' + $Tier + '"; Model = "' + $Model + '" }'
    $matches = [regex]::Matches($launcherText, $tierPattern)
    if ($matches.Count -ne 1) {
        throw "Temporary launcher tier mapping is not uniquely patchable: $Tier"
    }
    $launcherText = [regex]::Replace($launcherText, $tierPattern, $tierReplacement)
    Write-Utf8NoBom -Path $launcherPath -Value $launcherText

    $agentText = [System.IO.File]::ReadAllText($agentPath, [System.Text.Encoding]::UTF8)
    $agentMatches = [regex]::Matches($agentText, '(?m)^model: [^\r\n]+$')
    if ($agentMatches.Count -ne 1) {
        throw "Temporary Agent model field is not uniquely patchable: $agentName"
    }
    $agentText = [regex]::Replace($agentText, '(?m)^model: [^\r\n]+$', "model: $Model")
    Write-Utf8NoBom -Path $agentPath -Value $agentText

    $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 |
        ConvertFrom-Json -Depth 30
    foreach ($entry in @($manifest.artifacts)) {
        $target = if ([string]$entry.root -ceq "copilot_home") {
            Join-Path ([string]$Install.CopilotHome) ([string]$entry.path).Replace("/", "\")
        }
        else {
            Join-Path ([string]$Install.InstallRoot) ([string]$entry.path).Replace("/", "\")
        }
        if ((Get-FullPath -Path $target).Equals($launcherPath, [System.StringComparison]::OrdinalIgnoreCase) -or
            (Get-FullPath -Path $target).Equals((Get-FullPath -Path $agentPath), [System.StringComparison]::OrdinalIgnoreCase)) {
            $entry.sha256 = Get-Sha256File -Path $target
            $entry.bytes = (Get-Item -LiteralPath $target).Length
        }
    }
    Write-JsonFile -Path $manifestPath -Value $manifest
    $afterIdentity = Get-LauncherIdentity -Launcher $launcherPath
    $after = [ordered]@{
        launcher_sha256 = $afterIdentity.LauncherSha256
        agent_sha256 = Get-Sha256File -Path $agentPath
        manifest_sha256 = $afterIdentity.ManifestSha256
    }
    Write-JsonFile -Path $AuditPath -Value ([ordered]@{
        schema_version = $MutationSchema
        purpose = "test-only exact model selection in an isolated temporary install"
        tier = $Tier
        requested_model = $Model
        changed_paths = @($launcherPath, (Get-FullPath -Path $agentPath), $manifestPath)
        before = $before
        after = $after
        production_launcher_path = $ProductionIdentity.LauncherPath
        production_launcher_sha256 = $ProductionIdentity.LauncherSha256
        production_manifest_path = $ProductionIdentity.ManifestPath
        production_manifest_sha256 = $ProductionIdentity.ManifestSha256
        production_artifacts_modified = $false
    })
    return $afterIdentity
}

function Invoke-CollectorSnapshot {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Collector,
        [Parameter(Mandatory)][string]$CopilotHome,
        [Parameter(Mandatory)][string]$Output,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($Output)
    $collectorArguments = @(
        "-B", $Collector,
        "--snapshot-copilot-home", $CopilotHome,
        "--snapshot-output", $Output
    )
    if (-not [string]::IsNullOrWhiteSpace($script:ExpectedRecoveryImportSha256)) {
        $collectorArguments += @(
            "--raw-root", $script:EvidenceOutputRoot,
            "--expected-recovery-import-sha256", $script:ExpectedRecoveryImportSha256
        )
    }
    $result = Invoke-NativeProcessToFiles `
        -FileName $Python `
        -Arguments $collectorArguments `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath (Join-Path (Split-Path -Parent $Output) "$stem.stdout.log") `
        -StderrPath (Join-Path (Split-Path -Parent $Output) "$stem.stderr.log")
    if ($result.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $Output -PathType Leaf)) {
        throw "Session usage snapshot failed: $Output"
    }
}

function Invoke-AggregateCollector {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Collector,
        [Parameter(Mandatory)][string]$Authority,
        [Parameter(Mandatory)][string]$RawRoot,
        [Parameter(Mandatory)][string]$Output,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($Output)
    $collectorArguments = @(
        "-B", $Collector,
        "--authority", $Authority,
        "--raw-root", $RawRoot,
        "--output", $Output
    )
    if (-not [string]::IsNullOrWhiteSpace($script:ExpectedRecoveryImportSha256)) {
        $collectorArguments += @(
            "--expected-recovery-import-sha256", $script:ExpectedRecoveryImportSha256
        )
    }
    $result = Invoke-NativeProcessToFiles `
        -FileName $Python `
        -Arguments $collectorArguments `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath (Join-Path (Split-Path -Parent $Output) "$stem.stdout.log") `
        -StderrPath (Join-Path (Split-Path -Parent $Output) "$stem.stderr.log")
    if (-not (Test-Path -LiteralPath $Output -PathType Leaf)) {
        throw "Aggregate collector produced no report: $Output"
    }
    $report = Get-Content -Raw -LiteralPath $Output -Encoding UTF8 |
        ConvertFrom-Json -Depth 40
    if ($result.ExitCode -ne 0 -or [string]$report.overall_status -ceq "STOP_CREDIT_OR_EVIDENCE") {
        throw "Aggregate Credit/evidence gate stopped; inspect $Output"
    }
    return $report
}

function New-RunDirectory {
    param(
        [Parameter(Mandatory)][string]$RunsRoot,
        [Parameter(Mandatory)][int]$Ordinal,
        [Parameter(Mandatory)][string]$RunId
    )
    $safe = $RunId -replace '[^A-Za-z0-9._-]', '_'
    $path = Join-Path $RunsRoot ("{0:D2}-{1}" -f $Ordinal, $safe)
    if (Test-Path -LiteralPath $path) { throw "Run evidence already exists: $path" }
    [void](New-Item -ItemType Directory -Path $path)
    return $path
}

function Add-SkippedRun {
    param(
        [Parameter(Mandatory)][string]$RunsRoot,
        [Parameter(Mandatory)][int]$Ordinal,
        [Parameter(Mandatory)][string]$RunId,
        [Parameter(Mandatory)][string]$Candidate,
        [Parameter(Mandatory)][int]$Attempt,
        [Parameter(Mandatory)][string]$Prompt,
        [Parameter(Mandatory)][ValidateSet("skipped_not_help_listed", "skipped_policy_preinference")][string]$State,
        [Parameter()][string]$ReasonRunId
    )
    $root = New-RunDirectory -RunsRoot $RunsRoot -Ordinal $Ordinal -RunId $RunId
    Write-JsonFile -Path (Join-Path $root "run.json") -Value ([ordered]@{
        schema_version = $RunSchema
        plan_ordinal = $Ordinal
        run_id = $RunId
        case_kind = "savings"
        candidate_model = $Candidate
        requested_model = $Candidate
        attempt = $Attempt
        execution_state = $State
        prompt_sha256 = Get-Sha256Text -Value $Prompt
        runner_path = $script:RunnerIdentityPath
        runner_sha256 = $script:RunnerIdentitySha256
        collector_path = $script:CollectorIdentityPath
        collector_sha256 = $script:CollectorIdentitySha256
        authority_path = $script:AuthorityIdentityPath
        authority_sha256 = $script:AuthorityIdentitySha256
        skip_reason_run_id = $ReasonRunId
        ai_inference_invoked = $false
        fresh_session = $false
        retry_count = 0
    })
}

function Get-LatestAggregateCredits {
    param([object]$Report)
    if ($null -eq $Report) { return 0.0 }
    if ($Report.credit_observable -ne $true) {
        throw "Aggregate Credit is unknown; stopping."
    }
    $properties = @($Report.PSObject.Properties.Name)
    if ($properties -contains "true_total_ai_credits" -and
        $null -ne $Report.true_total_ai_credits) {
        return [double]$Report.true_total_ai_credits
    }
    if ($null -eq $Report.aggregate_ai_credits) {
        throw "Aggregate Credit is unknown; stopping."
    }
    return [double]$Report.aggregate_ai_credits
}

function Get-ExpectedSavingsRunId {
    param([Parameter(Mandatory)][ValidateRange(1, 21)][int]$Ordinal)
    $candidateOrdinal = [int][System.Math]::Floor(($Ordinal - 1) / 3) + 1
    $attempt = (($Ordinal - 1) % 3) + 1
    return "LRR-AGENT003-CLI-MODEL-SAVINGS-C{0:D2}-R{1}" -f `
        $candidateOrdinal, $attempt
}

function Get-ResumePendingOrdinals {
    param(
        [Parameter(Mandatory)][ValidateSet(0, 5)][int]$CompletedOrdinal,
        [Parameter()][ValidateRange(1, 24)][int]$FinalOrdinal = 24
    )
    if ($CompletedOrdinal -gt $FinalOrdinal) {
        throw "Resume completed ordinal exceeds final ordinal."
    }
    if ($CompletedOrdinal -eq $FinalOrdinal) { return @() }
    return @(($CompletedOrdinal + 1)..$FinalOrdinal)
}

function Get-FileAuthority {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$File
    )
    $rootPath = Get-FullPath -Path $Root
    $filePath = Get-FullPath -Path $File
    if (-not (Test-PathInside -Path $filePath -Root $rootPath) -or
        -not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        throw "Recovery file escapes its evidence root or is missing: $filePath"
    }
    return [ordered]@{
        relative_path = [System.IO.Path]::GetRelativePath($rootPath, $filePath).Replace("\", "/")
        sha256 = Get-Sha256File -Path $filePath
        bytes = (Get-Item -LiteralPath $filePath).Length
    }
}

function Copy-RecoveryDirectoryWithAuthority {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$DestinationParent
    )
    $sourcePath = Get-FullPath -Path $Source
    $destinationParentPath = Get-FullPath -Path $DestinationParent
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Container) -or
        -not (Test-Path -LiteralPath $destinationParentPath -PathType Container)) {
        throw "Recovery copy source or destination parent is missing."
    }
    foreach ($item in @(Get-ChildItem -LiteralPath $sourcePath -Recurse -Force)) {
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "Recovery evidence crosses a reparse point: $($item.FullName)"
        }
    }
    $destination = Join-Path $destinationParentPath (Split-Path -Leaf $sourcePath)
    if (Test-Path -LiteralPath $destination) {
        throw "Recovery destination already exists: $destination"
    }
    Copy-Item -LiteralPath $sourcePath -Destination $destinationParentPath -Recurse -Force
    $destination = Get-FullPath -Path $destination
    $files = @()
    foreach ($sourceFile in @(
        Get-ChildItem -LiteralPath $sourcePath -Recurse -File -Force |
            Sort-Object FullName
    )) {
        $relative = [System.IO.Path]::GetRelativePath($sourcePath, $sourceFile.FullName)
        $destinationFile = Join-Path $destination $relative
        if (-not (Test-Path -LiteralPath $destinationFile -PathType Leaf) -or
            (Get-Item -LiteralPath $destinationFile).Length -ne $sourceFile.Length -or
            (Get-Sha256File -Path $destinationFile) -cne (Get-Sha256File -Path $sourceFile.FullName)) {
            throw "Recovery evidence copy mismatch: $relative"
        }
        $files += ,[ordered]@{
            relative_path = $relative.Replace("\", "/")
            sha256 = Get-Sha256File -Path $sourceFile.FullName
            bytes = $sourceFile.Length
        }
    }
    if ($files.Count -lt 1) { throw "Recovery evidence directory is empty: $sourcePath" }
    return [pscustomobject]@{
        Source = $sourcePath
        Destination = $destination
        Files = @($files)
    }
}

function Assert-ImportedReportMatchesSource {
    param(
        [Parameter(Mandatory)][object]$SourceReport,
        [Parameter(Mandatory)][object]$ImportedReport,
        [Parameter(Mandatory)][int]$CompletedOrdinal
    )
    foreach ($field in @(
        "schema_version", "authority_sha256", "credit_epoch",
        "aggregate_ai_credit_cap", "aggregate_total_nano_aiu",
        "aggregate_ai_credits", "credit_observable", "overall_status",
        "all_savings_candidates_unavailable", "forbid_auto_fallback",
        "candidate_summaries", "ranking_contract", "ranking", "winner",
        "auxiliary_status", "runs"
    )) {
        $sourceProperty = $SourceReport.PSObject.Properties[$field]
        $importedProperty = $ImportedReport.PSObject.Properties[$field]
        if ($null -eq $sourceProperty -or $null -eq $importedProperty) {
            throw "Resume report comparison field is missing: $field"
        }
        $sourceJson = $sourceProperty.Value | ConvertTo-Json -Depth 50 -Compress
        $importedJson = $importedProperty.Value | ConvertTo-Json -Depth 50 -Compress
        if ($sourceJson -cne $importedJson) {
            throw "Imported formal prefix differs from source report at field: $field"
        }
    }
    if (@($ImportedReport.runs).Count -ne $CompletedOrdinal) {
        throw "Imported collector report does not contain the exact completed prefix."
    }
}

function New-RecoveryImport {
    param(
        [Parameter(Mandatory)][string]$SourceRoot,
        [Parameter(Mandatory)][string]$OutputRoot,
        [Parameter(Mandatory)][string]$RunsRoot,
        [Parameter(Mandatory)][string]$ReportsRoot,
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Collector,
        [Parameter(Mandatory)][string]$Authority,
        [Parameter(Mandatory)][string]$HistoricalHarnessArchive,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][object]$ProductionIdentity,
        [Parameter(Mandatory)][ValidateSet(5)][int]$CompletedOrdinal
    )
    $source = Get-FullPath -Path $SourceRoot
    $output = Get-FullPath -Path $OutputRoot
    $historicalArchive = Get-FullPath -Path $HistoricalHarnessArchive
    if (-not (Test-Path -LiteralPath $source -PathType Container) -or
        $source.Equals($output, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Test-PathInside -Path $source -Root $output) -or
        (Test-PathInside -Path $output -Root $source)) {
        throw "ResumeEvidenceRoot is missing or aliases OutputRoot."
    }
    if (-not (Test-Path -LiteralPath $historicalArchive -PathType Container) -or
        (Get-Item -LiteralPath $historicalArchive).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Historical harness archive is missing or is a reparse point."
    }
    $sourceRuns = Join-Path $source "runs"
    $sourceReportPath = Join-Path $source ("reports\report-{0:D2}.json" -f $CompletedOrdinal)
    if (-not (Test-Path -LiteralPath $sourceRuns -PathType Container) -or
        -not (Test-Path -LiteralPath $sourceReportPath -PathType Leaf)) {
        throw "Resume source formal runs or report-$CompletedOrdinal are missing."
    }
    $sourceReport = Get-Content -Raw -LiteralPath $sourceReportPath -Encoding UTF8 |
        ConvertFrom-Json -Depth 50
    if ($sourceReport.schema_version -cne "lrr-agent003-cli-model-bakeoff-report-v1" -or
        @($sourceReport.runs).Count -ne $CompletedOrdinal) {
        throw "Resume source report is not the exact completed formal prefix."
    }
    $importedRuns = @()
    $historicalIdentity = $null
    for ($ordinal = 1; $ordinal -le $CompletedOrdinal; $ordinal++) {
        $expectedRunId = Get-ExpectedSavingsRunId -Ordinal $ordinal
        $expectedLeaf = "{0:D2}-{1}" -f $ordinal, $expectedRunId
        $sourceDirectory = Join-Path $sourceRuns $expectedLeaf
        if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) {
            throw "Resume source run directory is missing: $expectedLeaf"
        }
        $runPath = Join-Path $sourceDirectory "run.json"
        if (-not (Test-Path -LiteralPath $runPath -PathType Leaf)) {
            throw "Resume source run metadata is missing: $expectedLeaf"
        }
        $run = Get-Content -Raw -LiteralPath $runPath -Encoding UTF8 |
            ConvertFrom-Json -Depth 40
        if ([int]$run.plan_ordinal -ne $ordinal -or [string]$run.run_id -cne $expectedRunId) {
            throw "Resume source run identity/order is invalid: $expectedLeaf"
        }
        $identity = [ordered]@{
            runner_path = [string]$run.runner_path
            runner_sha256 = [string]$run.runner_sha256
            collector_path = [string]$run.collector_path
            collector_sha256 = [string]$run.collector_sha256
            authority_path = [string]$run.authority_path
            authority_sha256 = [string]$run.authority_sha256
        }
        if ($null -eq $historicalIdentity) {
            $historicalIdentity = $identity
        }
        elseif (($identity | ConvertTo-Json -Compress) -cne ($historicalIdentity | ConvertTo-Json -Compress)) {
            throw "Resume source formal runs do not share one harness identity."
        }
        $copied = Copy-RecoveryDirectoryWithAuthority `
            -Source $sourceDirectory `
            -DestinationParent $RunsRoot
        $importedRuns += ,[ordered]@{
            ordinal = $ordinal
            run_id = $expectedRunId
            source_directory = $copied.Source
            destination_directory = $copied.Destination
            files = @($copied.Files)
        }
    }

    $historicalArchiveFiles = @(
        Get-ChildItem -LiteralPath $historicalArchive -Recurse -File -Force |
            Sort-Object FullName
    )
    $historicalArchiveDirectories = @(
        Get-ChildItem -LiteralPath $historicalArchive -Recurse -Directory -Force
    )
    if ($historicalArchiveFiles.Count -ne 3 -or $historicalArchiveDirectories.Count -ne 0) {
        throw "Historical harness archive must contain exactly three top-level files."
    }
    foreach ($kind in @("runner", "collector", "authority")) {
        $expected = $HistoricalHarnessExpected[$kind]
        $matches = @($historicalArchiveFiles | Where-Object {
            $_.Name -ceq [string]$expected.relative_path -and
            $_.DirectoryName -ceq $historicalArchive
        })
        $shaProperty = "${kind}_sha256"
        if ($matches.Count -ne 1 -or
            (Get-Sha256File -Path $matches[0].FullName) -cne [string]$expected.sha256 -or
            [string]$historicalIdentity[$shaProperty] -cne [string]$expected.sha256) {
            throw "Historical harness $kind bytes do not match the imported r3 identity."
        }
    }

    $abortedOrdinal = $CompletedOrdinal + 1
    $abortedRunId = Get-ExpectedSavingsRunId -Ordinal $abortedOrdinal
    $abortedLeaf = "{0:D2}-{1}" -f $abortedOrdinal, $abortedRunId
    $abortedSource = Join-Path $sourceRuns $abortedLeaf
    if (-not (Test-Path -LiteralPath $abortedSource -PathType Container)) {
        throw "Resume aborted attempt directory is missing: $abortedLeaf"
    }
    foreach ($forbidden in @("run.json", "copilot.jsonl")) {
        if (Test-Path -LiteralPath (Join-Path $abortedSource $forbidden)) {
            throw "Aborted attempt unexpectedly contains formal evidence: $forbidden"
        }
    }
    $beforePath = Join-Path $abortedSource "usage-before.json"
    $afterPath = Join-Path $abortedSource "usage-after-recovered.json"
    foreach ($snapshot in @($beforePath, $afterPath)) {
        if (-not (Test-Path -LiteralPath $snapshot -PathType Leaf)) {
            throw "Aborted attempt recovery snapshot is missing: $snapshot"
        }
    }
    $before = Get-Content -Raw -LiteralPath $beforePath -Encoding UTF8 |
        ConvertFrom-Json -Depth 20
    $after = Get-Content -Raw -LiteralPath $afterPath -Encoding UTF8 |
        ConvertFrom-Json -Depth 20
    if ($before.schema_version -cne "lrr-agent003-cli-session-usage-snapshot-v1" -or
        $after.schema_version -cne "lrr-agent003-cli-session-usage-snapshot-v1" -or
        [string]$before.copilot_home -cne [string]$after.copilot_home) {
        throw "Aborted attempt recovery snapshots are not comparable."
    }
    $rowDelta = [long]$after.row_count - [long]$before.row_count
    $nanoDelta = [long]$after.total_nano_aiu - [long]$before.total_nano_aiu
    if ($rowDelta -lt 0 -or $nanoDelta -lt 0 -or $nanoDelta -ne 283155000) {
        throw "Aborted attempt Credit delta is invalid; expected exact 0.283155 AI Credits."
    }
    $recoveryRoot = Join-Path $output "recovery"
    [void](New-Item -ItemType Directory -Path $recoveryRoot)
    $historicalCopied = Copy-RecoveryDirectoryWithAuthority `
        -Source $historicalArchive `
        -DestinationParent $recoveryRoot
    $historicalArtifacts = @()
    foreach ($kind in @("runner", "collector", "authority")) {
        $expected = $HistoricalHarnessExpected[$kind]
        $fileAuthority = @($historicalCopied.Files | Where-Object {
            [string]$_.relative_path -ceq [string]$expected.relative_path
        })
        $pathProperty = "${kind}_path"
        if ($fileAuthority.Count -ne 1 -or
            [string]$fileAuthority[0].sha256 -cne [string]$expected.sha256) {
            throw "Historical harness copy authority is invalid for $kind."
        }
        $historicalArtifacts += ,[ordered]@{
            kind = $kind
            relative_path = [string]$fileAuthority[0].relative_path
            original_path = [string]$historicalIdentity[$pathProperty]
            sha256 = [string]$fileAuthority[0].sha256
            bytes = [long]$fileAuthority[0].bytes
        }
    }
    $abortedCopied = Copy-RecoveryDirectoryWithAuthority `
        -Source $abortedSource `
        -DestinationParent $recoveryRoot
    $sourceReportCopy = Join-Path $recoveryRoot "source-report-05.json"
    Copy-Item -LiteralPath $sourceReportPath -Destination $sourceReportCopy
    if ((Get-Sha256File -Path $sourceReportCopy) -cne (Get-Sha256File -Path $sourceReportPath)) {
        throw "Resume source report preservation copy mismatch."
    }
    $manifestPath = Join-Path $output "recovery-import.json"
    Write-JsonFile -Path $manifestPath -Value ([ordered]@{
        schema_version = "lrr-agent003-cli-model-bakeoff-recovery-import-v1"
        source_evidence_root = $source
        output_root = $output
        resume_completed_ordinal = $CompletedOrdinal
        source_report_path = Get-FullPath -Path $sourceReportPath
        source_report_sha256 = Get-Sha256File -Path $sourceReportPath
        preserved_source_report_path = Get-FullPath -Path $sourceReportCopy
        historical_harness_identity = $historicalIdentity
        historical_harness_source_directory = $historicalCopied.Source
        historical_harness_preserved_directory = $historicalCopied.Destination
        historical_harness_files = @($historicalCopied.Files)
        historical_harness_artifacts = @($historicalArtifacts)
        resume_harness_identity = [ordered]@{
            runner_path = $script:RunnerIdentityPath
            runner_sha256 = $script:RunnerIdentitySha256
            collector_path = $script:CollectorIdentityPath
            collector_sha256 = $script:CollectorIdentitySha256
            authority_path = $script:AuthorityIdentityPath
            authority_sha256 = $script:AuthorityIdentitySha256
        }
        production_identity = [ordered]@{
            launcher_path = $ProductionIdentity.LauncherPath
            launcher_sha256 = $ProductionIdentity.LauncherSha256
            manifest_path = $ProductionIdentity.ManifestPath
            manifest_sha256 = $ProductionIdentity.ManifestSha256
        }
        imported_runs = @($importedRuns)
        aborted_attempt = [ordered]@{
            ordinal = $abortedOrdinal
            run_id = $abortedRunId
            classification = "ABORTED_INCOMPLETE_NOT_A_FORMAL_RUN"
            incomplete_session_state = $true
            source_directory = $abortedCopied.Source
            preserved_directory = $abortedCopied.Destination
            required_absent_files = @("run.json", "copilot.jsonl")
            files = @($abortedCopied.Files)
            usage_before_sha256 = Get-Sha256File -Path $beforePath
            usage_after_recovered_sha256 = Get-Sha256File -Path $afterPath
            usage_row_delta = $rowDelta
            recovery_total_nano_aiu = $nanoDelta
            recovery_ai_credits = $nanoDelta / 1000000000.0
        }
    })
    $script:ExpectedRecoveryImportSha256 = Get-Sha256File -Path $manifestPath
    $revalidatedReportPath = Join-Path $ReportsRoot (
        "report-{0:D2}.json" -f $CompletedOrdinal
    )
    $revalidated = Invoke-AggregateCollector `
        -Python $Python `
        -Collector $Collector `
        -Authority $Authority `
        -RawRoot $output `
        -Output $revalidatedReportPath `
        -WorkingDirectory $WorkingDirectory
    Assert-ImportedReportMatchesSource `
        -SourceReport $sourceReport `
        -ImportedReport $revalidated `
        -CompletedOrdinal $CompletedOrdinal
    return [pscustomobject]@{
        ManifestPath = Get-FullPath -Path $manifestPath
        ManifestSha256 = Get-Sha256File -Path $manifestPath
        RecoveryAiCredits = $nanoDelta / 1000000000.0
        RecoveryTotalNanoAiu = $nanoDelta
        ImportedReport = $revalidated
        SourceReportPath = Get-FullPath -Path $sourceReportPath
        SourceReportSha256 = Get-Sha256File -Path $sourceReportPath
    }
}

function Invoke-MeteredCase {
    param(
        [Parameter(Mandatory)][string]$RunsRoot,
        [Parameter(Mandatory)][int]$Ordinal,
        [Parameter(Mandatory)][string]$RunId,
        [Parameter(Mandatory)][ValidateSet("savings", "standard", "thorough", "boundary")][string]$CaseKind,
        [Parameter()][string]$Candidate,
        [Parameter(Mandatory)][int]$Attempt,
        [Parameter(Mandatory)][object]$Case,
        [Parameter(Mandatory)][object]$Install,
        [Parameter(Mandatory)][object]$ProductionIdentity,
        [Parameter(Mandatory)][object]$CliEvidence,
        [Parameter(Mandatory)][string]$PowerShell,
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Collector,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )
    $requestedModel = if ($CaseKind -eq "savings") { $Candidate } else { "auto" }
    $tier = [string]$Case.tier
    $root = New-RunDirectory -RunsRoot $RunsRoot -Ordinal $Ordinal -RunId $RunId
    $logs = Join-Path $root "copilot-logs"
    [void](New-Item -ItemType Directory -Path $logs)
    $mutationPath = Join-Path $root "temporary-model-mutation.json"
    $identity = Set-TemporaryTierModel `
        -Install $Install `
        -Tier $tier `
        -Model $requestedModel `
        -ProductionIdentity $ProductionIdentity `
        -AuditPath $mutationPath
    $beforePath = Join-Path $root "usage-before.json"
    Invoke-CollectorSnapshot `
        -Python $Python `
        -Collector $Collector `
        -CopilotHome $identity.CopilotHome `
        -Output $beforePath `
        -WorkingDirectory $WorkingDirectory
    $stdout = Join-Path $root "copilot.jsonl"
    $stderr = Join-Path $root "stderr.log"
    $otel = Join-Path $root "otel.jsonl"
    $copilotArgs = @(
        "--prompt", [string]$Case.prompt,
        "--output-format", "json",
        "--stream", "off",
        "--max-ai-credits", [string]$PerSessionSoftCap,
        "--no-auto-update",
        "--no-ask-user",
        "--no-remote",
        "--no-remote-export",
        "--log-dir", $logs
    )
    $result = Invoke-NativeProcessToFiles `
        -FileName $PowerShell `
        -Arguments (@(
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", $identity.LauncherPath, "-Tier", $tier
        ) + $copilotArgs) `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath $stdout `
        -StderrPath $stderr `
        -Environment @{
            COPILOT_OTEL_ENABLED = "true"
            COPILOT_OTEL_EXPORTER_TYPE = "file"
            COPILOT_OTEL_FILE_EXPORTER_PATH = $otel
            COPILOT_OTEL_SOURCE_NAME = "lrr-agent003-cli-model-bakeoff"
            OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = "false"
        } `
        -TimeoutSeconds $TimeoutSeconds
    if (-not (Test-Path -LiteralPath $otel -PathType Leaf)) {
        Write-Utf8NoBom -Path $otel -Value ""
    }
    $afterPath = Join-Path $root "usage-after.json"
    Invoke-CollectorSnapshot `
        -Python $Python `
        -Collector $Collector `
        -CopilotHome $identity.CopilotHome `
        -Output $afterPath `
        -WorkingDirectory $WorkingDirectory
    Write-JsonFile -Path (Join-Path $root "run.json") -Value ([ordered]@{
        schema_version = $RunSchema
        plan_ordinal = $Ordinal
        run_id = $RunId
        case_kind = $CaseKind
        candidate_model = $Candidate
        requested_model = $requestedModel
        attempt = $Attempt
        execution_state = "executed"
        help_listed = if ($CaseKind -eq "savings") { $Candidate -cin $CliEvidence.ListedCandidates } else { $true }
        help_evidence_path = $CliEvidence.HelpEvidencePath
        help_evidence_sha256 = $CliEvidence.HelpEvidenceSha256
        runner_path = $script:RunnerIdentityPath
        runner_sha256 = $script:RunnerIdentitySha256
        collector_path = $script:CollectorIdentityPath
        collector_sha256 = $script:CollectorIdentitySha256
        authority_path = $script:AuthorityIdentityPath
        authority_sha256 = $script:AuthorityIdentitySha256
        prompt_sha256 = Get-Sha256Text -Value ([string]$Case.prompt)
        fresh_session = $true
        retry_count = 0
        max_ai_credits = $PerSessionSoftCap
        aggregate_credit_cap = $AggregateCreditCap
        minimum_remaining_credit_before_launch = [double]$Case.minimum_remaining_credit_before_launch
        copilot_home = $identity.CopilotHome
        launcher_path = $identity.LauncherPath
        launcher_sha256 = $identity.LauncherSha256
        launcher_manifest_path = $identity.ManifestPath
        launcher_manifest_sha256 = $identity.ManifestSha256
        cli_path = $CliEvidence.CliPath
        cli_sha256 = $CliEvidence.CliSha256
        cli_version_evidence_path = $CliEvidence.VersionEvidencePath
        cli_version_evidence_sha256 = $CliEvidence.VersionEvidenceSha256
        noninteractive_permission_contract = [ordered]@{
            available_tools = @($ExpectedAvailableTools)
            allow_tools = @($ExpectedAllowedTools)
            no_custom_instructions = $true
            no_ask_user = $true
            output_format = "json"
            stream = "off"
            no_auto_update = $true
            no_remote = $true
            no_remote_export = $true
        }
        process_id = $result.ProcessId
        started_at = $result.StartedAt
        finished_at = $result.FinishedAt
        elapsed_seconds = $result.ElapsedSeconds
        exit_code = $result.ExitCode
        timeout_seconds = $TimeoutSeconds
        timed_out = $result.TimedOut
        process_tree_terminated = $result.ProcessTreeTerminated
        stdout_bytes = $result.StdoutBytes
        stderr_bytes = $result.StderrBytes
    })
    if ($result.TimedOut) {
        throw "Run timed out and was terminated without retry: $RunId"
    }
    return $root
}

$AuthorityPath = Get-FullPath -Path $AuthorityPath
$CollectorPath = Get-FullPath -Path $CollectorPath
$HistoricalHarnessArchiveRoot = Get-FullPath -Path $HistoricalHarnessArchiveRoot
if (-not (Test-Path -LiteralPath $AuthorityPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $CollectorPath -PathType Leaf)) {
    throw "Authority or collector is missing."
}
$authority = Read-Authority -Path $AuthorityPath
$script:RunnerIdentityPath = Get-FullPath -Path $PSCommandPath
$script:RunnerIdentitySha256 = Get-Sha256File -Path $script:RunnerIdentityPath
$script:CollectorIdentityPath = $CollectorPath
$script:CollectorIdentitySha256 = Get-Sha256File -Path $CollectorPath
$script:AuthorityIdentityPath = $AuthorityPath
$script:AuthorityIdentitySha256 = Get-Sha256File -Path $AuthorityPath
$script:ExpectedRecoveryImportSha256 = ""
$script:EvidenceOutputRoot = ""

if ($SelfTest) {
    if (-not (Test-Path -LiteralPath $CollectorPython -PathType Leaf)) {
        $fallback = Get-Command "python" -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $fallback) { throw "Python is unavailable for self-test." }
        $CollectorPython = [string]$fallback.Source
    }
    $selfTestRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
        "lrr-agent003-cli-model-bakeoff-selftest-" + [guid]::NewGuid().ToString("N")
    )
    [void](New-Item -ItemType Directory -Path $selfTestRoot)
    try {
        $result = Invoke-NativeProcessToFiles `
            -FileName (Get-FullPath -Path $CollectorPython) `
            -Arguments @("-B", $CollectorPath, "--authority", $AuthorityPath, "--self-test") `
            -WorkingDirectory $selfTestRoot `
            -StdoutPath (Join-Path $selfTestRoot "collector.stdout.log") `
            -StderrPath (Join-Path $selfTestRoot "collector.stderr.log")
        if ($result.ExitCode -ne 0) {
            throw "Collector self-test failed: $selfTestRoot"
        }
        $fixture = Join-Path $selfTestRoot "boundary_fixture.py"
        Write-Utf8NoBom -Path $fixture -Value (New-BoundaryFixtureServerSource)
        $syntax = Invoke-NativeProcessToFiles `
            -FileName (Get-FullPath -Path $CollectorPython) `
            -Arguments @(
                "-B", "-c",
                "import ast,pathlib,sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))",
                $fixture
            ) `
            -WorkingDirectory $selfTestRoot `
            -StdoutPath (Join-Path $selfTestRoot "fixture.stdout.log") `
            -StderrPath (Join-Path $selfTestRoot "fixture.stderr.log")
        if ($syntax.ExitCode -ne 0) {
            throw "Boundary fixture syntax self-test failed: $selfTestRoot"
        }
        $normalPlan = @(Get-ResumePendingOrdinals -CompletedOrdinal 0)
        $resumePlan = @(Get-ResumePendingOrdinals -CompletedOrdinal 5)
        if ($normalPlan.Count -ne 24 -or $normalPlan[0] -ne 1 -or
            $normalPlan[-1] -ne 24 -or $resumePlan.Count -ne 19 -or
            $resumePlan[0] -ne 6 -or $resumePlan[-1] -ne 24) {
            throw "Resume plan self-test failed; formal ordinals could be resent or skipped."
        }
        $archiveFiles = @(
            Get-ChildItem -LiteralPath $HistoricalHarnessArchiveRoot -File -Force
        )
        if ($archiveFiles.Count -ne 3 -or
            @(Get-ChildItem -LiteralPath $HistoricalHarnessArchiveRoot -Directory -Force).Count -ne 0) {
            throw "Historical harness archive self-test found an extra/missing entry."
        }
        foreach ($kind in @("runner", "collector", "authority")) {
            $expected = $HistoricalHarnessExpected[$kind]
            $match = @($archiveFiles | Where-Object { $_.Name -ceq [string]$expected.relative_path })
            if ($match.Count -ne 1 -or
                (Get-Sha256File -Path $match[0].FullName) -cne [string]$expected.sha256) {
                throw "Historical harness archive self-test failed for $kind."
            }
        }
        Write-Output "PASS: model bakeoff authority, collector, ranking, boundary fixture, and resume-plan self-tests"
    }
    finally {
        if (Test-Path -LiteralPath $selfTestRoot -PathType Container) {
            [System.IO.Directory]::Delete($selfTestRoot, $true)
        }
    }
    exit 0
}

if (-not $AllowMeteredRun -and -not $ValidateResumeOnly) {
    throw "Metered UAT is disabled by default. Re-run with -AllowMeteredRun after the zero-Credit preflight."
}
$resumeRequested = -not [string]::IsNullOrWhiteSpace($ResumeEvidenceRoot)
if (($resumeRequested -and $ResumeCompletedOrdinal -ne 5) -or
    (-not $resumeRequested -and $ResumeCompletedOrdinal -ne 0)) {
    throw "ResumeEvidenceRoot and ResumeCompletedOrdinal=5 must be supplied together."
}
if ($ValidateResumeOnly -and -not $resumeRequested) {
    throw "ValidateResumeOnly requires a resume source and completed ordinal 5."
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    throw "-OutputRoot is required for metered UAT."
}
$OutputRoot = Get-FullPath -Path $OutputRoot
$script:EvidenceOutputRoot = $OutputRoot
$repoRoot = Get-FullPath -Path (Join-Path $PSScriptRoot "..\..\..")
if (Test-PathInside -Path $OutputRoot -Root $repoRoot) {
    throw "Raw model bakeoff output must be outside the repository."
}
if (Test-Path -LiteralPath $OutputRoot) {
    if (@(Get-ChildItem -LiteralPath $OutputRoot -Force).Count -ne 0) {
        throw "OutputRoot must be absent or empty; raw evidence is never overwritten."
    }
}
else {
    [void](New-Item -ItemType Directory -Path $OutputRoot)
}
$runsRoot = Join-Path $OutputRoot "runs"
$reportsRoot = Join-Path $OutputRoot "reports"
$neutralWorkspace = Join-Path $OutputRoot "neutral-workspace"
[void](New-Item -ItemType Directory -Path $runsRoot)
[void](New-Item -ItemType Directory -Path $reportsRoot)
[void](New-Item -ItemType Directory -Path $neutralWorkspace)

$CollectorPython = Get-FullPath -Path $CollectorPython
$CandidateRuntimeRoot = Get-FullPath -Path $CandidateRuntimeRoot
$ProductionLauncherPath = Get-FullPath -Path $ProductionLauncherPath
foreach ($path in @($CollectorPython, $CandidateRuntimeRoot, $ProductionLauncherPath)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required path is missing: $path" }
}
$powerShell = Get-PowerShellExecutable
$productionIdentity = Get-LauncherIdentity -Launcher $ProductionLauncherPath
if (-not $productionIdentity.InstallRoot.Equals(
    $CandidateRuntimeRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Production launcher manifest does not match CandidateRuntimeRoot."
}
$cliEvidence = Get-CopilotHelpEvidence `
    -PowerShell $powerShell `
    -WorkingDirectory $neutralWorkspace `
    -OutputRoot $OutputRoot
$resumeState = $null
$latestReport = $null
if ($resumeRequested) {
    $ResumeEvidenceRoot = Get-FullPath -Path $ResumeEvidenceRoot
    $resumeState = New-RecoveryImport `
        -SourceRoot $ResumeEvidenceRoot `
        -OutputRoot $OutputRoot `
        -RunsRoot $runsRoot `
        -ReportsRoot $reportsRoot `
        -Python $CollectorPython `
        -Collector $CollectorPath `
        -Authority $AuthorityPath `
        -HistoricalHarnessArchive $HistoricalHarnessArchiveRoot `
        -WorkingDirectory $neutralWorkspace `
        -ProductionIdentity $productionIdentity `
        -CompletedOrdinal $ResumeCompletedOrdinal
    $latestReport = $resumeState.ImportedReport
}
Write-JsonFile -Path (Join-Path $OutputRoot "zero-credit-preflight.json") -Value ([ordered]@{
    schema_version = "lrr-agent003-cli-model-bakeoff-preflight-v1"
    authority_path = $AuthorityPath
    authority_sha256 = $script:AuthorityIdentitySha256
    runner_path = $script:RunnerIdentityPath
    runner_sha256 = $script:RunnerIdentitySha256
    collector_path = $script:CollectorIdentityPath
    collector_sha256 = $script:CollectorIdentitySha256
    credit_epoch_starts_at_zero = $true
    aggregate_ai_credit_cap = $AggregateCreditCap
    cli_path = $cliEvidence.CliPath
    cli_sha256 = $cliEvidence.CliSha256
    help_evidence_path = $cliEvidence.HelpEvidencePath
    help_evidence_sha256 = $cliEvidence.HelpEvidenceSha256
    authority_candidates = @($ExpectedCandidates)
    help_listed_candidates = @($cliEvidence.ListedCandidates)
    production_launcher_path = $productionIdentity.LauncherPath
    production_launcher_sha256 = $productionIdentity.LauncherSha256
    production_manifest_path = $productionIdentity.ManifestPath
    production_manifest_sha256 = $productionIdentity.ManifestSha256
    resume_mode = $resumeRequested
    resume_source_root = if ($resumeRequested) { $ResumeEvidenceRoot } else { $null }
    resume_completed_ordinal = $ResumeCompletedOrdinal
    recovery_import_path = if ($null -ne $resumeState) { $resumeState.ManifestPath } else { $null }
    recovery_import_sha256 = if ($null -ne $resumeState) { $resumeState.ManifestSha256 } else { $null }
    imported_formal_ai_credits = if ($null -ne $latestReport) { [double]$latestReport.formal_aggregate_ai_credits } else { 0.0 }
    aborted_recovery_ai_credits = if ($null -ne $resumeState) { [double]$resumeState.RecoveryAiCredits } else { 0.0 }
    true_ai_credits_before_new_prompt = if ($null -ne $latestReport) { [double]$latestReport.true_total_ai_credits } else { 0.0 }
    prompt_count = 0
    observed_new_ai_credits = 0.0
})

if ($ValidateResumeOnly) {
    Write-Output (
        "PASS: resume prefix 1-{0} revalidated; aborted ordinal {1} preserved; formal_ai_credits={2:N6}; recovery_ai_credits={3:N6}; true_total_ai_credits={4:N6}/50; no prompt was sent." -f
            $ResumeCompletedOrdinal,
            ($ResumeCompletedOrdinal + 1),
            [double]$latestReport.formal_aggregate_ai_credits,
            [double]$latestReport.recovery_ai_credits,
            [double]$latestReport.true_total_ai_credits
    )
    exit 0
}

$testInstall = New-TemporaryBakeoffInstall `
    -Root (Join-Path $OutputRoot "temporary-test-install") `
    -SourceRuntimeRoot $CandidateRuntimeRoot `
    -NeutralWorkspace $neutralWorkspace `
    -Python $CollectorPython
$boundaryInstall = $null
$ordinal = 0
$timeoutSeconds = $CaseTimeoutMinutes * 60

foreach ($candidate in @($authority.candidate_models)) {
    $firstPolicyRunId = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $ordinal++
        $candidateOrdinal = [System.Array]::IndexOf(@($authority.candidate_models), $candidate) + 1
        $runId = "LRR-AGENT003-CLI-MODEL-SAVINGS-C{0:D2}-R{1}" -f $candidateOrdinal, $attempt
        if ($ordinal -le $ResumeCompletedOrdinal) {
            $imported = @($latestReport.runs | Where-Object {
                [int]$_.plan_ordinal -eq $ordinal -and [string]$_.run_id -ceq $runId
            })
            if ($imported.Count -ne 1) {
                throw "Resume formal prefix is missing exact ordinal ${ordinal}: $runId"
            }
            if ([string]$imported[0].status -ceq "UNAVAILABLE_POLICY_PREINFERENCE") {
                $firstPolicyRunId = $runId
            }
            continue
        }
        if ($candidate -cnotin @($cliEvidence.ListedCandidates)) {
            Add-SkippedRun `
                -RunsRoot $runsRoot `
                -Ordinal $ordinal `
                -RunId $runId `
                -Candidate $candidate `
                -Attempt $attempt `
                -Prompt ([string]$authority.savings_case.prompt) `
                -State "skipped_not_help_listed"
        }
        elseif ($null -ne $firstPolicyRunId) {
            Add-SkippedRun `
                -RunsRoot $runsRoot `
                -Ordinal $ordinal `
                -RunId $runId `
                -Candidate $candidate `
                -Attempt $attempt `
                -Prompt ([string]$authority.savings_case.prompt) `
                -State "skipped_policy_preinference" `
                -ReasonRunId $firstPolicyRunId
        }
        else {
            $priorCredits = Get-LatestAggregateCredits -Report $latestReport
            $reserve = [double]$authority.savings_case.minimum_remaining_credit_before_launch
            if (($priorCredits + $reserve) -gt $AggregateCreditCap) {
                throw "Credit reserve gate stopped before $runId; observed=$priorCredits reserve=$reserve cap=$AggregateCreditCap"
            }
            [void](Invoke-MeteredCase `
                -RunsRoot $runsRoot `
                -Ordinal $ordinal `
                -RunId $runId `
                -CaseKind "savings" `
                -Candidate $candidate `
                -Attempt $attempt `
                -Case $authority.savings_case `
                -Install $testInstall `
                -ProductionIdentity $productionIdentity `
                -CliEvidence $cliEvidence `
                -PowerShell $powerShell `
                -Python $CollectorPython `
                -Collector $CollectorPath `
                -WorkingDirectory $neutralWorkspace `
                -TimeoutSeconds $timeoutSeconds)
        }
        $reportPath = Join-Path $reportsRoot ("report-{0:D2}.json" -f $ordinal)
        $latestReport = Invoke-AggregateCollector `
            -Python $CollectorPython `
            -Collector $CollectorPath `
            -Authority $AuthorityPath `
            -RawRoot $OutputRoot `
            -Output $reportPath `
            -WorkingDirectory $neutralWorkspace
        $lastRun = @($latestReport.runs | Where-Object { [int]$_.plan_ordinal -eq $ordinal })
        if ($lastRun.Count -ne 1) { throw "Collector did not return the exact latest run: $runId" }
        if ([string]$lastRun[0].status -ceq "UNAVAILABLE_POLICY_PREINFERENCE") {
            $firstPolicyRunId = $runId
        }
        elseif ([string]$lastRun[0].status -ceq "FAIL") {
            $hardFailures = @($lastRun[0].failures | Where-Object {
                $_ -match 'credit_unknown|cli_exit_nonzero_not_policy|session_usage_unavailable|temporary_model_mutation|foreign_tool|permission_or_user_input|timeout'
            })
            if ($hardFailures.Count -gt 0) {
                throw "Hard evidence/runtime failure after ${runId}: $($hardFailures -join ', ')"
            }
        }
    }
}

if ($latestReport.all_savings_candidates_unavailable -eq $true -or
    [string]::IsNullOrWhiteSpace([string]$latestReport.winner)) {
    Copy-Item -LiteralPath (Join-Path $reportsRoot ("report-{0:D2}.json" -f $ordinal)) `
        -Destination (Join-Path $OutputRoot "lrr-agent003-cli-model-bakeoff-report.json")
    throw "No eligible savings model exists; auto fallback is forbidden and auxiliary UAT was not started."
}

foreach ($auxiliary in @(
    [pscustomobject]@{ Kind = "standard"; Case = $authority.standard_case; Install = $testInstall },
    [pscustomobject]@{ Kind = "thorough"; Case = $authority.thorough_case; Install = $testInstall },
    [pscustomobject]@{ Kind = "boundary"; Case = $authority.boundary_case; Install = $null }
)) {
    $ordinal++
    if ($auxiliary.Kind -ceq "boundary" -and $null -eq $boundaryInstall) {
        $boundaryInstall = New-TemporaryBakeoffInstall `
            -Root (Join-Path $OutputRoot "temporary-boundary-install") `
            -SourceRuntimeRoot $CandidateRuntimeRoot `
            -NeutralWorkspace $neutralWorkspace `
            -Python $CollectorPython `
            -BoundaryFixture
        $auxiliary.Install = $boundaryInstall
    }
    $priorCredits = Get-LatestAggregateCredits -Report $latestReport
    $reserve = [double]$auxiliary.Case.minimum_remaining_credit_before_launch
    if (($priorCredits + $reserve) -gt $AggregateCreditCap) {
        throw "Credit reserve gate stopped before $($auxiliary.Case.id); observed=$priorCredits reserve=$reserve cap=$AggregateCreditCap"
    }
    [void](Invoke-MeteredCase `
        -RunsRoot $runsRoot `
        -Ordinal $ordinal `
        -RunId ([string]$auxiliary.Case.id) `
        -CaseKind ([string]$auxiliary.Kind) `
        -Attempt 1 `
        -Case $auxiliary.Case `
        -Install $auxiliary.Install `
        -ProductionIdentity $productionIdentity `
        -CliEvidence $cliEvidence `
        -PowerShell $powerShell `
        -Python $CollectorPython `
        -Collector $CollectorPath `
        -WorkingDirectory $neutralWorkspace `
        -TimeoutSeconds $timeoutSeconds)
    $reportPath = Join-Path $reportsRoot ("report-{0:D2}.json" -f $ordinal)
    $latestReport = Invoke-AggregateCollector `
        -Python $CollectorPython `
        -Collector $CollectorPath `
        -Authority $AuthorityPath `
        -RawRoot $OutputRoot `
        -Output $reportPath `
        -WorkingDirectory $neutralWorkspace
}

$finalReportPath = Join-Path $reportsRoot ("report-{0:D2}.json" -f $ordinal)
$finalCanonical = Join-Path $OutputRoot "lrr-agent003-cli-model-bakeoff-report.json"
Copy-Item -LiteralPath $finalReportPath -Destination $finalCanonical
if ([string]$latestReport.overall_status -cne "PASS" -or
    [double]$latestReport.true_total_ai_credits -gt $AggregateCreditCap -or
    [string]::IsNullOrWhiteSpace([string]$latestReport.winner)) {
    throw "Model bakeoff did not pass; inspect $finalCanonical"
}
if ($resumeRequested) {
    Write-JsonFile -Path (Join-Path $OutputRoot "recovery-budget-summary.json") -Value ([ordered]@{
        schema_version = "lrr-agent003-cli-model-bakeoff-recovery-budget-summary-v1"
        recovery_import_path = $resumeState.ManifestPath
        recovery_import_sha256 = $resumeState.ManifestSha256
        source_report_path = $resumeState.SourceReportPath
        source_report_sha256 = $resumeState.SourceReportSha256
        resume_completed_ordinal = $ResumeCompletedOrdinal
        first_new_formal_ordinal = $ResumeCompletedOrdinal + 1
        formal_aggregate_total_nano_aiu = [long]$latestReport.formal_aggregate_total_nano_aiu
        formal_aggregate_ai_credits = [double]$latestReport.formal_aggregate_ai_credits
        aborted_recovery_total_nano_aiu = [long]$latestReport.recovery_total_nano_aiu
        aborted_recovery_ai_credits = [double]$latestReport.recovery_ai_credits
        true_total_nano_aiu = [long]$latestReport.true_total_nano_aiu
        true_total_ai_credits = [double]$latestReport.true_total_ai_credits
        aggregate_ai_credit_cap = $AggregateCreditCap
        within_cap = ([double]$latestReport.true_total_ai_credits -le $AggregateCreditCap)
        final_report_path = Get-FullPath -Path $finalCanonical
        final_report_sha256 = Get-Sha256File -Path $finalCanonical
    })
}
$productionAfter = Get-LauncherIdentity -Launcher $ProductionLauncherPath
if ($productionAfter.LauncherSha256 -cne $productionIdentity.LauncherSha256 -or
    $productionAfter.ManifestSha256 -cne $productionIdentity.ManifestSha256) {
    throw "Production launcher/manifest changed during the test-only bakeoff."
}
Write-Output (
    "PASS: winner={0}; formal_ai_credits={1:N6}; recovery_ai_credits={2:N6}; true_total_ai_credits={3:N6}/50; 21 candidate sessions/skips plus standard auto, thorough auto, and >32KiB boundary were evaluated without production model override." -f
        [string]$latestReport.winner,
        [double]$latestReport.formal_aggregate_ai_credits,
        [double]$latestReport.recovery_ai_credits,
        [double]$latestReport.true_total_ai_credits
)
