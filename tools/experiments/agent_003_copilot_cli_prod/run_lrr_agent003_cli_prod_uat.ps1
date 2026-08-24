[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [string]$CasesPath = (Join-Path $PSScriptRoot "data\lrr-agent003-cli-prod-uat-cases-v1.jsonl"),

    [Parameter()]
    [string]$CollectorPath = (Join-Path $PSScriptRoot "collect_lrr_agent003_cli_prod_uat.py"),

    [Parameter()]
    [string]$CollectorPython = (Join-Path $env:USERPROFILE ".copilot\rag\query\.venv\Scripts\python.exe"),

    [Parameter()]
    [string]$CandidateRuntimeRoot = (Join-Path $env:USERPROFILE ".copilot"),

    [Parameter()]
    [string]$LauncherPath = (Join-Path $env:USERPROFILE ".copilot\copilot-cli\local-rag-agent003.ps1"),

    [Parameter()]
    [string]$OutputRoot,

    [Parameter()]
    [ValidateSet(15)]
    [int]$CaseTimeoutMinutes = 15,

    [Parameter()]
    [switch]$AllowMeteredRun,

    [Parameter()]
    [ValidateSet(30)]
    [int]$PerCaseMaxAiCredits = 30,

    [Parameter()]
    [ValidateRange(0.0, 50.0)]
    [double]$PreviouslyConsumedAiCredits = 0.0,

    [Parameter()]
    [ValidateRange(50.0, 80.0)]
    [double]$ApprovedCreditCap = 50.0,

    [Parameter()]
    [switch]$ResumeValidatedOutput,

    [Parameter()]
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$CaseSchema = "lrr-agent003-cli-prod-uat-case-v1"
$RunSchema = "lrr-agent003-cli-prod-uat-run-v1"
$ExpectedCaseCount = 5
$AggregateCreditCap = [int][System.Math]::Floor(
    $ApprovedCreditCap - $PreviouslyConsumedAiCredits
)
if ($AggregateCreditCap -le 0) {
    throw "No approved AI Credit capacity remains for this UAT run."
}
$BoundaryMarker = "LRR-CLI-LARGE-OUTPUT-TAIL-7F3C9A21"
$SupportedCopilotCliVersion = "1.0.77"
$ExpectedAvailableTools = @(
    "localragagent003-local_rag_search",
    "localragagent003-local_rag_get_evidence"
)
$ExpectedAllowedTools = @(
    "localragagent003(local_rag_search)",
    "localragagent003(local_rag_get_evidence)"
)
$ApprovalObservation = "NO_OBSERVED_PROMPT"

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
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $sha) { $sha.Dispose() }
    }
}

