[CmdletBinding()]
param(
    [string]$CasesPath,
    [string]$CollectorPath,
    [Parameter(Mandatory = $true)]
    [string]$CollectorPython,
    [string]$CopilotPath = "copilot",
    [string]$AutoModel = "auto",
    [string]$MiniModel,
    [string]$StandardModel,
    [string]$FixtureWorkspace,
    [string]$VariablesJson,
    [string]$OutputRoot,
    [double]$MaxAiCreditsPerTurn = 30.0,
    [ValidateSet("A", "B")]
    [string]$Phase = "A",
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
if ([string]::IsNullOrWhiteSpace($CasesPath)) {
    $CasesPath = Join-Path $PSScriptRoot `
        "data/copilot-compliance-cases-v1.jsonl"
}
if ([string]::IsNullOrWhiteSpace($CollectorPath)) {
    $CollectorPath = Join-Path $PSScriptRoot `
        "collect_copilot_compliance.py"
}

function Read-JsonLines {
    param([Parameter(Mandatory = $true)][string]$Path)
    $items = @()
    $lineNumber = 0
    foreach ($line in [System.IO.File]::ReadLines($Path)) {
        $lineNumber += 1
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        try {
            $items += ($line | ConvertFrom-Json)
        }
        catch {
            throw "Invalid JSONL at ${Path}:${lineNumber}: $($_.Exception.Message)"
        }
    }
    return $items
}

function Resolve-Placeholders {
    param(
        [Parameter(Mandatory = $false)]$Value,
        [Parameter(Mandatory = $true)]$Variables
    )
    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [string]) {
        $resolved = $Value
        foreach ($property in $Variables.PSObject.Properties) {
            $token = "{{" + [string]$property.Name + "}}"
            $resolved = $resolved.Replace($token, [string]$property.Value)
        }
        return $resolved
    }
    if ($Value -is [System.Collections.IEnumerable] -and
        $Value -isnot [System.Management.Automation.PSCustomObject] -and
        $Value -isnot [System.Collections.IDictionary]) {
        $result = @()
        foreach ($item in $Value) {
            $result += ,(Resolve-Placeholders -Value $item -Variables $Variables)
        }
        return ,$result
    }
    if ($Value -is [System.Management.Automation.PSCustomObject] -or
        $Value -is [System.Collections.IDictionary]) {
        $result = [ordered]@{}
        $properties = if ($Value -is [System.Collections.IDictionary]) {
            $Value.GetEnumerator() | ForEach-Object {
                [pscustomobject]@{ Name = [string]$_.Key; Value = $_.Value }
            }
        }
        else {
            $Value.PSObject.Properties
        }
        foreach ($property in $properties) {
            $result[$property.Name] = Resolve-Placeholders `
                -Value $property.Value -Variables $Variables
        }
        return [pscustomobject]$result
    }
    return $Value
}

function Get-WorkspaceSnapshot {
    param([Parameter(Mandatory = $true)][string]$Root)
    $snapshot = @{}
    $rootPrefix = $Root.TrimEnd("\", "/") +
        [System.IO.Path]::DirectorySeparatorChar
    $excludedPrefixes = @(
        ".git/",
        ".copilot/rag/dbs/",
        ".copilot/rag/models/",
        ".copilot/rag/query/.venv/",
        ".copilot/rag/query/run/",
        ".copilot/rag/logs/"
    )
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -Force -File) {
        $relative = $file.FullName.Substring($rootPrefix.Length).Replace(
            "\", "/"
        )
        $excluded = $false
        foreach ($prefix in $excludedPrefixes) {
            if ($relative.StartsWith(
                $prefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                $excluded = $true
                break
            }
        }
        if (-not $excluded) {
            $snapshot[$relative] = (
                Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
            ).Hash
        }
    }
    return $snapshot
}

function Compare-WorkspaceSnapshot {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Before,
        [Parameter(Mandatory = $true)][hashtable]$After
    )
    $changes = @()
    $allPaths = @($Before.Keys + $After.Keys | Sort-Object -Unique)
    foreach ($path in $allPaths) {
        if (-not $Before.ContainsKey($path)) {
            $changes += [ordered]@{ path = $path; change = "added" }
        }
        elseif (-not $After.ContainsKey($path)) {
            $changes += [ordered]@{ path = $path; change = "removed" }
        }
        elseif ($Before[$path] -ne $After[$path]) {
            $changes += [ordered]@{ path = $path; change = "modified" }
        }
    }
    return $changes
}

function Set-ProcessEnvironment {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Saved,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $false)][AllowNull()][string]$Value
    )
    if (-not $Saved.ContainsKey($Name)) {
        $Saved[$Name] = [Environment]::GetEnvironmentVariable(
            $Name, "Process"
        )
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Restore-ProcessEnvironment {
    param([Parameter(Mandatory = $true)][hashtable]$Saved)
    foreach ($name in $Saved.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name, $Saved[$name], "Process"
        )
    }
}

function ConvertTo-WindowsCommandLineArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append("\" * ($backslashes * 2 + 1))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append("\" * $backslashes)
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append("\" * ($backslashes * 2))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-NativeProcessToFiles {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FileName
    $startInfo.Arguments = (
        $Arguments |
            ForEach-Object {
                ConvertTo-WindowsCommandLineArgument -Value ([string]$_)
            }
    ) -join " "
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $stdout = [System.IO.File]::Open(
        $StdoutPath,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read
    )
    $stderr = [System.IO.File]::Open(
        $StderrPath,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read
    )
    try {
        if (-not $process.Start()) {
            throw "Failed to start native process."
        }
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
        $stderrTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
        $process.WaitForExit()
        [System.Threading.Tasks.Task]::WaitAll(
            [System.Threading.Tasks.Task[]]@($stdoutTask, $stderrTask)
        )
        $stdout.Flush()
        $stderr.Flush()
        return [int]$process.ExitCode
    }
    finally {
        $stdout.Dispose()
        $stderr.Dispose()
        $process.Dispose()
    }
}

function Resolve-CopilotLaunch {
    param([Parameter(Mandatory = $true)][string]$Command)
    $resolved = Get-Command $Command -ErrorAction Stop
    $source = [string]$resolved.Source
    $extension = [System.IO.Path]::GetExtension($source)
    if ($extension -in @(".ps1", ".cmd")) {
        $baseDirectory = Split-Path -Parent $source
        $loader = Join-Path $baseDirectory `
            "node_modules/@github/copilot/npm-loader.js"
        if (-not (Test-Path -LiteralPath $loader -PathType Leaf)) {
            throw "The Copilot npm loader could not be resolved."
        }
        $node = Join-Path $baseDirectory "node.exe"
        if (-not (Test-Path -LiteralPath $node -PathType Leaf)) {
            $node = [string](
                Get-Command "node.exe" -ErrorAction Stop
            ).Source
        }
        return [pscustomobject]@{
            FileName = $node
            PrefixArguments = @($loader)
        }
    }
    if ($extension -ne ".exe") {
        throw "CopilotPath must resolve to an executable or npm shim."
    }
    return [pscustomobject]@{
        FileName = $source
        PrefixArguments = @()
    }
}

if ($SelfTest) {
    $placeholderProbe = Resolve-Placeholders `
        -Value "before {{NAME}} after {NAME}" `
        -Variables ([pscustomobject]@{ NAME = "resolved" })
    if ($placeholderProbe -ne "before resolved after {NAME}") {
        throw "PowerShell exact double-brace placeholder regression."
    }
    $caseProbe = @(Read-JsonLines -Path $CasesPath)
    if ($caseProbe.Count -ne 16) {
        throw "PowerShell JSONL array regression: expected 16 cases."
    }
    $selfTestIds = @(1..16 | ForEach-Object { "CPL-{0:D3}" -f $_ })
    if (Compare-Object @($caseProbe.id) $selfTestIds) {
        throw "PowerShell case identity regression."
    }
    $selfTestPhaseB = @(
        $caseProbe | Where-Object {
            $null -ne $_.PSObject.Properties["phase_b_repetitions"]
        }
    )
    $selfTestPhaseBExecutions = 0
    foreach ($case in $selfTestPhaseB) {
        $selfTestPhaseBExecutions +=
            [int]$case.phase_b_repetitions.auto +
            [int]$case.phase_b_repetitions.mini +
            [int]$case.phase_b_repetitions.standard
    }
    if ($selfTestPhaseB.Count -ne 8 -or
        $selfTestPhaseBExecutions -ne 64) {
        throw "PowerShell Phase B execution matrix regression."
    }
    $changeProbe = @(
        Compare-WorkspaceSnapshot -Before @{} -After @{}
    )
    if ($changeProbe.Count -ne 0) {
        throw "PowerShell empty workspace-change array regression."
    }
    $utf8ProbePath = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText(
            $utf8ProbePath,
            '{"JAPANESE":"日本語の変数"}',
            [System.Text.UTF8Encoding]::new($false)
        )
        $utf8Probe = [System.IO.File]::ReadAllText(
            $utf8ProbePath,
            [System.Text.Encoding]::UTF8
        ) | ConvertFrom-Json
        if ($utf8Probe.JAPANESE -ne "日本語の変数") {
            throw "PowerShell UTF-8 no-BOM VariablesJson regression."
        }
    }
    finally {
        Remove-Item -LiteralPath $utf8ProbePath -Force -ErrorAction SilentlyContinue
    }
    $nativeUtf8Stdout = [System.IO.Path]::GetTempFileName()
    $nativeUtf8Stderr = [System.IO.Path]::GetTempFileName()
    try {
        $nativeUtf8Code = Invoke-NativeProcessToFiles `
            -FileName $CollectorPython `
            -Arguments @(
                "-c",
                (
                    "import json,sys; sys.stdout.buffer.write((json.dumps(" +
                    "{'value':'\u65e5\u672c\u8a9e\u201d'}, " +
                    "ensure_ascii=False, separators=(',', ':'))" +
                    "+'\n').encode('utf-8'))"
                )
            ) `
            -StdoutPath $nativeUtf8Stdout `
            -StderrPath $nativeUtf8Stderr
        if ($nativeUtf8Code -ne 0) {
            throw "PowerShell native UTF-8 process regression."
        }
        $nativeUtf8Value = (
            [System.IO.File]::ReadAllText(
                $nativeUtf8Stdout,
                [System.Text.Encoding]::UTF8
            ) | ConvertFrom-Json
        )
    }
    catch {
        throw "PowerShell native UTF-8 JSON parse regression."
    }
    finally {
        Remove-Item -LiteralPath $nativeUtf8Stdout `
            -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $nativeUtf8Stderr `
            -Force -ErrorAction SilentlyContinue
    }
    $nativeUtf8Expected = (
        [string][char]0x65E5 +
        [string][char]0x672C +
        [string][char]0x8A9E +
        [string][char]0x201D
    )
    if ($nativeUtf8Value.value -ne $nativeUtf8Expected) {
        throw "PowerShell native UTF-8 content regression."
    }
    & $CollectorPython $CollectorPath --self-test --cases $CasesPath
    $selfTestExitCode = [int]$LASTEXITCODE
    exit $selfTestExitCode
}

