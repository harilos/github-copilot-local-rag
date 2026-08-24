[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [string]$AuthorityPath = "",

    [Parameter()]
    [string]$CollectorPath = "",

    [Parameter()]
    [string]$CollectorPython = (Join-Path $env:USERPROFILE ".copilot\rag\query\.venv\Scripts\python.exe"),

    [Parameter()]
    [string]$BaselineReportPath,

    [Parameter()]
    [string]$CandidateRuntimeRoot,

    [Parameter()]
    [string]$LauncherPath,

    [Parameter()]
    [string]$ExpectedFreshCopilotHome,

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

if ([string]::IsNullOrWhiteSpace($AuthorityPath)) {
    $AuthorityPath = Join-Path $PSScriptRoot "data\lrr-agent003-cli-thorough-fix-uat-v1.json"
}
if ([string]::IsNullOrWhiteSpace($CollectorPath)) {
    $CollectorPath = Join-Path $PSScriptRoot "collect_lrr_agent003_cli_thorough_fix_uat.py"
}

$AuthoritySchema = "lrr-agent003-cli-thorough-fix-uat-authority-v1"
$RunSchema = "lrr-agent003-cli-thorough-fix-uat-run-v1"
$ExpectedCaseId = "LRR-AGENT003-CLI-THOROUGH-FIX-1"
$ExpectedPromptSha256 = "7b1c7a9f4ee8b3b58bd56b9d9d893f1742670f81a5d4f65025fbe4756df767b9"
$ExpectedBaselineSha256 = "07be1ac03414e7b832b84ff8f1bb5aeef2382d3f57f9dd339e2f21b3347f229c"
$ExpectedBaselineNanoAiu = [long]38075996000
$ExpectedTotalCapNanoAiu = [long]50000000000
$ExpectedRemainingNanoAiu = [long]11924004000
$SupportedCopilotCliVersion = "1.0.77"

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
    Write-Utf8NoBom -Path $Path -Value (($Value | ConvertTo-Json -Depth 20) + "`n")
}

function Read-Authority {
    param([Parameter(Mandatory)][string]$Path)
    $resolved = Get-FullPath -Path $Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Authority is missing: $resolved"
    }
    $authority = Get-Content -Raw -LiteralPath $resolved -Encoding UTF8 | ConvertFrom-Json
    if ([string]$authority.schema_version -cne $AuthoritySchema -or
        [int]$authority.formal_session_count -ne 1 -or
        [int]$authority.retry_count -ne 0) {
        throw "Authority does not permit exactly one no-retry formal session."
    }
    $case = $authority.case
    if ([string]$case.id -cne $ExpectedCaseId -or
        [string]$case.tier -cne "thorough" -or
        [string]$case.requested_model -cne "auto" -or
        [string]$case.expected_agent -cne "local-rag-agent003-thorough" -or
        [string]::IsNullOrWhiteSpace([string]$case.prompt) -or
        (Get-Sha256Text -Value ([string]$case.prompt)) -cne $ExpectedPromptSha256 -or
        [string]$case.expected_database -cne "fizzbuzz-planet-rag") {
        throw "Canonical one-case authority mismatch."
    }
    $credit = $authority.credit_authority
    if ([string]$credit.baseline_report_sha256 -cne $ExpectedBaselineSha256 -or
        [long]$credit.baseline_total_nano_aiu -ne $ExpectedBaselineNanoAiu -or
        [long]$credit.maximum_total_nano_aiu -ne $ExpectedTotalCapNanoAiu -or
        [long]$credit.maximum_additional_nano_aiu -ne $ExpectedRemainingNanoAiu -or
        [int]$credit.cli_max_ai_credits -ne 30 -or
        $credit.cli_floor_is_not_spend_authority -ne $true) {
        throw "Exact nano-AIU authority mismatch."
    }
    if ($credit.PSObject.Properties.Name -contains "baseline_report_path") {
        throw "Tracked authority must not contain a local baseline path."
    }
    $execution = $authority.execution_contract
    if ($execution.fresh_copilot_home -ne $true -or
        $execution.fresh_workspace -ne $true -or
        $execution.fresh_session -ne $true -or
        $execution.reuse_session -ne $false -or
        [int]$execution.formal_prompt_send_count -ne 1 -or
        [int]$execution.retry_count -ne 0) {
        throw "Fresh profile/session authority mismatch."
    }
    return [pscustomobject]@{
        Value = $authority
        Path = $resolved
        Sha256 = Get-Sha256File -Path $resolved
        Case = $case
    }
}

