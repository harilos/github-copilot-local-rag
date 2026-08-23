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
    [switch]$AllowMeteredRun,

    [Parameter()]
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$CaseSchema = "lrr-agent003-cli-prod-uat-case-v1"
$RunSchema = "lrr-agent003-cli-prod-uat-run-v1"
$ExpectedCaseCount = 5
$PerCaseMaxAiCredits = 30
$AggregateCreditCap = 50
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
    return [pscustomobject]@{
        LauncherPath = $resolvedLauncher
        LauncherSha256 = $launcherHash
        ManifestPath = $manifestPath
        ManifestSha256 = Get-Sha256File -Path $manifestPath
        CopilotHome = $copilotHome
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
        $values[4].launcher_scope -cne "temporary_boundary_fixture") {
        throw "The deterministic boundary fixture revision is not canonical."
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
        [Parameter()][hashtable]$Environment = @{}
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
    try {
        if (-not $process.Start()) { throw "Process did not start: $FileName" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        Write-Utf8NoBom -Path $StdoutPath -Value $stdout
        Write-Utf8NoBom -Path $StderrPath -Value $stderr
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            ProcessId = $process.Id
            StartedAt = $startedAt.ToString("o")
            FinishedAt = [DateTimeOffset]::UtcNow.ToString("o")
            StdoutBytes = [System.Text.Encoding]::UTF8.GetByteCount($stdout)
            StderrBytes = [System.Text.Encoding]::UTF8.GetByteCount($stderr)
        }
    }
    finally {
        $process.Dispose()
    }
}

function Get-PowerShellExecutable {
    $candidate = Join-Path $PSHOME "pwsh.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "PowerShell 7 executable was not found in PSHOME."
    }
    return (Get-FullPath -Path $candidate)
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
    foreach ($source in @($sourceVenv, $sourceSetup, (Join-Path $sourceRoot "rag\copilot-cli"))) {
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
if (Test-Path -LiteralPath $OutputRoot) {
    if (@(Get-ChildItem -LiteralPath $OutputRoot -Force).Count -ne 0) {
        throw "OutputRoot must be absent or empty; existing evidence is never overwritten."
    }
}
else {
    [void](New-Item -ItemType Directory -Path $OutputRoot)
}

$neutralWorkspace = Join-Path $OutputRoot "neutral-workspace"
[void](New-Item -ItemType Directory -Path $neutralWorkspace)
$CollectorPython = Get-FullPath -Path $CollectorPython
$LauncherPath = Get-FullPath -Path $LauncherPath
$powerShell = Get-PowerShellExecutable
$boundaryLauncher = $null
$finalCollectorExit = 1
$reportPath = Join-Path $OutputRoot "lrr-agent003-cli-prod-uat-report.json"

for ($index = 0; $index -lt $cases.Count; $index++) {
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
        if ($priorCredits -lt 0 -or
            ($priorCredits + $PerCaseMaxAiCredits) -gt $AggregateCreditCap) {
            throw "The next CLI minimum soft cap could exceed the aggregate Credit cap; stopping before case $($index + 1)."
        }
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
        }
    Write-JsonFile -Path (Join-Path $caseRoot "run.json") -Value ([ordered]@{
        schema_version = $RunSchema
        case_id = [string]$case.id
        tier = [string]$case.tier
        launcher_scope = [string]$case.launcher_scope
        launcher_sha256 = $launcherIdentity.LauncherSha256
        launcher_manifest_schema = 1
        launcher_manifest_path = $launcherIdentity.ManifestPath
        launcher_manifest_sha256 = $launcherIdentity.ManifestSha256
        copilot_home = $launcherIdentity.CopilotHome
        prompt_sha256 = Get-Sha256Text -Value ([string]$case.prompt)
        max_ai_credits = $PerCaseMaxAiCredits
        fresh_session = $true
        retry_count = 0
        process_id = $result.ProcessId
        started_at = $result.StartedAt
        finished_at = $result.FinishedAt
        exit_code = $result.ExitCode
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
    if ($collector.ExitCode -in @(2, 3)) {
        throw "Evidence or aggregate Credit gate stopped after case $ordinal; inspect $reportPath and collector logs."
    }
}

if ($finalCollectorExit -ne 0) {
    throw "The five-case UAT completed with one or more failed gates; inspect the aggregate report in OutputRoot."
}
Write-Output "PASS: five fresh, sequential, retry-free UAT cases completed within the aggregate Credit gate."