foreach ($required in @(
    "MiniModel",
    "StandardModel",
    "FixtureWorkspace",
    "VariablesJson",
    "OutputRoot"
)) {
    if ([string]::IsNullOrWhiteSpace((Get-Variable $required -ValueOnly))) {
        throw "-$required is required unless -SelfTest is used."
    }
}
if ($MaxAiCreditsPerTurn -lt 30) {
    throw "-MaxAiCreditsPerTurn must be at least 30 for this Copilot CLI."
}
if ($AutoModel -ne "auto") {
    throw "-AutoModel must be exactly 'auto'."
}
if ($MiniModel -eq "auto" -or $StandardModel -eq "auto") {
    throw "-MiniModel and -StandardModel must be explicit non-Auto models."
}
if ($MiniModel -eq $StandardModel) {
    throw "-MiniModel and -StandardModel must be distinct."
}

$cases = @(Read-JsonLines -Path $CasesPath)
if ($cases.Count -ne 16) {
    throw "Exactly 16 compliance cases are required; found $($cases.Count)."
}
$expectedProfiles = @("auto", "mini", "standard")
foreach ($case in $cases) {
    if ($case.schema_version -ne "copilot-compliance-case-v1") {
        throw "Unsupported case schema in $($case.id)."
    }
    if (@($case.profiles).Count -ne 3 -or
        (Compare-Object @($case.profiles) $expectedProfiles)) {
        throw "$($case.id) must declare auto, mini, and standard."
    }
}
$expectedIds = @(1..16 | ForEach-Object { "CPL-{0:D3}" -f $_ })
if (Compare-Object @($cases.id) $expectedIds) {
    throw "Cases must be ordered CPL-001 through CPL-016."
}
$phaseBFocused = @(
    $cases | Where-Object {
        $null -ne $_.PSObject.Properties["phase_b_repetitions"]
    }
)
if ($phaseBFocused.Count -ne 8) {
    throw "Exactly eight cases must define phase_b_repetitions."
}
foreach ($case in $phaseBFocused) {
    if ([int]$case.phase_b_repetitions.auto -ne 4 -or
        [int]$case.phase_b_repetitions.mini -ne 2 -or
        [int]$case.phase_b_repetitions.standard -ne 2) {
        throw "$($case.id) has an invalid Phase B repetition plan."
    }
}

