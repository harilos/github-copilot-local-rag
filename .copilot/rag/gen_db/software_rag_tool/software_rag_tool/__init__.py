"""MIT-licensed Japanese-first software RAG implementation."""

from . import source_links as _source_links
from .document_extensions import install_document_extension_runtime


_source_links.ALLOWED_SOURCE_TYPES = frozenset(
    set(_source_links.ALLOWED_SOURCE_TYPES) | {"teams"}
)
install_document_extension_runtime()

__all__ = ["__version__"]

__version__ = "0.1.0"