function Get-LauncherManifestIdentity {
    param([Parameter(Mandatory)][string]$Launcher)
    $resolvedLauncher = Get-FullPath -Path $Launcher
    if (-not (Test-Path -LiteralPath $resolvedLauncher -PathType Leaf)) {
        throw "Launcher is missing: $resolvedLauncher"
    }
    $manifestPath = Join-Path (Split-Path -Parent $resolvedLauncher) "owned-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Owned launcher manifest is missing: $manifestPath"
    }
    $manifestPath = Get-FullPath -Path $manifestPath
    $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 |
        ConvertFrom-Json -Depth 20
    if ([int]$manifest.schema -ne 1 -or
        [string]::IsNullOrWhiteSpace([string]$manifest.copilot_home) -or
        [string]::IsNullOrWhiteSpace([string]$manifest.install_root) -or
        -not [System.IO.Path]::IsPathRooted([string]$manifest.copilot_home) -or
        -not [System.IO.Path]::IsPathRooted([string]$manifest.install_root)) {
        throw "Owned launcher manifest identity is invalid: $manifestPath"
    }
    $copilotHome = Get-FullPath -Path ([string]$manifest.copilot_home)
    $installRoot = Get-FullPath -Path ([string]$manifest.install_root)
    $manifestLauncher = Get-FullPath -Path (Join-Path $installRoot "copilot-cli\local-rag-agent003.ps1")
    if (-not $manifestLauncher.Equals($resolvedLauncher, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Launcher does not belong to the manifest install_root."
    }
    $entries = @($manifest.artifacts | Where-Object {
        [string]$_.root -ceq "install_root" -and
        ([string]$_.path).Replace("\", "/") -ceq "copilot-cli/local-rag-agent003.ps1"
    })
    if ($entries.Count -ne 1) {
        throw "Owned launcher manifest has no unique launcher artifact."
    }
    $launcherHash = Get-Sha256File -Path $resolvedLauncher
    $launcherBytes = (Get-Item -LiteralPath $resolvedLauncher).Length
    if ([string]$entries[0].sha256 -cne $launcherHash -or
        [long]$entries[0].bytes -ne $launcherBytes) {
        throw "Launcher bytes do not match the owned manifest."
    }
    $launcherText = [System.IO.File]::ReadAllText(
        $resolvedLauncher,
        [System.Text.Encoding]::UTF8
    )
    foreach ($requiredFragment in @(
        '$ToolList = "localragagent003-local_rag_search,localragagent003-local_rag_get_evidence"',
        '$AllowList = "localragagent003(local_rag_search),localragagent003(local_rag_get_evidence)"',
        '"--available-tools=$ToolList"',
        '"--allow-tool=$AllowList"',
        '"--no-custom-instructions"'
    )) {
        if (-not $launcherText.Contains($requiredFragment)) {
            throw "Launcher is missing the fixed noninteractive permission contract."
        }
    }
    return [pscustomobject]@{
        LauncherPath = $resolvedLauncher
        LauncherSha256 = $launcherHash
        ManifestPath = $manifestPath
        ManifestSha256 = Get-Sha256File -Path $manifestPath
        CopilotHome = $copilotHome
        InstallRoot = $installRoot
    }
}

function Assert-OwnedMcpSpoolWritable {
    param([Parameter(Mandatory)][object]$LauncherIdentity)

    $pinnedPath = Join-Path `
        (Join-Path ([string]$LauncherIdentity.InstallRoot) "copilot-cli") `
        "local-rag-agent003.pinned-mcp.json"
    if (-not (Test-Path -LiteralPath $pinnedPath -PathType Leaf)) {
        throw "Pinned MCP configuration is missing: $pinnedPath"
    }
    $pinned = Get-Content -Raw -LiteralPath $pinnedPath -Encoding UTF8 |
        ConvertFrom-Json -Depth 20
    $server = $pinned.mcpServers.localragagent003
    if ($null -eq $server -or [string]$server.type -cne "local") {
        throw "Pinned MCP configuration has no owned local server."
    }
    $temporaryRoot = Get-FullPath -Path ([string]$server.env.TEMP)
    $arguments = @($server.args)
    $spoolFlagIndex = [System.Array]::IndexOf($arguments, "--spool-root")
    if ($spoolFlagIndex -lt 0 -or $spoolFlagIndex + 1 -ge $arguments.Count) {
        throw "Pinned MCP configuration has no spool root."
    }
    $spoolRoot = Get-FullPath -Path ([string]$arguments[$spoolFlagIndex + 1])
    foreach ($path in @($temporaryRoot, $spoolRoot)) {
        if (-not [System.IO.Path]::IsPathRooted($path) -or
            -not (Test-PathInside -Path $path -Root ([string]$LauncherIdentity.InstallRoot))) {
            throw "Pinned MCP temporary path escapes the installed product: $path"
        }
    }
    if (-not (Test-PathInside -Path $spoolRoot -Root $temporaryRoot)) {
        throw "Pinned MCP spool root escapes its owned temporary root."
    }

    $probeContainer = $spoolRoot
    while (-not (Test-Path -LiteralPath $probeContainer)) {
        $parent = [System.IO.Path]::GetDirectoryName($probeContainer)
        if ([string]::IsNullOrWhiteSpace($parent) -or
            -not (Test-PathInside -Path $parent -Root $temporaryRoot)) {
            throw "No writable MCP spool ancestor exists inside the owned temporary root."
        }
        $probeContainer = $parent
    }
    $containerItem = Get-Item -LiteralPath $probeContainer -Force
    if (-not $containerItem.PSIsContainer -or
        ($containerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "MCP spool probe target is not a regular directory: $probeContainer"
    }
    $installBoundary = Get-FullPath -Path ([string]$LauncherIdentity.InstallRoot)
    $currentItem = $containerItem
    $reachedBoundary = $false
    while ($null -ne $currentItem -and
        (Test-PathInside -Path $currentItem.FullName -Root $installBoundary)) {
        if ($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "MCP spool probe path crosses a reparse point: $($currentItem.FullName)"
        }
        if ($currentItem.FullName.Equals(
            $installBoundary,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            $reachedBoundary = $true
            break
        }
        $currentItem = $currentItem.Parent
    }
    if (-not $reachedBoundary) {
        throw "MCP spool probe path did not resolve to the installed-product boundary."
    }

    $probe = Join-Path $probeContainer (".lrr-uat-write-probe-" + [guid]::NewGuid().ToString("N"))
    $probeFile = Join-Path $probe "probe"
    try {
        [void][System.IO.Directory]::CreateDirectory($probe)
        [System.IO.File]::WriteAllText(
            $probeFile,
            "probe",
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    catch {
        throw "Current UAT principal cannot write the owned MCP spool path '$probeContainer': $($_.Exception.Message)"
    }
    finally {
        if (Test-Path -LiteralPath $probeFile -PathType Leaf) {
            [System.IO.File]::Delete($probeFile)
        }
        if (Test-Path -LiteralPath $probe -PathType Container) {
            [System.IO.Directory]::Delete($probe)
        }
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
    Write-Utf8NoBom -Path $Path -Value (($Value | ConvertTo-Json -Depth 12) + "`n")
}

function Read-CanonicalCases {
    param([Parameter(Mandatory)][string]$Path)
    $values = @()
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $values += ,($line | ConvertFrom-Json -Depth 20)
    }
    if ($values.Count -ne $ExpectedCaseCount) {
        throw "The case authority must contain exactly $ExpectedCaseCount cases."
    }
    for ($index = 0; $index -lt $values.Count; $index++) {
        $expectedId = "LRR-AGENT003-CLI-PROD-$($index + 1)"
        if ($values[$index].schema_version -cne $CaseSchema -or
            $values[$index].id -cne $expectedId) {
            throw "The case authority is not canonical at ordinal $($index + 1)."
        }
    }
    if ($values[4].required_response_fragment -cne $BoundaryMarker -or
        $values[4].launcher_scope -cne "temporary_boundary_fixture" -or
        [int]$values[4].tool_result_tail_window_bytes -ne 256) {
        throw "The deterministic boundary fixture revision is not canonical."
    }
    for ($index = 0; $index -lt $values.Count; $index++) {
        $hasUrlContract = $values[$index].PSObject.Properties.Name -contains
            "minimum_markdown_source_urls"
        if ($index -in @(2, 3)) {
            if (-not $hasUrlContract -or
                [int]$values[$index].minimum_markdown_source_urls -lt 1 -or
                $values[$index].require_all_response_urls_from_tool_evidence -ne $true) {
                throw "The source URL provenance contract is not canonical at ordinal $($index + 1)."
            }
        }
        elseif ($hasUrlContract) {
            throw "Unexpected source URL provenance contract at ordinal $($index + 1)."
        }
    }
    return $values
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
    foreach ($argument in $Arguments) {
        [void]$start.ArgumentList.Add($argument)
    }
    foreach ($name in $Environment.Keys) {
        $start.Environment[[string]$name] = [string]$Environment[$name]
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    $startedAt = [DateTimeOffset]::UtcNow
    $timedOut = $false
    $processTreeTerminated = $false
    try {
        if (-not $process.Start()) { throw "Process did not start: $FileName" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($TimeoutSeconds -gt 0) {
            $completed = $process.WaitForExit($TimeoutSeconds * 1000)
            if (-not $completed) {
                $timedOut = $true
                try {
                    if (-not $process.HasExited) {
                        $process.Kill($true)
                    }
                }
                catch {
                    if (-not $process.HasExited) { throw }
                }
                if (-not $process.WaitForExit(30000)) {
                    throw "Timed-out process tree did not terminate: $FileName"
                }
                $processTreeTerminated = $true
            }
        }
        else {
            $process.WaitForExit()
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        Write-Utf8NoBom -Path $StdoutPath -Value $stdout
        Write-Utf8NoBom -Path $StderrPath -Value $stderr
        return [pscustomobject]@{
            ExitCode = if ($timedOut) { $null } else { $process.ExitCode }
            ProcessId = $process.Id
            StartedAt = $startedAt.ToString("o")
            FinishedAt = [DateTimeOffset]::UtcNow.ToString("o")
            StdoutBytes = [System.Text.Encoding]::UTF8.GetByteCount($stdout)
            StderrBytes = [System.Text.Encoding]::UTF8.GetByteCount($stderr)
            TimedOut = $timedOut
            TimeoutSeconds = $TimeoutSeconds
            ProcessTreeTerminated = $processTreeTerminated
        }
    }
    finally {
        $process.Dispose()
    }
}

function Get-CopilotCliIdentity {
    param(
        [Parameter(Mandatory)][string]$PowerShell,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][string]$StdoutPath,
        [Parameter(Mandatory)][string]$StderrPath
    )
    $commands = @(Get-Command "copilot" -CommandType Application -All -ErrorAction Stop)
    if ($commands.Count -lt 1) { throw "Copilot CLI executable was not found." }
    $cliPath = Get-FullPath -Path ([string]$commands[0].Source)
    if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
        throw "Resolved Copilot CLI executable is not a file: $cliPath"
    }
    $versionResult = Invoke-NativeProcessToFiles `
        -FileName $PowerShell `
        -Arguments @(
            "-NoProfile", "-NonInteractive", "-Command",
            '& ([System.Environment]::GetEnvironmentVariable(''LOCAL_RAG_UAT_CLI_PATH'', ''Process'')) --version'
        ) `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath $StdoutPath `
        -StderrPath $StderrPath `
        -Environment @{ LOCAL_RAG_UAT_CLI_PATH = $cliPath } `
        -TimeoutSeconds 30
    if ($versionResult.TimedOut -or $versionResult.ExitCode -ne 0) {
        throw "Copilot CLI version preflight failed."
    }
    $versionOutput = [System.IO.File]::ReadAllText(
        $StdoutPath,
        [System.Text.Encoding]::UTF8
    ).Trim()
    $versionLines = @($versionOutput -split "\r?\n")
    $match = [regex]::Match(
        [string]$versionLines[0],
        '^GitHub Copilot CLI (?<version>[0-9]+\.[0-9]+\.[0-9]+)\.?$'
    )
    if (-not $match.Success) {
        throw "Copilot CLI version output is not recognized."
    }
    $version = $match.Groups['version'].Value
    if ($version -cne $SupportedCopilotCliVersion) {
        throw "Unsupported Copilot CLI event schema version: $version"
    }
    return [pscustomobject]@{
        Path = $cliPath
        Sha256 = Get-Sha256File -Path $cliPath
        Version = $version
        VersionEvidencePath = Get-FullPath -Path $StdoutPath
        VersionEvidenceSha256 = Get-Sha256File -Path $StdoutPath
    }
}

function Get-PowerShellExecutable {
    $candidate = Join-Path $PSHOME "pwsh.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "PowerShell 7 executable was not found in PSHOME."
    }
    return (Get-FullPath -Path $candidate)
}

function Assert-ResumeIdentityMatches {
    param(
        [Parameter(Mandatory)][object]$Report,
        [Parameter(Mandatory)][object]$LauncherIdentity,
        [Parameter(Mandatory)][object]$CliIdentity,
        [Parameter(Mandatory)][int]$PerCaseCreditCap
    )

    $completedCases = @($Report.cases)
    if ($completedCases.Count -lt 1) {
        throw "Resume identity authority has no completed case."
    }
    foreach ($completedCase in $completedCases) {
        if ([int]$completedCase.max_ai_credits -ne $PerCaseCreditCap) {
            throw "Resume per-case Credit cap differs from the completed prefix."
        }
    }
    $expectedLauncher = $completedCases[0].launcher_identity
    $expectedCli = $completedCases[0].cli_identity
    foreach ($comparison in @(
        @([string]$LauncherIdentity.InstallRoot, [string]$expectedLauncher.install_root),
        @([string]$LauncherIdentity.CopilotHome, [string]$expectedLauncher.copilot_home),
        @([string]$LauncherIdentity.LauncherPath, [string]$expectedLauncher.launcher_path),
        @([string]$LauncherIdentity.ManifestPath, [string]$expectedLauncher.launcher_manifest_path),
        @([string]$CliIdentity.Path, [string]$expectedCli.path)
    )) {
        if ([string]::IsNullOrWhiteSpace($comparison[1]) -or
            -not $comparison[0].Equals(
                $comparison[1],
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Current executable path identity differs from the completed prefix."
        }
    }
    foreach ($comparison in @(
        @([string]$LauncherIdentity.LauncherSha256, [string]$expectedLauncher.launcher_sha256),
        @([string]$LauncherIdentity.ManifestSha256, [string]$expectedLauncher.launcher_manifest_sha256),
        @([string]$CliIdentity.Sha256, [string]$expectedCli.sha256),
        @([string]$CliIdentity.Version, [string]$expectedCli.version)
    )) {
        if ([string]::IsNullOrWhiteSpace($comparison[1]) -or
            $comparison[0] -cne $comparison[1]) {
            throw "Current executable hash or version identity differs from the completed prefix."
        }
    }
}

function New-LauncherProcessArguments {
    param(
        [Parameter(Mandatory)][string]$Launcher,
        [Parameter(Mandatory)][string]$Tier,
        [string[]]$CopilotArguments = @()
    )
    return @(
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", $Launcher, "-Tier", $Tier
    ) + @($CopilotArguments)
}

function New-BoundaryFixtureServerSource {
    return @'
from __future__ import annotations

import json
import sys

MARKER = "LRR-CLI-LARGE-OUTPUT-TAIL-7F3C9A21"
FILLER = "境界検証データ" * 5000

TOOLS = [
    {
        "name": "local_rag_search",
        "description": "Read-only deterministic Local RAG boundary search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "database": {"type": "string"}
            },
            "required": ["question"],
            "additionalProperties": False
        }
    },
    {
        "name": "local_rag_get_evidence",
        "description": "Read-only deterministic evidence lookup.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "result_token": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["result_token", "evidence_ids"],
            "additionalProperties": False
        }
    }
]


def write_result(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def packet():
    return {
        "schema_version": "lrr-agent003-cli-prod-large-output-fixture-v1",
        "status": "ok",
        "answerability": "full",
        "database": "lrr-agent003-cli-prod-boundary-rag",
        "next_action": "answer_now",
        "result_token": "fixture-token",
        "notices": [],
        "evidence": [{
            "id": "E1",
            "title": "Deterministic large-output boundary fixture",
            "text": FILLER + MARKER
        }],
        "tail_marker": MARKER
    }


for raw in sys.stdin:
    try:
        request = json.loads(raw)
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            requested_protocol = (request.get("params") or {}).get("protocolVersion")
            write_result(request_id, {
                "protocolVersion": requested_protocol or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "localragagent003", "version": "fixture-v1"}
            })
        elif method == "tools/list":
            write_result(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "local_rag_search" and not arguments.get("database"):
                value = {
                    "status": "database_required",
                    "candidates": [{
                        "name": "lrr-agent003-cli-prod-boundary-rag",
                        "description": "Deterministic long Local RAG boundary validation result"
                    }],
                    "instruction": "Routing only; retrieval has not run."
                }
            elif name in {"local_rag_search", "local_rag_get_evidence"}:
                value = packet()
            else:
                value = {"status": "unknown_tool"}
            write_result(request_id, {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "isError": name not in {"local_rag_search", "local_rag_get_evidence"}
            })
        elif request_id is not None:
            write_result(request_id, {})
    except Exception as exc:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": type(exc).__name__}}) + "\n")
        sys.stdout.flush()
'@
}

function New-TemporaryBoundaryInstall {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$SourceRuntimeRoot,
        [Parameter(Mandatory)][string]$NeutralWorkspace
    )
    $sourceRoot = Get-FullPath -Path $SourceRuntimeRoot
    $sourceVenv = Join-Path $sourceRoot "rag\query\.venv"
    $sourceSetup = Join-Path $sourceRoot "rag\query\copilot_cli_setup.py"
    $sourceMcpConfig = Join-Path $sourceRoot "rag\query\mcp_config.py"
    foreach ($source in @(
        $sourceVenv,
        $sourceSetup,
        $sourceMcpConfig,
        (Join-Path $sourceRoot "rag\copilot-cli")
    )) {
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Candidate runtime prerequisite is missing: $source"
        }
    }
    $installRoot = Join-Path $Root "install"
    $queryRoot = Join-Path $installRoot "rag\query"
    $copilotHome = Join-Path $Root "copilot-home"
    $profilePath = Join-Path $Root "profile.ps1"
    [void](New-Item -ItemType Directory -Path $queryRoot -Force)
    Copy-Item -LiteralPath $sourceVenv -Destination $queryRoot -Recurse
    Copy-Item -LiteralPath $sourceMcpConfig -Destination (Join-Path $queryRoot "mcp_config.py")
    $fixtureServer = Join-Path $queryRoot "mcp_server.py"
    Write-Utf8NoBom -Path $fixtureServer -Value (New-BoundaryFixtureServerSource)
    $temporaryPython = Join-Path $queryRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $temporaryPython -PathType Leaf)) {
        throw "Copied candidate virtual environment is incomplete."
    }
    $setupStdout = Join-Path $Root "setup.stdout.log"
    $setupStderr = Join-Path $Root "setup.stderr.log"
    $setup = Invoke-NativeProcessToFiles `
        -FileName $temporaryPython `
        -Arguments @(
            "-B", $sourceSetup, "install",
            "--copilot-home", $copilotHome,
            "--install-root", $installRoot,
            "--profile-path", $profilePath
        ) `
        -WorkingDirectory $NeutralWorkspace `
        -StdoutPath $setupStdout `
        -StderrPath $setupStderr
    if ($setup.ExitCode -ne 0) {
        throw "Temporary boundary fixture install failed; inspect $setupStderr"
    }
    $launcher = Join-Path $installRoot "copilot-cli\local-rag-agent003.ps1"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Temporary boundary fixture launcher is missing after setup."
    }
    return (Get-FullPath -Path $launcher)
}

function Invoke-Collector {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Cases,
        [Parameter(Mandatory)][string]$RawRoot,
        [Parameter(Mandatory)][string]$Output,
        [Parameter(Mandatory)][int]$CompletedCount,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    $stdout = Join-Path $RawRoot "collector-$CompletedCount.stdout.log"
    $stderr = Join-Path $RawRoot "collector-$CompletedCount.stderr.log"
    return Invoke-NativeProcessToFiles `
        -FileName $Python `
        -Arguments @(
            "-B", $CollectorPath,
            "--cases", $Cases,
            "--raw-root", $RawRoot,
            "--output", $Output,
            "--completed-count", [string]$CompletedCount,
            "--aggregate-credit-cap", [string]$AggregateCreditCap
        ) `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath $stdout `
        -StderrPath $stderr
}

$CasesPath = Get-FullPath -Path $CasesPath
$CollectorPath = Get-FullPath -Path $CollectorPath
if (-not (Test-Path -LiteralPath $CasesPath -PathType Leaf)) {
    throw "Case authority is missing: $CasesPath"
}
if (-not (Test-Path -LiteralPath $CollectorPath -PathType Leaf)) {
    throw "Collector is missing: $CollectorPath"
}
$cases = @(Read-CanonicalCases -Path $CasesPath)

if ($SelfTest) {
    if (-not (Test-Path -LiteralPath $CollectorPython -PathType Leaf)) {
        $fallback = Get-Command "python" -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $fallback) { throw "Python is unavailable for self-test." }
        $CollectorPython = $fallback.Source
    }
    $selfTestRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("lrr-agent003-cli-prod-selftest-" + [guid]::NewGuid().ToString("N"))
    [void](New-Item -ItemType Directory -Path $selfTestRoot)
    try {
        $result = Invoke-NativeProcessToFiles `
            -FileName (Get-FullPath -Path $CollectorPython) `
            -Arguments @("-B", $CollectorPath, "--cases", $CasesPath, "--self-test") `
            -WorkingDirectory $selfTestRoot `
            -StdoutPath (Join-Path $selfTestRoot "stdout.log") `
            -StderrPath (Join-Path $selfTestRoot "stderr.log")
        if ($result.ExitCode -ne 0) {
            throw "Collector self-test failed in $selfTestRoot"
        }
        $fixturePath = Join-Path $selfTestRoot "large_output_fixture_server.py"
        Write-Utf8NoBom -Path $fixturePath -Value (New-BoundaryFixtureServerSource)
        $fixtureSyntax = Invoke-NativeProcessToFiles `
            -FileName (Get-FullPath -Path $CollectorPython) `
            -Arguments @(
                "-B", "-c",
                "import ast,pathlib,sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))",
                $fixturePath
            ) `
            -WorkingDirectory $selfTestRoot `
            -StdoutPath (Join-Path $selfTestRoot "fixture-syntax.stdout.log") `
            -StderrPath (Join-Path $selfTestRoot "fixture-syntax.stderr.log")
        if ($fixtureSyntax.ExitCode -ne 0) {
            throw "Boundary fixture syntax self-test failed in $selfTestRoot"
        }
        $selfTestPowerShell = Get-PowerShellExecutable
        $childPidPath = Join-Path $selfTestRoot "timeout-child.pid"
        $timeoutParentPath = Join-Path $selfTestRoot "timeout-parent.ps1"
        $timeoutParentSource = @'
param(
    [Parameter(Mandatory)][string]$PowerShellPath,
    [Parameter(Mandatory)][string]$ChildPidPath
)
$child = Start-Process -FilePath $PowerShellPath -ArgumentList @(
    '-NoProfile', '-NonInteractive', '-Command',
    '[System.Threading.Thread]::Sleep(30000)'
) -PassThru -WindowStyle Hidden
[System.IO.File]::WriteAllText($ChildPidPath, [string]$child.Id)
[System.Threading.Thread]::Sleep(30000)
'@
        Write-Utf8NoBom -Path $timeoutParentPath -Value $timeoutParentSource
        $timeoutResult = Invoke-NativeProcessToFiles `
            -FileName $selfTestPowerShell `
            -Arguments @(
                "-NoProfile", "-NonInteractive", "-File", $timeoutParentPath,
                "-PowerShellPath", $selfTestPowerShell,
                "-ChildPidPath", $childPidPath
            ) `
            -WorkingDirectory $selfTestRoot `
            -StdoutPath (Join-Path $selfTestRoot "timeout.stdout.log") `
            -StderrPath (Join-Path $selfTestRoot "timeout.stderr.log") `
            -TimeoutSeconds 2
        if (-not (Test-Path -LiteralPath $childPidPath -PathType Leaf)) {
            throw "Process-tree timeout self-test did not start its child."
        }
        $childPid = [int]([System.IO.File]::ReadAllText($childPidPath).Trim())
        if (-not $timeoutResult.TimedOut -or
            -not $timeoutResult.ProcessTreeTerminated -or
            $null -ne $timeoutResult.ExitCode -or
            $timeoutResult.TimeoutSeconds -ne 2 -or
            $null -ne (Get-Process -Id $timeoutResult.ProcessId -ErrorAction SilentlyContinue) -or
            $null -ne (Get-Process -Id $childPid -ErrorAction SilentlyContinue)) {
            throw "Process-tree timeout self-test failed."
        }
        $launcherArguments = @(New-LauncherProcessArguments `
            -Launcher "C:\sentinel\launcher.ps1" `
            -Tier "standard" `
            -CopilotArguments @("--prompt", "sentinel prompt"))
        if ($launcherArguments.Count -ne 10 -or
            $launcherArguments[5] -cne "C:\sentinel\launcher.ps1" -or
            $launcherArguments[7] -cne "standard" -or
            $launcherArguments[8] -cne "--prompt" -or
            $launcherArguments[9] -cne "sentinel prompt") {
            throw "Launcher process argument assembly self-test failed."
        }
        $syntheticLauncherIdentity = [pscustomobject]@{
            InstallRoot = "C:\sentinel\install"
            CopilotHome = "C:\sentinel\home"
            LauncherPath = "C:\sentinel\install\launcher.ps1"
            ManifestPath = "C:\sentinel\install\manifest.json"
            LauncherSha256 = "a" * 64
            ManifestSha256 = "b" * 64
        }
        $syntheticCliIdentity = [pscustomobject]@{
            Path = "C:\sentinel\copilot.exe"
            Sha256 = "c" * 64
            Version = $SupportedCopilotCliVersion
        }
        $syntheticResumeReport = [pscustomobject]@{
            cases = @([pscustomobject]@{
                max_ai_credits = $PerCaseMaxAiCredits
                launcher_identity = [pscustomobject]@{
                    install_root = $syntheticLauncherIdentity.InstallRoot
                    copilot_home = $syntheticLauncherIdentity.CopilotHome
                    launcher_path = $syntheticLauncherIdentity.LauncherPath
                    launcher_manifest_path = $syntheticLauncherIdentity.ManifestPath
                    launcher_sha256 = $syntheticLauncherIdentity.LauncherSha256
                    launcher_manifest_sha256 = $syntheticLauncherIdentity.ManifestSha256
                }
                cli_identity = [pscustomobject]@{
                    path = $syntheticCliIdentity.Path
                    sha256 = $syntheticCliIdentity.Sha256
                    version = $syntheticCliIdentity.Version
                }
            })
        }
        Assert-ResumeIdentityMatches `
            -Report $syntheticResumeReport `
            -LauncherIdentity $syntheticLauncherIdentity `
            -CliIdentity $syntheticCliIdentity `
            -PerCaseCreditCap $PerCaseMaxAiCredits
        $syntheticCliIdentity.Sha256 = "d" * 64
        $identityMismatchRejected = $false
        try {
            Assert-ResumeIdentityMatches `
                -Report $syntheticResumeReport `
                -LauncherIdentity $syntheticLauncherIdentity `
                -CliIdentity $syntheticCliIdentity `
                -PerCaseCreditCap $PerCaseMaxAiCredits
        }
        catch {
            $identityMismatchRejected = $true
        }
        if (-not $identityMismatchRejected) {
            throw "Resume identity swap self-test failed."
        }
        Write-Output "SELF-TEST OK: runner authority and collector synthetic gates passed; no launcher or prompt was invoked."
    }
    finally {
        if (Test-Path -LiteralPath $selfTestRoot) {
            $resolvedSelfTestRoot = Get-FullPath -Path $selfTestRoot
            $resolvedSystemTemp = Get-FullPath -Path ([System.IO.Path]::GetTempPath())
            if (-not (Test-PathInside -Path $resolvedSelfTestRoot -Root $resolvedSystemTemp)) {
                throw "Refusing to clean a self-test directory outside the system temp root."
            }
            Remove-Item -LiteralPath $selfTestRoot -Recurse -Force
        }
    }
    exit 0
}

