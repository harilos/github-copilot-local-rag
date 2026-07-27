# Source-Link v2 Live E2E Report

## Scope

This run validated Source-Link v2 URL generation against:

- a public synthetic Git hosting fixture;
- a disposable loopback Redmine container;
- an explicit CA bundle on the HTTPS fixture.

Normal Local RAG lookup was not allowed to perform external HTTP requests.
Reports contain URL hashes only.

## Results

| Provider | Required | Passed | Result |
| --- | ---: | ---: | --- |
| GitHub-compatible | 6 | 6 | PASS |
| Redmine | 5 | 5 | PASS |
| Total | 11 | 11 | PASS |

Verified behavior:

- ref and commit-permalink targets returned HTTP 200;
- a ref containing `/` resolved;
- Japanese text, spaces, `#`, and parentheses were encoded and resolved;
- missing Git and Redmine targets returned HTTP 404;
- the Redmine issue marker matched in both page and JSON API output;
- unmatched Redmine paths remained path-only;
- `source_permalink` remained preferred over `source_url`;
- search status, answerability, and result order were unchanged;
- every URL-bearing GitHub and Redmine target measured the search invariant;
- no retry or credential-bearing report field was used.

## Environment and artifacts

- Runtime: installed Local RAG virtual-environment Python
- Redmine: disposable local container removed after the test
- Detailed records:
  `data/source-link-e2e-20260728-macos.jsonl`
- `source_links.py` SHA-256:
  `278f463e3893694f5f12079f2a8de598837170aa3d8ce5f3c47d3103e047330b`
- E2E runner SHA-256:
  `bde7bb5eda6e8db3f7bb0badc850c161512a0eb444aeca568c15d9fa2dc3b411`
- E2E runner contract-test SHA-256:
  `6ab7090c25df9d41589b8c129a867e7c3451e1ea0eb4295680c8a126b17c978f`
- E2E specification SHA-256:
  `c1227471c5718d5351d549a6fb4b776c2c2037f4a25289086046d23241f7bccf`
- Detailed-record SHA-256:
  `f23db04a801e2f07b40fb80efc3be73be360006817c0b0dd1cd77658978b0d59`

No database, index, or production Source-Link setting was changed.
