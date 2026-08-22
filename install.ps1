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
$Transaction = [Guid]::NewGuid().ToString("N")
$ProductBackupRoot = Join-Path $QueryRoot (".source-product-backup-" + $Transaction)
$ProductBackedUp = @()
$ProductCreatedFiles = @()
$ProductCreatedDirectories = @()
$MCPStatus = "NOT_CONFIGURED"

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

function Get-NormalizedUtf8Sha256 {
    param([string]$Path)
    try {
        $StrictUtf8 = [Text.UTF8Encoding]::new($false, $true)
        $Text = [IO.File]::ReadAllText($Path, $StrictUtf8)
    } catch { return "" }
    $Bytes = [Text.Encoding]::UTF8.GetBytes(
        $Text.Replace("`r`n", "`n").Replace("`r", "`n")
    )
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return -join ($Hasher.ComputeHash($Bytes) | ForEach-Object {
            $_.ToString("x2")
        })
    } finally { $Hasher.Dispose() }
}

function Test-KnownProductAgentRevision {
    param([string]$Relative, [string]$Destination)
    $Known = switch ($Relative.Replace("/", "\").ToLowerInvariant()) {
        "agents\internal-doc-search.agent.md" { @(
            "93c395b28ca84c3cd328fae8b3a9b5702b4089ef49703b7322527502a5520cf8",
            "486dddb48dd394c131932511a97a80938bee4a8eec02b26f17fb32931ede4fca"
        ) }
        "agents\internal-doc-deep-research.agent.md" { @(
            "5bc8ba97a9d51ebca3f441724cfdd392d258a1d6e551802220b6c01b7768ef39",
            "bae16f42a6fdba678d8cf3ae0ab6facecbe97b3e8f5be8589db8e4c4312fc2a9"
        ) }
        "agents\agent003-readonly-local-rag.agent.md" { @(
            "e9c3591c7ae5a0b17ec9759c67f580eb080b02a8a8b834a3834d32779ea87836"
        ) }
        default { @() }
    }
    return $Known -contains (Get-NormalizedUtf8Sha256 -Path $Destination)
}

function Test-ProductAgentRelativePath {
    param([string]$Relative)
    return @(
        "agents\agent003-readonly-local-rag.agent.md",
        "agents\internal-doc-deep-research.agent.md",
        "agents\internal-doc-search.agent.md"
    ) -icontains $Relative.Replace("/", "\")
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
                if ((Test-ProductAgentRelativePath -Relative $Relative) -and
                    -not (Test-KnownProductAgentRevision `
                        -Relative $Relative -Destination $Destination)) {
                    return
                }
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
    "rag\query\vscode_settings.py",
    "skills\local-rag-admin\SKILL.md"
)
foreach ($RelativePath in $RetiredFiles) {
    $RetiredPath = Join-Path $Target $RelativePath
    if (Test-Path -LiteralPath $RetiredPath -PathType Leaf) {
        Backup-ProductFile -Relative $RelativePath -Destination $RetiredPath
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

$InstallStage = "mcp_config"
$McpRelative = "mcp-config.json"
$McpTarget = Join-Path $Target $McpRelative
if (Test-PathHasReparse -Path $McpTarget -Boundary $Target) {
    throw "MCP configuration target contains a reparse point."
}
if (Test-Path -LiteralPath $McpTarget -PathType Leaf) {
    Backup-ProductFile -Relative $McpRelative -Destination $McpTarget
} else {
    $ProductCreatedFiles += $McpTarget
}
$McpOutput = & $RuntimePython -B (
    Join-Path $Target "rag\query\mcp_config.py"
) --copilot-home $Target --no-backup
$McpExitCode = $LASTEXITCODE
try {
    $McpPayload = $McpOutput | ConvertFrom-Json
} catch {
    throw "Local RAG MCP configuration returned invalid status output."
}
if ($McpExitCode -ne 0 -or
    @("configured_on_disk", "already_configured") -inotcontains (
        [string]$McpPayload.status
    )) {
    throw "Local RAG MCP configuration failed."
}
$MCPStatus = [string]$McpPayload.status

$InstallStage = "complete"
Write-Host "Installed Copilot Local RAG files to: $Target"
Write-Host "Existing copilot-instructions.md was not overwritten by this repository."
Write-Host "Existing databases were not overwritten by this source-clone install."
Write-Host "Existing machine-local network and Source connection settings were preserved."
Write-Host ""
Write-Host "=== Local RAG install: SUCCESS ===" -ForegroundColor Green
Write-Host "Runtime: $RuntimeStatus"
Write-Host "Databases: $DatabaseListStatus"
Write-Host "MCP: $MCPStatus"
if (Test-Path -LiteralPath $ProductBackupRoot) {
    Remove-Item -LiteralPath $ProductBackupRoot -Recurse -Force
}
} catch {
    Restore-ProductFiles
    if (Test-Path -LiteralPath $ProductBackupRoot) {
        Remove-Item -LiteralPath $ProductBackupRoot -Recurse -Force
    }
    Write-Host ""
    Write-Host "=== Local RAG install: FAILED ===" -ForegroundColor Red
    Write-Host "Failed stage: $InstallStage"
    Write-Host "Runtime: $RuntimeStatus"
    Write-Host "Databases: $DatabaseListStatus"
    Write-Host "MCP: $MCPStatus"
    throw
}
