[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$PackageRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent $MyInvocation.MyCommand.Path)
)
$Payload = Join-Path $PackageRoot ".copilot"
$Target = Join-Path $env:USERPROFILE ".copilot"
$SourceQuery = Join-Path $Payload "rag\query"
$TargetQuery = Join-Path $Target "rag\query"
$SourceRuntime = Join-Path $SourceQuery ".venv"
$TargetRuntime = Join-Path $TargetQuery ".venv"
$SourceModel = Join-Path $Payload "rag\models\ruri-v3-30m-onnx-int8"
$TargetModel = Join-Path $Target "rag\models\ruri-v3-30m-onnx-int8"
$ManifestPath = Join-Path $PackageRoot "PACKAGE-MANIFEST.json"
$Transaction = [Guid]::NewGuid().ToString("N")
$StageRuntime = Join-Path $TargetQuery (".venv.stage-" + $Transaction)
$BackupRuntime = Join-Path $TargetQuery (".venv.backup-" + $Transaction)
$StageModel = Join-Path (Split-Path -Parent $TargetModel) (
    ".ruri-v3-30m-onnx-int8.stage-" + $Transaction
)
$BackupModel = Join-Path (Split-Path -Parent $TargetModel) (
    ".ruri-v3-30m-onnx-int8.backup-" + $Transaction
)

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

function Remove-TransactionDirectory {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return
    }
    Assert-ChildPath -Root $Target -Candidate $Path
    [System.IO.Directory]::Delete($Path, $true)
}

function Test-ProtectedRelativePath {
    param([string]$Relative)
    $Normalized = $Relative.Replace("/", "\")
    if ($Normalized.StartsWith("rag\dbs\", [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    if ($Normalized.StartsWith("rag\query\run\", [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    if ($Normalized.StartsWith("rag\query\.venv\", [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    if ($Normalized.StartsWith("rag\models\ruri-v3-30m-onnx-int8\", [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $Protected = @(
        "rag\config\network.json",
        "rag\config\manage-custom.json",
        "rag\config\sensitive-terms.local",
        "rag\config\source-connections.json",
        "rag\config\source-connections.secrets.json",
        "rag\config\.source-connections.key",
        "rag\config\windows-test-connection.local.json"
    )
    return $Protected -icontains $Normalized
}

if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
    throw "portable package payload is missing"
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "PACKAGE-MANIFEST.json is missing"
}
$Manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath | ConvertFrom-Json
if ($Manifest.schema -ne "local-rag.windows-package.v1") {
    throw "unsupported package manifest"
}
foreach ($Entry in $Manifest.files) {
    $Relative = [string]$Entry.path
    if (
        [System.IO.Path]::IsPathRooted($Relative) -or
        $Relative.Contains("..") -or
        $Relative.Contains(":")
    ) {
        throw "unsafe package manifest path"
    }
    $File = Join-Path $PackageRoot $Relative
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw ("package file is missing: " + $Relative)
    }
    if ((Get-Item -LiteralPath $File).Length -ne [Int64]$Entry.size) {
        throw ("package file size mismatch: " + $Relative)
    }
    $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $File).Hash
    if ($ActualHash -ine [string]$Entry.sha256) {
        throw ("package file hash mismatch: " + $Relative)
    }
}

$SourcePython = Join-Path $SourceRuntime "Scripts\python.exe"
& $SourcePython (Join-Path $SourceQuery "setup.py") --verify-only --format json |
    Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "source packaged runtime verification failed"
}
$DaemonState = Join-Path $TargetQuery "run\ragd.json"
if (Test-Path -LiteralPath $DaemonState -PathType Leaf) {
    throw "stop the owned Local RAG daemon before updating the runtime"
}

New-Item -ItemType Directory -Force -Path $TargetQuery | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetModel) |
    Out-Null
Assert-ChildPath -Root $Target -Candidate $StageRuntime
Assert-ChildPath -Root $Target -Candidate $BackupRuntime
Assert-ChildPath -Root $Target -Candidate $StageModel
Assert-ChildPath -Root $Target -Candidate $BackupModel

try {
    Copy-Item -LiteralPath $SourceRuntime -Destination $StageRuntime -Recurse
    Copy-Item -LiteralPath $SourceModel -Destination $StageModel -Recurse

    $PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
    Get-ChildItem -LiteralPath $Payload -Force -Recurse | ForEach-Object {
        $Relative = $_.FullName.Substring($PayloadRoot.Length).TrimStart(
            [System.IO.Path]::DirectorySeparatorChar
        )
        if (-not (Test-ProtectedRelativePath -Relative $Relative)) {
            $Destination = Join-Path $Target $Relative
            if ($_.PSIsContainer) {
                New-Item -ItemType Directory -Force -Path $Destination |
                    Out-Null
            } else {
                New-Item -ItemType Directory -Force -Path (
                    Split-Path -Parent $Destination
                ) | Out-Null
                Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
            }
        }
    }

    if (Test-Path -LiteralPath $TargetRuntime) {
        [System.IO.Directory]::Move($TargetRuntime, $BackupRuntime)
    }
    [System.IO.Directory]::Move($StageRuntime, $TargetRuntime)
    if (Test-Path -LiteralPath $TargetModel) {
        [System.IO.Directory]::Move($TargetModel, $BackupModel)
    }
    [System.IO.Directory]::Move($StageModel, $TargetModel)

    $TargetPython = Join-Path $TargetRuntime "Scripts\python.exe"
    & $TargetPython (Join-Path $Target "rag\setup.py") --format json | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "installed runtime verification failed"
    }

    Remove-TransactionDirectory -Path $BackupRuntime
    Remove-TransactionDirectory -Path $BackupModel
} catch {
    if (
        (Test-Path -LiteralPath $BackupRuntime) -and
        -not (Test-Path -LiteralPath $TargetRuntime)
    ) {
        [System.IO.Directory]::Move($BackupRuntime, $TargetRuntime)
    }
    if (
        (Test-Path -LiteralPath $BackupModel) -and
        -not (Test-Path -LiteralPath $TargetModel)
    ) {
        [System.IO.Directory]::Move($BackupModel, $TargetModel)
    }
    Remove-TransactionDirectory -Path $StageRuntime
    Remove-TransactionDirectory -Path $StageModel
    throw
}

Write-Host ("Installed Local RAG Windows portable runtime to: " + $Target)
Write-Host "Use Agent mode and enable runInTerminal in Configure Tools."
Write-Host "Enable readFile when using file result delivery."
