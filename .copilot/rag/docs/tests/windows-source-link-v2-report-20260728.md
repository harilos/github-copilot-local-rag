# Windows Source Link v2 Reliability Report

## Result

The Windows real-machine gate passed twice from a clean start on the exact
same implementation and runner revision: 23/23 cases in each run, including
every required case and the optional 1,000-iteration stress case.

| Run | Result | Required/optional cases | Save stress |
|---|---|---:|---:|
| 1 | PASS | 23/23 | 1,000/1,000 |
| 2 | PASS | 23/23 | 1,000/1,000 |

The tracked JSONL is the sanitized second-run result.

## Verified inputs

- `source_links.py` SHA-256:
  `278f463e3893694f5f12079f2a8de598837170aa3d8ce5f3c47d3103e047330b`
- Windows reliability runner SHA-256:
  `007ee24e07aeb28b5ef65c11b8fb70ff8313890b2e9216e44fb19b1e9c0e08f3`
- Installed Manager SHA-256:
  `a180bc23b0a8f778ce195a2c902586f2fb35ec04085c6970dd8b62d5de6fea46`
- Tracked second-run JSONL SHA-256:
  `8448ec940f6f0ac8f98fc71b43ce7bde6c793243f2375cc113a6ae2434d5abcf`
- Platform: Windows, CPython 3.13.1
- Installed layout and venv runtime: present

The local source and installed Windows copies were hash-equal before the
formal runs.

## Covered behavior

- Source Link v2 schema and catalog-derived observed roots
- safe, explicit legacy migration with raw v1 backup retention
- 100 sequential atomic saves
- eight-writer compare-and-swap conflict handling
- killed-writer and interrupted-save recovery
- a persistent opaque kernel-lock file whose contents and identity are not
  rewritten
- Windows `msvcrt` byte-range lock contention and close-only lock release
- credential-bearing paths, queries, fragments, and settings are rejected
  without publishing a sidecar or backup
- 100 Manager start/exit cycles, nested EOF, double launch, and console break
- legacy migration is cancelled without mutation and runs only after a
  separate explicit confirmation
- intentional Windows replacement sharing violation and retry
- paths near 248 and 259 characters, an alternate drive, and a disposable UNC
  share
- concurrent read-only SQLite access and Source-Link saves
- 1,000 save/load stress iterations
- final catalog hash equality and SQLite `quick_check=ok`

No case in either formal run reported malformed JSON, wrong-Source URL
resolution, database mutation, an unbounded wait, or an unexpected save
failure. Both runs completed 25 concurrent sidecar writes, retained backup
revision 104, rejected 53 credential-bearing configurations without
publishing a file, and reached revision 1135 after the 1,000-save stress
cohort.

Earlier development runs exposed a UNC SQLite URI incompatibility and one
transient Windows sharing violation during `os.replace`. The read-only
connection retained `mode=ro` while encoding UNC paths correctly, and
sidecar publication now retries only Windows access-denied/sharing-violation
errors within a bounded two-second window. A test-oracle issue that attempted
to read a byte-range-locked file through a second descriptor was also fixed;
the final check now reads the opaque contents only after close releases the
kernel lock. The two complete formal runs above started after those fixes and
after implementation/runner hash equality was verified.

## Privacy

The tracked JSONL contains only synthetic identifiers and reserved
`.example.invalid` behavior. It excludes workstation paths, user names,
computer names, host names, UNC values, and real URLs.
