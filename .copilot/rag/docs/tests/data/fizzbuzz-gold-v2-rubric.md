# FizzBuzz Gold v2 annotation rubric

Rubric version: `fizzbuzz-gold-v2-rubric-v2`

Revision history: v2 is the single permitted clarification cycle. It resolves
the v1 ambiguity between grades 3 and 2; no query or source facts were added.

Reviewers work independently and without retrieval ranks, scores, reranker
output, or another reviewer's decisions. Agreement is reported as
**independent-agent agreement, not human IAA**.

## Passage grade

| Grade | Meaning | Binary label |
|---|---|---|
| 3 — essential | The sole alternative in a required group, or otherwise indispensable to one explicit query deliverable | Evidence |
| 2 — direct | Direct support or interchangeable corroboration whose removal still leaves every explicit query deliverable supported | Evidence |
| 1 — contextual | Helpful context but not sufficient for a required group | Related |
| 0 — irrelevant/unsafe | Does not support the answer, or supports an excluded claim | Distractor |

A grade 1 passage must never be referenced by `required_evidence_groups`.
Reviewers must verify that every sole alternative really is indispensable; if
it is not, the dataset must remove or regroup the anchor instead of inflating
its grade. Dataset-declared required status is therefore reviewable, not an
automatic grade 3.

For multi-evidence questions, all required groups must be satisfiable. An OR
alternative is valid only when it independently proves the same fact as the
other alternatives in that group.

For ambiguity questions, reviewers confirm that the corpus does not provide a
unique choice under the query's stated constraints. For unanswerable questions,
reviewers confirm that no positive anchor exists and that answering would
require an unsafe or unsupported assertion.

## Blind review gate

Two independent reviewers label every proposed anchor and every abstention
record. A third reviewer adjudicates every disagreement against the frozen
source. Before adjudication, quadratic weighted kappa across grades must be at
least `0.70`, and binary relevant/non-relevant agreement must be at least
`0.75`. At most one rubric revision and complete reannotation cycle is allowed.
No unresolved disagreement may remain at freeze time.
