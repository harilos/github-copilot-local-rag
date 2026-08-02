# FizzBuzz Gold v2 freeze report

Status: frozen after score-blind independent review and adjudication

Frozen at: `2026-08-02T11:39:14.450045+00:00`

Product base: `23196354ab521df18170761e1a67960bf8089117`

Corpus commit: `e936dae1fe79f5e8e885c768ffaf7b42dc6fb74b`

## Dataset

- 30 new questions: DEV 18 and sealed holdout 12.
- Six strata: exact, paraphrase, procedure, multi-evidence, ambiguity, and
  unanswerable; each contains DEV 3 and holdout 2.
- DEV and holdout fact, document, and template families are disjoint.
- The previous 32-question PoC set is marked legacy DEV only and is not part of
  the sealed holdout.
- Holdout query text, labels, rationales, and anchors are not committed.

Frozen identities:

- corpus logical snapshot:
  `sha256:a64778063e8d51dd3b7552b59e262cedca80d5a2579a665a6630892988558bcb`
- DEV JSONL:
  `sha256:8c279988433639a532a8babf6baf5598691b49ca3cf9392a0fc0e7a9d8c36d45`
- sealed holdout JSONL:
  `sha256:b3f22dfd101e78c3e427db4c1e22ede3807d2fd6e68c2806612f2b9ce4902bdc`
- repository-safe holdout manifest:
  `sha256:9ae05235adbb7c36a320be37ce4afea43adb1b6fc8de3f128f0e9f37258c986f`

## Independent annotation gate

Reviewers received no retrieval ranks, retrieval scores, reranker scores, or
other reviewer's decisions. These values are independent-agent agreement, not
human IAA.

- rubric: `fizzbuzz-gold-v2-rubric-v2`
- reviewed items before final redundancy removal: 43
- exact grade agreement: 97.67%
- quadratic weighted kappa: 0.7882 (gate: at least 0.70)
- binary relevant agreement: 1.0000 (gate: at least 0.75)
- unresolved disagreements after third-party adjudication: 0

The single permitted rubric clarification made grade 3/2 depend on whether a
passage is indispensable to an explicit query deliverable. The adjudicator
then removed three redundant anchor/group pairs. The frozen dataset contains
35 positive anchors and 5 abstention decisions; no required grade-1 passage
remains.

## Validation and rebuild boundary

The validator checks record hashes, split counts, stratum balance, family
leakage, sealed-manifest disclosure, corpus snapshot, source/provider hashes,
clean-chunk UIDs, span hashes, and span coordinates. It accepts only explicit
source and clean-record paths and does not resolve a user's real `.copilot`
directory.

Gold v2 creation read the existing FizzBuzz database only. It did not rebuild
or mutate the database. Query-time evaluation and reranker changes apply to an
existing compatible database; corpus scope, chunking, extractor, embedding,
tokenizer/catalog schema, or indexed-field changes retain their separate
rebuild requirements.
