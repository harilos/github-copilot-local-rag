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
cp -R "$PAYLOAD_DIR"/. "$TARGET_DIR"/

echo "Installed Copilot Local RAG files to: $TARGET_DIR"
echo "Existing copilot-instructions.md was not overwritten by this repository."
