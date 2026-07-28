# Source Metadata v1 design review

An independent design agent reviewed the Local RAG boundary before
implementation. The external Source Manager is intentionally out of scope and
Local RAG has no runtime or storage dependency on it.

## Decisions

- Keep the physical DB-local filename `source-links.json` to prevent an older
  Manager from creating a parallel configuration file.
- Use `rag-source-metadata-v1` with optional `display_name`, optional
  `source_type`, and optional nested `link`.
- Never infer an unspecified Source as `folder`.
- Require `source_type` when a new Link is saved; forbid a Provider field
  inside `link`.
- Keep build/add, catalog, Chroma, clean records, document identity, and
  ranking outside the metadata boundary.
- Read legacy v2 without writing. Migrate only through an explicit command.
- Treat any ambiguous legacy v1 Source as a DB-wide `manual_required` result,
  retaining the original file and failing open to path-only search.
- Preserve unmatched Source settings during migration.
- Use revision plus SHA-256 compare-and-swap and the existing atomic sidecar
  writer.
- The standalone migration command may scan all DBs, but the human Manager
  must always pass the selected DB through `--db`.

## Review findings applied

- P1: a Link without `source_type` or with a nested Provider could create
  conflicting identity. Strict key validation now rejects both.
- P1: migrating only the safe Sources from an ambiguous v1 file could silently
  discard settings. Migration now leaves the whole DB unchanged.
- P2: migration status must distinguish the schema found on disk from the
  normalized in-memory schema. The loader exposes `loaded_schema_version`.
- P2: migration status handling uses an explicit safe-status allowlist.

No P0 finding remained after these changes.
