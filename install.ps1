param(
    [string]$Target = (Join-Path $HOME ".copilot"),
    [string]$BootstrapPython = "",
    [switch]$ConfigureVSCodeAutoApprove
)

$ErrorActionPreference = "Stop"

$Payload = Join-Path $PSScriptRoot ".copilot"
$QueryRoot = Join-Path $Target "rag\query"
$RuntimePython = Join-Path $QueryRoot ".venv\Scripts\python.exe"
$LegacyMarker = Join-Path $QueryRoot ".venv\.rag-deps-installed"
$LegacyBackup = $null

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
            & $Command @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 3)" 2>$null
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
        "setup_required: Python 3.10 or newer was not found. " +
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

$InstallStage = "validate_payload"
$RuntimeStatus = "NOT_READY"
$DatabaseListStatus = "NOT_CHECKED"
$VSCodeStatus = "NOT_REQUESTED"

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
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        } else {
            $Parent = Split-Path -Parent $Destination
            if ($Parent) {
                New-Item -ItemType Directory -Force -Path $Parent | Out-Null
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
    "skills\local-rag-admin\SKILL.md"
)
foreach ($RelativePath in $RetiredFiles) {
    $RetiredPath = Join-Path $Target $RelativePath
    if (Test-Path -LiteralPath $RetiredPath -PathType Leaf) {
        [System.IO.File]::Delete($RetiredPath)
    }
}
$RetiredAdminSkill = Join-Path $Target "skills\local-rag-admin"
if (
    (Test-Path -LiteralPath $RetiredAdminSkill -PathType Container) -and
    -not (Get-ChildItem -LiteralPath $RetiredAdminSkill -Force)
) {
    [System.IO.Directory]::Delete($RetiredAdminSkill)
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

if ($ConfigureVSCodeAutoApprove) {
    $InstallStage = "vscode_auto_approve"
    $VSCodeResult = & $RuntimePython `
        -B `
        (Join-Path $Target "rag\query\vscode_settings.py") `
        --copilot-home $Target
    if ($LASTEXITCODE -ne 0) {
        throw "VS Code Local RAG auto-approve configuration failed."
    }
    try {
        $VSCodeStatus = ($VSCodeResult | ConvertFrom-Json).status
    } catch {
        throw "VS Code Local RAG auto-approve returned invalid status output."
    }
    if ($VSCodeStatus -notin @("configured_on_disk", "already_configured")) {
        throw (
            "VS Code Local RAG auto-approve needs manual action: " +
            "$VSCodeStatus. The Local RAG runtime itself is ready."
        )
    }
    Write-Host "VS Code Local RAG auto-approve: $VSCodeStatus"
    Write-Host "Restart VS Code before testing the Local RAG command."
} else {
    Write-Host (
        "VS Code auto-approve was not changed. " +
        "Rerun with -ConfigureVSCodeAutoApprove to allow only the fixed Local RAG commands."
    )
}

$InstallStage = "complete"
Write-Host "Installed Copilot Local RAG files to: $Target"
Write-Host "Existing copilot-instructions.md was not overwritten by this repository."
Write-Host "Existing databases were not overwritten by this source-clone install."
Write-Host "Existing machine-local network and Source connection settings were preserved."
Write-Host ""
Write-Host "=== Local RAG install: SUCCESS ===" -ForegroundColor Green
Write-Host "Runtime: $RuntimeStatus"
Write-Host "Database list command: $DatabaseListStatus"
Write-Host "VS Code auto-approve: $VSCodeStatus"
} catch {
    Write-Host ""
    Write-Host "=== Local RAG install: FAILED ===" -ForegroundColor Red
    Write-Host "Failed stage: $InstallStage"
    Write-Host "Runtime: $RuntimeStatus"
    Write-Host "Database list command: $DatabaseListStatus"
    Write-Host "VS Code auto-approve: $VSCodeStatus"
    throw
}
