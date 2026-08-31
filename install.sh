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
    --exclude='./rag/query/agent003_answer_packet.py' \
    --exclude='./rag/query/mcp_server.py' \
    --exclude='./rag/copilot-cli' \
    --exclude='./rag/copilot-cli/*' \
    --exclude='./instructions/rag.instructions.md' \
    --exclude='./skills/local-rag-setup' \
    --exclude='./skills/local-rag-setup/*' \
    --exclude='./rag/query/.rag-deps-installed' \
    --exclude='./rag/query/.rag-deps-installed.*' \
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
  "$TARGET_DIR/rag/query/.packaged-runtime.json" \
  "$TARGET_DIR/rag/query/.rag-deps-installed" \
  "$TARGET_DIR/rag/query/portable_runtime.py" \
  "$TARGET_DIR/rag/query/portable_db_install.py" \
  "$TARGET_DIR/rag/query/portable_db_smoke.py" \
  "$TARGET_DIR/rag/query/agent003_answer_packet.py" \
  "$TARGET_DIR/rag/query/mcp_server.py" \
  "$TARGET_DIR/rag/copilot-cli/local-rag-agent003-savings.agent.md" \
  "$TARGET_DIR/rag/copilot-cli/local-rag-agent003-standard.agent.md" \
  "$TARGET_DIR/rag/copilot-cli/local-rag-agent003-thorough.agent.md" \
  "$TARGET_DIR/rag/copilot-cli/local-rag-agent003.ps1" \
  "$TARGET_DIR/instructions/rag.instructions.md" \
  "$TARGET_DIR/skills/local-rag-setup/SKILL.md" \
  "$TARGET_DIR/skills/local-rag-admin/SKILL.md"
for retired_directory in \
  "$TARGET_DIR/rag/copilot-cli" \
  "$TARGET_DIR/instructions" \
  "$TARGET_DIR/skills/local-rag-setup" \
  "$TARGET_DIR/skills/local-rag-admin"
do
  rmdir -- "$retired_directory" 2>/dev/null || true
done

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

echo "Installed Copilot Local RAG files to: $TARGET_DIR"
echo "Use /local-rag in GitHub Copilot Chat to search Local RAG."
echo "Existing copilot-instructions.md was not overwritten by this repository."
echo "Existing machine-local network and Source connection settings were preserved."
