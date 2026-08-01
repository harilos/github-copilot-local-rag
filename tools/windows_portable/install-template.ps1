[CmdletBinding()]
param(
    [switch]$ConfigureVSCodeAutoApprove,
    [switch]$ReplaceExistingDatabases
)

$ErrorActionPreference = "Stop"
$PackageRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Payload = Join-Path $PackageRoot ".copilot"
$Target = Join-Path $env:USERPROFILE ".copilot"
$SourceQuery = Join-Path $Payload "rag\query"
$TargetQuery = Join-Path $Target "rag\query"
$SourceRuntime = Join-Path $SourceQuery ".venv"
$TargetRuntime = Join-Path $TargetQuery ".venv"
$SourceModel = Join-Path $Payload "rag\models\ruri-v3-30m-onnx-int8"
$TargetModel = Join-Path $Target "rag\models\ruri-v3-30m-onnx-int8"
$TargetDbs = Join-Path $Target "rag\dbs"
$ManifestPath = Join-Path $PackageRoot "PACKAGE-MANIFEST.json"
$Transaction = [Guid]::NewGuid().ToString("N")
$StageRuntime = Join-Path $TargetQuery (".venv.stage-" + $Transaction)
$BackupRuntime = Join-Path $TargetQuery (".venv.backup-" + $Transaction)
$StageModel = Join-Path (Split-Path -Parent $TargetModel) (".model.stage-" + $Transaction)
$BackupModel = Join-Path (Split-Path -Parent $TargetModel) (".model.backup-" + $Transaction)
$BackupProduct = Join-Path $TargetQuery (".product.backup-" + $Transaction)
$BackupDbs = Join-Path $TargetDbs (".portable.backup-" + $Transaction)
$ActiveMarker = Join-Path $TargetQuery ".rag-deps-installed"
$LegacyMarker = Join-Path $TargetRuntime ".rag-deps-installed"
$MarkerBackups = @()
$ProductBackedUp = @()
$ProductCreatedFiles = @()
$ProductCreatedDirectories = @()
$DatabaseBackedUp = @()
$DatabaseFresh = @()
$RuntimePublished = $false
$ModelPublished = $false
$PreexistingDatabaseNames = @()
if (Test-Path -LiteralPath $TargetDbs -PathType Container) {
    $PreexistingDatabaseNames = @(Get-ChildItem -LiteralPath $TargetDbs -Directory -Force | Where-Object { -not $_.Name.StartsWith(".") } | ForEach-Object { $_.Name })
}

