#!/usr/bin/env sh
set -eu

EXPORT_SCHEMA="local-rag-migration-v1"
BUNDLE_ROOT="local-rag-migration-v1"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
DEFAULT_COPILOT_HOME=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
COPILOT_HOME_PATH=${COPILOT_HOME:-$DEFAULT_COPILOT_HOME}
OUTPUT_PATH=""
INCLUDE_NETWORK_CONFIG=true
NETWORK_OPTION_SET=false
VERIFY_ARCHIVE=""
TEMP_ROOT=""
PARTIAL_ARCHIVE=""

usage() {
  cat <<'EOF'
Usage:
  export_migration.sh [--output ARCHIVE] [--copilot-home DIRECTORY]
                      [--include-network-config|--exclude-network-config]
  export_migration.sh --verify ARCHIVE

Create a Local RAG migration archive. Inside .copilot/rag, files are included
by default and only transient, platform-specific, or private files are
blacklisted. Outside .copilot/rag, only the Local RAG instruction and its two
skills are included.

Options:
  --output ARCHIVE              Destination .tar.gz path.
  --copilot-home DIRECTORY      Source .copilot directory.
  --include-network-config      Include network.json when present (default).
  --exclude-network-config      Exclude machine-local network.json.
  --verify ARCHIVE              Verify an existing migration archive.
  -h, --help                    Show this help.

The archive contains local document indexes and may contain company data.
It is created with mode 0600 and is never uploaded automatically.
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [ -n "$PARTIAL_ARCHIVE" ] && [ -f "$PARTIAL_ARCHIVE" ]; then
    rm -f -- "$PARTIAL_ARCHIVE"
  fi
  if [ -n "$TEMP_ROOT" ] && [ -d "$TEMP_ROOT" ]; then
    case "$TEMP_ROOT" in
      */local-rag-export.*) rm -rf -- "$TEMP_ROOT" ;;
      *) printf 'warning: refusing to remove unexpected temporary path\n' >&2 ;;
    esac
  fi
}

trap cleanup EXIT HUP INT TERM
umask 077

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)
      [ "$#" -ge 2 ] || fail "--output requires a path"
      OUTPUT_PATH=$2
      shift 2
      ;;
    --copilot-home)
      [ "$#" -ge 2 ] || fail "--copilot-home requires a directory"
      COPILOT_HOME_PATH=$2
      shift 2
      ;;
    --include-network-config)
      INCLUDE_NETWORK_CONFIG=true
      NETWORK_OPTION_SET=true
      shift
      ;;
    --exclude-network-config)
      INCLUDE_NETWORK_CONFIG=false
      NETWORK_OPTION_SET=true
      shift
      ;;
    --verify)
      [ "$#" -ge 2 ] || fail "--verify requires an archive"
      VERIFY_ARCHIVE=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

command -v tar >/dev/null 2>&1 || fail "tar is required"
command -v find >/dev/null 2>&1 || fail "find is required"
command -v gzip >/dev/null 2>&1 || fail "gzip is required"
command -v cmp >/dev/null 2>&1 || fail "cmp is required"

if command -v sha256sum >/dev/null 2>&1; then
  SHA256_KIND="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  SHA256_KIND="shasum"
else
  fail "sha256sum or shasum is required"
fi

MIGRATION_HELPER="$SCRIPT_DIR/migration_archive.py"
[ -f "$MIGRATION_HELPER" ] ||
  fail "migration archive helper is missing: $MIGRATION_HELPER"
if [ -n "${RAG_MIGRATION_PYTHON:-}" ]; then
  MIGRATION_PYTHON=$RAG_MIGRATION_PYTHON
elif [ -x "$SCRIPT_DIR/query/.venv/bin/python" ]; then
  MIGRATION_PYTHON="$SCRIPT_DIR/query/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  MIGRATION_PYTHON=$(command -v python3)
else
  fail "Python 3 is required for safe archive verification"
