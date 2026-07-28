# Source Metadata v1 Windows Reliability Specification

This specification defines the Windows release gate for the optional
`source-links.json` sidecar. The automated runner creates only synthetic
temporary databases and does not modify an installed database.

## Current Source Metadata contract

The sidecar is DB-local and does not persist a database name. Each Source has
an optional type and at most one optional URL configuration:

```json
{
  "schema_version": "rag-source-metadata-v1",
  "revision": 1,
  "sources": [
    {
      "source_id": "source-a",
      "source_type": "other",
      "link": {
        "enabled": true,
        "strategy": "append-relative-path",
        "settings": {
          "source_web_root": "https://fixture.example.invalid/root"
        }
      }
    }
  ]
}
```

`source_type` and `link` are independently optional unless a Link is present;
then `source_type` is required. `link` never stores a duplicate Provider.
`strategy` is required for a configured Source Link. Display-name-only and
type-only Sources are valid.

The following fields are forbidden in persisted Source Metadata:

- `database`
- `mappings`
- `mapping_id`
- `path_prefix`

There is no prefix matching, priority, or longest-prefix selection.
`source_id` selects the one configuration.

Every save is compare-and-swap and supplies both the revision and content
hash observed at load time. Creating a new sidecar uses revision `0` and the
`missing` content hash. Omitting either value is rejected.

## Observed-root contract

Observed roots are not configuration. At load and resolution time they are
derived from `catalog.sqlite`:

1. Read `document.source_id` and `document.path`.
2. If `visible_until` exists, use only rows where it is `NULL`.
3. Canonicalize stored paths to relative POSIX form.
4. For each Source, collect the first path component as its observed root.
5. Require exactly one observed root for per-file URL strategies.
6. Remove that one root component exactly once.
7. Pass the resulting Source-relative path to the provider strategy.

For example:

```text
stored_path:          Engineering Documents/design/spec.md
observed root:        Engineering Documents/
Source-relative path: design/spec.md
```

A hidden historical document must not affect the root. Zero roots, multiple
roots, a path outside the root, or an invalid path fail open to a path-only
search result. Search status, ordering, and database contents remain
unchanged.

Saving a per-file configuration is also rejected unless the authoritative
current catalog has exactly one observed root for that Source.

`home-only` has no per-document URL and therefore does not require
Source-relative resolution.

## Legacy compatibility

Compatibility is intentionally narrow:

- the file must use the legacy schema;
- the Source must contain exactly one mapping;
- current visible catalog documents must yield exactly one observed root;
- the former `path_prefix`, after canonicalization, must equal that root.

Only then may the mapping's `provider`, `enabled`, `strategy`, and `settings`
be normalized in memory. A mismatch remains unconfigured and reports
`legacy_root_mismatch`. Explicitly migrating a compatible legacy load publishes
Source Metadata and
keeps the raw legacy file as the immediate backup.

Multiple legacy mappings are not selected, merged, or prioritized.

## Running the P0 gate

Run with the installed virtual environment. The UNC argument must name a
disposable share dedicated to the test:

```powershell
& "<rag-root>\query\.venv\Scripts\python.exe" `
  "<rag-root>\docs\tests\run_source_link_windows_reliability.py" `
  --installed-rag "<rag-root>" `
  --output "<private-result-directory>" `
  --unc-root "\\<test-server>\<disposable-share>" `
  --manager-iterations 100 `
  --save-iterations 100 `
  --concurrent-writers 8
