param(
    [string]$Target = (Join-Path $HOME ".copilot"),
    [string]$BootstrapPython = "",
    [switch]$ConfigureVSCodeAutoApprove,
    [switch]$ConfigureVSCodeRunnerApproval,
    [switch]$SkipVSCodeAutoApprove
)

$ErrorActionPreference = "Stop"

$Payload = Join-Path $PSScriptRoot ".copilot"
$QueryRoot = Join-Path $Target "rag\query"
$RuntimePython = Join-Path $QueryRoot ".venv\Scripts\python.exe"
$LegacyMarker = Join-Path $QueryRoot ".venv\.rag-deps-installed"
$LegacyBackup = $null
$Transaction = [Guid]::NewGuid().ToString("N")
$ProductBackupRoot = Join-Path $QueryRoot (".source-product-backup-" + $Transaction)
$ProductBackedUp = @()
$ProductCreatedFiles = @()
$ProductCreatedDirectories = @()
$SlashSkillStatus = "not_reached"
$LegacyAgent003Status = "not_reached"

function Resolve-BootstrapPython {
    param([string]$Requested)

    $Candidates = @()
    if ($Requested) {
        $Candidates += [PSCustomObject]@{
            Command = $Requested
            Prefix = @()
        }
    } else {
        $Candidates += [PSCustomObject]@{
            Command = "py"
            Prefix = @("-3")
        }
        $Candidates += [PSCustomObject]@{
            Command = "python"
            Prefix = @()
        }
        $Candidates += [PSCustomObject]@{
            Command = "python3"
            Prefix = @()
        }
    }

    foreach ($Candidate in $Candidates) {
        $Command = [string]$Candidate.Command
        $Prefix = @($Candidate.Prefix)
        try {
            & $Command @Prefix -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and (3, 13) <= sys.version_info[:2] < (3, 14) else 3)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return [PSCustomObject]@{
                    Command = $Command
                    Prefix = $Prefix
                }
            }
        } catch {
            # Continue to the next conventional Windows Python entry point.
        }
    }

    throw (
        "setup_required: CPython 3.13.x was not found. " +
        "Install Python, or rerun with -BootstrapPython C:\path\to\python.exe."
    )
}

function Invoke-Setup {
    param(
        [Parameter(Mandatory = $true)]$PythonCommand,
        [string[]]$PrefixArguments = @(),
        [string[]]$SetupArguments = @()
    )

    $SetupScript = Join-Path $Target "rag\query\setup.py"
    $SetupOutput = & $PythonCommand @PrefixArguments -B $SetupScript @SetupArguments
    $SetupExitCode = $LASTEXITCODE
    if ($SetupExitCode -ne 0) {
        if ($SetupOutput) {
            Write-Host ($SetupOutput -join [Environment]::NewLine)
        }
        throw (
            "setup_required: Local RAG runtime setup failed. " +
            "Review the sanitized setup diagnostics above."
        )
    }
}

function Move-CompletionMarker {
    param([string]$Marker, [string]$Label)
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) { return $null }
    $Backup = Join-Path $QueryRoot (".rag-deps-installed." + $Label + ".pre-update." + $PID + "." + [Guid]::NewGuid().ToString("N"))
    [System.IO.File]::Move($Marker, $Backup)
    return $Backup
}
function Close-CompletionMarkerGate {
    param([string[]]$Markers)
    foreach ($Marker in $Markers) {
        if ($Marker -and (Test-Path -LiteralPath $Marker -PathType Leaf)) {
            [System.IO.File]::Delete($Marker)
        }
    }
}

function Remove-CompletionMarkerBackups {
    param([string[]]$Backups)
    $Snapshots = @{}
    foreach ($Backup in $Backups) {
        if ($Backup -and (Test-Path -LiteralPath $Backup -PathType Leaf)) {
            $Snapshots[$Backup] = [System.IO.File]::ReadAllBytes($Backup)
        }
    }
    try {
        foreach ($Backup in $Snapshots.Keys) {
            [System.IO.File]::Delete($Backup)
        }
    } catch {
        foreach ($Backup in $Snapshots.Keys) {
            if (-not (Test-Path -LiteralPath $Backup -PathType Leaf)) {
                [System.IO.File]::WriteAllBytes($Backup, $Snapshots[$Backup])
            }
        }
        throw
    }
}

