from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


SCRIPT_ROOT = Path(__file__).resolve().parent
RAG_ROOT = SCRIPT_ROOT.parent.parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_links


GITHUB_MARKER = "LOCAL_RAG_SOURCE_LINK_FIXTURE_V1"
REDMINE_MARKER = "LOCAL_RAG_SOURCE_LINK_ISSUE_V1"
FIXTURE_PREFIX = "Fixture Root/"
FIXTURE_REPOSITORY_PATH = (
    ".copilot/rag/docs/tests/source-link-fixtures"
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    final_url: str
    body: bytes
    latency_seconds: float
    redirects: int
    error_kind: str | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def fetch_url(
    url: str,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    total_timeout: float = TOTAL_TIMEOUT_SECONDS,
    max_redirects: int = MAX_REDIRECTS,
) -> HttpResult:
    """Perform one bounded GET flow without retries or cookie persistence."""
    started = time.monotonic()
    current = url
    redirects = 0
    while True:
        elapsed = time.monotonic() - started
        remaining = total_timeout - elapsed
        if remaining <= 0:
            return HttpResult(
                None,
                current,
                b"",
                elapsed,
                redirects,
                "overall_deadline_exceeded",
            )
        timeout = min(connect_timeout, remaining)
        split = urlsplit(current)
        handlers: list[Any] = [_NoRedirect()]
        if split.hostname in {"127.0.0.1", "::1", "localhost"}:
            handlers.insert(0, ProxyHandler({}))
        opener = build_opener(*handlers)
        request = Request(
            current,
            method="GET",
            headers={
                "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.1",
                "User-Agent": "Local-RAG-Source-Link-E2E/1",
            },
        )
        status: int | None = None
        headers: Any = {}
        body = b""
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                headers = response.headers
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = int(exc.code)
            headers = exc.headers
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        except (URLError, TimeoutError, OSError) as exc:
            return HttpResult(
                None,
                current,
                b"",
                time.monotonic() - started,
                redirects,
                type(exc).__name__,
            )
        if len(body) > MAX_RESPONSE_BYTES:
            return HttpResult(
                status,
                current,
                b"",
                time.monotonic() - started,
                redirects,
                "response_too_large",
            )
        if status in REDIRECT_STATUSES:
            location = str(headers.get("Location") or "")
            if not location:
                return HttpResult(
                    status,
                    current,
                    body,
                    time.monotonic() - started,
                    redirects,
                    "redirect_without_location",
                )
            if redirects >= max_redirects:
                return HttpResult(
                    status,
                    current,
                    body,
                    time.monotonic() - started,
                    redirects,
                    "redirect_limit_exceeded",
                )
            current = urljoin(current, location)
            redirects += 1
            continue
        return HttpResult(
            status,
            current,
            body,
            time.monotonic() - started,
            redirects,
        )


def _mapping(
    *,
    provider: str,
    strategy: str,
    path_prefix: str = FIXTURE_PREFIX,
    settings: dict[str, Any],
) -> dict[str, Any]:
    return {
        "mapping_id": str(uuid.uuid4()),
        "enabled": True,
        "path_prefix": path_prefix,
        "provider": provider,
        "strategy": strategy,
        "settings": settings,
    }


def _github_mapping(args: argparse.Namespace) -> dict[str, Any]:
    return source_links.validate_mapping(
        _mapping(
            provider="github",
            strategy="github-blob",
            settings={
                "repository_url": args.repository_url,
                "ref": args.ref,
                "repository_path_prefix": args.repository_path_prefix,
                "commit": args.commit,
                "permalink_enabled": True,
            },
        )
    )


def _redmine_mapping(
    base_url: str,
    *,
    pattern: str = r"issues/issue-(?P<id>[0-9]+)\.md",
    template: str | None = None,
) -> dict[str, Any]:
    root = base_url.rstrip("/")
    return source_links.validate_mapping(
        _mapping(
            provider="redmine",
            strategy="regex-template",
            settings={
                "path_pattern": pattern,
                "url_template": template or f"{root}/issues/{{id}}",
            },
        )
    )


def _preview(
    mapping: dict[str, Any],
    relative_path: str,
) -> dict[str, Any]:
    values = source_links.resolve_mapping_preview(
        mapping,
        [FIXTURE_PREFIX + relative_path],
    )
    if len(values) != 1:
        raise RuntimeError("unexpected preview result count")
    return values[0]


def _search_contract_unchanged(
    mapping: dict[str, Any],
    relative_path: str,
    *,
    expect_link: bool,
) -> bool:
    before = {
        "status": "ok",
        "answerability": "full",
        "evidence": [
            {
                "id": "E1",
                "_source_id": "fixture-source",
                "source": {
                    "path": FIXTURE_PREFIX + relative_path,
                },
                "text": "Synthetic evidence.",
                "authoritative": True,
            }
        ],
        "background_context": [],
        "document_results": [
            {
                "id": "D1",
                "_source_id": "fixture-source",
                "path": FIXTURE_PREFIX + relative_path,
            }
        ],
    }
    with tempfile.TemporaryDirectory(
        prefix="rag-source-link-e2e-"
    ) as temporary:
        db_root = Path(temporary) / "fixture-rag"
        db_root.mkdir()
        source_links.save_source_links(
            db_root,
            {
                "schema_version": source_links.SCHEMA_VERSION,
                "database": "fixture-rag",
                "revision": 1,
                "sources": [
                    {
                        "source_id": "fixture-source",
                        "mappings": [mapping],
                    }
                ],
            },
            db_name="fixture-rag",
            existing_sources={"fixture-source"},
            observed_paths={
                "fixture-source": [
                    FIXTURE_PREFIX + relative_path,
                ]
            },
        )
        after = source_links.enrich_search_payload(
            before,
            db_root,
            "fixture-rag",
        )
    invariant = (
        after.get("status") == before["status"]
        and after.get("answerability") == before["answerability"]
        and [item.get("id") for item in after["evidence"]] == ["E1"]
        and [item.get("id") for item in after["document_results"]] == ["D1"]
        and bool(after["evidence"][0].get("source_url")) is expect_link
    )
    return invariant


def _marker_in_body(body: bytes, marker: str) -> bool:
    return marker.encode("utf-8") in body


def _github_marker_verified(url: str, result: HttpResult) -> bool:
    if _marker_in_body(result.body, GITHUB_MARKER):
        return True
    separator = "&" if "?" in url else "?"
    raw = fetch_url(f"{url}{separator}raw=1")
    return raw.status == 200 and _marker_in_body(
        raw.body,
        GITHUB_MARKER,
    )


def _record(
    *,
    case_id: str,
    provider: str,
    generated_url: str | None,
    expected_status: int | None,
    actual_status: int | None,
    latency_seconds: float,
    target_verified: bool,
    marker_verified: bool | None,
    search_status_unchanged: bool,
    passed: bool,
    url_reporting: str,
    error_kind: str | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "case_id": case_id,
        "provider": provider,
        "expected_status": expected_status,
        "actual_status": actual_status,
        "target_verified": target_verified,
        "marker_verified": marker_verified,
        "latency_seconds": round(latency_seconds, 6),
        "search_status_unchanged": search_status_unchanged,
        "passed": bool(passed),
    }
    if generated_url:
        output["url_sha256"] = hashlib.sha256(
            generated_url.encode("utf-8")
        ).hexdigest()
        if url_reporting == "public":
            output["generated_url"] = generated_url
            output["final_host"] = (
                urlsplit(generated_url).hostname or ""
            )
    if error_kind:
        output["error_kind"] = error_kind
    return output


def run_github(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", args.commit or ""):
        raise ValueError("--commit must be a full hexadecimal commit ID")
    mapping = _github_mapping(args)
    expected_host = urlsplit(args.repository_url).hostname or ""
    cases: list[dict[str, Any]] = []

    plain = _preview(mapping, "plain.txt")
    plain_url = str(plain["source_url"])
    plain_http = fetch_url(plain_url)
    plain_marker = (
        plain_http.status == 200
        and _github_marker_verified(plain_url, plain_http)
    )
    plain_target = (
        plain_http.status == 200
        and urlsplit(plain_http.final_url).hostname == expected_host
    )
    cases.append(
        _record(
            case_id="GH-E2E-001",
            provider="github",
            generated_url=plain_url,
            expected_status=200,
            actual_status=plain_http.status,
            latency_seconds=plain_http.latency_seconds,
            target_verified=plain_target,
            marker_verified=plain_marker,
            search_status_unchanged=_search_contract_unchanged(
                mapping,
                "plain.txt",
                expect_link=True,
            ),
            passed=plain_target and plain_marker,
            url_reporting=args.url_reporting,
            error_kind=plain_http.error_kind,
        )
    )

    nested = _preview(mapping, "nested/document.md")
    permalink = str(nested["source_permalink"])
    permalink_http = fetch_url(permalink)
    permalink_marker = (
        permalink_http.status == 200
        and _github_marker_verified(permalink, permalink_http)
    )
    permalink_target = (
        permalink_http.status == 200
        and args.commit.lower() in permalink.lower()
        and urlsplit(permalink_http.final_url).hostname == expected_host
    )
    cases.append(
        _record(
            case_id="GH-E2E-002",
            provider="github",
            generated_url=permalink,
            expected_status=200,
            actual_status=permalink_http.status,
            latency_seconds=permalink_http.latency_seconds,
            target_verified=permalink_target,
            marker_verified=permalink_marker,
            search_status_unchanged=True,
            passed=permalink_target and permalink_marker,
            url_reporting=args.url_reporting,
            error_kind=permalink_http.error_kind,
        )
    )

    special = _preview(
        mapping,
        "日本語 空白 #1 (final).txt",
    )
    special_url = str(special["source_url"])
    special_http = fetch_url(special_url)
    encoded = all(
        value in special_url
        for value in (
            "%E6%97%A5%E6%9C%AC%E8%AA%9E",
            "%20",
            "%23",
            "%28final%29",
        )
    )
    special_marker = (
        special_http.status == 200
        and _github_marker_verified(special_url, special_http)
    )
    special_target = (
        special_http.status == 200
        and encoded
        and urlsplit(special_http.final_url).hostname == expected_host
    )
    cases.append(
        _record(
            case_id="GH-E2E-003",
            provider="github",
            generated_url=special_url,
            expected_status=200,
            actual_status=special_http.status,
            latency_seconds=special_http.latency_seconds,
            target_verified=special_target,
            marker_verified=special_marker,
            search_status_unchanged=True,
            passed=special_target and special_marker,
            url_reporting=args.url_reporting,
            error_kind=special_http.error_kind,
        )
    )

    slash_target = (
        "/" in args.ref
        and plain_http.status == 200
        and f"/blob/{args.ref}/" in plain_url
    )
    cases.append(
        _record(
            case_id="GH-E2E-004",
            provider="github",
            generated_url=plain_url,
            expected_status=200,
            actual_status=plain_http.status,
            latency_seconds=plain_http.latency_seconds,
            target_verified=slash_target,
            marker_verified=plain_marker,
            search_status_unchanged=True,
            passed=slash_target and plain_marker,
            url_reporting=args.url_reporting,
            error_kind=plain_http.error_kind,
        )
    )

    preferred = source_links.preferred_source_link(nested)
    preferred_ok = (
        preferred == nested.get("source_permalink")
        and preferred != nested.get("source_url")
        and permalink_http.status == 200
    )
    cases.append(
        _record(
            case_id="GH-E2E-005",
            provider="github",
            generated_url=preferred,
            expected_status=200,
            actual_status=permalink_http.status,
            latency_seconds=permalink_http.latency_seconds,
            target_verified=preferred_ok,
            marker_verified=permalink_marker,
            search_status_unchanged=True,
            passed=preferred_ok and permalink_marker,
            url_reporting=args.url_reporting,
            error_kind=permalink_http.error_kind,
        )
    )

    missing = _preview(mapping, "missing-fixture.txt")
    missing_url = str(missing["source_url"])
    missing_http = fetch_url(missing_url)
    missing_invariant = _search_contract_unchanged(
        mapping,
        "missing-fixture.txt",
        expect_link=True,
    )
    missing_ok = missing_http.status == 404 and missing_invariant
    cases.append(
        _record(
            case_id="GH-E2E-006",
            provider="github",
            generated_url=missing_url,
            expected_status=404,
            actual_status=missing_http.status,
            latency_seconds=missing_http.latency_seconds,
            target_verified=missing_http.status == 404,
            marker_verified=None,
            search_status_unchanged=missing_invariant,
            passed=missing_ok,
            url_reporting=args.url_reporting,
            error_kind=missing_http.error_kind,
        )
    )
    return cases


def run_redmine(args: argparse.Namespace) -> list[dict[str, Any]]:
    mapping = _redmine_mapping(args.redmine_base_url)
    issue_path = f"issues/issue-{args.issue_id}.md"
    issue = _preview(mapping, issue_path)
    issue_url = str(issue["source_url"])
    issue_http = fetch_url(issue_url)
    page_marker = _marker_in_body(
        issue_http.body,
        args.issue_marker,
    )
    api_http = fetch_url(issue_url + ".json")
    api_verified = False
    if api_http.status == 200:
        try:
            api_payload = json.loads(api_http.body.decode("utf-8"))
            api_issue = api_payload.get("issue") or {}
            api_verified = (
                int(api_issue.get("id")) == int(args.issue_id)
                and args.issue_marker in str(api_issue.get("subject") or "")
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            api_verified = False
    issue_invariant = _search_contract_unchanged(
        mapping,
        issue_path,
        expect_link=True,
    )
    issue_ok = (
        issue_http.status == 200
        and page_marker
        and api_verified
        and issue_invariant
    )
    cases = [
        _record(
            case_id="RM-E2E-001",
            provider="redmine",
            generated_url=issue_url,
            expected_status=200,
            actual_status=issue_http.status,
            latency_seconds=issue_http.latency_seconds + api_http.latency_seconds,
            target_verified=api_verified,
            marker_verified=page_marker and api_verified,
            search_status_unchanged=issue_invariant,
            passed=issue_ok,
            url_reporting=args.url_reporting,
            error_kind=issue_http.error_kind or api_http.error_kind,
        )
    ]

    unmatched = _preview(mapping, "issues/no-id.md")
    unmatched_invariant = _search_contract_unchanged(
        mapping,
        "issues/no-id.md",
        expect_link=False,
    )
    unmatched_ok = (
        unmatched.get("status") == "unconfigured"
        and "source_url" not in unmatched
        and unmatched_invariant
    )
    cases.append(
        _record(
            case_id="RM-E2E-002",
            provider="redmine",
            generated_url=None,
            expected_status=None,
            actual_status=None,
            latency_seconds=0.0,
            target_verified=unmatched_ok,
            marker_verified=None,
            search_status_unchanged=unmatched_invariant,
            passed=unmatched_ok,
            url_reporting=args.url_reporting,
        )
    )

    missing_id = int(args.issue_id) + 1_000_000_000
    missing_path = f"issues/issue-{missing_id}.md"
    missing = _preview(mapping, missing_path)
    missing_url = str(missing["source_url"])
    missing_http = fetch_url(missing_url)
    missing_invariant = _search_contract_unchanged(
        mapping,
        missing_path,
        expect_link=True,
    )
    missing_ok = missing_http.status == 404 and missing_invariant
    cases.append(
        _record(
            case_id="RM-E2E-003",
            provider="redmine",
            generated_url=missing_url,
            expected_status=404,
            actual_status=missing_http.status,
            latency_seconds=missing_http.latency_seconds,
            target_verified=missing_http.status == 404,
            marker_verified=None,
            search_status_unchanged=missing_invariant,
            passed=missing_ok,
            url_reporting=args.url_reporting,
            error_kind=missing_http.error_kind,
        )
    )

    without_slash = _redmine_mapping(
        args.redmine_base_url.rstrip("/")
    )
    with_slash = _redmine_mapping(
        args.redmine_base_url.rstrip("/") + "/"
    )
    first_url = _preview(without_slash, issue_path).get("source_url")
    second_url = _preview(with_slash, issue_path).get("source_url")
    slash_ok = (
        first_url == second_url == issue_url
        and issue_http.status == 200
    )
    cases.append(
        _record(
            case_id="RM-E2E-004",
            provider="redmine",
            generated_url=issue_url,
            expected_status=200,
            actual_status=issue_http.status,
            latency_seconds=issue_http.latency_seconds,
            target_verified=slash_ok,
            marker_verified=page_marker,
            search_status_unchanged=True,
            passed=slash_ok and page_marker,
            url_reporting=args.url_reporting,
            error_kind=issue_http.error_kind,
        )
    )

    invalid_settings = [
        (
            r"issues/issue-(?P<id>[0-9]+)\.md",
            args.redmine_base_url.rstrip("/") + "/issues/{missing}",
        ),
        (
            r"issues/issue-[0-9]+\.md",
            args.redmine_base_url.rstrip("/") + "/issues/{id}",
        ),
        (
            r"issues/issue-(?P<id>[0-9]+)\.md",
            "https://user:password@example.invalid/issues/{id}",
        ),
        (
            r"issues/issue-(?P<id>[0-9]+)\.md",
            "https://example.invalid/issues/{id}?access_token=blocked",
        ),
        (
            r"(?P<id>(a+)+$)",
            args.redmine_base_url.rstrip("/") + "/issues/{id}",
        ),
    ]
    rejected = 0
    for pattern, template in invalid_settings:
        try:
            _redmine_mapping(
                args.redmine_base_url,
                pattern=pattern,
                template=template,
            )
        except source_links.SourceLinkError:
            rejected += 1
    invalid_ok = rejected == len(invalid_settings)
    cases.append(
        _record(
            case_id="RM-E2E-005",
            provider="redmine",
            generated_url=None,
            expected_status=None,
            actual_status=None,
            latency_seconds=0.0,
            target_verified=invalid_ok,
            marker_verified=None,
            search_status_unchanged=True,
            passed=invalid_ok,
            url_reporting=args.url_reporting,
        )
    )
    return cases


def write_jsonl(
    records: Iterable[dict[str, Any]],
    path: Path | None,
) -> None:
    lines = [
        json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for record in records
    ]
    if path is not None:
        target = path.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line + "\n")
            handle.flush()
        temporary.replace(target)
    for line in lines:
        print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicit live HTTP validation for Local RAG Source-Link mappings."
        )
    )
    parser.add_argument(
        "--provider",
        choices=["github", "redmine"],
        required=True,
    )
    parser.add_argument(
        "--url-reporting",
        choices=["redacted", "public"],
        default="redacted",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repository-url")
    parser.add_argument("--ref")
    parser.add_argument("--commit")
    parser.add_argument(
        "--repository-path-prefix",
        default=FIXTURE_REPOSITORY_PATH,
    )
    parser.add_argument("--redmine-base-url")
    parser.add_argument("--issue-id", type=int)
    parser.add_argument(
        "--issue-marker",
        default=REDMINE_MARKER,
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.provider == "github":
        missing = [
            name
            for name in ("repository_url", "ref", "commit")
            if not getattr(args, name)
        ]
    else:
        missing = [
            name
            for name in ("redmine_base_url", "issue_id")
            if not getattr(args, name)
        ]
    if missing:
        raise ValueError(
            "missing required provider arguments: " + ", ".join(missing)
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_args(args)
        records = (
            run_github(args)
            if args.provider == "github"
            else run_redmine(args)
        )
    except (
        ValueError,
        RuntimeError,
        source_links.SourceLinkError,
    ) as exc:
        parser.error(str(exc))
    write_jsonl(records, args.output)
    return 0 if all(record["passed"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
