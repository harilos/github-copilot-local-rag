from __future__ import annotations


_MARKER = "_local_rag_document_filter_package_contract_installed"


def install_document_filter_package_contract() -> None:
    """Keep the legacy installer hook idempotent.

    Product runtime is collected by denylist in ``source_manager.packages``.
    Document-filter modules and their transitive dependencies no longer need
    to mutate file allowlists at import time.
    """

    from . import packages

    if bool(getattr(packages, _MARKER, False)):
        return
    setattr(packages, _MARKER, True)


__all__ = ["install_document_filter_package_contract"]