function Test-PathHasReparse {
    param([string]$Path, [string]$Boundary)
    $Current = [System.IO.Path]::GetFullPath($Path)
    $Root = [System.IO.Path]::GetFullPath($Boundary).TrimEnd("\")
    while ($Current.Length -ge $Root.Length) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $true
            }
        }
        if ($Current -ieq $Root) { break }
        $Parent = Split-Path -Parent $Current
        if (-not $Parent -or $Parent -eq $Current) { break }
        $Current = $Parent
    }
    return $false
}

function Backup-ProductFile {
    param([string]$Relative, [string]$Destination)
    if ($ProductBackedUp -icontains $Relative) { return }
    $Backup = Join-Path $ProductBackupRoot $Relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Backup) |
        Out-Null
    Copy-Item -LiteralPath $Destination -Destination $Backup
    $script:ProductBackedUp += $Relative
}

function Restore-ProductFiles {
    foreach ($Path in $ProductCreatedFiles) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Delete($Path)
        }
    }
    foreach ($Relative in $ProductBackedUp) {
        $Backup = Join-Path $ProductBackupRoot $Relative
        $Destination = Join-Path $Target $Relative
        if (Test-Path -LiteralPath $Backup -PathType Leaf) {
            New-Item -ItemType Directory -Force -Path (
                Split-Path -Parent $Destination
            ) | Out-Null
            Copy-Item -LiteralPath $Backup -Destination $Destination -Force
        }
    }
    foreach ($Directory in ($ProductCreatedDirectories |
        Sort-Object { $_.Length } -Descending)) {
        if ((Test-Path -LiteralPath $Directory -PathType Container) -and
            -not (Get-ChildItem -LiteralPath $Directory -Force)) {
            [IO.Directory]::Delete($Directory)
        }
    }
}

$InstallStage = "validate_payload"
$RuntimeStatus = "NOT_READY"
$DatabaseListStatus = "NOT_CHECKED"

try {
if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
    throw "Missing install payload: $Payload"
}

$InstallStage = "create_target"
New-Item -ItemType Directory -Force -Path $Target | Out-Null

$InstallStage = "copy_payload"

