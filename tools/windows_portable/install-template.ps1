[CmdletBinding()]
param(
    [switch]$ConfigureVSCodeAutoApprove,
    [switch]$ConfigureVSCodeRunnerApproval,
    [switch]$SkipVSCodeAutoApprove,
    [switch]$RetryVSCodeApprovals,
    [switch]$ReplaceExistingDatabases,
    [switch]$LauncherArgumentError
)

$ErrorActionPreference = "Stop"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$PackageRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Payload = Join-Path $PackageRoot ".copilot"
$Target = Join-Path $env:USERPROFILE ".copilot"
$SourceQuery = Join-Path $Payload "rag\query"
$TargetQuery = Join-Path $Target "rag\query"
$SourceRuntime = Join-Path $SourceQuery ".venv"
$TargetRuntime = Join-Path $TargetQuery ".venv"
$SourceModel = Join-Path $Payload "rag\models\ruri-v3-30m-onnx-int8"
$TargetModel = Join-Path $Target "rag\models\ruri-v3-30m-onnx-int8"
$SourceDbs = Join-Path $Payload "rag\dbs"
$TargetDbs = Join-Path $Target "rag\dbs"
$Transaction = [Guid]::NewGuid().ToString("N")
$StageRuntime = Join-Path $TargetQuery (".venv.stage-" + $Transaction)
$BackupRuntime = Join-Path $TargetQuery (".venv.backup-" + $Transaction)
$StageModel = Join-Path (Split-Path -Parent $TargetModel) (".model.stage-" + $Transaction)
$BackupModel = Join-Path (Split-Path -Parent $TargetModel) (".model.backup-" + $Transaction)
$StageDbs = Join-Path $TargetDbs (".portable.stage-" + $Transaction)
$BackupDbs = Join-Path $TargetDbs (".portable.backup-" + $Transaction)
$BackupProduct = Join-Path $TargetQuery (".product.backup-" + $Transaction)
$ProductBackedUp = @()
$ProductCreatedFiles = @()
$ProductCreatedDirectories = @()
$DatabaseBackedUp = @()
$DatabaseFresh = @()
$RuntimePublished = $false
$ModelPublished = $false
$InstallStage = "validate_package"
$RuntimeStatus = "NOT_READY"
$DatabaseStatus = "NOT_CHECKED"
$SlashSkillStatus = "not_reached"
$LegacyAgent003Status = "not_reached"
$InstallLogPath = ""
$InstallTranscriptStarted = $false

function Start-InstallTranscript {
    $FileName = "portable-install-{0}-{1}.log" -f (
        Get-Date -Format "yyyyMMdd-HHmmss"
    ), $PID
    $Directories = @()
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $Directories += Join-Path $env:LOCALAPPDATA "LocalRAG\logs"
    }
    $TempRoot = $env:TEMP
    if ([string]::IsNullOrWhiteSpace($TempRoot)) {
        $TempRoot = [System.IO.Path]::GetTempPath()
    }
    $Directories += Join-Path $TempRoot "LocalRAG\logs"

    foreach ($Directory in @($Directories | Select-Object -Unique)) {
        $Candidate = ""
        try {
            New-Item -ItemType Directory -Path $Directory -Force | Out-Null
            $Candidate = [System.IO.Path]::GetFullPath(
                (Join-Path $Directory $FileName)
            )
            Start-Transcript -Path $Candidate -Force | Out-Null
            $script:InstallLogPath = $Candidate
            $script:InstallTranscriptStarted = $true
            return
        } catch {
            if (
                -not [string]::IsNullOrWhiteSpace($Candidate) -and
                (Test-Path -LiteralPath $Candidate -PathType Leaf)
            ) {
                Remove-Item -LiteralPath $Candidate -Force -ErrorAction SilentlyContinue
            }
        }
    }
    throw "portable installer could not create its run log"
}

function Stop-InstallTranscript {
    if (-not $script:InstallTranscriptStarted) {
        return
    }
    try {
        Stop-Transcript | Out-Null
    } catch {
        Write-Warning "portable installer could not finalize its run log"
    } finally {
        $script:InstallTranscriptStarted = $false
    }
}