function Assert-ChildPath {
    param([string]$Root, [string]$Candidate)
    $ResolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $ResolvedCandidate = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $ResolvedCandidate.StartsWith($ResolvedRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "transaction path escapes the Local RAG target"
    }
}
function Assert-NoReparsePath {
    param([string]$Path)
    $Current = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($Path))
    while ($null -ne $Current) {
        try {
            $Attributes = [System.IO.File]::GetAttributes($Current.FullName)
            if (($Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ("target path contains a reparse point: " + $Current.FullName)
            }
        } catch [System.IO.FileNotFoundException] {
        } catch [System.IO.DirectoryNotFoundException] {
        }
        if ($null -ne $Current.Parent -and $Current.Parent.Exists) {
            $Entry = Get-ChildItem -LiteralPath $Current.Parent.FullName -Force | Where-Object { $_.Name -ceq $Current.Name } | Select-Object -First 1
            if ($null -ne $Entry -and (($Entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
                throw ("target path contains a reparse point: " + $Current.FullName)
            }
        }
        $Current = $Current.Parent
    }
}
function Remove-Tree {
    param([string]$Path)
    if ($Path -and (Test-Path -LiteralPath $Path)) {
        Assert-ChildPath -Root $Target -Candidate $Path
        [System.IO.Directory]::Delete($Path, $true)
    }
}
function Close-CompletionMarkerGate {
    foreach ($Marker in @($ActiveMarker, $LegacyMarker)) {
        if (Test-Path -LiteralPath $Marker -PathType Leaf) { [System.IO.File]::Delete($Marker) }
    }
}
function Test-ProtectedRelativePath {
    param([string]$Relative)
    $Value = $Relative.Replace("/", "\")
    if ($Value.StartsWith("rag\dbs\", [StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($Value.StartsWith("rag\query\run\", [StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($Value.StartsWith("rag\query\.venv\", [StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($Value.StartsWith("rag\models\ruri-v3-30m-onnx-int8\", [StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($Value -ieq "rag\query\.rag-deps-installed" -or $Value.StartsWith("rag\query\.rag-deps-installed.", [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return @("rag\config\network.json", "rag\config\manage-custom.json", "rag\config\sensitive-terms.local", "rag\config\source-connections.json", "rag\config\source-connections.secrets.json", "rag\config\.source-connections.key", "rag\config\windows-test-connection.local.json") -icontains $Value
}
function Move-CompletionMarker {
    param([string]$Path, [string]$Label)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $Backup = Join-Path $TargetQuery (".rag-deps-installed." + $Label + ".pre-update." + $Transaction)
        [System.IO.File]::Move($Path, $Backup)
        $script:MarkerBackups += $Backup
    }
}

Assert-NoReparsePath -Path $Target
Assert-NoReparsePath -Path $TargetDbs
Assert-NoReparsePath -Path $TargetQuery
Assert-NoReparsePath -Path $TargetRuntime
Assert-NoReparsePath -Path $TargetModel
if (-not (Test-Path -LiteralPath $Payload -PathType Container)) { throw "portable package payload is missing" }
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw "PACKAGE-MANIFEST.json is missing" }
$Manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath | ConvertFrom-Json
if ($Manifest.schema -ne "local-rag.windows-package.v2") { throw "unsupported package manifest" }
$Declared = @{}
foreach ($Entry in $Manifest.files) {
    $Relative = [string]$Entry.path
    $Folded = $Relative.ToLowerInvariant()
    if ($Declared.ContainsKey($Folded)) { throw "duplicate or case-colliding manifest path" }
    $Declared[$Folded] = $Relative
    if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains("..") -or $Relative.Contains(":")) { throw "unsafe package manifest path" }
    $File = Join-Path $PackageRoot $Relative
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw ("package file is missing: " + $Relative) }
    if ((Get-Item -LiteralPath $File).Length -ne [Int64]$Entry.size) { throw ("package file size mismatch: " + $Relative) }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $File).Hash -ine [string]$Entry.sha256) { throw ("package file hash mismatch: " + $Relative) }
}
$Actual = @{}
Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force -File | ForEach-Object {
    $Relative = $_.FullName.Substring($PackageRoot.Length).TrimStart("\").Replace("\", "/")
    $Folded = $Relative.ToLowerInvariant()
    if ($Actual.ContainsKey($Folded)) { throw "case-colliding package path" }
    $Actual[$Folded] = $Relative
}
if ($Actual.Count -ne ($Declared.Count + 1) -or -not $Actual.ContainsKey("package-manifest.json")) { throw "package tree is not the manifest closed set" }
foreach ($Key in $Declared.Keys) { if (-not $Actual.ContainsKey($Key)) { throw "package tree is not the manifest closed set" } }

$SourcePython = Join-Path $SourceRuntime "Scripts\python.exe"
$SourceVerificationText = (& $SourcePython (Join-Path $SourceQuery "setup.py") --verify-only --format json | Out-String)
if ($LASTEXITCODE -ne 0) { throw "source packaged runtime verification failed" }
$SourceVerification = $SourceVerificationText | ConvertFrom-Json
if (-not $SourceVerification.setup_complete) { throw "source packaged runtime is incomplete" }
if (Test-Path -LiteralPath (Join-Path $TargetQuery "run\ragd.json") -PathType Leaf) { throw "stop the owned Local RAG daemon before updating" }
$DbArguments = @("--package-root", $PackageRoot, "--target-root", $TargetDbs, "--preflight")
if ($ReplaceExistingDatabases) { $DbArguments += "--replace-existing" }
& $SourcePython (Join-Path $SourceQuery "portable_db_install.py") @DbArguments | Out-Null
if ($LASTEXITCODE -ne 0) { throw "database preflight failed" }

try {
    New-Item -ItemType Directory -Force -Path $TargetQuery, (Split-Path -Parent $TargetModel), $TargetDbs | Out-Null
    Move-CompletionMarker -Path $ActiveMarker -Label "active"
    Move-CompletionMarker -Path $LegacyMarker -Label "legacy"
    Copy-Item -LiteralPath $SourceRuntime -Destination $StageRuntime -Recurse
    Copy-Item -LiteralPath $SourceModel -Destination $StageModel -Recurse

    $PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
    Get-ChildItem -LiteralPath $Payload -Force -Recurse | ForEach-Object {
        $Relative = $_.FullName.Substring($PayloadRoot.Length).TrimStart([System.IO.Path]::DirectorySeparatorChar)
        if (-not (Test-ProtectedRelativePath -Relative $Relative)) {
            $Destination = Join-Path $Target $Relative
            Assert-NoReparsePath -Path $Destination
            if ($_.PSIsContainer) {
                if (-not (Test-Path -LiteralPath $Destination)) {
                    New-Item -ItemType Directory -Path $Destination | Out-Null
                    $script:ProductCreatedDirectories += $Destination
                }
            } else {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
                if (Test-Path -LiteralPath $Destination -PathType Leaf) {
                    $Backup = Join-Path $BackupProduct $Relative
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Backup) | Out-Null
                    Copy-Item -LiteralPath $Destination -Destination $Backup
                    $script:ProductBackedUp += $Relative
                } else {
                    $script:ProductCreatedFiles += $Destination
                }
                Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
            }
        }
    }

    $Rechecked = @{}
    Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force -File | ForEach-Object {
        $Relative = $_.FullName.Substring($PackageRoot.Length).TrimStart("\").Replace("\", "/")
        $Key = $Relative.ToLowerInvariant()
        if ($Rechecked.ContainsKey($Key)) { throw "package changed during installation" }
        $Rechecked[$Key] = $Relative
    }
    if ($Rechecked.Count -ne ($Declared.Count + 1)) { throw "package changed during installation" }
    foreach ($Entry in $Manifest.files) {
        $File = Join-Path $PackageRoot ([string]$Entry.path)
        if (-not (Test-Path -LiteralPath $File -PathType Leaf) -or (Get-Item -LiteralPath $File).Length -ne [Int64]$Entry.size -or (Get-FileHash -Algorithm SHA256 -LiteralPath $File).Hash -ine [string]$Entry.sha256) {
            throw ("package changed during installation: " + [string]$Entry.path)
        }
    }

    Assert-NoReparsePath -Path $Target
    Assert-NoReparsePath -Path $TargetQuery
    Assert-NoReparsePath -Path $TargetRuntime
    Assert-NoReparsePath -Path $TargetModel
    Assert-NoReparsePath -Path $TargetDbs
    if (Test-Path -LiteralPath $TargetRuntime) { [System.IO.Directory]::Move($TargetRuntime, $BackupRuntime) }
    [System.IO.Directory]::Move($StageRuntime, $TargetRuntime); $RuntimePublished = $true
    if (Test-Path -LiteralPath $TargetModel) { [System.IO.Directory]::Move($TargetModel, $BackupModel) }
    [System.IO.Directory]::Move($StageModel, $TargetModel); $ModelPublished = $true

    foreach ($Database in $Manifest.databases) {
        $Name = [string]$Database.name
        $Existing = Join-Path $TargetDbs $Name
        if (Test-Path -LiteralPath $Existing -PathType Container) {
            New-Item -ItemType Directory -Force -Path $BackupDbs | Out-Null
            Copy-Item -LiteralPath $Existing -Destination (Join-Path $BackupDbs $Name) -Recurse
            $DatabaseBackedUp += $Name
        } else { $DatabaseFresh += $Name }
    }
    $DbArguments = @("--package-root", $PackageRoot, "--target-root", $TargetDbs)
    if ($ReplaceExistingDatabases) { $DbArguments += "--replace-existing" }
    & (Join-Path $TargetRuntime "Scripts\python.exe") (Join-Path $TargetQuery "portable_db_install.py") @DbArguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "database publish failed" }

    $SetupArguments = @((Join-Path $Target "rag\setup.py"), "--format", "json", "--defer-completion-marker")
    if ($ConfigureVSCodeAutoApprove) { $SetupArguments += "--configure-vscode-auto-approve" }
    $SetupText = (& (Join-Path $TargetRuntime "Scripts\python.exe") @SetupArguments | Out-String)
    if ($LASTEXITCODE -ne 0) {
        if ($ConfigureVSCodeAutoApprove) { throw "installed runtime verification failed: explicit VS Code auto-approve opt-in did not complete" }
        throw "installed runtime verification failed"
    }
    $Setup = $SetupText | ConvertFrom-Json
    if (-not $Setup.setup_complete) { throw "installed setup contract is incomplete" }
    if (@($Manifest.databases).Count -gt 0 -and -not $Setup.lookup_ready) { throw "installed setup is not lookup-ready" }
    $Healthy = @($Setup.databases.healthy)
    foreach ($Database in $Manifest.databases) { if ($Healthy -notcontains [string]$Database.name) { throw ("installed database is unhealthy: " + $Database.name) } }

    $ListText = (& (Join-Path $TargetRuntime "Scripts\python.exe") (Join-Path $TargetQuery "list_dbs.py") --format json | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "installed database listing failed" }
    $ListPayload = $ListText | ConvertFrom-Json
    $ListedNames = @($ListPayload.databases | ForEach-Object { [string]$_.name })
    $ManifestNames = @($Manifest.databases | ForEach-Object { [string]$_.name })
    foreach ($Name in $ManifestNames) { if ($ListedNames -notcontains $Name) { throw ("installed database is absent from list_dbs: " + $Name) } }
    if ($PreexistingDatabaseNames.Count -eq 0 -and @(Compare-Object $ManifestNames $ListedNames).Count -ne 0) { throw "fresh install database set differs from manifest" }

    if (@($Manifest.databases).Count -gt 0) {
        $SmokeArguments = @((Join-Path $TargetQuery "portable_db_smoke.py"))
        foreach ($Database in $Manifest.databases) { $SmokeArguments += @("--db", [string]$Database.name) }
        & (Join-Path $TargetRuntime "Scripts\python.exe") @SmokeArguments | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "database smoke search failed" }
    }
    $RefreshText = (& (Join-Path $TargetRuntime "Scripts\python.exe") (Join-Path $Target "rag\setup.py") --refresh-completion-marker --format json | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "completion marker refresh failed" }
    $Refresh = $RefreshText | ConvertFrom-Json
    if (-not $Refresh.setup_complete -or -not (Test-Path -LiteralPath $ActiveMarker -PathType Leaf)) { throw "completion marker postvalidation failed" }

} catch {
    Close-CompletionMarkerGate
    if ($RuntimePublished -and (Test-Path -LiteralPath $TargetRuntime)) { Remove-Tree $TargetRuntime }
    if (Test-Path -LiteralPath $BackupRuntime) { [System.IO.Directory]::Move($BackupRuntime, $TargetRuntime) }
    if ($ModelPublished -and (Test-Path -LiteralPath $TargetModel)) { Remove-Tree $TargetModel }
    if (Test-Path -LiteralPath $BackupModel) { [System.IO.Directory]::Move($BackupModel, $TargetModel) }
    foreach ($Name in @($DatabaseBackedUp) + @($DatabaseFresh)) {
        $Current = Join-Path $TargetDbs $Name
        if (Test-Path -LiteralPath $Current) { Remove-Tree $Current }
        $Backup = Join-Path $BackupDbs $Name
        if (Test-Path -LiteralPath $Backup) { [System.IO.Directory]::Move($Backup, $Current) }
    }
    foreach ($Path in $ProductCreatedFiles) { if (Test-Path -LiteralPath $Path -PathType Leaf) { [System.IO.File]::Delete($Path) } }
    foreach ($Relative in $ProductBackedUp) {
        $Backup = Join-Path $BackupProduct $Relative; $Destination = Join-Path $Target $Relative
        if (Test-Path -LiteralPath $Backup -PathType Leaf) { Copy-Item -LiteralPath $Backup -Destination $Destination -Force }
    }
    foreach ($Path in ($ProductCreatedDirectories | Sort-Object { $_.Length } -Descending)) {
        if ((Test-Path -LiteralPath $Path -PathType Container) -and -not (Get-ChildItem -LiteralPath $Path -Force)) { [System.IO.Directory]::Delete($Path) }
    }
    Remove-Tree $StageRuntime; Remove-Tree $StageModel
    foreach ($Path in @($TargetQuery, (Split-Path -Parent $TargetModel), $TargetDbs, (Join-Path $Target "rag"), $Target)) {
        if ((Test-Path -LiteralPath $Path -PathType Container) -and -not (Get-ChildItem -LiteralPath $Path -Force)) { [System.IO.Directory]::Delete($Path) }
    }
    throw
}

foreach ($Path in @($BackupRuntime, $BackupModel, $BackupProduct, $BackupDbs)) {
    try { Remove-Tree $Path } catch { Write-Warning ("verified install retained backup: " + $Path) }
}
foreach ($Backup in $MarkerBackups) {
    try { if (Test-Path -LiteralPath $Backup -PathType Leaf) { [System.IO.File]::Delete($Backup) } } catch { Write-Warning ("verified install retained marker backup: " + $Backup) }
}

Write-Host ("Installed Local RAG Windows portable runtime to: " + $Target)
Write-Host "Use Agent mode and enable runInTerminal in Configure Tools."
Write-Host "Enable readFile when using file result delivery."