function Assert-BaselinePin {
    param([Parameter(Mandatory)][string]$Path)
    $path = Get-FullPath -Path $Path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Pinned r8 baseline report is missing: $path"
    }
    $liveSha = Get-Sha256File -Path $path
    if ($liveSha -cne $ExpectedBaselineSha256) {
        throw "Pinned r8 baseline report SHA changed."
    }
    $report = Get-Content -Raw -LiteralPath $path -Encoding UTF8 | ConvertFrom-Json
    $nano = $null
    if ($report.PSObject.Properties.Name -contains "true_total_nano_aiu") {
        $nano = [long]$report.true_total_nano_aiu
    }
    elseif ($report.PSObject.Properties.Name -contains "true_total_ai_credits") {
        $decimalCredits = [decimal]([string]$report.true_total_ai_credits)
        $scaled = $decimalCredits * [decimal]1000000000
        if ($scaled -ne [decimal]::Truncate($scaled)) {
            throw "Pinned r8 baseline cannot be represented exactly in nano-AIU."
        }
        $nano = [long]$scaled
    }
    if ($null -eq $nano -or $nano -ne $ExpectedBaselineNanoAiu) {
        throw "Pinned r8 baseline total is not 38.075996 Credits."
    }
    if (($ExpectedBaselineNanoAiu + $ExpectedRemainingNanoAiu) -ne $ExpectedTotalCapNanoAiu) {
        throw "Pinned nano-AIU arithmetic is inconsistent."
    }
    return [pscustomobject]@{ Path = $path; Sha256 = $liveSha; TotalNanoAiu = $nano }
}