fi
"$MIGRATION_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' ||
  fail "Python 3.9 or newer is required for safe archive verification"

sha256_file() {
  if [ "$SHA256_KIND" = "sha256sum" ]; then
    sha256sum "$1"
  else
    shasum -a 256 "$1"
  fi
}

absolute_existing_file() {
  path=$1
  [ -f "$path" ] || fail "archive not found: $path"
  directory=$(CDPATH= cd -- "$(dirname -- "$path")" && pwd -P)
  printf '%s/%s\n' "$directory" "$(basename -- "$path")"
}

absolute_output_file() {
  path=$1
  directory=$(dirname -- "$path")
  [ -d "$directory" ] || fail "output directory does not exist: $directory"
  directory=$(CDPATH= cd -- "$directory" && pwd -P)
  printf '%s/%s\n' "$directory" "$(basename -- "$path")"
}

verify_archive() {
  archive=$(absolute_existing_file "$1")
  TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/local-rag-export.XXXXXX")
  verify_root="$TEMP_ROOT/verify"
  mkdir -p "$verify_root"
  gzip -t "$archive" || fail "gzip integrity check failed"
  "$MIGRATION_PYTHON" "$MIGRATION_HELPER" verify \
    --archive "$archive" \
    --destination "$verify_root" \
    --bundle-root "$BUNDLE_ROOT" ||
    fail "safe archive verification failed"
  printf 'verified_archive=%s\n' "$archive"
}

if [ -n "$VERIFY_ARCHIVE" ]; then
  [ -z "$OUTPUT_PATH" ] || fail "--verify cannot be combined with --output"
  [ "$NETWORK_OPTION_SET" = false ] ||
    fail "--verify cannot be combined with network inclusion options"
  verify_archive "$VERIFY_ARCHIVE"
  exit 0
fi

[ -d "$COPILOT_HOME_PATH" ] ||
  fail "Copilot home does not exist: $COPILOT_HOME_PATH"
COPILOT_HOME_ABS=$(CDPATH= cd -- "$COPILOT_HOME_PATH" && pwd -P)
RAG_SOURCE="$COPILOT_HOME_ABS/rag"
[ -d "$RAG_SOURCE" ] || fail "Local RAG directory is missing: $RAG_SOURCE"
if [ "$INCLUDE_NETWORK_CONFIG" = true ] &&
   [ -e "$RAG_SOURCE/config/network.json" ]; then
  [ -f "$RAG_SOURCE/config/network.json" ] ||
    fail "network.json must be a regular file"
  [ ! -L "$RAG_SOURCE/config/network.json" ] ||
    fail "network.json must not be a symlink"
  "$MIGRATION_PYTHON" "$MIGRATION_HELPER" validate-network-config \
    --path "$RAG_SOURCE/config/network.json" ||
    fail "network.json contains invalid or persisted proxy credentials"
fi

for required in \
  "instructions/rag.instructions.md" \
  "skills/local-rag/SKILL.md" \
  "skills/local-rag-admin/SKILL.md"
do
  [ -f "$COPILOT_HOME_ABS/$required" ] ||
    fail "required Local RAG integration file is missing: $required"
  [ ! -L "$COPILOT_HOME_ABS/$required" ] ||
    fail "whitelisted integration files must not be symlinks: $required"
done

if [ -z "$OUTPUT_PATH" ]; then
  timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
  OUTPUT_PATH="$HOME/local-rag-migration-$timestamp.tar.gz"
fi
OUTPUT_ABS=$(absolute_output_file "$OUTPUT_PATH")

