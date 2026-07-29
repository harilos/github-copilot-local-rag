"""MIT-licensed Japanese-first software RAG implementation."""

from . import source_links as _source_links


_source_links.ALLOWED_SOURCE_TYPES = frozenset(
    set(_source_links.ALLOWED_SOURCE_TYPES) | {"teams"}
)

__all__ = ["__version__"]

__version__ = "0.1.0"