if (-not $AllowMeteredRun) {
    throw "Actual UAT is metered. Re-run with -AllowMeteredRun after reviewing the five exact prompts."
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    throw "-OutputRoot is required for an actual UAT."
}
foreach ($path in @($CollectorPython, $LauncherPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required executable or launcher is missing: $path"
    }
}

$repoRoot = Get-FullPath -Path (Join-Path $PSScriptRoot "..\..\..")
$OutputRoot = Get-FullPath -Path $OutputRoot
if (Test-PathInside -Path $OutputRoot -Root $repoRoot) {
    throw "Raw UAT output must be outside the repository."
}
$reportPath = Join-Path $OutputRoot "lrr-agent003-cli-prod-uat-report.json"
$resumeCompletedCount = 0
$resumeReportSha256 = $null
$resumeValidationReportPath = $null
$resumeValidationReportSha256 = $null
if (Test-Path -LiteralPath $OutputRoot) {
    if ($ResumeValidatedOutput) {
        if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            throw "Resume requires an existing authoritative aggregate report."
        }
        $resumeReport = Get-Content -Raw -LiteralPath $reportPath -Encoding UTF8 |
            ConvertFrom-Json -Depth 20
        $resumeCompletedCount = [int]$resumeReport.completed_count
        if ($resumeReport.schema_version -cne "lrr-agent003-cli-prod-uat-report-v1" -or
            $resumeCompletedCount -lt 1 -or
            $resumeCompletedCount -ge $ExpectedCaseCount -or
            [int]$resumeReport.canonical_case_count -ne $ExpectedCaseCount -or
            [int]$resumeReport.aggregate_credit_cap -ne $AggregateCreditCap -or
            $resumeReport.overall_status -cne "PASS_WITH_RESIDUAL" -or
            $resumeReport.stop_required -ne $false -or
            @($resumeReport.cases).Count -ne $resumeCompletedCount) {
            throw "Existing aggregate report is not a resumable PASS_WITH_RESIDUAL prefix."
        }
        for ($resumeIndex = 0; $resumeIndex -lt $resumeCompletedCount; $resumeIndex++) {
            if ($resumeReport.cases[$resumeIndex].case_id -cne $cases[$resumeIndex].id -or
                $resumeReport.cases[$resumeIndex].status -cne "PASS_WITH_RESIDUAL" -or
                $resumeReport.cases[$resumeIndex].timed_out -ne $false) {
                throw "Existing aggregate report has an invalid completed case prefix."
            }
            $completedOrdinal = $resumeIndex + 1
            $completedCaseRoot = Join-Path $OutputRoot (
                "{0:D2}-{1}" -f $completedOrdinal, $cases[$resumeIndex].id
            )
            $completedRunPath = Join-Path $completedCaseRoot "run.json"
            if (-not (Test-Path -LiteralPath $completedRunPath -PathType Leaf)) {
                throw "Resume completed run metadata is missing: $completedRunPath"
            }
            $completedRun = Get-Content -Raw -LiteralPath $completedRunPath -Encoding UTF8 |
                ConvertFrom-Json -Depth 20
            if ([double]$completedRun.approved_credit_cap -ne $ApprovedCreditCap -or
                [math]::Abs(
                    [double]$completedRun.previously_consumed_ai_credits -
                    $PreviouslyConsumedAiCredits
                ) -gt 0.000000001 -or
                [int]$completedRun.remaining_run_credit_cap -ne $AggregateCreditCap) {
                throw "Resume Credit continuity does not match the completed prefix."
            }
        }
        for ($futureIndex = $resumeCompletedCount; $futureIndex -lt $ExpectedCaseCount; $futureIndex++) {
            $futureOrdinal = $futureIndex + 1
            $futureCaseRoot = Join-Path $OutputRoot (
                "{0:D2}-{1}" -f $futureOrdinal, $cases[$futureIndex].id
            )
            foreach ($futureArtifact in @(
                $futureCaseRoot,
                (Join-Path $OutputRoot "collector-$futureOrdinal.stdout.log"),
                (Join-Path $OutputRoot "collector-$futureOrdinal.stderr.log")
            )) {
                if (Test-Path -LiteralPath $futureArtifact) {
                    throw "Resume refuses an unvalidated future artifact: $futureArtifact"
                }
            }
        }
        foreach ($resumeArtifact in @(
            (Join-Path $OutputRoot "boundary-fixture"),
            (Join-Path $OutputRoot "copilot-version-resume-$resumeCompletedCount.stdout.log"),
            (Join-Path $OutputRoot "copilot-version-resume-$resumeCompletedCount.stderr.log")
        )) {
            if (Test-Path -LiteralPath $resumeArtifact) {
                throw "Resume refuses an existing continuation artifact: $resumeArtifact"
            }
        }
        $resumeReportSha256 = Get-Sha256File -Path $reportPath
    }
    elseif (@(Get-ChildItem -LiteralPath $OutputRoot -Force).Count -ne 0) {
        throw "OutputRoot must be absent or empty; existing evidence is never overwritten."
    }
}
else {
    if ($ResumeValidatedOutput) {
        throw "Resume requires an existing OutputRoot."
    }
    [void](New-Item -ItemType Directory -Path $OutputRoot)
}