function Write-InstallSummary {
    param(
        [bool]$Succeeded,
        [string]$Reason = ""
    )
    $Utf8 = [Text.Encoding]::UTF8
    $OutcomeJa = if ($Succeeded) {
        $Utf8.GetString([Convert]::FromBase64String("5oiQ5Yqf"))
    } else {
        $Utf8.GetString([Convert]::FromBase64String("5aSx5pWX"))
    }
    $OutcomeCode = if ($Succeeded) { "SUCCESS" } else { "FAILED" }
    $Color = if ($Succeeded) { "Green" } else { "Red" }
    $SummaryFormat = $Utf8.GetString([Convert]::FromBase64String(
        "PT09IExvY2FsIFJBRyDjgqTjg7Pjgrnjg4jjg7zjg6vntZDmnpw6IHswfSAoezF9KSA9PT0="
    ))
    Write-Host ""
    Write-Host (
        $SummaryFormat -f (
            $OutcomeJa,
            $OutcomeCode
        )
    ) -ForegroundColor $Color
    Write-Host (($Utf8.GetString([Convert]::FromBase64String(
        "5Yem55CG5q616ZqOOiA="
    ))) + $InstallStage)
    Write-Host (($Utf8.GetString([Convert]::FromBase64String(
        "44Op44Oz44K/44Kk44OgOiA="
    ))) + $RuntimeStatus)
    Write-Host (($Utf8.GetString([Convert]::FromBase64String(
        "44OH44O844K/44OZ44O844K5OiA="
    ))) + $DatabaseStatus)
    Write-Host ("Local RAG slash Skill: " + $SlashSkillStatus)
    Write-Host ("Legacy Agent003 integration: " + $LegacyAgent003Status)
    if (-not $Succeeded -and -not [string]::IsNullOrWhiteSpace($Reason)) {
        Write-Host (($Utf8.GetString([Convert]::FromBase64String(
            "55CG55SxOiA="
        ))) + $Reason)
    }
    $DisplayedLogPath = if (
        [string]::IsNullOrWhiteSpace($InstallLogPath)
    ) { "UNAVAILABLE" } else { $InstallLogPath }
    Write-Host (($Utf8.GetString([Convert]::FromBase64String(
        "44Ot44KwOiA="
    ))) + $DisplayedLogPath)
}

trap {
    $FailureReason = $_.Exception.Message
    Write-InstallSummary -Succeeded $false -Reason $FailureReason
    Stop-InstallTranscript
    exit 1
}

Start-InstallTranscript
if ($LauncherArgumentError) {
    throw "install.cmd received an unsupported argument"
}

function Assert-ChildPath {
    param([string]$Root, [string]$Candidate)
    $ResolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar
    )
    $ResolvedCandidate = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $ResolvedCandidate.StartsWith(
        $ResolvedRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "transaction path escapes the Local RAG target"
    }
}

function Assert-NoReparsePath {
    param([string]$Path)
    $Current = [System.IO.DirectoryInfo]::new(
        [System.IO.Path]::GetFullPath($Path)
    )
    while ($null -ne $Current) {
        try {
            $Attributes = [System.IO.File]::GetAttributes($Current.FullName)
            if (($Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ("path contains a reparse point: " + $Current.FullName)
            }
        } catch [System.IO.FileNotFoundException] {
        } catch [System.IO.DirectoryNotFoundException] {
        }
        $Current = $Current.Parent
    }
}

function Assert-NoReparseTree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw ("portable package directory is missing: " + $Path)
    }
    $Entries = @((Get-Item -LiteralPath $Path -Force)) + @(
        Get-ChildItem -LiteralPath $Path -Recurse -Force
    )
    foreach ($Entry in $Entries) {
        if (($Entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ("portable package contains a reparse point: " + $Entry.FullName)
        }
    }
}

