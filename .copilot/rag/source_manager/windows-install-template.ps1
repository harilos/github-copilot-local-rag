[CmdletBinding()]
param(
    [switch]$ConfigureVSCodeAutoApprove,
    [switch]$SkipVSCodeAutoApprove,
    [switch]$ReplaceExistingDatabases
)

$ErrorActionPreference = "Stop"
if ($SkipVSCodeAutoApprove) {
    $ConfigureVSCodeAutoApprove = $false
}
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
$VSCodeStatus = if ($ConfigureVSCodeAutoApprove) { "PENDING" } else { "NOT_REQUESTED" }

trap {
    Write-Host ""
    Write-Host "=== Local RAG install: FAILED ===" -ForegroundColor Red
    Write-Host ("Failed stage: " + $InstallStage)
    Write-Host ("Runtime: " + $RuntimeStatus)
    Write-Host ("Databases: " + $DatabaseStatus)
    Write-Host ("VS Code auto-approve: " + $VSCodeStatus)
    Write-Host ("Reason: " + $_.Exception.Message)
    exit 1
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
        "rag\query\portable_db_smoke.py"
    )) {
        $Retired = Join-Path $Target $Relative
        if (Test-Path -LiteralPath $Retired -PathType Leaf) {
            Backup-ProductFile -Relative $Relative -Destination $Retired
            [System.IO.File]::Delete($Retired)
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

    if ($ConfigureVSCodeAutoApprove) {
        $InstallStage = "vscode_auto_approve"
        $VscodeText = (& (Join-Path $StageRuntime "Scripts\python.exe") -B (
            Join-Path $TargetQuery "vscode_settings.py"
        ) --copilot-home $Target | Out-String)
        if ($LASTEXITCODE -ne 0) {
            throw (
                "explicit VS Code auto-approve opt-in did not complete: " +
                $VscodeText.Trim()
            )
        }
        try {
            $VscodeResult = $VscodeText | ConvertFrom-Json
        } catch {
            throw (
                "explicit VS Code auto-approve opt-in returned invalid JSON: " +
                $VscodeText.Trim()
            )
        }
        if (@("configured_on_disk", "already_configured") -inotcontains (
            [string]$VscodeResult.status
        )) {
            throw (
                "explicit VS Code auto-approve opt-in did not complete: " +
                $VscodeText.Trim()
            )
        }
        $VSCodeStatus = [string]$VscodeResult.status
    }

    $InstallStage = "publish_runtime"
    [System.IO.Directory]::Move($StageRuntime, $TargetRuntime)
    $RuntimePublished = $true
    $RuntimeStatus = "READY"
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

Write-Host ("Installed Local RAG Windows portable runtime to: " + $Target)
Write-Host "Use Agent mode and enable runInTerminal in Configure Tools."
Write-Host "Enable readFile when using file result delivery."
Write-Host ""
Write-Host "=== Local RAG install: SUCCESS ===" -ForegroundColor Green
Write-Host ("Runtime: " + $RuntimeStatus)
Write-Host ("Databases: " + $DatabaseStatus)
Write-Host ("VS Code auto-approve: " + $VSCodeStatus)