case "$OUTPUT_ABS" in
  "$COPILOT_HOME_ABS"|"$COPILOT_HOME_ABS"/*)
    fail "output must be outside the source .copilot directory"
    ;;
esac
[ ! -e "$OUTPUT_ABS" ] || fail "output already exists: $OUTPUT_ABS"

DAEMON_STATE="$RAG_SOURCE/query/run/ragd.json"
if [ -f "$DAEMON_STATE" ]; then
  daemon_pid=$(
    sed -n \
      's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
      "$DAEMON_STATE" | sed -n '1p'
  )
  if [ -n "$daemon_pid" ] && kill -0 "$daemon_pid" 2>/dev/null; then
    fail "Local RAG daemon is running; stop it before exporting"
  fi
  printf 'warning: ignoring a stale daemon state file\n' >&2
fi

busy_sqlite=""
if [ -d "$RAG_SOURCE/dbs" ]; then
  find "$RAG_SOURCE/dbs" -type f -path '*/logs/progress.json' 2>/dev/null |
  while IFS= read -r progress_file; do
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"running"' "$progress_file"; then
      printf 'error: database maintenance is running: %s\n' \
        "$progress_file" >&2
      exit 1
    fi
  done

  busy_sqlite=$(
    find "$RAG_SOURCE/dbs" -type f \
      \( -name '*-wal' -o -name '*-journal' \) \
      -size +0 -print 2>/dev/null | sed -n '1p'
  )
fi
[ -z "$busy_sqlite" ] ||
  fail "uncheckpointed SQLite state exists; stop maintenance and checkpoint: $busy_sqlite"

included_symlink=$(
  find "$RAG_SOURCE" \
    \( -path '*/.venv' -o -path '*/query/run' -o -path '*/__pycache__' \) \
      -prune -o \
    -type l -print 2>/dev/null | sed -n '1p'
)
[ -z "$included_symlink" ] ||
  fail "symlinks are not allowed in migration payloads: $included_symlink"

TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/local-rag-export.XXXXXX")
STAGE_ROOT="$TEMP_ROOT/$BUNDLE_ROOT"
STAGE_COPILOT="$STAGE_ROOT/.copilot"
mkdir -p "$STAGE_COPILOT"

DB_FINGERPRINT_BEFORE="$TEMP_ROOT/db-before.json"
DB_FINGERPRINT_AFTER="$TEMP_ROOT/db-after.json"
MODEL_FINGERPRINT_BEFORE="$TEMP_ROOT/model-before.json"
MODEL_FINGERPRINT_AFTER="$TEMP_ROOT/model-after.json"
"$MIGRATION_PYTHON" "$MIGRATION_HELPER" fingerprint-tree \
  --root "$RAG_SOURCE/dbs" \
  --output "$DB_FINGERPRINT_BEFORE" ||
  fail "could not fingerprint databases before export"
"$MIGRATION_PYTHON" "$MIGRATION_HELPER" fingerprint-tree \
  --root "$RAG_SOURCE/models" \
  --output "$MODEL_FINGERPRINT_BEFORE" ||
  fail "could not fingerprint models before export"

set -- \
  --exclude='./rag/query/.venv' \
  --exclude='./rag/query/.venv/*' \
  --exclude='./rag/query/run' \
  --exclude='./rag/query/run/*' \
  --exclude='*/.venv' \
  --exclude='*/.venv/*' \
  --exclude='*/__pycache__' \
  --exclude='*/__pycache__/*' \
  --exclude='*/.pytest_cache' \
  --exclude='*/.pytest_cache/*' \
  --exclude='*/.mypy_cache' \
  --exclude='*/.mypy_cache/*' \
  --exclude='*/.ruff_cache' \
  --exclude='*/.ruff_cache/*' \
  --exclude='*/.git' \
  --exclude='*/.git/*' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='*.swp' \
  --exclude='*~' \
  --exclude='.DS_Store' \
  --exclude='*/.DS_Store' \
  --exclude='._*' \
  --exclude='*/._*' \
  --exclude='Thumbs.db' \
  --exclude='*/Thumbs.db' \
  --exclude='*/.env' \
  --exclude='*/.env.*' \
  --exclude='*/credentials.json' \
  --exclude='*/credentials.*' \
  --exclude='*/credentials' \
  --exclude='*/secret.json' \
  --exclude='*/secret.*' \
  --exclude='*/secret' \
  --exclude='*/secrets.json' \
  --exclude='*/secrets.*' \
  --exclude='*/secrets' \
  --exclude='*/.netrc' \
  --exclude='*/.pypirc' \
  --exclude='*/.npmrc' \
  --exclude='*/.git-credentials' \
  --exclude='*/id_rsa' \
  --exclude='*/id_ed25519' \
  --exclude='*/id_ecdsa' \
  --exclude='*.key' \
  --exclude='*.pem' \
  --exclude='*.p12' \
  --exclude='*.pfx' \
  --exclude='*-shm' \
  --exclude='*-wal' \
  --exclude='*-journal'

