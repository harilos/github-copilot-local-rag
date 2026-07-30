from __future__ import annotations


_MARKER = "_local_rag_document_filter_package_contract_installed"


def install_document_filter_package_contract() -> None:
    """Keep document-filter runtime files in generated copy-only packages."""

    from . import packages

    if bool(getattr(packages, _MARKER, False)):
        return
    packages._DISTRIBUTION_TOOL_MODULES = frozenset(
        set(packages._DISTRIBUTION_TOOL_MODULES) | {"document_extensions.py"}
    )
    packages._ADMIN_GEN_DB_FILES = frozenset(
        set(packages._ADMIN_GEN_DB_FILES) | {"add_data_documents_only.py"}
    )
    setattr(packages, _MARKER, True)


__all__ = ["install_document_filter_package_contract"]