$variables = [System.IO.File]::ReadAllText(
    $VariablesJson,
    [System.Text.Encoding]::UTF8
) | ConvertFrom-Json
$workspace = (Resolve-Path -LiteralPath $FixtureWorkspace).Path
$outputPath = [System.IO.Path]::GetFullPath($OutputRoot)
$workspaceWithSeparator = $workspace.TrimEnd("\", "/") +
    [System.IO.Path]::DirectorySeparatorChar
if ($outputPath.Equals(
    $workspace,
    [System.StringComparison]::OrdinalIgnoreCase
) -or $outputPath.StartsWith(
    $workspaceWithSeparator,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "-OutputRoot must be outside -FixtureWorkspace."
}
[System.IO.Directory]::CreateDirectory($outputPath) | Out-Null

$renderedCases = @()
foreach ($case in $cases) {
    $rendered = Resolve-Placeholders -Value $case -Variables $variables
    $renderedJson = $rendered | ConvertTo-Json -Depth 30 -Compress
    $unresolvedProbe = $renderedJson.Replace("{{EXECUTION_TAG}}", "")
    if ($unresolvedProbe -match "\{\{[A-Z][A-Z0-9_]*\}\}") {
        throw "$($case.id) has unresolved fixture placeholders."
    }
    $renderedCases += ,$rendered
}

$models = @(
    [ordered]@{ profile = "auto"; requested = $AutoModel },
    [ordered]@{ profile = "mini"; requested = $MiniModel },
    [ordered]@{ profile = "standard"; requested = $StandardModel }
)
$copilotLaunch = Resolve-CopilotLaunch -Command $CopilotPath
$creditText = $MaxAiCreditsPerTurn.ToString(
    [System.Globalization.CultureInfo]::InvariantCulture
)

$executionPlan = @()
foreach ($model in $models) {
    foreach ($staticCase in $renderedCases) {
        $repeatCount = 1
        if ($Phase -eq "B") {
            if ($null -eq $staticCase.PSObject.Properties[
                "phase_b_repetitions"
            ]) {
                continue
            }
            $profileRepeat = $staticCase.phase_b_repetitions.PSObject.Properties[
                [string]$model.profile
            ]
            if ($null -eq $profileRepeat) {
                throw "$($staticCase.id) lacks a Phase B count for $($model.profile)."
            }
            $repeatCount = [int]$profileRepeat.Value
        }
        for ($repetition = 1; $repetition -le $repeatCount; $repetition++) {
            $executionPlan += ,[pscustomobject]@{
                model = $model
                case = $staticCase
                repetition = $repetition
            }
        }
    }
}
$expectedExecutionCount = if ($Phase -eq "A") { 48 } else { 64 }
if ($executionPlan.Count -ne $expectedExecutionCount) {
    throw "Phase $Phase requires $expectedExecutionCount executions; found $($executionPlan.Count)."
}

foreach ($execution in $executionPlan) {
        $model = $execution.model
        $sessionId = [guid]::NewGuid().ToString()
        $case = Resolve-Placeholders `
            -Value $execution.case `
            -Variables ([pscustomobject]@{ EXECUTION_TAG = $sessionId })
        $caseBase = Join-Path (
            Join-Path $outputPath $model.profile
        ) $case.id
        if ($Phase -eq "B") {
            $caseBase = Join-Path (
                Join-Path (
                    Join-Path (
                        Join-Path $outputPath "phase-b"
                    ) $model.profile
                ) $case.id
            ) ("repeat-{0:D2}" -f [int]$execution.repetition)
        }
        $caseDirectory = $caseBase
        [System.IO.Directory]::CreateDirectory($caseDirectory) | Out-Null
        $cliJsonl = Join-Path $caseDirectory "copilot.jsonl"
        $otelJsonl = Join-Path $caseDirectory "otel.jsonl"
        $logDirectory = Join-Path $caseDirectory "copilot-logs"
        [System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
        [System.IO.File]::WriteAllText(
            $cliJsonl, "", [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::WriteAllText(
            $otelJsonl, "", [System.Text.UTF8Encoding]::new($false)
        )

        $before = Get-WorkspaceSnapshot -Root $workspace
        $exitCodes = @()
        $renderedTurns = @()
        $startedAt = [DateTimeOffset]::UtcNow.ToString("o")
        $savedEnvironment = @{}
        try {
            for ($turnIndex = 0; $turnIndex -lt @($case.turns).Count; $turnIndex++) {
                $prompt = [string]$case.turns[$turnIndex].prompt
                $renderedTurns += $prompt
                $turnNumber = $turnIndex + 1
                $turnOtel = Join-Path $caseDirectory (
                    "otel-turn-{0}.jsonl" -f $turnNumber
                )
                $turnStderr = Join-Path $caseDirectory (
                    "stderr-turn-{0}.log" -f $turnNumber
                )
                $turnStdout = Join-Path $caseDirectory (
                    "stdout-turn-{0}.jsonl" -f $turnNumber
                )
                Set-ProcessEnvironment -Saved $savedEnvironment `
                    -Name "COPILOT_OTEL_ENABLED" -Value "true"
                Set-ProcessEnvironment -Saved $savedEnvironment `
                    -Name "COPILOT_OTEL_EXPORTER_TYPE" -Value "file"
                Set-ProcessEnvironment -Saved $savedEnvironment `
                    -Name "COPILOT_OTEL_FILE_EXPORTER_PATH" -Value $turnOtel
                Set-ProcessEnvironment -Saved $savedEnvironment `
                    -Name "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" `
                    -Value "false"
                Set-ProcessEnvironment -Saved $savedEnvironment `
                    -Name "COPILOT_OTEL_SOURCE_NAME" `
                    -Value "local-rag-compliance"

                $arguments = @(
                    "-C", $workspace,
                    "--model", [string]$model.requested,
                    "--prompt", $prompt,
                    "--output-format", "json",
                    "--stream", "off",
                    "--allow-all-tools",
                    "--allow-all-paths",
                    "--disable-builtin-mcps",
                    "--no-auto-update",
                    "--no-ask-user",
                    "--no-remote",
                    "--no-remote-export",
                    "--max-ai-credits", $creditText,
                    "--log-dir", $logDirectory
                )
                if ($turnIndex -eq 0) {
                    $arguments += @("--session-id", $sessionId)
                }
                else {
                    $arguments += "--resume=$sessionId"
                }
                $nativeArguments = @(
                    @($copilotLaunch.PrefixArguments) + $arguments
                )
                $exitCodes += Invoke-NativeProcessToFiles `
                    -FileName ([string]$copilotLaunch.FileName) `
                    -Arguments $nativeArguments `
                    -StdoutPath $turnStdout `
                    -StderrPath $turnStderr
                $turnStdoutStream = [System.IO.File]::OpenRead($turnStdout)
                $combinedStream = [System.IO.File]::Open(
                    $cliJsonl,
                    [System.IO.FileMode]::Append,
                    [System.IO.FileAccess]::Write,
                    [System.IO.FileShare]::Read
                )
                try {
                    $turnStdoutStream.CopyTo($combinedStream)
                    $combinedStream.Flush()
                }
                finally {
                    $turnStdoutStream.Dispose()
                    $combinedStream.Dispose()
                }
                if (Test-Path -LiteralPath $turnOtel) {
                    $otelText = [System.IO.File]::ReadAllText($turnOtel)
                    if (-not [string]::IsNullOrEmpty($otelText)) {
                        [System.IO.File]::AppendAllText(
                            $otelJsonl,
                            $otelText.TrimEnd() + [Environment]::NewLine,
                            [System.Text.UTF8Encoding]::new($false)
                        )
                    }
                    Remove-Item -LiteralPath $turnOtel -Force
                }
            }
        }
        finally {
            Restore-ProcessEnvironment -Saved $savedEnvironment
        }
        $after = Get-WorkspaceSnapshot -Root $workspace
        $changes = @(
            Compare-WorkspaceSnapshot -Before $before -After $after
        )
        $run = [ordered]@{
            schema_version = "copilot-compliance-run-v1"
            case_id = $case.id
            profile = $model.profile
            phase = $Phase
            repetition = [int]$execution.repetition
            requested_model = [string]$model.requested
            session_id = $sessionId
            started_at = $startedAt
            completed_at = [DateTimeOffset]::UtcNow.ToString("o")
            exit_codes = $exitCodes
            rendered_turns = $renderedTurns
            workspace_changes = $changes
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $caseDirectory "run.json"),
            ($run | ConvertTo-Json -Depth 30),
            [System.Text.UTF8Encoding]::new($false)
        )
}

$reportName = if ($Phase -eq "A") {
    "copilot-compliance-report-v1.json"
}
else {
    "copilot-compliance-report-v1-phase-b.json"
}
$report = Join-Path $outputPath $reportName
$phaseArgument = $Phase.ToLowerInvariant()
& $CollectorPython $CollectorPath `
    --cases $CasesPath `
    --raw-root $outputPath `
    --output $report `
    --variables $VariablesJson `
    --fixture-workspace $workspace `
    --phase $phaseArgument
exit $LASTEXITCODE
