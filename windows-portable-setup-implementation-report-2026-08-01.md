# Windows portable setup replacement implementation report

> **Superseded / release blocked.** This report describes the original
> `d22e031` implementation and is not release evidence. The fix-forward work
> on `fix/windows-portable-release-blockers` found release-blocking defects
> that invalidate the earlier “complete” assessments below. Do not publish a
> ZIP, create a tag or GitHub Release, or close Issue #8 from this report.

- Date: 2026-08-01
- Worktree: `<WORKTREE>`
- Branch: `refactor/windows-portable-setup`
- Base: `origin/main`
- Source instruction: `local-rag-windows-portable-setup-replacement-instructions-2026-08-01.md`
- Release status: **SUPERSEDED / RELEASE BLOCKED**
- Integration status: `d22e031` is the fix-forward base; no release approval
  follows from its presence on `main`

## Fix-forward corrections

The original report did not detect that setup wrote mutable state inside the
closed-set runtime, package DBs were skipped by the installer, product overlay
was not transactional, archive verification was not exact-coverage, raw live
DB input lacked a secret-Source classification proof, VS Code auto-approval
was default-on and JSONC-unsafe, and the runtime/license/SBOM inputs were
incomplete.

Fix-forward commits completed so far:

- `f875fe0`: move packaged completion state outside `.venv`, post-validate
  normal writes, restore a valid previous marker on failure, and keep
  `--verify-only` from creating the setup lock.
- `9369427`: make VS Code auto-approval an explicit opt-in, preserve inline
  JSONC comments, use public script paths and full-command-line rule objects,
  and avoid creating VS Code paths when VS Code is absent.

All package v2, DB artifact, trust bootstrap, full transaction, provenance,
license, migration, and Windows acceptance claims remain `NOT RUN` or
`BLOCKED`.

## Outcome

The tracked NSIS/EXE setup builder was removed and replaced by an offline Windows x64 portable ZIP build path. Packaged setup now verifies a closed runtime manifest before doing setup work, does not resolve or install dependencies, preserves explicit VS Code deny settings, and records package identity in the completion marker.

No production runtime ZIP was published. A real embedded CPython runtime, locked wheel set, model payload, license bundle, and clean Windows test environment were not available in this worktree. The implementation therefore remains fail-closed and is not represented as release-ready.

## Requirement status

| Requirement | Status | Evidence / remaining work |
|---|---|---|
| Work isolated from concurrent repair work | Complete | Dedicated worktree and branch listed above |
| Replace NSIS/EXE builder | Complete | `setup_exe_build/` is deleted; `tools/windows_portable/` is the replacement |
| Copy-ready Windows x64 ZIP | Implemented, release test pending | Deterministic ZIP builder and verification CLI exist; contract test builds and inspects a synthetic package |
| Search-only and admin-full profiles | Complete in contract | Both are accepted by builder/runtime manifest; profile-specific verification imports are enforced |
| Fixed packaged Python, no PATH Python | Complete | Runtime executable is manifest-declared and setup docs use the fixed interpreter |
| Normal packaged setup performs no network or pip | Complete in automated contract | Network resolver, pip install, venv creation, and model preparation paths are bypassed and tested |
| Runtime integrity and architecture validation | Complete in installer/runtime contract | Exact file coverage, size, SHA-256, safe relative paths, reparse rejection, required runtime files, distribution inventory, and AMD64 PE checks |
| Dependency locking | Partially complete | CPython runtime lock and direct requirement lock files are checked in; a production wheelhouse/runtime must still be assembled and audited |
| Model identity | Complete in contract | Package/runtime manifest fingerprint is checked against the model manifest during verification |
| Completion-marker identity | Complete | Product/profile/platform/Python/dependency/runtime/model fingerprints are stored and revalidated |
| Setup concurrency protection | Complete | Cross-process setup lock is acquired before packaged setup |
| VS Code settings update | Complete in contract | JSONC/comments/CRLF preserved, atomic backup/write, Stable/existing Insiders/profiles, duplicate/malformed fail-closed behavior |
| Preserve user auto-approval denials | Complete | Global `false` and existing command-level values are not overwritten |
| Auto-approve scope | Complete in contract | Only full fixed-interpreter command lines for `list_dbs.py` and `search.py` are added |
| Exclude secrets, machine config, DB/model state | Complete in builder contract | Recursive DB/model exclusion plus forbidden-name/suffix and reparse checks are tested |
| Transactional update and rollback | Partial / release blocker | Runtime and model are staged, swapped, verified, and rolled back. Product-file overlay is not yet a single reversible tree swap |
| Manager-generated `install.ps1` parity | Not complete / release blocker | Existing generic Manager distribution installer remains separate from the new official portable-package template |
| Documentation and Skill routing | Complete for implemented path | Root/RAG READMEs, system design, Manager guide, instructions, and Skills were updated |
| Real Windows acceptance matrix | Not run / release blocker | Clean install, update/rollback fault injection, non-ASCII paths, Defender, VS Code UI behavior, performance, and admin-full end-to-end remain |
| Signing and notices | Not complete / release blocker | SBOM/NOTICE scaffolding exists; complete third-party license texts and optional Authenticode/release signing remain |