if [ "$INCLUDE_NETWORK_CONFIG" = false ]; then
  set -- "$@" --exclude='./rag/config/network.json'
fi

SOURCE_TAR="$TEMP_ROOT/source-payload.tar"
(
  COPYFILE_DISABLE=1
  COPY_EXTENDED_ATTRIBUTES_DISABLE=1
  export COPYFILE_DISABLE COPY_EXTENDED_ATTRIBUTES_DISABLE
  cd "$COPILOT_HOME_ABS"
  tar "$@" -cf "$SOURCE_TAR" ./rag
) || fail "could not create a complete source snapshot"
(
  COPYFILE_DISABLE=1
  COPY_EXTENDED_ATTRIBUTES_DISABLE=1
  export COPYFILE_DISABLE COPY_EXTENDED_ATTRIBUTES_DISABLE
  cd "$STAGE_COPILOT"
  tar -xf "$SOURCE_TAR"
) || fail "could not stage the source snapshot"
rm -f -- "$SOURCE_TAR"

"$MIGRATION_PYTHON" "$MIGRATION_HELPER" fingerprint-tree \
  --root "$RAG_SOURCE/dbs" \
  --output "$DB_FINGERPRINT_AFTER" ||
  fail "could not fingerprint databases after export"
"$MIGRATION_PYTHON" "$MIGRATION_HELPER" fingerprint-tree \
  --root "$RAG_SOURCE/models" \
  --output "$MODEL_FINGERPRINT_AFTER" ||
  fail "could not fingerprint models after export"
cmp -s "$DB_FINGERPRINT_BEFORE" "$DB_FINGERPRINT_AFTER" ||
  fail "database files changed during export; no archive was published"
cmp -s "$MODEL_FINGERPRINT_BEFORE" "$MODEL_FINGERPRINT_AFTER" ||
  fail "model files changed during export; no archive was published"

for whitelisted in \
  "instructions/rag.instructions.md" \
  "skills/local-rag/SKILL.md" \
  "skills/local-rag-admin/SKILL.md"
do
  destination="$STAGE_COPILOT/$whitelisted"
  mkdir -p "$(dirname -- "$destination")"
  cp -p "$COPILOT_HOME_ABS/$whitelisted" "$destination"
done

if [ -f "$STAGE_COPILOT/rag/config/network.json" ]; then
  chmod 600 "$STAGE_COPILOT/rag/config/network.json"
fi

rag_version="unknown"
if [ -f "$RAG_SOURCE/VERSION" ]; then
  rag_version=$(sed -n '1p' "$RAG_SOURCE/VERSION" | tr -d '\r\n')
fi
created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
platform=$(uname -s 2>/dev/null || printf 'unknown')
architecture=$(uname -m 2>/dev/null || printf 'unknown')
network_included=false
if [ -f "$STAGE_COPILOT/rag/config/network.json" ]; then
  network_included=true
fi
file_count=$(find "$STAGE_COPILOT" -type f | wc -l | tr -d ' ')
payload_kib=$(du -sk "$STAGE_COPILOT" | awk '{print $1}')

