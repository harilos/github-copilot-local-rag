from __future__ import annotations

import copy
import functools
import sys
from typing import Any

from .git_host_urls import GIT_SOURCE_TYPES, SOURCE_LABELS

_HOOK_MARKER = "_local_rag_git_host_ui_fix_hook_installed"
_CLASS_MARKER = "_local_rag_git_host_ui_fix_class_installed"
_ACTUAL_TYPE = "_git_host_actual_source_type"


def install_git_host_ui_fix_runtime() -> None:
    """Keep the selected Git service visible inside the shared Git editor."""

    from . import manager_connections

    if bool(getattr(manager_connections, _HOOK_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_ui(manager_class)

    manager_connections.install_manager_connection_ui = (
        install_manager_connection_ui
    )
    setattr(manager_connections, _HOOK_MARKER, True)


def _install_manager_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _CLASS_MARKER, False)):
        return
    module = sys.modules.get(manager_class.__module__)
    original_show = manager_class._show_source_fetch_settings
    original_edit = manager_class._edit_source_fetch_settings

    @functools.wraps(original_show)
    def show_source_fetch_settings(
        self: Any,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        actual = str(
            source.get(_ACTUAL_TYPE)
            or source.get("source_type")
            or ""
        ).strip().lower()
        if actual not in GIT_SOURCE_TYPES:
            return original_show(self, source)
        display = copy.deepcopy(source)
        display.pop(_ACTUAL_TYPE, None)
        display["source_type"] = actual
        labels = (
            getattr(module, "_PROVIDER_JA", None)
            if module is not None
            else None
        )
        previous = labels.get("github") if isinstance(labels, dict) else None
        if isinstance(labels, dict):
            labels["github"] = SOURCE_LABELS[actual]
        try:
            return original_show(self, display)
        finally:
            if isinstance(labels, dict):
                if previous is None:
                    labels.pop("github", None)
                else:
                    labels["github"] = previous

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        actual = str(source.get("source_type") or "").strip().lower()
        if actual not in GIT_SOURCE_TYPES:
            return original_edit(self, db_name, source)
        display = copy.deepcopy(source)
        display[_ACTUAL_TYPE] = actual
        return original_edit(self, db_name, display)

    manager_class._show_source_fetch_settings = show_source_fetch_settings
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    setattr(manager_class, _CLASS_MARKER, True)


__all__ = ["install_git_host_ui_fix_runtime"]