## Primary files

### New runtime/setup contracts

- `.copilot/rag/query/portable_runtime.py`
- `.copilot/rag/query/vscode_settings.py`
- `.copilot/rag/query/test_windows_portable_contracts.py`
- `.copilot/rag/query/test_vscode_settings_contracts.py`

### New portable builder

- `tools/windows_portable/windows_package_builder.py`
- `tools/windows_portable/build_package.py`
- `tools/windows_portable/build_package.ps1`
- `tools/windows_portable/verify_package.py`
- `tools/windows_portable/install-template.ps1`
- `tools/windows_portable/runtime-lock.json`
- `tools/windows_portable/requirements-search.lock`
- `tools/windows_portable/requirements-admin.lock`
- `tools/windows_portable/test_windows_package_builder_contracts.py`

### Modified integration points

- `.copilot/rag/query/setup.py`
- `.copilot/rag/query/setup_contract.py`
- `.copilot/rag/query/setup_verification.py`
- `README.md`
- `.copilot/rag/README.md`
- `.copilot/rag/docs/local-rag-system-design.md`
- `.copilot/rag/docs/local-rag-manager-guide-ja.md`
- `.copilot/instructions/rag.instructions.md`
- `.copilot/skills/local-rag-setup/SKILL.md`
- `.copilot/skills/local-rag/SKILL.md`
- `.gitignore`

### Removed legacy builder

All tracked files below `setup_exe_build/`, including the NSIS builder, post-install helper, runtime staging helpers, and the former VS Code auto-approval script.

## Verification performed

| Verification | Result |
|---|---|
| Portable runtime/setup contracts | 4 passed |
| VS Code settings contracts | 6 passed |
| Portable builder contracts | 2 passed |
| Existing setup-network contracts | 29 passed, 1 skipped |
| Completion-marker repair contracts | 5 passed |
| Copilot setup routing contracts | 4 passed |
| Lightweight routing contracts | 10 passed |
| Python compilation of changed/new implementation modules | Passed |
| PowerShell AST parse: new build/install scripts and root installer | Passed |
| `git diff --check` | Passed |
| Full query discovery | **Invalid as release evidence: collection/import errors present** |

The earlier `432 run; 38 errors; no assertion failures` wording was
incorrect: import/discovery errors mean affected assertions were not
collected. Another environment also observed a different count, so this was
not a reproducible baseline. The historical local log location is redacted:

`<TEMP>/local-rag-windows-portable-query-tests-final.log`

Real Windows 10/11, PowerShell 5.1/7, MOTW/AllSigned, Defender, network-zero,
VS Code UI/security, actual CPython/model/DB, and migration acceptance were
not performed by the original implementation and must be recorded as
`NOT RUN`, not inferred from unit tests or AST parsing.

## Build interface

Expected release artifact name:

`local-rag-windows-x64-<version>.zip`

Builder entry point:

> The following interface is historical and blocked. It accepts caller
> self-asserted identities and does not implement the required package-v2
> trust chain. Do not use it to create a release artifact.

```powershell
.\tools\windows_portable\build_package.ps1 `
  -PayloadRoot <path-to-.copilot> `
  -RuntimeRoot <assembled-runtime> `
  -ModelRoot <model-payload> `
  -OutputDir <empty-output-directory> `
  -Version <version> `
  -Profile search-only `
  -PythonVersion 3.13.5 `
  -DependencyLockSha256 <sha256> `
  -ModelFingerprint <sha256>
```

A release build refuses to overwrite an existing artifact and emits the ZIP SHA-256, package manifest, runtime manifest, `SHA256SUMS`, SBOM, NOTICE, and Windows README.

## Release blockers

1. Make the product payload update fully reversible, not only runtime/model.
2. Either route Manager-generated Windows distribution packages through the same portable installer contract or explicitly retire that route.
3. Assemble the real offline runtime/wheel/model payload from approved sources and verify every hash.
4. Complete third-party license text aggregation and legal review.
5. Run the clean Windows acceptance matrix, including paths with spaces/Japanese, update failure injection, VS Code Stable/Insiders/profiles, Defender, performance, and both package profiles.
6. Decide whether release signing is required and apply it before publishing.
7. Resolve or baseline the 38 unrelated full-suite discovery errors before merge policy approval.

## Repository state

`d22e031` is present on `main` and remains the required fix-forward
ancestor. No GitHub PR or release was created. Fix-forward branch commits are
not approved for local-main merge or origin push, and no such operation is
reported here.
