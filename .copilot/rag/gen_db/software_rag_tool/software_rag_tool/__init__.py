"""MIT-licensed Japanese-first software RAG implementation."""

from . import source_links as _source_links
from .document_extensions import install_document_extension_runtime
from .gitlab_wiki_links import install_gitlab_wiki_link_runtime


_source_links.ALLOWED_SOURCE_TYPES = frozenset(
    set(_source_links.ALLOWED_SOURCE_TYPES) | {"teams", "gitlab_wiki"}
)
install_gitlab_wiki_link_runtime(_source_links)
install_document_extension_runtime()

__all__ = ["__version__"]

__version__ = "0.1.0"