```

The runner uses an isolated temporary `RAG_DBS_ROOT`. It creates a real
synthetic `document`/`chunk` catalog with:

- Japanese, spaces, emoji, and mixed separators;
- one Source with one visible root;
- a hidden document under another root;
- another Source with a distinct root;
- a Source with two visible roots.

No organization content, network URL, or existing DB is copied into the
fixture.

## Automated P0 cases

| Case | Gate |
| --- | --- |
| `V2-001` | Exact v2 Source shape, required strategy, and rejection of every legacy field. |
| `V2-002` | A single legacy mapping migrates only when its former prefix equals the current visible observed root; raw v1 becomes backup. |
| `V2-003` | Catalog-derived root, hidden-row exclusion, one-time root removal, Unicode and mixed-separator resolution. |
| `V2-004` | Source identity, disabled/unknown fail-open, rejection of a multiple-root save, and fail-open behavior for a hand-edited multiple-root configuration. |
| `SC-001-003` | 100 atomic saves, exact revisions, immediately prior backup, no v1 fields. |
| `SC-004` | Invalid current fails open; explicit atomic restoration from backup is readable. |
| `SC-005` | Concurrent compare-and-swap writers produce exactly one success while the stable lock inode remains. |
| `SC-006` | Terminating a kernel-lock holder releases ownership and permits a save in at most five seconds. |
| `SC-006B` | The persistent lock file is never replaced, unlinked, read as ownership metadata, or modified by locking. |
| `SC-006C` | Windows `msvcrt` byte-range contention is detected and ownership is released by closing the handle. |
| `SEC-001` | Credential-bearing URL paths, queries, fragments, and settings are rejected without publishing output. |
| `SC-007` | Termination after the new temporary file is flushed leaves current valid and permits an immediate retry. |
| `MGR-001` | 100 manager start/exit cycles with strict UTF-8 output. |
| `MGR-002` | Nested EOF and two simultaneous manager processes exit without a traceback. |
| `MGR-003` | A targeted Windows console interrupt terminates the manager without a traceback. |
| `MGR-004` | Legacy migration requires a separate confirmation; cancel preserves v1 and approval publishes Source Metadata with a v1 backup. |
| `WIN-001` | `CreateFileW` denies delete sharing; save fails without changing current and succeeds after release. |
| `WIN-002` | Sidecar round trips with absolute paths near 248 and 259 characters. |
| `WIN-003` | The same fixture round trips through a temporary `subst` drive. |
| `WIN-004` | Sidecar round trip through a disposable UNC path. |
| `SQL-001` | Read-only SQLite checks and search enrichment run while sidecar saves continue. |
| `SQL-002` | Catalog hash is unchanged and `PRAGMA quick_check` returns `ok`. |

The interruption case may report the count of orphaned per-process temporary
files, but it removes them only inside the synthetic fixture. Current JSON,
backup integrity, kernel-lock release, and successful retry are the P0 gates.

The console case uses a targeted `CTRL_BREAK_EVENT`, which is reliable for a
new Windows process group under SSH. A release checklist may additionally
record one interactive Ctrl+C smoke, but it does not replace the automated
console-interrupt case.

## Windows path and lock expectations

Stored paths and physical Windows paths are separate contracts:

- stored paths are relative logical paths and become canonical POSIX paths;
- drive-absolute, UNC, and traversal strings are rejected as stored paths;
- the DB root itself is exercised with Unicode, spaces, emoji, near-MAX_PATH,
  an alternate drive letter, and UNC.

`WIN-001` opens `source-links.json` with read/write sharing but without
`FILE_SHARE_DELETE`. Replacement must fail in a controlled way. After the
handle closes, the same save must succeed and leave no live sidecar lock.

The runner never kills by image name. Every forced termination targets the
exact child PID created by the current run.

## Result evidence and privacy

The runner writes:

- `preflight.json`
- `cases.jsonl`
- `summary.json`
- an optional synthetic fixture copy when `--keep-fixture` is supplied

Result artifacts must not contain:

- a real user name;
- computer or SSH host name;
- an absolute installed, home, output, UNC, or fixture path;
- a real Source or database name;
- a real organization URL.

Preflight records booleans and version numbers, not paths. Error details
replace known roots and URL/path-shaped text with placeholders. All fixture
URLs use the reserved `.example.invalid` domain.

Case states are:

- `PASS`
- `FAIL`
- `INCOMPLETE_ENV`
- `NOT_RUN`

Missing UNC provisioning or another required Windows capability makes the
normal run `INCOMPLETE_ENV`; it is never promoted to release evidence.
Exit codes are 0 for PASS, 1 for FAIL, and 2 for INCOMPLETE_ENV.

## Local synthetic smoke

The runner can validate its portable logic outside Windows:

```bash
python run_source_link_windows_reliability.py \
  --installed-rag "<rag-root>" \
  --output "<temporary-output>" \
  --manager-iterations 2 \
  --save-iterations 3 \
  --concurrent-writers 2 \
  --stress-iterations 2 \
  --synthetic-smoke
```

Windows-only cases become `NOT_RUN`. This mode validates the harness but is
not Windows release evidence.

## Extended priorities

The optional `--stress-iterations 1000` cohort repeatedly saves and reloads
the same synthetic v2 configuration. Full P1/P2/P3 environment tests remain
separate from this P0 runner:

- P1: manager/search/build concurrency, sidecar rename/delete/restore, DB
  release/rename/copy, and two interactive manager saves;
- P2: Defender custom scan, OneDrive synchronization, `taskkill /F`, and a
  VM-only forced-power-off recovery;
- P3: 1,000 manager configuration cycles with native handle, thread, RSS,
  orphan-process, and failure-rate measurements.

Raw copying of a SQLite main file while WAL contains uncheckpointed data is
not a supported backup procedure. Use SQLite online backup or release all DB
users before copying.
