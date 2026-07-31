from __future__ import annotations

import copy
import functools
from typing import Any


_MARKER = "_local_rag_git_provider_link_types_installed"


def install_git_provider_link_runtime(source_links: Any) -> None:
    """Add exact Git Source type names to the Source Link contract."""

    if bool(getattr(source_links, _MARKER, False)):
        return

    source_links.ALLOWED_SOURCE_TYPES = frozenset(
        set(source_links.ALLOWED_SOURCE_TYPES)
        | {"azure-devops", "other-git"}
    )
    source_links._ALLOWED_PROVIDERS.add("azure-devops")
    source_links._GIT_PROVIDER_STRATEGIES[
        "azure-devops"
    ] = "azure-devops-item"
    original_generate = source_links._generate_provider_urls

    @functools.wraps(original_generate)
    def generate_provider_urls(
        source_link: dict[str, Any],
        stored_path: str,
    ) -> dict[str, str]:
        value = copy.deepcopy(source_link)
        if (
            isinstance(value, dict)
            and str(value.get("provider") or "").strip().lower()
            == "azure-devops"
        ):
            value["provider"] = "azure_devops"
        return dict(original_generate(value, stored_path))

    source_links._generate_provider_urls = generate_provider_urls
    setattr(source_links, _MARKER, True)


__all__ = ["install_git_provider_link_runtime"]
