from __future__ import annotations

import copy
import functools
from typing import Any


_MARKER = "_local_rag_git_provider_link_types_installed"


def install_git_provider_link_runtime(source_links: Any) -> None:
    """Accept persisted Git Source types while reusing Azure's link engine."""

    if bool(getattr(source_links, _MARKER, False)):
        return

    source_links.ALLOWED_SOURCE_TYPES = frozenset(
        set(source_links.ALLOWED_SOURCE_TYPES)
        | {"azure-devops", "other-git"}
    )
    source_links._ALLOWED_PROVIDERS.add("azure-devops")
    original = source_links.validate_source_link

    @functools.wraps(original)
    def validate_source_link(
        link: Any,
        *,
        allow_legacy_provider_settings: bool = False,
    ) -> dict[str, Any]:
        translated = copy.deepcopy(link)
        if isinstance(translated, dict):
            if (
                str(translated.get("provider") or "").strip().lower()
                == "azure-devops"
            ):
                translated["provider"] = "azure_devops"
            if (
                str(translated.get("source_type") or "").strip().lower()
                == "azure-devops"
            ):
                translated["source_type"] = "azure_devops"
        return dict(
            original(
                translated,
                allow_legacy_provider_settings=(
                    allow_legacy_provider_settings
                ),
            )
        )

    source_links.validate_source_link = validate_source_link
    setattr(source_links, _MARKER, True)


__all__ = ["install_git_provider_link_runtime"]
