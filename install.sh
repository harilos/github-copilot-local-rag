#!/usr/bin/env sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PAYLOAD_DIR="$SOURCE_DIR/.copilot"
TARGET_DIR="${COPILOT_HOME:-$HOME/.copilot}"

if [ ! -d "$PAYLOAD_DIR" ]; then
  echo "Missing install payload: $PAYLOAD_DIR" >&2
  exit 1
fi

if [ -L "$TARGET_DIR" ]; then
  echo "Refusing symlinked Local RAG install target: $TARGET_DIR" >&2
  exit 1
fi
mkdir -p "$TARGET_DIR"

RETIRED_FILES='rag/export_migration.sh
rag/migration_archive.py
rag/gen_db/migrate_source_metadata.py
rag/gen_db/software_rag_tool/software_rag_tool/source_metadata_migration.py
rag/query/.packaged-runtime.json
rag/query/.rag-deps-installed
rag/query/portable_runtime.py
rag/query/portable_db_install.py
rag/query/portable_db_smoke.py
rag/query/agent003_answer_packet.py
rag/query/mcp_server.py
rag/copilot-cli/local-rag-agent003-savings.agent.md
rag/copilot-cli/local-rag-agent003-standard.agent.md
rag/copilot-cli/local-rag-agent003-thorough.agent.md
rag/copilot-cli/local-rag-agent003.ps1
instructions/rag.instructions.md
skills/local-rag-setup/SKILL.md
skills/local-rag-admin/SKILL.md'
RETIRED_DIRECTORIES='rag/copilot-cli
instructions
skills/local-rag-setup
skills/local-rag-admin'

assert_target_path_parents_not_symlinks() {
  relative_path="$1"
  case "$relative_path" in
    /*) echo "Refusing unsafe retired path outside the Local RAG target: $relative_path" >&2; return 1 ;;
  esac
  case "/$relative_path/" in
    *"//"*|*"/./"*|*"/../"*) echo "Refusing unsafe retired path: $relative_path" >&2; return 1 ;;
  esac

  parent_path=${relative_path%/*}
  if [ "$parent_path" = "$relative_path" ]; then parent_path=""; fi
  current_path="$TARGET_DIR"
  while [ -n "$parent_path" ]; do
    case "$parent_path" in
      */*) component=${parent_path%%/*}; parent_path=${parent_path#*/} ;;
      *) component=$parent_path; parent_path="" ;;
    esac
    current_path="$current_path/$component"
    if [ -L "$current_path" ]; then
      echo "Refusing retired-file cleanup through symlink parent: $current_path" >&2
      return 1
    fi
  done
}

preflight_retired_cleanup() {
  for relative_path in $RETIRED_FILES $RETIRED_DIRECTORIES; do
    assert_target_path_parents_not_symlinks "$relative_path"
  done
}

remove_retired_files() {
  for relative_path in $RETIRED_FILES; do
    # Recheck immediately before each removal in case the copied payload
    # introduced a path that did not exist during the initial preflight.
    assert_target_path_parents_not_symlinks "$relative_path"
    rm -f -- "$TARGET_DIR/$relative_path"
  done
}

remove_retired_directories() {
  for relative_path in $RETIRED_DIRECTORIES; do
    assert_target_path_parents_not_symlinks "$relative_path"
    rmdir -- "$TARGET_DIR/$relative_path" 2>/dev/null || true
  done
}

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
    if assert_target_path_parents_not_symlinks "rag/query/.venv/.rag-deps-installed"; then
      rm -f -- "$LEGACY_MARKER" || true
    fi
  fi
  exit "$status"
}
preflight_retired_cleanup
assert_target_path_parents_not_symlinks "rag/query/.venv/.rag-deps-installed"
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
remove_retired_files
remove_retired_directories

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
