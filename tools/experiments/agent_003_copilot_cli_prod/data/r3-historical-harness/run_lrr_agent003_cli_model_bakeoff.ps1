[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [string]$AuthorityPath = (Join-Path $PSScriptRoot "data\lrr-agent003-cli-model-bakeoff-v1.json"),

    [Parameter()]
    [string]$CollectorPath = (Join-Path $PSScriptRoot "collect_lrr_agent003_cli_model_bakeoff.py"),

    [Parameter()]
    [string]$CollectorPython = (Join-Path $env:USERPROFILE ".copilot\rag\query\.venv\Scripts\python.exe"),

    [Parameter()]
    [string]$CandidateRuntimeRoot = (Join-Path $env:USERPROFILE ".copilot"),

    [Parameter()]
    [string]$ProductionLauncherPath = (Join-Path $env:USERPROFILE ".copilot\copilot-cli\local-rag-agent003.ps1"),

    [Parameter()]
    [string]$OutputRoot,

    [Parameter()]
    [ValidateSet(15)]
    [int]$CaseTimeoutMinutes = 15,

    [Parameter()]
    [switch]$AllowMeteredRun,

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
    $result = Invoke-NativeProcessToFiles `
        -FileName $Python `
        -Arguments @(
            "-B", $Collector,
            "--snapshot-copilot-home", $CopilotHome,
            "--snapshot-output", $Output
        ) `
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
    $result = Invoke-NativeProcessToFiles `
        -FileName $Python `
        -Arguments @(
            "-B", $Collector,
            "--authority", $Authority,
            "--raw-root", $RawRoot,
            "--output", $Output
        ) `
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
    if ($Report.credit_observable -ne $true -or $null -eq $Report.aggregate_ai_credits) {
        throw "Aggregate Credit is unknown; stopping."
    }
    return [double]$Report.aggregate_ai_credits
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
        Write-Output "PASS: model bakeoff authority, collector, ranking, and boundary fixture self-tests"
    }
    finally {
        if (Test-Path -LiteralPath $selfTestRoot -PathType Container) {
            [System.IO.Directory]::Delete($selfTestRoot, $true)
        }
    }
    exit 0
}

if (-not $AllowMeteredRun) {
    throw "Metered UAT is disabled by default. Re-run with -AllowMeteredRun after the zero-Credit preflight."
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    throw "-OutputRoot is required for metered UAT."
}
$OutputRoot = Get-FullPath -Path $OutputRoot
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
    prompt_count = 0
    observed_ai_credits = 0.0
})

$testInstall = New-TemporaryBakeoffInstall `
    -Root (Join-Path $OutputRoot "temporary-test-install") `
    -SourceRuntimeRoot $CandidateRuntimeRoot `
    -NeutralWorkspace $neutralWorkspace `
    -Python $CollectorPython
$boundaryInstall = $null
$latestReport = $null
$ordinal = 0
$timeoutSeconds = $CaseTimeoutMinutes * 60

foreach ($candidate in @($authority.candidate_models)) {
    $firstPolicyRunId = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $ordinal++
        $candidateOrdinal = [System.Array]::IndexOf(@($authority.candidate_models), $candidate) + 1
        $runId = "LRR-AGENT003-CLI-MODEL-SAVINGS-C{0:D2}-R{1}" -f $candidateOrdinal, $attempt
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
    [double]$latestReport.aggregate_ai_credits -gt $AggregateCreditCap -or
    [string]::IsNullOrWhiteSpace([string]$latestReport.winner)) {
    throw "Model bakeoff did not pass; inspect $finalCanonical"
}
$productionAfter = Get-LauncherIdentity -Launcher $ProductionLauncherPath
if ($productionAfter.LauncherSha256 -cne $productionIdentity.LauncherSha256 -or
    $productionAfter.ManifestSha256 -cne $productionIdentity.ManifestSha256) {
    throw "Production launcher/manifest changed during the test-only bakeoff."
}
Write-Output (
    "PASS: winner={0}; observed_ai_credits={1:N6}/50; 21 candidate sessions/skips plus standard auto, thorough auto, and >32KiB boundary were evaluated without production model override." -f
        [string]$latestReport.winner,
        [double]$latestReport.aggregate_ai_credits
)
