from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from typing import Literal, Protocol

Mode = Literal["document", "query"]


class Embedder(Protocol):
    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]: ...


@dataclass
class HashEmbedder:
    dim: int = 384

    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]:
        output: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(f"{mode}:{text}".encode("utf-8", errors="replace")).digest()
            values = list(digest)
            reps = math.ceil(self.dim / len(values))
            vec = [float(value) for value in (values * reps)[: self.dim]]
            norm = math.sqrt(sum(value * value for value in vec))
            if norm:
                vec = [value / norm for value in vec]
            output.append(vec)
        return output


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, document_prefix: str, query_prefix: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        print(f"Loading embedding model: {model_name}")
        self._model = SentenceTransformer(model_name)
        print("Embedding model loaded")

    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]:
        prefix = self.document_prefix if mode == "document" else self.query_prefix
        encoded_texts = [prefix + text for text in texts]
        vecs = self._model.encode(encoded_texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]


def get_embedder() -> Embedder:
    model = os.getenv("EMBEDDING_MODEL", "cl-nagoya/ruri-v3-130m").strip()
    if model == "__hash__":
        return HashEmbedder()
    return SentenceTransformerEmbedder(
        model_name=model,
        document_prefix=os.getenv("EMBED_DOCUMENT_PREFIX", "検索文書: "),
        query_prefix=os.getenv("EMBED_QUERY_PREFIX", "検索クエリ: "),
    )