function Get-LauncherManifestIdentity {
    param(
        [Parameter(Mandatory)][string]$Launcher,
        [Parameter(Mandatory)][string]$CandidateRoot,
        [Parameter(Mandatory)][string]$ExpectedCopilotHome
    )
    $resolvedLauncher = Get-FullPath -Path $Launcher
    $candidate = Get-FullPath -Path $CandidateRoot
    $expectedHome = Get-FullPath -Path $ExpectedCopilotHome
    if (-not (Test-Path -LiteralPath $resolvedLauncher -PathType Leaf)) {
        throw "Launcher is missing: $resolvedLauncher"
    }
    $manifestPath = Get-FullPath -Path (Join-Path (Split-Path -Parent $resolvedLauncher) "owned-manifest.json")
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Owned launcher manifest is missing."
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 | ConvertFrom-Json
    if ([int]$manifest.schema -ne 1) { throw "Owned launcher manifest schema mismatch." }
    $installRoot = Get-FullPath -Path ([string]$manifest.install_root)
    $copilotHome = Get-FullPath -Path ([string]$manifest.copilot_home)
    if (-not $installRoot.Equals($candidate, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Manifest install_root does not equal CandidateRuntimeRoot."
    }
    if (-not $copilotHome.Equals($expectedHome, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Manifest COPILOT_HOME does not equal the explicitly pinned fresh profile."
    }
    $expectedLauncher = Get-FullPath -Path (Join-Path $installRoot "copilot-cli\local-rag-agent003.ps1")
    if (-not $resolvedLauncher.Equals($expectedLauncher, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Launcher path does not belong to the candidate install."
    }
    $entries = @($manifest.artifacts | Where-Object {
        [string]$_.root -ceq "install_root" -and
        ([string]$_.path).Replace("\", "/") -ceq "copilot-cli/local-rag-agent003.ps1"
    })
    if ($entries.Count -ne 1) { throw "Launcher artifact is not uniquely owned." }
    $launcherSha = Get-Sha256File -Path $resolvedLauncher
    $launcherBytes = (Get-Item -LiteralPath $resolvedLauncher).Length
    if ([string]$entries[0].sha256 -cne $launcherSha -or [long]$entries[0].bytes -ne $launcherBytes) {
        throw "Launcher differs from the owned manifest."
    }
    $launcherText = [System.IO.File]::ReadAllText($resolvedLauncher, [System.Text.Encoding]::UTF8)
    foreach ($fragment in @(
        'thorough = @{ Agent = "local-rag-agent003-thorough"; Model = "auto" }',
        '"--available-tools=$ToolList"',
        '"--allow-tool=$AllowList"',
        '"--no-custom-instructions"'
    )) {
        if (-not $launcherText.Contains($fragment)) {
            throw "Launcher is missing the tested thorough/permission contract: $fragment"
        }
    }
    return [pscustomobject]@{
        LauncherPath = $resolvedLauncher
        LauncherSha256 = $launcherSha
        ManifestPath = $manifestPath
        ManifestSha256 = Get-Sha256File -Path $manifestPath
        InstallRoot = $installRoot
        CopilotHome = $copilotHome
    }
}

function Get-PowerShell7 {
    $commands = @(Get-Command "pwsh.exe" -CommandType Application -All -ErrorAction Stop)
    if ($commands.Count -lt 1) { throw "PowerShell 7 (pwsh.exe) is required." }
    return Get-FullPath -Path ([string]$commands[0].Source)
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
    foreach ($name in $Environment.Keys) { $start.Environment[[string]$name] = [string]$Environment[$name] }
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
                throw "Timed-out process tree did not terminate."
            }
            $treeTerminated = $true
        }
        elseif (-not $process.HasExited) {
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
            TimedOut = $timedOut
            TimeoutSeconds = $TimeoutSeconds
            ProcessTreeTerminated = $treeTerminated
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
        [Parameter(Mandatory)][string]$EvidenceRoot
    )
    $commands = @(Get-Command "copilot" -CommandType Application -All -ErrorAction Stop)
    if ($commands.Count -lt 1) { throw "Copilot CLI executable was not found." }
    $path = Get-FullPath -Path ([string]$commands[0].Source)
    $stdout = Join-Path $EvidenceRoot "copilot-version.stdout.log"
    $stderr = Join-Path $EvidenceRoot "copilot-version.stderr.log"
    $result = Invoke-NativeProcessToFiles `
        -FileName $PowerShell `
        -Arguments @(
            "-NoProfile", "-NonInteractive", "-Command",
            '& ([System.Environment]::GetEnvironmentVariable(''LOCAL_RAG_UAT_CLI_PATH'', ''Process'')) --version'
        ) `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath $stdout `
        -StderrPath $stderr `
        -Environment @{ LOCAL_RAG_UAT_CLI_PATH = $path } `
        -TimeoutSeconds 30
    if ($result.TimedOut -or $result.ExitCode -ne 0) { throw "Copilot CLI version preflight failed." }
    $text = [System.IO.File]::ReadAllText($stdout, [System.Text.Encoding]::UTF8).Trim()
    $match = [regex]::Match($text, '^GitHub Copilot CLI (?<version>[0-9]+\.[0-9]+\.[0-9]+)\.?$')
    if (-not $match.Success -or $match.Groups['version'].Value -cne $SupportedCopilotCliVersion) {
        throw "This evidence collector is pinned to Copilot CLI $SupportedCopilotCliVersion."
    }
    return [pscustomobject]@{
        Path = $path
        Sha256 = Get-Sha256File -Path $path
        Version = $match.Groups['version'].Value
        VersionEvidencePath = Get-FullPath -Path $stdout
        VersionEvidenceSha256 = Get-Sha256File -Path $stdout
    }
}

function Invoke-CollectorSnapshot {
    param(
        [Parameter(Mandatory)][string]$CopilotHome,
        [Parameter(Mandatory)][string]$Output,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    $stdout = $Output + ".stdout.log"
    $stderr = $Output + ".stderr.log"
    $result = Invoke-NativeProcessToFiles `
        -FileName $CollectorPython `
        -Arguments @(
            "-B", $CollectorPath,
            "--snapshot-copilot-home", $CopilotHome,
            "--snapshot-output", $Output
        ) `
        -WorkingDirectory $WorkingDirectory `
        -StdoutPath $stdout `
        -StderrPath $stderr `
        -TimeoutSeconds 30
    if ($result.TimedOut -or $result.ExitCode -ne 0 -or
        -not (Test-Path -LiteralPath $Output -PathType Leaf)) {
        throw "Session-store snapshot failed; inspect $stderr"
    }
    return (Get-Content -Raw -LiteralPath $Output -Encoding UTF8 | ConvertFrom-Json)
}

function Get-FormalCopilotArguments {
    param([Parameter(Mandatory)][string]$Prompt)
    return @(
        "--prompt", $Prompt,
        "--output-format", "json",
        "--stream", "off",
        "--max-ai-credits", "30",
        "--no-auto-update",
        "--no-ask-user",
        "--no-remote",
        "--no-remote-export"
    )
}

function Invoke-RunnerSelfTest {
    $authority = Read-Authority -Path $AuthorityPath
    if (($ExpectedBaselineNanoAiu + $ExpectedRemainingNanoAiu) -ne $ExpectedTotalCapNanoAiu) {
        throw "Self-test nano pin failed."
    }
    $prompt = [string]$authority.Case.prompt
    $arguments = @(Get-FormalCopilotArguments -Prompt $prompt)
    if (@($arguments | Where-Object { $_ -ceq "--prompt" }).Count -ne 1 -or
        @($arguments | Where-Object { $_ -ceq $prompt }).Count -ne 1 -or
        @($arguments | Where-Object { $_ -ceq "--max-ai-credits" }).Count -ne 1 -or
        @($arguments | Where-Object { $_ -ceq "30" }).Count -ne 1 -or
        $arguments -contains "--resume" -or
        $arguments -contains "--session-id" -or
        $arguments -contains "--model") {
        throw "Self-test formal invocation is not exactly one fresh auto request."
    }
    $collectorResult = & $CollectorPython -B $CollectorPath --authority $authority.Path --self-test 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Collector self-test failed: $($collectorResult -join [Environment]::NewLine)"
    }
    Write-Output "runner self-test: PASS (exact-one invocation shape; no launcher or prompt executed)"
    Write-Output ($collectorResult -join [Environment]::NewLine)
}

if ($SelfTest) {
    Invoke-RunnerSelfTest
    return
}

$authority = Read-Authority -Path $AuthorityPath
foreach ($required in @($CollectorPath, $CollectorPython)) {
    if ([string]::IsNullOrWhiteSpace($required) -or -not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required harness file is missing: $required"
    }
}
foreach ($requiredValue in @(
    $BaselineReportPath, $CandidateRuntimeRoot, $LauncherPath,
    $ExpectedFreshCopilotHome, $OutputRoot
)) {
    if ([string]::IsNullOrWhiteSpace($requiredValue)) {
        throw "BaselineReportPath, CandidateRuntimeRoot, LauncherPath, ExpectedFreshCopilotHome, and OutputRoot are required."
    }
}
$baseline = Assert-BaselinePin -Path $BaselineReportPath

$CandidateRuntimeRoot = Get-FullPath -Path $CandidateRuntimeRoot
$LauncherPath = Get-FullPath -Path $LauncherPath
$ExpectedFreshCopilotHome = Get-FullPath -Path $ExpectedFreshCopilotHome
$OutputRoot = Get-FullPath -Path $OutputRoot
$repositoryRoot = Get-FullPath -Path (Join-Path $PSScriptRoot "..\..\..")
if (Test-PathInside -Path $OutputRoot -Root $repositoryRoot) {
    throw "Raw paid-UAT evidence must be outside the repository."
}
if (Test-Path -LiteralPath $OutputRoot) {
    throw "OutputRoot already exists. This runner never resumes or retries a formal session."
}

$launcher = Get-LauncherManifestIdentity `
    -Launcher $LauncherPath `
    -CandidateRoot $CandidateRuntimeRoot `
    -ExpectedCopilotHome $ExpectedFreshCopilotHome

if (-not $AllowMeteredRun) {
    Write-Output "AUTHORITY_AND_MANIFEST_PREFLIGHT_PASS_NO_PROMPT_SENT"
    Write-Output "Fresh-usage and CLI identity are checked under a new OutputRoot immediately before the authorized send."
    Write-Output "Re-run once with -AllowMeteredRun and a new non-existent OutputRoot to authorize the single formal session."
    return
}

[void](New-Item -ItemType Directory -Path $OutputRoot)
$neutralWorkspace = Join-Path $OutputRoot "neutral-workspace"
$rawRoot = Join-Path $OutputRoot "raw"
$runRoot = Join-Path $rawRoot "run-001"
$logRoot = Join-Path $runRoot "copilot-logs"
$reportRoot = Join-Path $OutputRoot "reports"
foreach ($directory in @($neutralWorkspace, $rawRoot, $runRoot, $logRoot, $reportRoot)) {
    [void](New-Item -ItemType Directory -Path $directory)
}
$powerShell = Get-PowerShell7
$cli = Get-CopilotCliIdentity `
    -PowerShell $powerShell `
    -WorkingDirectory $neutralWorkspace `
    -EvidenceRoot $OutputRoot

$beforePath = Join-Path $runRoot "usage-before.json"
$before = Invoke-CollectorSnapshot `
    -CopilotHome $launcher.CopilotHome `
    -Output $beforePath `
    -WorkingDirectory $neutralWorkspace
if ([long]$before.row_count -ne 0 -or [long]$before.total_nano_aiu -ne 0) {
    throw "ExpectedFreshCopilotHome already contains usage. Refusing the only formal send."
}

$preflightPath = Join-Path $OutputRoot "preflight.json"
Write-JsonFile -Path $preflightPath -Value ([ordered]@{
    schema_version = "lrr-agent003-cli-thorough-fix-uat-preflight-v1"
    status = "PASS"
    captured_at = [DateTimeOffset]::UtcNow.ToString("o")
    authority_path = $authority.Path
    authority_sha256 = $authority.Sha256
    runner_path = Get-FullPath -Path $PSCommandPath
    runner_sha256 = Get-Sha256File -Path $PSCommandPath
    collector_path = Get-FullPath -Path $CollectorPath
    collector_sha256 = Get-Sha256File -Path $CollectorPath
    baseline_report_path = $baseline.Path
    baseline_report_sha256 = $baseline.Sha256
    baseline_total_nano_aiu = $baseline.TotalNanoAiu
    maximum_additional_nano_aiu = $ExpectedRemainingNanoAiu
    maximum_total_nano_aiu = $ExpectedTotalCapNanoAiu
    cli_max_ai_credits = 30
    cli_floor_is_not_spend_authority = $true
    candidate_runtime_root = $CandidateRuntimeRoot
    launcher_path = $launcher.LauncherPath
    launcher_sha256 = $launcher.LauncherSha256
    launcher_manifest_path = $launcher.ManifestPath
    launcher_manifest_sha256 = $launcher.ManifestSha256
    copilot_home = $launcher.CopilotHome
    usage_before_sha256 = Get-Sha256File -Path $beforePath
    usage_before_row_count = [long]$before.row_count
    usage_before_total_nano_aiu = [long]$before.total_nano_aiu
    neutral_workspace = $neutralWorkspace
    cli_path = $cli.Path
    cli_sha256 = $cli.Sha256
    cli_version = $cli.Version
    requested_model = "auto"
    prompt_sha256 = $ExpectedPromptSha256
    formal_prompt_send_count_before = 0
    retry_count = 0
})

$sendLockPath = Join-Path $OutputRoot "formal-send-lock.json"
if (Test-Path -LiteralPath $sendLockPath) {
    throw "Formal send lock already exists. Refusing any retry."
}
Write-JsonFile -Path $sendLockPath -Value ([ordered]@{
    schema_version = "lrr-agent003-cli-thorough-fix-uat-send-lock-v1"
    created_at = [DateTimeOffset]::UtcNow.ToString("o")
    authority_sha256 = $authority.Sha256
    case_id = $ExpectedCaseId
    prompt_sha256 = $ExpectedPromptSha256
    maximum_formal_prompt_send_count = 1
    retry_count = 0
})

$copilotJsonl = Join-Path $runRoot "copilot.jsonl"
$stderr = Join-Path $runRoot "stderr.log"
$otel = Join-Path $runRoot "otel.jsonl"
$formalArguments = @(
    "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    "-File", $launcher.LauncherPath, "-Tier", "thorough"
) + @(Get-FormalCopilotArguments -Prompt ([string]$authority.Case.prompt)) + @("--log-dir", $logRoot)

# This is the sole code path that invokes the launcher.  The send lock is
# durable before entry, and this runner has no resume/retry branch.
$formal = Invoke-NativeProcessToFiles `
    -FileName $powerShell `
    -Arguments $formalArguments `
    -WorkingDirectory $neutralWorkspace `
    -StdoutPath $copilotJsonl `
    -StderrPath $stderr `
    -Environment @{
        COPILOT_OTEL_ENABLED = "true"
        COPILOT_OTEL_EXPORTER_TYPE = "file"
        COPILOT_OTEL_FILE_EXPORTER_PATH = $otel
        COPILOT_OTEL_SOURCE_NAME = "lrr-agent003-cli-thorough-fix-uat"
        OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = "false"
    } `
    -TimeoutSeconds ($CaseTimeoutMinutes * 60)

if (-not (Test-Path -LiteralPath $otel -PathType Leaf)) {
    Write-Utf8NoBom -Path $otel -Value ""
}
$afterPath = Join-Path $runRoot "usage-after.json"
$after = Invoke-CollectorSnapshot `
    -CopilotHome $launcher.CopilotHome `
    -Output $afterPath `
    -WorkingDirectory $neutralWorkspace

Write-JsonFile -Path (Join-Path $runRoot "run.json") -Value ([ordered]@{
    schema_version = $RunSchema
    case_id = $ExpectedCaseId
    authority_sha256 = $authority.Sha256
    tier = "thorough"
    requested_model = "auto"
    prompt_sha256 = $ExpectedPromptSha256
    formal_prompt_send_count = 1
    retry_count = 0
    fresh_session = $true
    fresh_copilot_home = $true
    fresh_workspace = $true
    max_ai_credits = 30
    logical_remaining_nano_aiu = $ExpectedRemainingNanoAiu
    baseline_total_nano_aiu = $ExpectedBaselineNanoAiu
    baseline_report_path = $baseline.Path
    maximum_total_nano_aiu = $ExpectedTotalCapNanoAiu
    launcher_scope = "installed_product"
    candidate_runtime_root = $CandidateRuntimeRoot
    launcher_path = $launcher.LauncherPath
    launcher_install_root = $launcher.InstallRoot
    launcher_sha256 = $launcher.LauncherSha256
    launcher_manifest_schema = 1
    launcher_manifest_path = $launcher.ManifestPath
    launcher_manifest_sha256 = $launcher.ManifestSha256
    copilot_home = $launcher.CopilotHome
    cli_path = $cli.Path
    cli_sha256 = $cli.Sha256
    cli_version = $cli.Version
    cli_version_evidence_path = $cli.VersionEvidencePath
    cli_version_evidence_sha256 = $cli.VersionEvidenceSha256
    exit_code = $formal.ExitCode
    process_id = $formal.ProcessId
    started_at = $formal.StartedAt
    finished_at = $formal.FinishedAt
    timeout_seconds = $formal.TimeoutSeconds
    timed_out = $formal.TimedOut
    process_tree_terminated = $formal.ProcessTreeTerminated
    usage_before_sha256 = Get-Sha256File -Path $beforePath
    usage_after_sha256 = Get-Sha256File -Path $afterPath
    noninteractive_permission_contract = [ordered]@{
        available_tools = @(
            "localragagent003-local_rag_search",
            "localragagent003-local_rag_get_evidence"
        )
        allow_tools = @(
            "localragagent003(local_rag_search)",
            "localragagent003(local_rag_get_evidence)"
        )
        no_custom_instructions = $true
        no_ask_user = $true
        no_auto_update = $true
        no_remote = $true
        no_remote_export = $true
    }
})

$reportPath = Join-Path $reportRoot "lrr-agent003-cli-thorough-fix-uat-report.json"
$collectorRun = Invoke-NativeProcessToFiles `
    -FileName $CollectorPython `
    -Arguments @(
        "-B", $CollectorPath,
        "--authority", $authority.Path,
        "--raw-root", $rawRoot,
        "--output", $reportPath
    ) `
    -WorkingDirectory $neutralWorkspace `
    -StdoutPath (Join-Path $reportRoot "collector.stdout.log") `
    -StderrPath (Join-Path $reportRoot "collector.stderr.log") `
    -TimeoutSeconds 120

if ($collectorRun.TimedOut -or $collectorRun.ExitCode -ne 0) {
    throw "Formal session was sent exactly once and failed collection. No retry is permitted; inspect $reportRoot"
}
Write-Output "PASS: exactly one formal session completed and independently collected."
Write-Output $reportPath
