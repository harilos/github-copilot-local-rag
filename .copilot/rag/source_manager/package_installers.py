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
if [ -f "$QUERY_ROOT/.packaged-runtime.json" ]; then
  COMPLETION_MARKER="$QUERY_ROOT/.rag-deps-installed"
else
  COMPLETION_MARKER="$QUERY_ROOT/.venv/.rag-deps-installed"
fi
PRE_UPDATE_MARKER=""
if [ -f "$COMPLETION_MARKER" ]; then
  PRE_UPDATE_MARKER="${COMPLETION_MARKER}.pre-update.$$"
  if ! mv "$COMPLETION_MARKER" "$PRE_UPDATE_MARKER"; then
    echo "setup_required: could not close the Local RAG lookup gate before update." >&2
    exit 1
  fi
fi

(
  cd "$PAYLOAD_DIR"
  tar -cf - .
) | (
  cd "$TARGET_DIR"
  tar -xf -
)

if [ -x "$RUNTIME_PYTHON" ]; then
  if ! "$RUNTIME_PYTHON" "$TARGET_DIR/rag/query/setup.py" \
      --refresh-completion-marker --format json >/dev/null; then
    echo "setup_required: existing RAG runtime verification failed; run Local RAG setup before lookup." >&2
    exit 1
  fi
elif [ -n "$PRE_UPDATE_MARKER" ]; then
  echo "setup_required: the existing Local RAG runtime Python is missing after update." >&2
  exit 1
fi

if [ -n "$PRE_UPDATE_MARKER" ]; then
  rm -f "$PRE_UPDATE_MARKER"
fi

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
if (Test-Path -LiteralPath (Join-Path $QueryRoot ".packaged-runtime.json") -PathType Leaf) {
    $CompletionMarker = Join-Path $QueryRoot ".rag-deps-installed"
} else {
    $CompletionMarker = Join-Path $QueryRoot ".venv\.rag-deps-installed"
}
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

$PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
Get-ChildItem -LiteralPath $Payload -Force -Recurse | ForEach-Object {
    $Relative = $_.FullName.Substring($PayloadRoot.Length).TrimStart(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
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

Write-Host "Copied Copilot Local RAG files to: $Target"
Write-Host "Existing files not present in this package were preserved."
Write-Host "Run Local RAG setup before the first lookup on this computer."
"""


__all__ = ["INSTALL_PS1_TEXT", "INSTALL_SH_TEXT"]
