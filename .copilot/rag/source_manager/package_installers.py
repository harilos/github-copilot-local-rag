from __future__ import annotations


INSTALL_SH_TEXT = r"""#!/usr/bin/env sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PAYLOAD_DIR="$SOURCE_DIR/.copilot"
TARGET_DIR="${COPILOT_HOME:-$HOME/.copilot}"

if [ ! -d "$PAYLOAD_DIR" ]; then
  echo "Missing install payload: $PAYLOAD_DIR" >&2
  exit 1
fi

fail_install() { echo "$1" >&2; exit 1; }
assert_plain_ancestors() {
  checked_path="$1"
  while :; do
    [ ! -L "$checked_path" ] || fail_install "install_path_symlink_forbidden: $checked_path"
    if [ -e "$checked_path" ] && [ ! -d "$checked_path" ]; then
      fail_install "install_directory_required: $checked_path"
    fi
    parent_path=$(dirname -- "$checked_path")
    [ "$parent_path" != "$checked_path" ] || break
    checked_path="$parent_path"
  done
}
assert_plain_tree() {
  if ! invalid_entries=$(find "$1" ! -type d ! -type f -print); then
    fail_install "install_tree_unreadable: $1"
  fi
  [ -z "$invalid_entries" ] || fail_install "install_link_or_special_file_forbidden: $1"
}
# Check corresponding product paths before copying; DB contents are replaced separately.
assert_product_destinations() (
  for source_entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    [ -e "$source_entry" ] || continue
    destination_entry="$2/${source_entry##*/}"
    [ ! -L "$destination_entry" ] || fail_install "install_path_symlink_forbidden: $destination_entry"
    if [ -d "$source_entry" ]; then
      if [ -e "$destination_entry" ] && [ ! -d "$destination_entry" ]; then
        fail_install "install_directory_required: $destination_entry"
      fi
      if [ "$source_entry" != "$PAYLOAD_DIR/rag/dbs" ]; then
        assert_product_destinations "$source_entry" "$destination_entry" || exit 1
      fi
    elif [ -d "$destination_entry" ]; then
      fail_install "install_file_required: $destination_entry"
    fi
  done
)

assert_plain_ancestors "$PAYLOAD_DIR"
assert_plain_tree "$PAYLOAD_DIR"
case "$TARGET_DIR" in /*) ;; *) TARGET_DIR="$PWD/$TARGET_DIR" ;; esac
assert_plain_ancestors "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
TARGET_DIR=$(CDPATH= cd -- "$TARGET_DIR" && pwd -P)
case "$TARGET_DIR/" in "$SOURCE_DIR/"*) fail_install "install_source_target_overlap" ;; esac
case "$SOURCE_DIR/" in "$TARGET_DIR/"*) fail_install "install_source_target_overlap" ;; esac
assert_product_destinations "$PAYLOAD_DIR" "$TARGET_DIR"

DATABASE_PAYLOAD="$PAYLOAD_DIR/rag/dbs"
DATABASE_TARGET="$TARGET_DIR/rag/dbs"
seen_database_names="|"
if [ -d "$DATABASE_PAYLOAD" ]; then
  assert_plain_ancestors "$DATABASE_TARGET"
  for source_database in "$DATABASE_PAYLOAD"/* "$DATABASE_PAYLOAD"/.[!.]* "$DATABASE_PAYLOAD"/..?*; do
    [ -e "$source_database" ] || continue
    database_name="${source_database##*/}"
    case "$database_name" in [A-Za-z0-9]*-rag) ;; *) fail_install "invalid_database_name: $database_name" ;; esac
    case "$database_name" in *[!A-Za-z0-9_.-]*) fail_install "invalid_database_name: $database_name" ;; esac
    [ -d "$source_database" ] || fail_install "invalid_database_directory: $database_name"
    folded_name=$(printf '%s' "$database_name" | tr '[:upper:]' '[:lower:]')
    case "$seen_database_names" in *"|$folded_name|"*) fail_install "duplicate_database_name: $database_name" ;; esac
    seen_database_names="$seen_database_names$folded_name|"
    destination_database="$DATABASE_TARGET/$database_name"
    assert_plain_ancestors "$destination_database"
    if [ -d "$destination_database" ]; then assert_plain_tree "$destination_database"; fi
  done
fi

QUERY_ROOT="$TARGET_DIR/rag/query"
RUNTIME_PYTHON="$QUERY_ROOT/.venv/bin/python"
LEGACY_MARKER="$QUERY_ROOT/.venv/.rag-deps-installed"
LEGACY_BACKUP=""
LEGACY_RESCUE=""
PRODUCT_COPY_STATUS=""
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
  if [ -n "$PRODUCT_COPY_STATUS" ]; then rm -f -- "$PRODUCT_COPY_STATUS" || true; fi
  if [ "$status" -ne 0 ]; then
    if [ -n "$LEGACY_RESCUE" ] && [ -f "$LEGACY_RESCUE" ] && [ ! -f "$LEGACY_BACKUP" ]; then mv "$LEGACY_RESCUE" "$LEGACY_BACKUP" || true; fi
    rm -f -- "$LEGACY_MARKER" || true
  fi
  exit "$status"
}
trap close_markers EXIT
move_marker "$LEGACY_MARKER" legacy

# A POSIX pipeline reports only its last command; retain the producer result too.
PRODUCT_COPY_STATUS=$(mktemp "$TARGET_DIR/.rag-product-copy.XXXXXX")
(
  cd "$PAYLOAD_DIR"
  if tar \
    --exclude='./rag/dbs' \
    --exclude='./rag/query/.rag-deps-installed' \
    --exclude='./rag/query/.rag-deps-installed.*' \
    -cf - .; then
    printf '%s' complete > "$PRODUCT_COPY_STATUS"
  else
    exit 1
  fi
) | (
  cd "$TARGET_DIR"
  tar -xf -
)
[ "$(cat "$PRODUCT_COPY_STATUS")" = complete ] || fail_install "product_copy_failed: database replacement was not started"
rm -f -- "$PRODUCT_COPY_STATUS"
PRODUCT_COPY_STATUS=""

# Keep only the supplied version of each packaged DB, without a second DB copy.
if [ -d "$DATABASE_PAYLOAD" ]; then
  mkdir -p "$DATABASE_TARGET"
  for source_database in "$DATABASE_PAYLOAD"/* "$DATABASE_PAYLOAD"/.[!.]* "$DATABASE_PAYLOAD"/..?*; do
    [ -e "$source_database" ] || continue
    destination_database="$DATABASE_TARGET/${source_database##*/}"
    if ! rm -rf -- "$destination_database"; then
      fail_install "database_replace_failed: reinstall required: $destination_database"
    fi
    if ! cp -pR -- "$source_database" "$destination_database"; then
      rm -rf -- "$destination_database" || echo "database_partial_cleanup_failed: $destination_database" >&2
      fail_install "database_copy_failed: previous DB removed; reinstall required: $destination_database"
    fi
  done
fi

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
echo "Packaged databases replaced existing same-name databases without backups; other databases were preserved."
echo "Other existing files not present in this package were preserved."
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

function Get-InstallItem {
    param([string]$Path)
    try { return Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch [System.Management.Automation.ItemNotFoundException] { return $null }
}
function Assert-PlainAncestors {
    param([string]$Path)
    if ($Path -match '(^|[\\/])\.\.([\\/]|$)') { throw "install_parent_traversal_forbidden: $Path" }
    $Current = [System.IO.Path]::GetFullPath($Path)
    while ($Current) {
        $Item = Get-InstallItem $Current
        if ($null -ne $Item) {
            if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "install_path_reparse_forbidden: $Current"
            }
            if (-not $Item.PSIsContainer) { throw "install_directory_required: $Current" }
        }
        $Current = Split-Path -Parent $Current
    }
}
function Assert-PlainTree {
    param([string]$Path)
    $RootItem = Get-Item -LiteralPath $Path -Force
    if (($RootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "install_path_reparse_forbidden: $Path"
    }
    foreach ($Item in Get-ChildItem -LiteralPath $Path -Force) {
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "install_path_reparse_forbidden: $($Item.FullName)"
        }
        if ($Item.PSIsContainer) { Assert-PlainTree $Item.FullName }
    }
}

Assert-PlainAncestors $Payload
Assert-PlainTree $Payload
$PayloadRoot = [System.IO.Path]::GetFullPath($Payload)
$SourceRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
Assert-PlainAncestors $Target
$Target = [System.IO.Path]::GetFullPath($Target)
$TargetPrefix = $Target.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if ($SourceRoot.StartsWith($TargetPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
    $TargetPrefix.StartsWith($SourceRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "install_source_target_overlap"
}
$DatabasePayload = Join-Path $Payload "rag\dbs"
$DatabaseTarget = Join-Path $Target "rag\dbs"
$DatabaseSources = @()
$SeenDatabaseNames = @{}
if (Test-Path -LiteralPath $DatabasePayload -PathType Container) {
    Assert-PlainAncestors $DatabaseTarget
    $DatabaseSources = @(Get-ChildItem -LiteralPath $DatabasePayload -Force | Sort-Object Name)
    foreach ($Database in $DatabaseSources) {
        if (-not $Database.PSIsContainer -or $Database.Name -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$') {
            throw "invalid_database_name: $($Database.Name)"
        }
        if ($SeenDatabaseNames.ContainsKey($Database.Name)) { throw "duplicate_database_name: $($Database.Name)" }
        $SeenDatabaseNames[$Database.Name] = $true
        $DatabaseDestination = Join-Path $DatabaseTarget $Database.Name
        Assert-PlainAncestors $DatabaseDestination
        if (Test-Path -LiteralPath $DatabaseDestination -PathType Container) {
            Assert-PlainTree $DatabaseDestination
        }
    }
}
$ProductItems = @(Get-ChildItem -LiteralPath $Payload -Force -Recurse | Where-Object {
    $_.FullName -ine $DatabasePayload -and
    -not $_.FullName.StartsWith($DatabasePayload + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
})
foreach ($Item in $ProductItems) {
    $Relative = $Item.FullName.Substring($PayloadRoot.Length).TrimStart('\', '/')
    $Destination = Join-Path $Target $Relative
    Assert-PlainAncestors (Split-Path -Parent $Destination)
    $Existing = Get-InstallItem $Destination
    if ($null -ne $Existing) {
        if (($Existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "install_path_reparse_forbidden: $Destination"
        }
        if ($Existing.PSIsContainer -ne $Item.PSIsContainer) { throw "install_path_type_mismatch: $Destination" }
    }
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
$ProductItems | ForEach-Object {
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

# Replace only included DBs directly; a failed copy cannot restore the previous DB.
foreach ($Database in $DatabaseSources) {
    $DatabaseDestination = Join-Path $DatabaseTarget $Database.Name
    New-Item -ItemType Directory -Force -Path $DatabaseTarget | Out-Null
    if (Test-Path -LiteralPath $DatabaseDestination) {
        try {
            Remove-Item -LiteralPath $DatabaseDestination -Recurse -Force
        } catch {
            throw "database_replace_failed: reinstall required: $DatabaseDestination; $_"
        }
    }
    try {
        Copy-Item -LiteralPath $Database.FullName -Destination $DatabaseDestination -Recurse -Force
    } catch {
        $CopyFailure = $_
        if (Test-Path -LiteralPath $DatabaseDestination) {
            try { Remove-Item -LiteralPath $DatabaseDestination -Recurse -Force }
            catch { Write-Warning "database_partial_cleanup_failed: $DatabaseDestination; $_" }
        }
        throw "database_copy_failed: previous DB removed; reinstall required: $DatabaseDestination; $CopyFailure"
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
Write-Host "Packaged databases replaced existing same-name databases without backups; other databases were preserved."
Write-Host "Other existing files not present in this package were preserved."
Write-Host "Run Local RAG setup before the first lookup on this computer."
"""


__all__ = ["INSTALL_PS1_TEXT", "INSTALL_SH_TEXT"]
