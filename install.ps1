param(
    [string]$Target = (Join-Path $HOME ".copilot")
)

$ErrorActionPreference = "Stop"

$Payload = Join-Path $PSScriptRoot ".copilot"

if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
    throw "Missing install payload: $Payload"
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null

$RuntimePython = Join-Path $Target "rag\query\.venv\Scripts\python.exe"
$CompletionMarker = Join-Path $Target "rag\query\.venv\.rag-deps-installed"
$PreUpdateMarker = $null
if (Test-Path -LiteralPath $CompletionMarker -PathType Leaf) {
    $PreUpdateMarker = (
        $CompletionMarker +
        ".pre-update." +
        $PID +
        "." +
        [Guid]::NewGuid().ToString("N")
    )
    [System.IO.File]::Move($CompletionMarker, $PreUpdateMarker)
}

function Test-InstallPayloadExcluded {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $Normalized = $RelativePath.Replace("/", "\").TrimStart("\")
    if ($Normalized -ieq "rag\config\network.json") {
        return $true
    }
    if ($Normalized -ieq "rag\config\sensitive-terms.local") {
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
    & $RuntimePython (Join-Path $Target "rag\query\setup.py") `
        --refresh-completion-marker --format json | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw (
            "setup_required: existing RAG runtime verification failed; " +
            "run Local RAG setup before lookup."
        )
    }
} elseif ($null -ne $PreUpdateMarker) {
    throw (
        "setup_required: the existing Local RAG runtime Python is missing " +
        "after update."
    )
}

if ($null -ne $PreUpdateMarker) {
    [System.IO.File]::Delete($PreUpdateMarker)
}

Write-Host "Installed Copilot Local RAG files to: $Target"
Write-Host "Existing copilot-instructions.md was not overwritten by this repository."
Write-Host "Existing rag/config/network.json was preserved."
