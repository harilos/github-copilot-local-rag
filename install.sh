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
(
  cd "$PAYLOAD_DIR"
  tar --exclude='./rag/config/network.json' -cf - .
) | (
  cd "$TARGET_DIR"
  tar -xf -
)

LEGACY_PYTHON="$TARGET_DIR/rag/query/.venv/bin/python"
LEGACY_MARKER="$TARGET_DIR/rag/query/.venv/.rag-deps-installed"
if [ -x "$LEGACY_PYTHON" ] && [ -f "$LEGACY_MARKER" ] &&
   [ "$(tr -d '\r\n ' < "$LEGACY_MARKER")" = "ok" ]; then
  if ! "$LEGACY_PYTHON" "$TARGET_DIR/rag/query/setup.py" \
      --migrate-legacy-marker --format json >/dev/null; then
    echo "Warning: existing RAG runtime needs setup verification before lookup." >&2
  fi
fi

echo "Installed Copilot Local RAG files to: $TARGET_DIR"
echo "Existing copilot-instructions.md was not overwritten by this repository."
echo "Existing rag/config/network.json was preserved."