try {
$LegacyBackup = Move-CompletionMarker -Marker $LegacyMarker -Label "legacy"

function Test-InstallPayloadExcluded {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $Normalized = $RelativePath.Replace("/", "\").TrimStart("\")
    if (
        ($Normalized -ieq "rag\query\.rag-deps-installed") -or
        $Normalized.StartsWith(
            "rag\query\.rag-deps-installed.",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }
    if ($Normalized -ieq "rag\config\network.json") {
        return $true
    }
    if ($Normalized -ieq "rag\config\manage-custom.json") {
        return $true
    }
    if ($Normalized -ieq "rag\config\sensitive-terms.local") {
        return $true
    }
    if ($Normalized -ieq "rag\config\windows-test-connection.local.json") {
        return $true
    }
    if (
        ($Normalized -ieq "rag\config\source-connections.json") -or
        ($Normalized -ieq "rag\config\source-connections.secrets.json") -or
        ($Normalized -ieq "rag\config\.source-connections.key") -or
        $Normalized.StartsWith(
            "rag\config\.source-connections.",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }
    if (
        ($Normalized -ieq "rag\query\run") -or
        $Normalized.StartsWith(
            "rag\query\run\",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }
    if (
        ($Normalized -ieq "rag\copilot-cli") -or
        $Normalized.StartsWith(
            "rag\copilot-cli\",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }
    if (
        ($Normalized -ieq "rag\query\mcp_server.py") -or
        ($Normalized -ieq "instructions\rag.instructions.md") -or
        ($Normalized -ieq "skills\local-rag-setup") -or
        $Normalized.StartsWith(
            "skills\local-rag-setup\",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }
    $Parts = @($Normalized -split "\\")
    if (
        ($Parts.Length -ge 2) -and
        ($Parts[0] -ieq "rag") -and
        ($Parts[1] -ieq "dbs")
    ) {
        # A source clone must never replace or add files inside the user's DB root.
        return $true
    }
    if (
        ($Parts -icontains ".venv") -or
        ($Parts -icontains "__pycache__")
    ) {
        return $true
    }
    $Leaf = $Parts[-1]
    if ($Leaf -ieq ".DS_Store") {
        return $true
    }
    $Extension = [System.IO.Path]::GetExtension($Leaf)
    return ($Extension -ieq ".pyc") -or ($Extension -ieq ".pyo")
}

    $PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
Get-ChildItem -LiteralPath $Payload -Force -Recurse | ForEach-Object {
    $Relative = $_.FullName.Substring($PayloadRoot.Length).TrimStart(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (-not (Test-InstallPayloadExcluded -RelativePath $Relative)) {
        $Destination = Join-Path $Target $Relative
        if (Test-PathHasReparse -Path $Destination -Boundary $Target) {
            throw ("installation target contains a reparse point: " + $Relative)
        }
        if ($_.PSIsContainer) {
            if (-not (Test-Path -LiteralPath $Destination)) {
                New-Item -ItemType Directory -Force -Path $Destination | Out-Null
                $ProductCreatedDirectories += $Destination
            }
        } else {
            $Parent = Split-Path -Parent $Destination
            if ($Parent) {
                New-Item -ItemType Directory -Force -Path $Parent | Out-Null
            }
            if (Test-Path -LiteralPath $Destination -PathType Leaf) {
                Backup-ProductFile -Relative $Relative -Destination $Destination
            } else {
                $ProductCreatedFiles += $Destination
            }
            Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
        }
    }
}

# Overlay installs do not remove files that disappeared from the payload.
# Delete only this explicit retired-file allowlist; never prune user content.
$RetiredFiles = @(
    "rag\export_migration.sh",
    "rag\migration_archive.py",
    "rag\gen_db\migrate_source_metadata.py",
    "rag\gen_db\software_rag_tool\software_rag_tool\source_metadata_migration.py",
    "rag\query\.packaged-runtime.json",
    "rag\query\.rag-deps-installed",
    "rag\query\portable_runtime.py",
    "rag\query\portable_db_install.py",
    "rag\query\portable_db_smoke.py",
    "rag\query\mcp_server.py",
    "rag\query\vscode_settings.py",
    "rag\copilot-cli\local-rag-agent003-savings.agent.md",
    "rag\copilot-cli\local-rag-agent003-standard.agent.md",
    "rag\copilot-cli\local-rag-agent003-thorough.agent.md",
    "rag\copilot-cli\local-rag-agent003.ps1",
    "instructions\rag.instructions.md",
    "skills\local-rag-setup\SKILL.md",
    "skills\local-rag-admin\SKILL.md"
)
foreach ($RelativePath in $RetiredFiles) {
    $RetiredPath = Join-Path $Target $RelativePath
    if (Test-Path -LiteralPath $RetiredPath -PathType Leaf) {
        if (Test-PathHasReparse -Path $RetiredPath -Boundary $Target) {
            throw ("retired product path contains a reparse point: " + $RelativePath)
        }
        Backup-ProductFile -Relative $RelativePath -Destination $RetiredPath
        [System.IO.File]::Delete($RetiredPath)
    }
}
foreach ($RetiredDirectoryRelative in @(
    "rag\copilot-cli",
    "instructions",
    "skills\local-rag-setup",
    "skills\local-rag-admin"
)) {
    $RetiredDirectory = Join-Path $Target $RetiredDirectoryRelative
    if (
        (Test-Path -LiteralPath $RetiredDirectory -PathType Container) -and
        (Test-PathHasReparse -Path $RetiredDirectory -Boundary $Target)
    ) {
        throw (
            "retired product directory contains a reparse point: " +
            $RetiredDirectoryRelative
        )
    }
    if (
        (Test-Path -LiteralPath $RetiredDirectory -PathType Container) -and
        -not (Get-ChildItem -LiteralPath $RetiredDirectory -Force)
    ) {
        [System.IO.Directory]::Delete($RetiredDirectory)
    }
}

if (Test-Path -LiteralPath $RuntimePython -PathType Leaf) {
    $InstallStage = "runtime_refresh"
    try {
        Invoke-Setup `
            -PythonCommand $RuntimePython `
            -SetupArguments @("--refresh-completion-marker", "--format", "json")
    } catch {
        Write-Warning (
            "The existing runtime needs dependency or model repair; " +
            "running normal setup once."
        )
        Invoke-Setup `
            -PythonCommand $RuntimePython `
            -SetupArguments @("--format", "json")
    }
} else {
    $InstallStage = "runtime_create"
    $Bootstrap = Resolve-BootstrapPython -Requested $BootstrapPython
    Write-Host "Creating the Local RAG runtime from the source clone..."
    Invoke-Setup `
        -PythonCommand $Bootstrap.Command `
        -PrefixArguments @($Bootstrap.Prefix) `
        -SetupArguments @("--format", "json")
    if (-not (Test-Path -LiteralPath $RuntimePython -PathType Leaf)) {
        throw "setup_required: setup completed without creating the RAG runtime Python."
    }
}
Remove-CompletionMarkerBackups -Backups @($LegacyBackup)
$RuntimeStatus = "READY"
} catch {
    Close-CompletionMarkerGate -Markers @($LegacyMarker)
    throw
}

$InstallStage = "list_dbs"
& $RuntimePython -B (Join-Path $Target "rag\list_dbs.py") --format json | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "setup_required: the installed list_dbs command failed."
}
$DatabaseListStatus = "READY"

$InstallStage = "slash_skill"
$SlashSkillPath = Join-Path $Target "skills\local-rag\SKILL.md"
if (-not (Test-Path -LiteralPath $SlashSkillPath -PathType Leaf)) {
    throw "Local RAG slash Skill is missing from the installed payload."
}
$SlashSkillStatus = "READY"

$InstallStage = "retire_agent003"
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
if (-not [string]::IsNullOrWhiteSpace($env:LOCAL_RAG_COPILOT_PROFILE_PATH)) {
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
$CopilotCliOutput = & $RuntimePython -B (
    Join-Path $Target "rag\query\copilot_cli_setup.py"
) @CopilotCliArguments
$CopilotCliExitCode = $LASTEXITCODE
try {
    $CopilotCliPayload = $CopilotCliOutput | ConvertFrom-Json
} catch {
    throw "Legacy Agent003 retirement returned invalid status output."
}
if ($CopilotCliExitCode -ne 0) {
    $CopilotCliError = [string]$CopilotCliPayload.error
    if ($CopilotCliError -match (
        "(?i)(collision|already owned|unowned artifact)"
    )) {
        $LegacyAgent003Status = "blocked_collision"
    }
    throw ("Legacy Agent003 retirement failed: " + $CopilotCliError)
}
if (@("retired", "absent") -inotcontains (
    [string]$CopilotCliPayload.status
)) {
    throw "Legacy Agent003 retirement returned an invalid success status."
}
$LegacyAgent003Status = [string]$CopilotCliPayload.status

if ($ConfigureVSCodeAutoApprove -and $ConfigureVSCodeRunnerApproval) {
    Write-Warning "Approval options conflict; no approval settings changed."
} elseif ($SkipVSCodeAutoApprove) {
    Write-Host "SkipVSCodeAutoApprove selected; no approval settings changed."
} else {
    $ApprovalMode = "choose"
    if ($ConfigureVSCodeAutoApprove) { $ApprovalMode = "global" }
    elseif ($ConfigureVSCodeRunnerApproval) { $ApprovalMode = "runner" }
    try {
        & $RuntimePython -X utf8 -B (
            Join-Path $Target "rag\query\vscode_approval_settings.py"
        ) --install-root $Target --mode $ApprovalMode
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Optional VS Code approval configuration NOT APPLIED."
        }
    } catch {
        Write-Warning "Optional VS Code approval configuration NOT APPLIED."
    }
}

$InstallStage = "complete"
Write-Host "Installed Copilot Local RAG files to: $Target"
Write-Host "Existing copilot-instructions.md was not overwritten by this repository."
Write-Host "Existing databases were not overwritten by this source-clone install."
Write-Host "Existing machine-local network and Source connection settings were preserved."
Write-Host ""
Write-Host "=== Local RAG install: SUCCESS ===" -ForegroundColor Green
Write-Host "Runtime: $RuntimeStatus"
Write-Host "Databases: $DatabaseListStatus"
Write-Host "Local RAG slash Skill: $SlashSkillStatus"
Write-Host "Legacy Agent003 integration: $LegacyAgent003Status"
if (Test-Path -LiteralPath $ProductBackupRoot) {
    try {
        Remove-Item -LiteralPath $ProductBackupRoot -Recurse -Force `
            -ErrorAction Stop
    } catch {
        Write-Warning (
            "install retained a product transaction directory: " +
            $ProductBackupRoot
        )
    }
}
} catch {
    Restore-ProductFiles
    if (Test-Path -LiteralPath $ProductBackupRoot) {
        try {
            Remove-Item -LiteralPath $ProductBackupRoot -Recurse -Force `
                -ErrorAction Stop
        } catch {
            Write-Warning (
                "install retained a product transaction directory: " +
                $ProductBackupRoot
            )
        }
    }
    Write-Host ""
    Write-Host "=== Local RAG install: FAILED ===" -ForegroundColor Red
    Write-Host "Failed stage: $InstallStage"
    Write-Host "Runtime: $RuntimeStatus"
    Write-Host "Databases: $DatabaseListStatus"
    Write-Host "Local RAG slash Skill: $SlashSkillStatus"
    Write-Host "Legacy Agent003 integration: $LegacyAgent003Status"
    throw
}
