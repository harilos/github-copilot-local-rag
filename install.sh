#!/usr/bin/env sh
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
PACKAGED_MANIFEST="$QUERY_ROOT/.packaged-runtime.json"
ACTIVE_MARKER="$QUERY_ROOT/.rag-deps-installed"
LEGACY_MARKER="$QUERY_ROOT/.venv/.rag-deps-installed"
ACTIVE_BACKUP=""
LEGACY_BACKUP=""
ACTIVE_RESCUE=""
LEGACY_RESCUE=""
move_marker() {
  marker="$1"; label="$2"
  if [ ! -f "$marker" ]; then return; fi
  backup="$QUERY_ROOT/.rag-deps-installed.$label.pre-update.$$"; suffix=0
  while [ -e "$backup" ]; do suffix=$((suffix + 1)); backup="$QUERY_ROOT/.rag-deps-installed.$label.pre-update.$$.$suffix"; done
  if ! mv "$marker" "$backup"; then echo "setup_required: could not close the Local RAG lookup gate before update." >&2; exit 1; fi
  if [ "$label" = "active" ]; then ACTIVE_BACKUP="$backup"; else LEGACY_BACKUP="$backup"; fi
}
restore_markers() {
  status=$?; trap - EXIT
  if [ "$status" -ne 0 ]; then
    active_source="$ACTIVE_BACKUP"; [ -f "$active_source" ] || active_source="$ACTIVE_RESCUE"
    legacy_source="$LEGACY_BACKUP"; [ -f "$legacy_source" ] || legacy_source="$LEGACY_RESCUE"
    if [ -n "$active_source" ] && [ -f "$active_source" ]; then rm -f -- "$ACTIVE_MARKER"; mkdir -p -- "$(dirname -- "$ACTIVE_MARKER")"; mv "$active_source" "$ACTIVE_MARKER" || true; fi
    if [ -n "$legacy_source" ] && [ -f "$legacy_source" ]; then rm -f -- "$LEGACY_MARKER"; mkdir -p -- "$(dirname -- "$LEGACY_MARKER")"; mv "$legacy_source" "$LEGACY_MARKER" || true; fi
    rm -f -- "$ACTIVE_RESCUE" "$LEGACY_RESCUE" || true
  fi
  exit "$status"
}
trap restore_markers EXIT
if [ -f "$PACKAGED_MANIFEST" ]; then move_marker "$ACTIVE_MARKER" active; move_marker "$LEGACY_MARKER" legacy; else move_marker "$LEGACY_MARKER" legacy; fi

(
  cd "$PAYLOAD_DIR"
  tar \
    --exclude='./rag/config/network.json' \
    --exclude='./rag/config/manage-custom.json' \
    --exclude='./rag/config/sensitive-terms.local' \
    --exclude='./rag/config/windows-test-connection.local.json' \
    --exclude='./rag/config/source-connections.json' \
    --exclude='./rag/config/source-connections.secrets.json' \
    --exclude='./rag/config/.source-connections.key' \
    --exclude='./rag/config/.source-connections.*' \
    --exclude='./rag/query/run' \
    --exclude='./rag/query/run/*' \
    --exclude='*/.venv' \
    --exclude='*/.venv/*' \
    --exclude='*/__pycache__' \
    --exclude='*/__pycache__/*' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='./.DS_Store' \
    --exclude='*/.DS_Store' \
    -cf - .
) | (
  cd "$TARGET_DIR"
  tar -xf -
)

# Remove only retired files that older overlay installs may have left behind.
# Keep this explicit allowlist narrow; the installer must not prune unrelated
# user files or database contents.
rm -f -- \
  "$TARGET_DIR/rag/export_migration.sh" \
  "$TARGET_DIR/rag/migration_archive.py" \
  "$TARGET_DIR/rag/gen_db/migrate_source_metadata.py" \
  "$TARGET_DIR/rag/gen_db/software_rag_tool/software_rag_tool/source_metadata_migration.py" \
  "$TARGET_DIR/skills/local-rag-admin/SKILL.md"
rmdir -- "$TARGET_DIR/skills/local-rag-admin" 2>/dev/null || true

if [ -x "$RUNTIME_PYTHON" ]; then
  if ! "$RUNTIME_PYTHON" "$TARGET_DIR/rag/query/setup.py" \
      --refresh-completion-marker --format json >/dev/null; then
    echo "setup_required: existing RAG runtime verification failed; run Local RAG setup before lookup." >&2
    exit 1
  fi
elif [ -n "$ACTIVE_BACKUP" ] || [ -n "$LEGACY_BACKUP" ]; then
  echo "setup_required: the existing Local RAG runtime Python is missing after update." >&2
  exit 1
fi
if [ -n "$ACTIVE_BACKUP" ]; then ACTIVE_RESCUE="$ACTIVE_BACKUP.cleanup"; cp -p -- "$ACTIVE_BACKUP" "$ACTIVE_RESCUE"; fi
if [ -n "$LEGACY_BACKUP" ]; then LEGACY_RESCUE="$LEGACY_BACKUP.cleanup"; cp -p -- "$LEGACY_BACKUP" "$LEGACY_RESCUE"; fi
rm -f -- "$ACTIVE_BACKUP" "$LEGACY_BACKUP"
ACTIVE_BACKUP=""; LEGACY_BACKUP=""
trap - EXIT
rm -f -- "$ACTIVE_RESCUE" "$LEGACY_RESCUE" || true
ACTIVE_RESCUE=""; LEGACY_RESCUE=""

echo "Installed Copilot Local RAG files to: $TARGET_DIR"
echo "Existing copilot-instructions.md was not overwritten by this repository."
echo "Existing machine-local network and Source connection settings were preserved."
