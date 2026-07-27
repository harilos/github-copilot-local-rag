# Broad Search Cases v1

This file documents the reviewed evaluation set in
`broad-search-cases-v1.jsonl`.

## Freeze record

- Frozen on: 2026-07-27
- Cases: 18
- Databases: 3
- Cases per database: 6
- Dataset SHA-256:
  `80afa1ef87297771d50a53c5eb65b836f0fe4f9679c10f3199da878da78e33c3`

Database snapshots used for review:

| Database | Documents | Chunks | `VERSION.json` `db_hash` | `catalog.sqlite` SHA-256 |
|---|---:|---:|---|---|
| `ac-rag` | 15 | 1,195 | `6b7bb428e1cec42bddaa35f510913c25f9a767bc22e9104444768322122fabf9` | `cc86a6a163f4a76d9d8aaf139518f43b9d3b1f82a8bc2a71b45e4ce061660649` |
| `incident-rag` | 201 | 2,144 | `7e3858deb3f4b69ee171a77ffeae8d4ddc69ff7a90b73162a37ef33ef1aada56` | `b9bd8e871e389fa27443638da021de517a2846c4b7e91e9eb623a76bcade9a44` |
| `rfc-full-20k-rag` | 386 | 19,518 | `c7f4fba2fc8266d4a0d67efdb443e01c811ff0c90bc8a81b35e979ee6f9051fc` | `3fb7be70a16a3901bb27f0838d037a8c247a5ee7e003eeb8316f511b632922da` |

## Review method

The cases and relevance judgments were created from read-only inspection of
the full SQLite catalogs and stored source text. The catalogs were opened with
SQLite URI options `mode=ro&immutable=1`. The review covered the complete
document inventory for each database, literal-identifier distributions, topic
distributions, and source excerpts for every positively graded document.

`search.py`, daemon retrieval, post-implementation search output, and search
evaluation artifacts were not used to select questions or grade documents.
The judgments therefore describe corpus relevance rather than observed system
ranking.

## Case matrix

Each database contains exactly one of each required case type:

1. existing identifier definition;
2. existing identifier asking for related documents;
3. absent near-collision identifier definition;
4. absent identifier asking for related documents;
5. general topic with at least six reviewed useful documents;
6. true one-document control.

All 18 cases are applicable. No `NOT_APPLICABLE` case was needed because a
corpus-unique one-document control was verified for each database.

## Judgment contract

Each JSONL record contains:

- `question`: the test question;
- `request`: a bounded `rag-search-request-v1` request preserving the question
  verbatim;
- `identifier_expectation`: Exact, unmatched-identifier, and near-collision
  expectations;
- `reviewed_documents`: only documents graded 1 through 3;
- `aspects`: independently reviewed coverage dimensions;
- `has_six_useful`: whether at least six reviewed grade-1-or-better documents
  exist;
- `one_document_control`: whether the case is a verified single-document
  control;
- `applicability` and `not_applicable_reason`.

Grades:

- 3: direct evidence or directly answers a major aspect;
- 2: strongly related and substantively useful;
- 1: weak but useful research lead;
- 0: noise. Grade-0 documents are intentionally not listed in
  `reviewed_documents`.

For absent identifiers, a grade-1 near-collision is a research lead only. It
must never become Exact evidence or authoritative support for the absent
identifier.

## One-document controls

The following literal-plus-topic checks each matched exactly one distinct
document in the reviewed catalog:

| Case | Read-only corpus check | Matching document |
|---|---|---|
| `AC-BROAD-ONE-DOCUMENT-006` | `673,215` | `nature_2024_inequalities_global_residential_cooling_energy_to_2050.pdf` |
| `INCIDENT-BROAD-ONE-DOCUMENT-012` | `N765DC` and `nose wheel fork` | `ntsb_aviation_report_67648.pdf` |
| `RFC-BROAD-ONE-DOCUMENT-018` | `RFC 9999` or `RFC9999` | `rfc9999.txt` |

The one-document controls do not require the system to fill discovery output
with unrelated grade-0 documents.

## Validation

The frozen dataset passed these structural checks:

- all 18 lines parse independently as JSON;
- case IDs are unique;
- each database has six cases;
- each required case type appears three times;
- `question` equals `request.original_question` for every case;
- request limits are respected: at most 3 literal identifiers, 5 entities,
  4 facets, and 3 inferred concepts;
- all requests use wide coverage;
- the largest serialized `request` is 1,084 bytes;
- every reviewed grade is 1, 2, or 3;
- every reviewed path exists exactly once in its database catalog;
- every `has_six_useful=true` case has at least six reviewed documents;
- every one-document control has exactly one reviewed document and one
  verified corpus match.

After any intentional edit, recompute the SHA-256 and treat the result as a new
dataset version rather than silently changing this frozen set.
