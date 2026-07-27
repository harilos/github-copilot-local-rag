# Semantic gold v2 development derivative

This dataset is for development and regression diagnosis only. It must not be
used to rejudge the failed frozen Semantic gold v2 release gate.

- Dataset: `semantic-gold-v2-dev.jsonl`
- Derived from: `semantic-gold-v2.jsonl`
- Frozen source SHA-256:
  `fdcf70a137d091d6b453495d7ce1d20b44d28f691177d3b8aaf9a9e27c56eafd`
- Development dataset SHA-256:
  `fea122072f9e87c859011e4d2b5819cf6afa481a1da1c93c01c74b49808f8c7b`
- Generated: 2026-07-27
- Status: development only

## Changes from frozen v2

- Preserved all 30 cases, questions, required claims, paths, chunk indexes,
  and span text.
- Relocated 17 stale `ac-rag` chunk UIDs to the current DB snapshot after
  verifying the original normalized span still exists at the same path and
  chunk index.
- Added explicit source-dataset provenance to every case.

## Body alternatives

No body alternative is active yet. Candidate passages observed during
diagnosis are intentionally not copied into the dataset automatically.
An alternative may be added only after a human reads the source document and
records that the passage independently proves the same claim. Record the
reviewer, review date, source path, chunk UID/index, exact span, and reason in
this file when that happens.

This restriction prevents development search results from silently changing
the meaning of the gold set.
