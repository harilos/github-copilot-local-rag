from __future__ import annotations

import functools
import re
from typing import Any
from urllib.parse import urlsplit


_PAGE_PATH = re.compile(r"^pages/(?P<page_id>[1-9][0-9]*)\.md$")
_MAX_PAGE_ID = 9_223_372_036_854_775_807
_MARKER = "_local_rag_confluence_links_installed"


def install_confluence_link_runtime(source_links: Any) -> None:
    """Install the bounded Confluence page-ID to exact-URL contract."""

    if bool(getattr(source_links, _MARKER, False)):
        return

    source_links._ALLOWED_PROVIDERS.add("confluence")
    source_links.ALLOWED_SOURCE_TYPES = frozenset(
        set(source_links.ALLOWED_SOURCE_TYPES) | {"confluence"}
    )
    original_validate = source_links._validate_provider_settings
    original_generate = source_links._generate_provider_urls

    @functools.wraps(original_validate)
    def validate_provider_settings(
        provider: str,
        strategy: str,
        settings: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if provider != "confluence":
            return original_validate(provider, strategy, settings, **kwargs)
        if (
            strategy != "confluence-page-map"
            or set(settings) != {"page_urls"}
            or not isinstance(settings.get("page_urls"), dict)
        ):
            raise source_links.SourceLinkError(
                "Confluence links require a page_urls map"
            )

        page_urls: dict[str, str] = {}
        expected_origin: tuple[str, str, int | None] | None = None
        seen_urls: set[str] = set()
        for page_id, raw_url in settings["page_urls"].items():
            normalized_id = _validate_page_id(
                page_id,
                error_type=source_links.SourceLinkError,
            )
            if not isinstance(raw_url, str):
                raise source_links.SourceLinkError(
                    "Confluence page URLs must be strings"
                )
            exact_url = source_links._required_url(raw_url)
            origin = _url_origin(
                exact_url,
                error_type=source_links.SourceLinkError,
            )
            if expected_origin is None:
                expected_origin = origin
            elif origin != expected_origin:
                raise source_links.SourceLinkError(
                    "Confluence page URLs must use one origin"
                )
            if exact_url in seen_urls:
                raise source_links.SourceLinkError(
                    "Confluence page URLs must be unique"
                )
            seen_urls.add(exact_url)
            page_urls[normalized_id] = exact_url

        return {
            "page_urls": {
                page_id: page_urls[page_id]
                for page_id in sorted(page_urls, key=int)
            }
        }

    @functools.wraps(original_generate)
    def generate_provider_urls(
        source_link: dict[str, Any],
        stored_path: str,
    ) -> dict[str, str]:
        if str(source_link.get("provider") or "") != "confluence":
            return original_generate(source_link, stored_path)
        path = source_links._normalize_stored_path(stored_path)
        match = _PAGE_PATH.fullmatch(path)
        if match is None:
            raise source_links.SourceLinkError(
                "Confluence stored path must be pages/<decimal-id>.md"
            )
        page_id = _validate_page_id(
            match.group("page_id"),
            error_type=source_links.SourceLinkError,
        )
        page_urls = (source_link.get("settings") or {}).get("page_urls")
        if not isinstance(page_urls, dict) or page_id not in page_urls:
            raise source_links.SourceLinkError(
                "Confluence stored page has no exact Source URL"
            )
        exact_url = page_urls[page_id]
        if not isinstance(exact_url, str):
            raise source_links.SourceLinkError(
                "Confluence stored page URL is invalid"
            )
        return {
            "source_provider": "confluence",
            "source_url": exact_url,
        }

    source_links._validate_provider_settings = validate_provider_settings
    source_links._generate_provider_urls = generate_provider_urls
    setattr(source_links, _MARKER, True)


def _validate_page_id(value: Any, *, error_type: type[Exception]) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise error_type("Confluence page ID must be a positive decimal string")
    if int(value) > _MAX_PAGE_ID:
        raise error_type("Confluence page ID is outside the supported range")
    return value


def _url_origin(
    value: str,
    *,
    error_type: type[Exception],
) -> tuple[str, str, int | None]:
    split = urlsplit(value)
    if "%" in split.netloc:
        raise error_type("Confluence page URL host must not be encoded")
    hostname = str(split.hostname or "").casefold()
    scheme = split.scheme.casefold()
    port = split.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


__all__ = ["install_confluence_link_runtime"]