$neutralWorkspace = Join-Path $OutputRoot "neutral-workspace"
if ($ResumeValidatedOutput) {
    if (-not (Test-Path -LiteralPath $neutralWorkspace -PathType Container)) {
        throw "Resume neutral workspace is missing."
    }
}
else {
    [void](New-Item -ItemType Directory -Path $neutralWorkspace)
}
$CollectorPython = Get-FullPath -Path $CollectorPython
$LauncherPath = Get-FullPath -Path $LauncherPath
$CandidateRuntimeRoot = Get-FullPath -Path $CandidateRuntimeRoot
if (-not (Test-Path -LiteralPath $CandidateRuntimeRoot -PathType Container)) {
    throw "Candidate runtime root is missing: $CandidateRuntimeRoot"
}
if ($ResumeValidatedOutput) {
    $resumeValidationRoot = Join-Path $OutputRoot (
        "resume-validation-$resumeCompletedCount-" + [guid]::NewGuid().ToString("N")
    )
    [void](New-Item -ItemType Directory -Path $resumeValidationRoot)
    $resumeValidationReportPath = Join-Path $resumeValidationRoot "report.json"
    $resumeValidation = Invoke-NativeProcessToFiles `
        -FileName $CollectorPython `
        -Arguments @(
            "-B", $CollectorPath,
            "--cases", $CasesPath,
            "--raw-root", $OutputRoot,
            "--output", $resumeValidationReportPath,
            "--completed-count", [string]$resumeCompletedCount,
            "--aggregate-credit-cap", [string]$AggregateCreditCap
        ) `
        -WorkingDirectory $neutralWorkspace `
        -StdoutPath (Join-Path $resumeValidationRoot "stdout.log") `
        -StderrPath (Join-Path $resumeValidationRoot "stderr.log")
    if ($resumeValidation.ExitCode -ne 0 -or
        -not (Test-Path -LiteralPath $resumeValidationReportPath -PathType Leaf)) {
        throw "Resume prefix failed independent collector revalidation: $resumeValidationRoot"
    }
    $resumeValidationReportSha256 = Get-Sha256File -Path $resumeValidationReportPath
    if ($resumeValidationReportSha256 -cne $resumeReportSha256) {
        throw "Resume prefix report differs from independent collector revalidation."
    }
}
$powerShell = Get-PowerShellExecutable
$installedLauncherIdentity = Get-LauncherManifestIdentity -Launcher $LauncherPath
if (-not $installedLauncherIdentity.InstallRoot.Equals(
    $CandidateRuntimeRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Installed-product launcher manifest install_root does not match CandidateRuntimeRoot."
}
Assert-OwnedMcpSpoolWritable -LauncherIdentity $installedLauncherIdentity
$versionEvidenceStem = if ($ResumeValidatedOutput) {
    "copilot-version-resume-$resumeCompletedCount"
}
else {
    "copilot-version"
}
$copilotCliIdentity = Get-CopilotCliIdentity `
    -PowerShell $powerShell `
    -WorkingDirectory $neutralWorkspace `
    -StdoutPath (Join-Path $OutputRoot "$versionEvidenceStem.stdout.log") `
    -StderrPath (Join-Path $OutputRoot "$versionEvidenceStem.stderr.log")
if ($ResumeValidatedOutput) {
    Assert-ResumeIdentityMatches `
        -Report $resumeReport `
        -LauncherIdentity $installedLauncherIdentity `
        -CliIdentity $copilotCliIdentity `
        -PerCaseCreditCap $PerCaseMaxAiCredits
}
$caseTimeoutSeconds = $CaseTimeoutMinutes * 60
$permissionContract = [ordered]@{
    available_tools = @($ExpectedAvailableTools)
    allow_tools = @($ExpectedAllowedTools)
    no_custom_instructions = $true
    no_ask_user = $true
    output_format = "json"
    stream = "off"
    no_auto_update = $true
    no_remote = $true
    no_remote_export = $true
    approval_observation = $ApprovalObservation
    approval_prompt_count_directly_observable = $false
}
$boundaryLauncher = $null
$finalCollectorExit = 1

for ($index = $resumeCompletedCount; $index -lt $cases.Count; $index++) {
    $priorCredits = 0.0
    if ($index -gt 0) {
        if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            throw "The prior aggregate Credit report is missing; refusing the next metered run."
        }
        $priorReport = Get-Content -Raw -LiteralPath $reportPath -Encoding UTF8 | ConvertFrom-Json -Depth 20
        if ($priorReport.schema_version -cne "lrr-agent003-cli-prod-uat-report-v1" -or
            [int]$priorReport.completed_count -ne $index -or
            $null -eq $priorReport.aggregate_ai_credits) {
            throw "The prior aggregate Credit report is not authoritative; refusing the next metered run."
        }
        $priorCredits = [double]$priorReport.aggregate_ai_credits
    }
    if ($priorCredits -lt 0 -or
        ($priorCredits + $PerCaseMaxAiCredits) -gt $AggregateCreditCap) {
        throw "The next CLI soft cap could exceed the aggregate Credit cap; stopping before case $($index + 1)."
    }
    $case = $cases[$index]
    $ordinal = $index + 1
    $caseRoot = Join-Path $OutputRoot ("{0:D2}-{1}" -f $ordinal, $case.id)
    [void](New-Item -ItemType Directory -Path $caseRoot)
    $logRoot = Join-Path $caseRoot "copilot-logs"
    [void](New-Item -ItemType Directory -Path $logRoot)
    $activeLauncher = $LauncherPath
    if ($case.launcher_scope -ceq "temporary_boundary_fixture") {
        if ($null -eq $boundaryLauncher) {
            $boundaryLauncher = New-TemporaryBoundaryInstall `
                -Root (Join-Path $OutputRoot "boundary-fixture") `
                -SourceRuntimeRoot $CandidateRuntimeRoot `
                -NeutralWorkspace $neutralWorkspace
        }
        $activeLauncher = $boundaryLauncher
    }
    $launcherIdentity = Get-LauncherManifestIdentity -Launcher $activeLauncher
    if ($case.launcher_scope -ceq "installed_product" -and
        -not $launcherIdentity.InstallRoot.Equals(
            $CandidateRuntimeRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Installed-product launcher escaped CandidateRuntimeRoot."
    }
    $stdout = Join-Path $caseRoot "copilot.jsonl"
    $stderr = Join-Path $caseRoot "stderr.log"
    $otel = Join-Path $caseRoot "otel.jsonl"
    $copilotArguments = @(
        "--prompt", [string]$case.prompt,
        "--output-format", "json",
        "--stream", "off",
        "--max-ai-credits", [string]$PerCaseMaxAiCredits,
        "--no-auto-update",
        "--no-ask-user",
        "--no-remote",
        "--no-remote-export",
        "--log-dir", $logRoot
    )
    $powerShellArguments = @(New-LauncherProcessArguments `
        -Launcher $activeLauncher `
        -Tier ([string]$case.tier) `
        -CopilotArguments $copilotArguments)
    $result = Invoke-NativeProcessToFiles `
        -FileName $powerShell `
        -Arguments $powerShellArguments `
        -WorkingDirectory $neutralWorkspace `
        -StdoutPath $stdout `
        -StderrPath $stderr `
        -Environment @{
            COPILOT_OTEL_ENABLED = "true"
            COPILOT_OTEL_EXPORTER_TYPE = "file"
            COPILOT_OTEL_FILE_EXPORTER_PATH = $otel
            COPILOT_OTEL_SOURCE_NAME = "lrr-agent003-cli-prod-uat"
            OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = "false"
        } `
        -TimeoutSeconds $caseTimeoutSeconds
    if (-not (Test-Path -LiteralPath $otel -PathType Leaf)) {
        Write-Utf8NoBom -Path $otel -Value ""
    }
    Write-JsonFile -Path (Join-Path $caseRoot "run.json") -Value ([ordered]@{
        schema_version = $RunSchema
        case_id = [string]$case.id
        tier = [string]$case.tier
        launcher_scope = [string]$case.launcher_scope
        candidate_runtime_root = $CandidateRuntimeRoot
        launcher_path = $launcherIdentity.LauncherPath
        launcher_install_root = $launcherIdentity.InstallRoot
        launcher_sha256 = $launcherIdentity.LauncherSha256
        launcher_manifest_schema = 1
        launcher_manifest_path = $launcherIdentity.ManifestPath
        launcher_manifest_sha256 = $launcherIdentity.ManifestSha256
        copilot_home = $launcherIdentity.CopilotHome
        cli_path = $copilotCliIdentity.Path
        cli_sha256 = $copilotCliIdentity.Sha256
        cli_version = $copilotCliIdentity.Version
        cli_version_evidence_path = $copilotCliIdentity.VersionEvidencePath
        cli_version_evidence_sha256 = $copilotCliIdentity.VersionEvidenceSha256
        noninteractive_permission_contract = $permissionContract
        prompt_sha256 = Get-Sha256Text -Value ([string]$case.prompt)
        max_ai_credits = $PerCaseMaxAiCredits
        approved_credit_cap = $ApprovedCreditCap
        previously_consumed_ai_credits = $PreviouslyConsumedAiCredits
        remaining_run_credit_cap = $AggregateCreditCap
        resume_from_completed_count = $resumeCompletedCount
        resume_report_sha256 = $resumeReportSha256
        resume_validation_report_path = $resumeValidationReportPath
        resume_validation_report_sha256 = $resumeValidationReportSha256
        fresh_session = $true
        retry_count = 0
        process_id = $result.ProcessId
        started_at = $result.StartedAt
        finished_at = $result.FinishedAt
        exit_code = $result.ExitCode
        timeout_seconds = $result.TimeoutSeconds
        timed_out = $result.TimedOut
        process_tree_terminated = $result.ProcessTreeTerminated
        stdout_bytes = $result.StdoutBytes
        stderr_bytes = $result.StderrBytes
    })
    $collector = Invoke-Collector `
        -Python $CollectorPython `
        -Cases $CasesPath `
        -RawRoot $OutputRoot `
        -Output $reportPath `
        -CompletedCount $ordinal `
        -WorkingDirectory $neutralWorkspace
    $finalCollectorExit = $collector.ExitCode
    if ($result.TimedOut) {
        throw "Case $ordinal timed out after $caseTimeoutSeconds seconds; its process tree was terminated and no retry is permitted."
    }
    if ($collector.ExitCode -ne 0) {
        throw "Evidence or aggregate Credit gate stopped after case $ordinal; inspect $reportPath and collector logs."
    }
}

if ($finalCollectorExit -ne 0) {
    throw "The five-case UAT completed with one or more failed gates; inspect the aggregate report in OutputRoot."
}
$finalReport = Get-Content -Raw -LiteralPath $reportPath -Encoding UTF8 |
    ConvertFrom-Json -Depth 20
if ($finalReport.schema_version -cne "lrr-agent003-cli-prod-uat-report-v1" -or
    $finalReport.overall_status -cne "PASS_WITH_RESIDUAL" -or
    $finalReport.approval_observation -cne $ApprovalObservation) {
    throw "The final collector report did not satisfy PASS_WITH_RESIDUAL / NO_OBSERVED_PROMPT."
}
Write-Output "PASS_WITH_RESIDUAL: five fresh, sequential, retry-free UAT cases completed; approval state is NO_OBSERVED_PROMPT because CLI 1.0.77 does not expose a direct prompt counter."
