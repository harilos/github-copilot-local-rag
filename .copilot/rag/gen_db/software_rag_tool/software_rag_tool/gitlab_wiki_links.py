from __future__ import annotations

import base64
import binascii
import functools
import re
from typing import Any
from urllib.parse import quote

_SLUG_PATH_CHUNK = re.compile(r"^s-([A-Za-z0-9_-]{1,120})$")
_SLUG_PATH_CHUNK_SIZE = 120
_MAX_STORED_PATH_CHARS = 2048


def install_gitlab_wiki_link_runtime(source_links: Any) -> None:
    marker = "_local_rag_gitlab_wiki_links_installed"
    if bool(getattr(source_links, marker, False)):
        return
    source_links._ALLOWED_PROVIDERS.add("gitlab_wiki")
    source_links.ALLOWED_SOURCE_TYPES = frozenset(
        set(source_links.ALLOWED_SOURCE_TYPES) | {"gitlab_wiki"}
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
        if provider != "gitlab_wiki":
            return original_validate(provider, strategy, settings, **kwargs)
        if strategy != "gitlab-wiki" or set(settings) != {"project_url"}:
            raise source_links.SourceLinkError(
                "GitLab Wiki links require the gitlab-wiki strategy"
            )
        project_url = source_links._required_root_url(settings.get("project_url"))
        lowered = project_url.casefold()
        if "/-/" in lowered or lowered.endswith("/-"):
            raise source_links.SourceLinkError(
                "GitLab Wiki project_url must identify a project top page"
            )
        return {"project_url": project_url}

    @functools.wraps(original_generate)
    def generate_provider_urls(
        source_link: dict[str, Any],
        stored_path: str,
    ) -> dict[str, str]:
        if str(source_link.get("provider") or "") != "gitlab_wiki":
            return original_generate(source_link, stored_path)
        path = source_links._normalize_stored_path(stored_path)
        slug = decode_gitlab_wiki_page_relative_path(
            path,
            error_type=source_links.SourceLinkError,
        )
        project_url = str((source_link.get("settings") or {})["project_url"]).rstrip("/")
        encoded = "/".join(
            quote(component, safe="-._~") for component in slug.split("/")
        )
        url = source_links._required_url(
            f"{project_url}/-/wikis/{encoded}"
        )
        return {
            "source_provider": "gitlab_wiki",
            "source_url": url,
        }

    source_links._validate_provider_settings = validate_provider_settings
    source_links._generate_provider_urls = generate_provider_urls
    setattr(source_links, marker, True)


def decode_gitlab_wiki_page_relative_path(
    value: Any,
    *,
    error_type: type[Exception] = ValueError,
) -> str:
    if not isinstance(value, str):
        raise error_type("GitLab Wiki page path is invalid")
    try:
        encoded_path = value.encode("ascii")
    except UnicodeError as exc:
        raise error_type("GitLab Wiki page path is invalid") from exc
    if (
        "\\" in value
        or len(value) > _MAX_STORED_PATH_CHARS
        or len(encoded_path) > _MAX_STORED_PATH_CHARS
    ):
        raise error_type("GitLab Wiki page path is invalid")
    components = value.split("/")
    if (
        len(components) < 4
        or components[:2] != ["wikis", "v1"]
        or components[-1] != "page.md"
    ):
        raise error_type("GitLab Wiki page path is invalid")
    chunks: list[str] = []
    middle = components[2:-1]
    for index, component in enumerate(middle):
        match = _SLUG_PATH_CHUNK.fullmatch(component)
        if match is None:
            raise error_type("GitLab Wiki page path is invalid")
        chunk = match.group(1)
        if index < len(middle) - 1 and len(chunk) != _SLUG_PATH_CHUNK_SIZE:
            raise error_type("GitLab Wiki page path is invalid")
        chunks.append(chunk)
    encoded = "".join(chunks)
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        slug = raw.decode("utf-8")
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise error_type("GitLab Wiki page path is invalid") from exc
    if (
        not slug
        or len(slug) > 4096
        or slug.startswith("/")
        or slug.endswith("/")
        or "\\" in slug
        or any(part in {"", ".", ".."} for part in slug.split("/"))
        or any(ord(character) < 0x20 for character in slug)
    ):
        raise error_type("GitLab Wiki page path is invalid")
    canonical = base64.urlsafe_b64encode(slug.encode("utf-8")).decode("ascii").rstrip("=")
    expected = ["wikis", "v1"]
    expected.extend(
        f"s-{canonical[index:index + _SLUG_PATH_CHUNK_SIZE]}"
        for index in range(0, len(canonical), _SLUG_PATH_CHUNK_SIZE)
    )
    expected.append("page.md")
    if "/".join(expected) != value:
        raise error_type("GitLab Wiki page path is not canonical")
    return slug