cat >"$STAGE_ROOT/MANIFEST.txt" <<EOF
schema=$EXPORT_SCHEMA
created_at=$created_at
rag_version=$rag_version
source_platform=$platform
source_architecture=$architecture
network_config_included=$network_included
network_config_policy=include_if_present_without_credentials
file_count=$file_count
payload_kib=$payload_kib
rag_policy=blacklist
outside_rag_policy=exact_whitelist
outside_rag_whitelist=instructions/rag.instructions.md,skills/local-rag/SKILL.md,skills/local-rag-admin/SKILL.md
excluded_runtime=query/.venv,query/run
excluded_private=.env,.env.*,credential*,secret*,.netrc,.pypirc,.npmrc,.git-credentials,id_*,*.key,*.pem,*.p12,*.pfx
excluded_transient=__pycache__,*.pyc,*.pyo,*-shm,*-wal,*-journal
EOF

cat >"$STAGE_ROOT/RESTORE.md" <<'EOF'
# Local RAG migration restore

This archive contains local document indexes and may contain company data.

1. Record the source-side SHA-256 shown by the exporter and compare it after
   transfer through a separate trusted channel.
2. Before manual extraction, run `export_migration.sh --verify ARCHIVE`.
   Internal checksums detect corruption but do not prove the archive's origin.
3. Extract the archive into a private temporary directory and inspect
   `MANIFEST.txt`.
4. Copy the contents of `.copilot` into the destination user's `.copilot`
   directory without deleting unrelated Copilot files.
5. The Python virtual environment is intentionally absent. Run Local RAG
   setup on the destination:

   - macOS/Linux:
     `python3 ~/.copilot/rag/query/setup.py --format json`
   - Windows PowerShell:
     `py -3 "$HOME\.copilot\rag\query\setup.py" --format json`

6. Confirm `setup_complete=true`, then run `--verify-only --format json`.

The top-level `copilot-instructions.md` is not included. Machine-local
`network.json` is included by default when present, but stored proxy
credentials are forbidden. Add the Local RAG routing line to an existing
top-level instruction file when needed, and confirm proxy/CA paths on the
destination machine.
EOF

"$MIGRATION_PYTHON" "$MIGRATION_HELPER" write-checksums \
  --root "$STAGE_ROOT" \
  --output "$STAGE_ROOT/SHA256SUMS" ||
  fail "could not generate payload checksums"

OUTPUT_DIR=$(dirname -- "$OUTPUT_ABS")
PARTIAL_ARCHIVE=$(mktemp "$OUTPUT_DIR/.local-rag-migration.XXXXXX")

(
  COPYFILE_DISABLE=1
  COPY_EXTENDED_ATTRIBUTES_DISABLE=1
  export COPYFILE_DISABLE COPY_EXTENDED_ATTRIBUTES_DISABLE
  cd "$TEMP_ROOT"
  tar -czf "$PARTIAL_ARCHIVE" "$BUNDLE_ROOT"
)
chmod 600 "$PARTIAL_ARCHIVE"
gzip -t "$PARTIAL_ARCHIVE" || fail "gzip integrity check failed"

VERIFY_ROOT="$TEMP_ROOT/verify"
mkdir -p "$VERIFY_ROOT"
"$MIGRATION_PYTHON" "$MIGRATION_HELPER" verify \
  --archive "$PARTIAL_ARCHIVE" \
  --destination "$VERIFY_ROOT" \
  --bundle-root "$BUNDLE_ROOT" ||
  fail "created archive failed safe payload verification"

archive_sha256=$(sha256_file "$PARTIAL_ARCHIVE" | awk '{print $1}')
archive_bytes=$(wc -c <"$PARTIAL_ARCHIVE" | tr -d ' ')
ln "$PARTIAL_ARCHIVE" "$OUTPUT_ABS" ||
  fail "output appeared during export; refusing to overwrite it"
rm -f -- "$PARTIAL_ARCHIVE"
PARTIAL_ARCHIVE=""

printf 'archive=%s\n' "$OUTPUT_ABS"
printf 'bytes=%s\n' "$archive_bytes"
printf 'sha256=%s\n' "$archive_sha256"
printf 'warning=archive_contains_local_documents_and_may_contain_company_data\n' >&2
