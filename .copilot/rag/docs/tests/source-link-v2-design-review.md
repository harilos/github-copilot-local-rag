# Source Link v2 Independent Design Review

> Superseding execution policy, 2026-07-28: Copilot product-compliance
> cohorts are optional metered tests. They are excluded from routine
> regression and the Local RAG software release gate. The design observations
> remain useful, but an unexecuted or quota-blocked cohort is `NOT_RUN` or
> `UNVERIFIED`, not a software release failure.

## Review context

- Baseline branch: `main`
- Baseline commit: `a935497306d692bd4383bb266814902feb449ec8`
- Remote baseline: the same commit
- Review scope: Source identity, observed-root derivation, v1 compatibility,
  sidecar v2, search fail-open behavior, Windows atomic persistence, export,
  live URL E2E, Copilot compliance, and rollback.
- Reviewer: an independent high-capability design agent. The implementation
  agent recorded this sanitized review artifact.

## Decision

The proposed feature is implementable without rebuilding a database or
changing search ranking. The intermediate implementation reviewed before this
design was not acceptable because it followed an earlier, conflicting
contract.

The following design is the required implementation baseline.

## P0 findings in the intermediate implementation

1. **Wrong source-relative path**

   The provider received the complete stored path. The final contract requires
   removal of the one catalog-observed top-level root component. Keeping the
   root could generate a valid URL for the wrong external path.

2. **Unsafe legacy selection**

   The adapter selected the first legacy mapping and ignored its path prefix.
   The final contract requires exactly one mapping, exactly one observed root,
   and canonical equality between the former prefix and that root. Multiple or
   mismatched legacy settings must be path-only.

3. **Unsafe stale-lock recovery**

   Age-only or PID-based lock removal can delete a replacement lock after an
   ownership check. The final design therefore uses a persistent opaque lock
   file plus an operating-system kernel lock. The application never rewrites
   or unlinks the lock file; process exit or descriptor close releases
   ownership.

## P1 findings in the intermediate implementation

1. v2 persisted a redundant `database` field, although the containing database
   directory is the identity.
2. v2 rejected `display_name` and `strategy`, which are part of the final
   schema.
3. compare-and-swap checked only revision and did not bind the edit to the
   exact bytes originally loaded.
4. the manager did not block per-file configuration for zero or multiple
   observed roots.
5. the live E2E runner still exercised the earlier full-stored-path behavior.

## Final data model

The v2 sidecar contains:

```json
{
  "schema_version": "rag-source-links-v2",
  "revision": 1,
  "sources": [
    {
      "source_id": "<source-id>",
      "display_name": "<optional-display-name>",
      "enabled": true,
      "provider": "other",
      "strategy": "append-relative-path",
      "settings": {
        "source_web_root": "https://<host>/<root>"
      }
    }
  ]
}
```

`database`, `mappings`, `mapping_id`, and Source-Link `path_prefix` are not
persisted. A display-name-only Source entry is valid. A configured Source has
exactly one provider, strategy, enabled state, and settings object.

## Observed-root contract

The catalog's currently visible documents are authoritative. Root derivation
must use the same `visible_until IS NULL` rules used by retrieval. The first
canonical path component is the observed root.

- zero observed roots: `no_observed_root`;
- one observed root: per-file links may be configured;
- multiple roots: `multiple_observed_roots` and per-file configuration is
  rejected.

Source-relative paths are derived component-wise by removing the single
observed root exactly once. Absolute paths, drive paths, UNC paths, and
traversal are rejected.

## Legacy adapter

- zero mappings: unconfigured;
- one mapping plus one matching observed root: normalize in memory;
- one mapping with no root, multiple roots, or mismatched prefix: path-only;
- multiple mappings: `legacy_multiple_mappings`, path-only;
- reading never rewrites the sidecar;
- explicit manager save publishes v2 and preserves the previous primary as the
  backup.

Legacy diagnostics must not leak provider settings into normal search output.

## Persistence and concurrency

Writers use a persistent, non-symlink, regular per-database lock file. POSIX
uses `flock`; Windows uses a one-byte `msvcrt` lock. The lock file is opaque,
is not rewritten or unlinked, and is released only by descriptor close or
process exit. This avoids both age/PID reclamation and the check-then-unlink
replacement race.

Under that kernel lock, writers use a revision plus raw-content hash
compare-and-swap, UTF-8 temporary files, flush/fsync where practical, backup
publication, final CAS recheck, atomic primary publication, and bounded
failure. Exactly one concurrent editor succeeds. Readers observe an old or
new complete file, never a partial file.

The backup is not an active search configuration. Search never silently falls
back to stale backup data.

## Search and identity invariants

Source links are added only after ranking, packing, and evidence
classification. Sidecar changes do not affect database hashes, database
snapshots, document or chunk identities, retrieval scores, ordering,
authority, answerability, or status. Normal search performs zero external HTTP
requests. Any Source-Link failure returns the original path-only result.

## Export and security

The active sidecar is portable database metadata and is included in a complete
database export only when it is a valid v2 configuration. Export validates the
live tree, the exact staged snapshot, and the final extracted archive before
publication. The rollback backup can contain an older internal URL and is
always excluded, as is the persistent edit-lock file. Credentials, tokens,
local roots, and embedded URL credentials are forbidden.

## Test and rollback gate

Implementation is acceptable only after offline contracts, both live URL E2E
suites, Windows real-machine gates, Copilot Auto/Mini/Standard compliance, and
an independent code review have no unresolved P0 or P1 finding. If a P0 or P1
cannot be safely resolved, revert only this feature's implementation to the
baseline and publish a sanitized blocked report.

## Follow-up review of the implementation design

The same independent reviewer inspected the first implementation pass and
reported two additional P0 and four P1 findings:

| Priority | Finding | Required resolution |
| --- | --- | --- |
| P0 | Credential-like assignments in a decoded URL path could be persisted and emitted. | Reject credential/token assignments in URL paths as well as userinfo, sensitive query keys, fragments, and settings keys. |
| P0 | PID-based recovery still admitted indeterminate-owner and replacement-lock races. | Superseded by a persistent opaque file plus POSIX `flock` or Windows `msvcrt` kernel ownership; never inspect an owner record or unlink the lock. |
| P1 | Root cardinality was enforced only by the manager UI. | Re-read current visible catalog roots under the edit lock and reject a per-file save unless exactly one root exists. |
| P1 | v1 conversion was not an explicit manager migration. | Refuse raw v1 saves, disclose migration statuses, require a separate confirmation, and retain the raw v1 primary as the backup. |
| P1 | Revision/hash compare-and-swap was optional. | Require both the loaded revision and raw-content hash, including the absent-file sentinel, and recheck before publication. |
| P1 | Inventory and search normalized Windows separators differently. | Use the shared canonical stored-path helper for both inventory and resolution. |

All six findings are release-blocking until their regression tests and the
Windows real-machine counterparts pass. They must be included again in the
independent post-implementation code review.

## Final design amendment

The kernel-lock design above supersedes every earlier proposal to recover a
stale lock by timestamp, PID liveness, or owner-record inspection. The final
Windows gate must demonstrate real `msvcrt` contention and close-only release
twice on the exact implementation and runner hashes.

The export decision is also final: only validated active v2 sidecars are
portable. `source-links.json.bak` and `.source-links.lock` are never migration
payload members. Invalid, credential-bearing, or legacy active sidecars abort
export without publishing an archive or disclosing their contents.
