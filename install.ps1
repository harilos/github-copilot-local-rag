param(
    [string]$Target = (Join-Path $HOME ".copilot")
)

$ErrorActionPreference = "Stop"

$Payload = Join-Path $PSScriptRoot ".copilot"

if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
    throw "Missing install payload: $Payload"
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null
Get-ChildItem -LiteralPath $Payload -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Target -Recurse -Force
}

Write-Host "Installed Copilot Local RAG files to: $Target"
Write-Host "Existing copilot-instructions.md was not overwritten by this repository."
