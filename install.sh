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

RUNTIME_PYTHON="$TARGET_DIR/rag/query/.venv/bin/python"
COMPLETION_MARKER="$TARGET_DIR/rag/query/.venv/.rag-deps-installed"
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
  tar \
    --exclude='./rag/config/network.json' \
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

echo "Installed Copilot Local RAG files to: $TARGET_DIR"
echo "Existing copilot-instructions.md was not overwritten by this repository."
echo "Existing rag/config/network.json was preserved."
