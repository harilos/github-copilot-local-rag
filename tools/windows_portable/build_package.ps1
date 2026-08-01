[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RuntimeRoot,
    [Parameter(Mandatory = $true)][string]$ModelRoot,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [ValidateSet("search-only", "admin-full")][string]$Profile = "search-only",
    [string]$DatabasesRoot,
    [string[]]$DatabaseNames,
    [switch]$NoDatabase,
    [string]$DatabaseRoot
)

$ErrorActionPreference = "Stop"
$ToolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $ToolRoot "..\.."))
$Lock = Get-Content -Raw -Encoding UTF8 (Join-Path $ToolRoot "runtime-lock.json") |
    ConvertFrom-Json
$RequirementsLock = Join-Path $ToolRoot (
    if ($Profile -eq "search-only") {
        "requirements-search.lock"
    } else {
        "requirements-admin.lock"
    }
)
$DependencyFingerprint = (
    Get-FileHash -Algorithm SHA256 -LiteralPath $RequirementsLock
).Hash.ToLowerInvariant()
$ModelManifest = Join-Path $ModelRoot "MODEL_MANIFEST.json"
if (-not (Test-Path -LiteralPath $ModelManifest -PathType Leaf)) {
    throw "MODEL_MANIFEST.json is required in ModelRoot"
}
$ModelFingerprint = (
    Get-FileHash -Algorithm SHA256 -LiteralPath $ModelManifest
).Hash.ToLowerInvariant()
$Python = Get-Command python -ErrorAction Stop

$Arguments = @(
    (Join-Path $ToolRoot "build_package.py"),
    "--payload-root", (Join-Path $RepositoryRoot ".copilot"),
    "--runtime-root", $RuntimeRoot,
    "--model-root", $ModelRoot,
    "--output-dir", $OutputDirectory,
    "--version", (
        Get-Content -Raw -Encoding UTF8 (
            Join-Path $RepositoryRoot ".copilot\rag\VERSION"
        )
    ).Trim(),
    "--profile", $Profile,
    "--python-version", [string]$Lock.python.version,
    "--dependency-lock-sha256", $DependencyFingerprint,
    "--model-fingerprint", $ModelFingerprint
)
if ($DatabaseRoot -and ($DatabasesRoot -or $DatabaseNames.Count)) {
    throw "legacy DatabaseRoot cannot be combined with canonical database arguments"
}
if ($NoDatabase -and ($DatabaseRoot -or $DatabasesRoot -or $DatabaseNames.Count)) {
    throw "NoDatabase cannot be combined with database arguments"
}
if ($DatabaseNames.Count -and -not $DatabasesRoot) {
    throw "DatabaseNames requires DatabasesRoot"
}
if ($NoDatabase) {
    $Arguments += "--no-database"
} elseif ($DatabasesRoot) {
    $Arguments += @("--dbs-root", $DatabasesRoot)
    foreach ($DatabaseName in $DatabaseNames) {
        $Arguments += @("--db", $DatabaseName)
    }
} elseif ($DatabaseRoot) {
    $Arguments += @("--database-root", $DatabaseRoot)
} else {
    $Arguments += @("--dbs-root", (Join-Path $RepositoryRoot ".copilot\rag\dbs"))
}
& $Python.Source @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "Windows portable package build failed"
}
