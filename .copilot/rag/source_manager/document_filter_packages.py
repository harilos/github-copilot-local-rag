from __future__ import annotations


_MARKER = "_local_rag_document_filter_package_contract_installed"


def install_document_filter_package_contract() -> None:
    """Install late runtime extensions after the document filter wrappers.

    Product runtime is collected by denylist in ``source_manager.packages``.
    Document-filter modules and their transitive dependencies no longer need
    to mutate file allowlists at import time.  The Git provider-type extension
    intentionally installs here so it can reuse the already-installed generic
    Git and document-filter contracts without changing the public entry point.
    """

    from . import packages
    from .git_provider_types import install_git_provider_types_runtime

    install_git_provider_types_runtime()
    if bool(getattr(packages, _MARKER, False)):
        return
    setattr(packages, _MARKER, True)


__all__ = ["install_document_filter_package_contract"]
