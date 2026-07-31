from __future__ import annotations


_MARKER = "_local_rag_document_filter_package_contract_installed"


def install_document_filter_package_contract() -> None:
    """Install late runtime extensions after document-filter wrappers.

    Product runtime is collected by denylist in ``source_manager.packages``.
    Git provider types intentionally install here so GitHub, GitLab,
    Azure DevOps, and other Git repositories can reuse the common sparse/date
    filter and document-only contracts without depending on another provider.
    """

    from . import packages
    from .git_host_sources import install_git_host_source_runtime
    from .git_host_ui_fix import install_git_host_ui_fix_runtime

    install_git_host_source_runtime()
    install_git_host_ui_fix_runtime()
    if bool(getattr(packages, _MARKER, False)):
        return
    setattr(packages, _MARKER, True)


__all__ = ["install_document_filter_package_contract"]