function Remove-Tree {
    param([string]$Path)
    if ($Path -and (Test-Path -LiteralPath $Path)) {
        Assert-ChildPath -Root $Target -Candidate $Path
        foreach ($Entry in @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
            if (($Entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ("refusing to delete a transaction tree containing a reparse point: " + $Entry.FullName)
            }
            if (($Entry.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0) {
                $WritableAttributes = $Entry.Attributes -band (
                    -bnot [System.IO.FileAttributes]::ReadOnly
                )
                [System.IO.File]::SetAttributes(
                    $Entry.FullName,
                    $WritableAttributes
                )
            }
        }
        $RootEntry = Get-Item -LiteralPath $Path -Force
        if (($RootEntry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ("refusing to delete a transaction reparse point: " + $RootEntry.FullName)
        }
        if (($RootEntry.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0) {
            $WritableAttributes = $RootEntry.Attributes -band (
                -bnot [System.IO.FileAttributes]::ReadOnly
            )
            [System.IO.File]::SetAttributes(
                $RootEntry.FullName,
                $WritableAttributes
            )
        }
        [System.IO.Directory]::Delete($Path, $true)
    }
}

function Move-PublishedRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    for ($Attempt = 1; $Attempt -le 4; $Attempt++) {
        Assert-NoReparsePath -Path $Source
        Assert-NoReparsePath -Path $Destination
        if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
            throw "staged runtime is missing before publish"
        }
        if (Test-Path -LiteralPath $Destination) {
            throw "runtime destination unexpectedly exists before publish"
        }
        try {
            [System.IO.Directory]::Move($Source, $Destination)
            return
        } catch [System.UnauthorizedAccessException], [System.IO.IOException] {
            if ($Attempt -eq 4) { throw }
            Start-Sleep -Milliseconds (100 * $Attempt)
        }
    }
}

function Assert-Amd64PeFile {
    param([string]$Path)
    $Stream = $null
    $Reader = $null
    try {
        $Stream = [System.IO.File]::OpenRead($Path)
        $Reader = [System.IO.BinaryReader]::new($Stream)
        if ($Reader.ReadUInt16() -ne 0x5A4D) { throw "invalid DOS header" }
        $Stream.Position = 0x3C
        $PeOffset = $Reader.ReadUInt32()
        if ($PeOffset -gt ($Stream.Length - 6)) { throw "invalid PE offset" }
        $Stream.Position = $PeOffset
        if ($Reader.ReadUInt32() -ne 0x00004550) { throw "invalid PE signature" }
        if ($Reader.ReadUInt16() -ne 0x8664) { throw "non-AMD64 PE" }
    } catch {
        throw ("portable runtime binary is not AMD64: " + $Path)
    } finally {
        if ($null -ne $Reader) { $Reader.Dispose() }
        elseif ($null -ne $Stream) { $Stream.Dispose() }
    }
}

function Assert-Amd64PortableRuntime {
    param([string]$Runtime)
    $Python = Join-Path $Runtime "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "portable runtime is missing Scripts\python.exe"
    }
    $Binaries = @(
        Get-ChildItem -LiteralPath $Runtime -Recurse -Force -File |
            Where-Object {
                @(".exe", ".dll", ".pyd") -icontains $_.Extension
            }
    )
    foreach ($Binary in $Binaries) {
        Assert-Amd64PeFile -Path $Binary.FullName
    }
}

function Test-ProtectedRelativePath {
    param([string]$Relative)
    $Value = $Relative.Replace("/", "\")
    if (
        $Value -ieq "rag\dbs" -or
        $Value.StartsWith("rag\dbs\", [StringComparison]::OrdinalIgnoreCase)
    ) {
        return $true
    }
    if (
        $Value -ieq "rag\query\run" -or
        $Value.StartsWith("rag\query\run\", [StringComparison]::OrdinalIgnoreCase)
    ) {
        return $true
    }
    if (
        $Value -ieq "rag\query\.venv" -or
        $Value.StartsWith("rag\query\.venv\", [StringComparison]::OrdinalIgnoreCase)
    ) {
        return $true
    }
    if ($Value.StartsWith(
        "rag\models\ruri-v3-30m-onnx-int8\",
        [StringComparison]::OrdinalIgnoreCase
    ) -or $Value -ieq "rag\models\ruri-v3-30m-onnx-int8") {
        return $true
    }
    if (
        $Value -ieq "rag\query\.rag-deps-installed" -or
        $Value.StartsWith(
            "rag\query\.rag-deps-installed.",
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }
    return @(
        "rag\config\network.json",
        "rag\config\manage-custom.json",
        "rag\config\sensitive-terms.local",
        "rag\config\source-connections.json",
        "rag\config\source-connections.secrets.json",
        "rag\config\.source-connections.key",
        "rag\config\windows-test-connection.local.json"
    ) -icontains $Value
}

function Backup-ProductFile {
    param([string]$Relative, [string]$Destination)
    if ($ProductBackedUp -icontains $Relative) { return }
    $Backup = Join-Path $BackupProduct $Relative
    New-Item -ItemType Directory -Force -Path (
        Split-Path -Parent $Backup
    ) | Out-Null
    Copy-Item -LiteralPath $Destination -Destination $Backup
    $script:ProductBackedUp += $Relative
}

Assert-NoReparsePath -Path $Target
Assert-NoReparsePath -Path $TargetDbs
Assert-NoReparsePath -Path $TargetQuery
Assert-NoReparsePath -Path $TargetRuntime
Assert-NoReparsePath -Path $TargetModel
Assert-NoReparseTree -Path $Payload
Assert-NoReparseTree -Path $SourceRuntime
Assert-NoReparseTree -Path $SourceModel
Assert-Amd64PortableRuntime -Runtime $SourceRuntime
if (Test-Path -LiteralPath (Join-Path $TargetQuery "run\ragd.json") -PathType Leaf) {
    throw "stop the owned Local RAG daemon before updating"
}

$DatabaseNames = @()
if (Test-Path -LiteralPath $SourceDbs -PathType Container) {
    foreach ($Database in @(
        Get-ChildItem -LiteralPath $SourceDbs -Directory -Force
    )) {
        if ($Database.Name -notmatch "^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$") {
            throw ("portable package database name is invalid: " + $Database.Name)
        }
        Assert-NoReparseTree -Path $Database.FullName
        $DatabaseNames += $Database.Name
    }
}
if (@($DatabaseNames | ForEach-Object { $_.ToLowerInvariant() } |
    Sort-Object -Unique).Count -ne $DatabaseNames.Count) {
    throw "portable package database names collide after case folding"
}
foreach ($Name in $DatabaseNames) {
    $Existing = Join-Path $TargetDbs $Name
    Assert-NoReparsePath -Path $Existing
    if (
        (Test-Path -LiteralPath $Existing) -and
        -not (Test-Path -LiteralPath $Existing -PathType Container)
    ) {
        throw ("same-name database target is not a directory: " + $Name)
    }
    if (
        (Test-Path -LiteralPath $Existing -PathType Container) -and
        -not $ReplaceExistingDatabases
    ) {
        throw (
            "database already exists; use -ReplaceExistingDatabases to replace it: " +
            $Name
        )
    }
}

$InstallStage = "stage_payload"
try {
    New-Item -ItemType Directory -Force -Path (
        $TargetQuery,
        (Split-Path -Parent $TargetModel),
        $TargetDbs
    ) | Out-Null
    Copy-Item -LiteralPath $SourceRuntime -Destination $StageRuntime -Recurse
    Copy-Item -LiteralPath $SourceModel -Destination $StageModel -Recurse
    if ($DatabaseNames.Count -gt 0) {
        New-Item -ItemType Directory -Path $StageDbs | Out-Null
        foreach ($Name in $DatabaseNames) {
            Copy-Item -LiteralPath (Join-Path $SourceDbs $Name) -Destination (
                Join-Path $StageDbs $Name
            ) -Recurse
        }
    }

    # Close both the managed-setup marker gate and the fixed portable-runtime
    # gate before changing product files or selected databases.  The staged
    # runtime is published last so a new process cannot observe a mixed tree.
    Assert-NoReparsePath -Path $TargetRuntime
    if (Test-Path -LiteralPath $TargetRuntime) {
        [System.IO.Directory]::Move($TargetRuntime, $BackupRuntime)
    }

    $PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
    Get-ChildItem -LiteralPath $Payload -Force -Recurse | ForEach-Object {
        $Relative = $_.FullName.Substring($PayloadRoot.Length).TrimStart(
            [System.IO.Path]::DirectorySeparatorChar
        )
        if (-not (Test-ProtectedRelativePath -Relative $Relative)) {
            $Destination = Join-Path $Target $Relative
            Assert-NoReparsePath -Path $Destination
            if ($_.PSIsContainer) {
                if (-not (Test-Path -LiteralPath $Destination)) {
                    New-Item -ItemType Directory -Path $Destination | Out-Null
                    $script:ProductCreatedDirectories += $Destination
                }
            } else {
                New-Item -ItemType Directory -Force -Path (
                    Split-Path -Parent $Destination
                ) | Out-Null
                if (Test-Path -LiteralPath $Destination -PathType Leaf) {
                    Backup-ProductFile -Relative $Relative -Destination $Destination
                } else {
                    $script:ProductCreatedFiles += $Destination
                }
                Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
            }
        }
    }

    foreach ($Relative in @(
        "rag\query\.packaged-runtime.json",
        "rag\query\.rag-deps-installed",
        "rag\query\portable_runtime.py",
        "rag\query\portable_db_install.py",
        "rag\query\portable_db_smoke.py",
        "rag\query\vscode_settings.py",
        "rag\query\mcp_server.py",
        "rag\copilot-cli\local-rag-agent003-savings.agent.md",
        "rag\copilot-cli\local-rag-agent003-standard.agent.md",
        "rag\copilot-cli\local-rag-agent003-thorough.agent.md",
        "rag\copilot-cli\local-rag-agent003.ps1",
        "instructions\rag.instructions.md",
        "skills\local-rag-setup\SKILL.md"
    )) {
        $Retired = Join-Path $Target $Relative
        if (Test-Path -LiteralPath $Retired -PathType Leaf) {
            Assert-NoReparsePath -Path $Retired
            Backup-ProductFile -Relative $Relative -Destination $Retired
            [System.IO.File]::Delete($Retired)
        }
    }
    foreach ($RetiredDirectoryRelative in @(
        "rag\copilot-cli",
        "instructions",
        "skills\local-rag-setup"
    )) {
        $RetiredDirectory = Join-Path $Target $RetiredDirectoryRelative
        if (Test-Path -LiteralPath $RetiredDirectory -PathType Container) {
            Assert-NoReparsePath -Path $RetiredDirectory
        }
        if (
            (Test-Path -LiteralPath $RetiredDirectory -PathType Container) -and
            -not (Get-ChildItem -LiteralPath $RetiredDirectory -Force)
        ) {
            [System.IO.Directory]::Delete($RetiredDirectory)
        }
    }

    Assert-NoReparsePath -Path $Target
    Assert-NoReparsePath -Path $TargetQuery
    Assert-NoReparsePath -Path $TargetRuntime
    Assert-NoReparsePath -Path $TargetModel
    Assert-NoReparsePath -Path $TargetDbs
    if (Test-Path -LiteralPath $TargetModel) {
        [System.IO.Directory]::Move($TargetModel, $BackupModel)
    }
    [System.IO.Directory]::Move($StageModel, $TargetModel)
    $ModelPublished = $true

    foreach ($Name in $DatabaseNames) {
        $Existing = Join-Path $TargetDbs $Name
        if (Test-Path -LiteralPath $Existing -PathType Container) {
            New-Item -ItemType Directory -Force -Path $BackupDbs | Out-Null
            [System.IO.Directory]::Move(
                $Existing,
                (Join-Path $BackupDbs $Name)
            )
            $DatabaseBackedUp += $Name
        } else {
            $DatabaseFresh += $Name
        }
        [System.IO.Directory]::Move(
            (Join-Path $StageDbs $Name),
            $Existing
        )
    }
    $DatabaseStatus = "READY"

    $InstallStage = "publish_runtime"
    Move-PublishedRuntime -Source $StageRuntime -Destination $TargetRuntime
    $RuntimePublished = $true
    $RuntimeStatus = "READY"

    $InstallStage = "slash_skill"
    $SlashSkillPath = Join-Path $Target "skills\local-rag\SKILL.md"
    if (-not (Test-Path -LiteralPath $SlashSkillPath -PathType Leaf)) {
        throw "Local RAG slash Skill is missing from the installed payload."
    }
    $SlashSkillStatus = "READY"

    $InstallStage = "retire_agent003"
    $VSCodeMcpTarget = $null
    if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
        $VSCodeMcpTarget = [System.IO.Path]::GetFullPath((
            Join-Path $env:APPDATA "Code\User\mcp.json"
        ))
        Assert-NoReparsePath -Path $VSCodeMcpTarget
    }
    if (-not [string]::IsNullOrWhiteSpace($env:COPILOT_HOME)) {
        if (-not [System.IO.Path]::IsPathRooted($env:COPILOT_HOME)) {
            throw "COPILOT_HOME must be an absolute path."
        }
        $CopilotCliHome = [System.IO.Path]::GetFullPath($env:COPILOT_HOME)
    } else {
        if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
            throw "USERPROFILE is required when COPILOT_HOME is unset."
        }
        if (-not [System.IO.Path]::IsPathRooted($env:USERPROFILE)) {
            throw "USERPROFILE must be an absolute path."
        }
        $CopilotCliHome = [System.IO.Path]::GetFullPath((
            Join-Path $env:USERPROFILE ".copilot"
        ))
    }
    if (-not [string]::IsNullOrWhiteSpace(
        $env:LOCAL_RAG_COPILOT_PROFILE_PATH
    )) {
        if (-not [System.IO.Path]::IsPathRooted(
            $env:LOCAL_RAG_COPILOT_PROFILE_PATH
        )) {
            throw "LOCAL_RAG_COPILOT_PROFILE_PATH must be an absolute path."
        }
        $CopilotProfilePath = [System.IO.Path]::GetFullPath(
            $env:LOCAL_RAG_COPILOT_PROFILE_PATH
        )
    } else {
        if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
            throw "USERPROFILE is required to select the PowerShell profile."
        }
        $CopilotProfilePath = [System.IO.Path]::GetFullPath((
            Join-Path $env:USERPROFILE "Documents\PowerShell\profile.ps1"
        ))
    }
    $CopilotCliArguments = @(
        "retire",
        "--copilot-home", $CopilotCliHome,
        "--install-root", $Target,
        "--profile-path", $CopilotProfilePath
    )
    if ($null -ne $VSCodeMcpTarget) {
        $CopilotCliArguments += @(
            "--vscode-mcp-config", $VSCodeMcpTarget
        )
    }
    $CopilotCliText = (& (Join-Path $TargetRuntime "Scripts\python.exe") -B (
        Join-Path $TargetQuery "copilot_cli_setup.py"
    ) @CopilotCliArguments | Out-String)
    $CopilotCliExitCode = $LASTEXITCODE
    try {
        $CopilotCliResult = $CopilotCliText | ConvertFrom-Json
    } catch {
        throw "Legacy Agent003 retirement returned invalid JSON."
    }
    if ($CopilotCliExitCode -ne 0) {
        $CopilotCliError = [string]$CopilotCliResult.error
        if ($CopilotCliError -match (
            "(?i)(collision|already owned|unowned artifact)"
        )) {
            $LegacyAgent003Status = "blocked_collision"
        }
        throw ("Legacy Agent003 retirement failed: " + $CopilotCliError)
    }
    if (@("retired", "absent") -inotcontains (
        [string]$CopilotCliResult.status
    )) {
        throw "Legacy Agent003 retirement returned an invalid success status."
    }
    $LegacyAgent003Status = [string]$CopilotCliResult.status
} catch {
    foreach ($Name in @($DatabaseBackedUp) + @($DatabaseFresh)) {
        $Current = Join-Path $TargetDbs $Name
        if (Test-Path -LiteralPath $Current) { Remove-Tree $Current }
        $Backup = Join-Path $BackupDbs $Name
        if (Test-Path -LiteralPath $Backup) {
            [System.IO.Directory]::Move($Backup, $Current)
        }
    }
    if ($RuntimePublished -and (Test-Path -LiteralPath $TargetRuntime)) {
        Remove-Tree $TargetRuntime
    }
    if (Test-Path -LiteralPath $BackupRuntime) {
        [System.IO.Directory]::Move($BackupRuntime, $TargetRuntime)
    }
    if ($ModelPublished -and (Test-Path -LiteralPath $TargetModel)) {
        Remove-Tree $TargetModel
    }
    if (Test-Path -LiteralPath $BackupModel) {
        [System.IO.Directory]::Move($BackupModel, $TargetModel)
    }
    foreach ($Path in $ProductCreatedFiles) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Delete($Path)
        }
    }
    foreach ($Relative in $ProductBackedUp) {
        $Backup = Join-Path $BackupProduct $Relative
        $Destination = Join-Path $Target $Relative
        if (Test-Path -LiteralPath $Backup -PathType Leaf) {
            New-Item -ItemType Directory -Force -Path (
                Split-Path -Parent $Destination
            ) | Out-Null
            Copy-Item -LiteralPath $Backup -Destination $Destination -Force
        }
    }
    Remove-Tree $BackupProduct
    Remove-Tree $BackupDbs
    Remove-Tree $StageDbs
    Remove-Tree $StageRuntime
    Remove-Tree $StageModel
    foreach ($Path in ($ProductCreatedDirectories |
        Sort-Object { $_.Length } -Descending)) {
        if (
            (Test-Path -LiteralPath $Path -PathType Container) -and
            -not (Get-ChildItem -LiteralPath $Path -Force)
        ) {
            [System.IO.Directory]::Delete($Path)
        }
    }
    foreach ($Path in @(
        $TargetQuery,
        (Split-Path -Parent $TargetModel),
        $TargetDbs,
        (Join-Path $Target "rag"),
        $Target
    )) {
        if (
            (Test-Path -LiteralPath $Path -PathType Container) -and
            -not (Get-ChildItem -LiteralPath $Path -Force)
        ) {
            [System.IO.Directory]::Delete($Path)
        }
    }
    throw
}

foreach ($Path in @(
    $BackupRuntime,
    $BackupModel,
    $BackupProduct,
    $BackupDbs,
    $StageDbs
)) {
    try {
        Remove-Tree $Path
    } catch {
        Write-Warning ("install retained a transaction directory: " + $Path)
    }
}

if ($ConfigureVSCodeAutoApprove -and $ConfigureVSCodeRunnerApproval) {
    Write-Warning "Approval options conflict; no approval settings changed."
} elseif ($SkipVSCodeAutoApprove) {
    Write-Host "SkipVSCodeAutoApprove selected; no approval settings changed."
} else {
    $ApprovalMode = "choose"
    if ($ConfigureVSCodeAutoApprove) { $ApprovalMode = "global" }
    elseif ($ConfigureVSCodeRunnerApproval) { $ApprovalMode = "runner" }
    try {
        & (Join-Path $TargetRuntime "Scripts\python.exe") -X utf8 -B (
            Join-Path $Target "rag\query\vscode_approval_settings.py"
        ) --install-root $Target --mode $ApprovalMode
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Optional VS Code approval configuration NOT APPLIED."
        }
    } catch {
        Write-Warning "Optional VS Code approval configuration NOT APPLIED."
    }
}

Write-Host ("Installed Local RAG Windows portable runtime to: " + $Target)
Write-Host "Use /local-rag in GitHub Copilot Chat to search Local RAG."
Write-Host "The installer did not enable MCP. Approval options are reported separately."
$InstallStage = "completed"
Write-InstallSummary -Succeeded $true
Stop-InstallTranscript
exit 0
