from __future__ import annotations


INSTALL_SH_TEXT = r"""#!/usr/bin/env sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PAYLOAD_DIR="$SOURCE_DIR/.copilot"
TARGET_DIR="${COPILOT_HOME:-$HOME/.copilot}"

if [ ! -d "$PAYLOAD_DIR" ]; then
  echo "Missing install payload: $PAYLOAD_DIR" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"

QUERY_ROOT="$TARGET_DIR/rag/query"
RUNTIME_PYTHON="$QUERY_ROOT/.venv/bin/python"
LEGACY_MARKER="$QUERY_ROOT/.venv/.rag-deps-installed"
LEGACY_BACKUP=""
LEGACY_RESCUE=""
move_marker() {
  marker="$1"; label="$2"
  if [ ! -f "$marker" ]; then return; fi
  backup="$QUERY_ROOT/.rag-deps-installed.$label.pre-update.$$"; suffix=0
  while [ -e "$backup" ]; do suffix=$((suffix + 1)); backup="$QUERY_ROOT/.rag-deps-installed.$label.pre-update.$$.$suffix"; done
  if ! mv "$marker" "$backup"; then echo "setup_required: could not close the Local RAG lookup gate before update." >&2; exit 1; fi
  LEGACY_BACKUP="$backup"
}
close_markers() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    if [ -n "$LEGACY_RESCUE" ] && [ -f "$LEGACY_RESCUE" ] && [ ! -f "$LEGACY_BACKUP" ]; then mv "$LEGACY_RESCUE" "$LEGACY_BACKUP" || true; fi
    rm -f -- "$LEGACY_MARKER" || true
  fi
  exit "$status"
}
trap close_markers EXIT
move_marker "$LEGACY_MARKER" legacy

(
  cd "$PAYLOAD_DIR"
  tar \
    --exclude='./rag/query/.rag-deps-installed' \
    --exclude='./rag/query/.rag-deps-installed.*' \
    -cf - .
) | (
  cd "$TARGET_DIR"
  tar -xf -
)

rm -f -- \
  "$TARGET_DIR/rag/query/.packaged-runtime.json" \
  "$TARGET_DIR/rag/query/.rag-deps-installed" \
  "$TARGET_DIR/rag/query/portable_runtime.py" \
  "$TARGET_DIR/rag/query/portable_db_install.py" \
  "$TARGET_DIR/rag/query/portable_db_smoke.py"

if [ -x "$RUNTIME_PYTHON" ]; then
  if ! "$RUNTIME_PYTHON" "$TARGET_DIR/rag/query/setup.py" \
      --refresh-completion-marker --format json >/dev/null; then
    echo "setup_required: existing RAG runtime verification failed; run Local RAG setup before lookup." >&2
    exit 1
  fi
elif [ -n "$LEGACY_BACKUP" ]; then
  echo "setup_required: the existing Local RAG runtime Python is missing after update." >&2
  exit 1
fi
if [ -n "$LEGACY_BACKUP" ]; then LEGACY_RESCUE="$LEGACY_BACKUP.cleanup"; cp -p -- "$LEGACY_BACKUP" "$LEGACY_RESCUE"; fi
rm -f -- "$LEGACY_BACKUP"
LEGACY_BACKUP=""
trap - EXIT
rm -f -- "$LEGACY_RESCUE" || true
LEGACY_RESCUE=""

echo "Copied Copilot Local RAG files to: $TARGET_DIR"
echo "Existing files not present in this package were preserved."
echo "Run Local RAG setup before the first lookup on this computer."
"""


INSTALL_PS1_TEXT = r"""param(
    [string]$Target = (Join-Path $HOME ".copilot")
)

$ErrorActionPreference = "Stop"

$Payload = Join-Path $PSScriptRoot ".copilot"

if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
    throw "Missing install payload: $Payload"
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null

$QueryRoot = Join-Path $Target "rag\query"
$RuntimePython = Join-Path $QueryRoot ".venv\Scripts\python.exe"
$LegacyMarker = Join-Path $QueryRoot ".venv\.rag-deps-installed"
$LegacyBackup = $null
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

try {
$LegacyBackup = Move-CompletionMarker -Marker $LegacyMarker -Label "legacy"
    $PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
Get-ChildItem -LiteralPath $Payload -Force -Recurse | ForEach-Object {
    $Relative = $_.FullName.Substring($PayloadRoot.Length).TrimStart(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (
        ($Relative -ieq "rag\query\.rag-deps-installed") -or
        $Relative.StartsWith(
            "rag\query\.rag-deps-installed.",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return
    }
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

foreach ($RelativePath in @(
    "rag\query\.packaged-runtime.json",
    "rag\query\.rag-deps-installed",
    "rag\query\portable_runtime.py",
    "rag\query\portable_db_install.py",
    "rag\query\portable_db_smoke.py"
)) {
    $RetiredPath = Join-Path $Target $RelativePath
    if (Test-Path -LiteralPath $RetiredPath -PathType Leaf) {
        [System.IO.File]::Delete($RetiredPath)
    }
}

if (Test-Path -LiteralPath $RuntimePython -PathType Leaf) {
    & $RuntimePython (Join-Path $Target "rag\query\setup.py") --refresh-completion-marker --format json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ("setup_required: existing RAG runtime verification failed; " + "run Local RAG setup before lookup.") }
} elseif ($null -ne $LegacyBackup) {
    throw ("setup_required: the existing Local RAG runtime Python is missing " + "after update.")
}
Remove-CompletionMarkerBackups -Backups @($LegacyBackup)
} catch {
    Close-CompletionMarkerGate -Markers @($LegacyMarker)
    throw
}

Write-Host "Copied Copilot Local RAG files to: $Target"
Write-Host "Existing files not present in this package were preserved."
Write-Host "Run Local RAG setup before the first lookup on this computer."
"""


__all__ = ["INSTALL_PS1_TEXT", "INSTALL_SH_TEXT"]
