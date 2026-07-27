param(
    [string]$Target = (Join-Path $HOME ".copilot")
)

$ErrorActionPreference = "Stop"

$Payload = Join-Path $PSScriptRoot ".copilot"

if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
    throw "Missing install payload: $Payload"
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null

$PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
Get-ChildItem -LiteralPath $Payload -Force -Recurse | ForEach-Object {
    $Relative = $_.FullName.Substring($PayloadRoot.Length).TrimStart(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if ($Relative -ine "rag\config\network.json") {
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

$LegacyPython = Join-Path $Target "rag\query\.venv\Scripts\python.exe"
$LegacyMarker = Join-Path $Target "rag\query\.venv\.rag-deps-installed"
if (
    (Test-Path -LiteralPath $LegacyPython -PathType Leaf) -and
    (Test-Path -LiteralPath $LegacyMarker -PathType Leaf) -and
    ((Get-Content -LiteralPath $LegacyMarker -Raw).Trim() -ceq "ok")
) {
    & $LegacyPython (Join-Path $Target "rag\query\setup.py") `
        --migrate-legacy-marker --format json | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Existing RAG runtime needs setup verification before lookup."
    }
}

Write-Host "Installed Copilot Local RAG files to: $Target"
Write-Host "Existing copilot-instructions.md was not overwritten by this repository."
Write-Host "Existing rag/config/network.json was preserved."
