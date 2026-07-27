from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .config import (
    DEFAULT_DOCUMENT_PREFIX,
    DEFAULT_EMBEDDING_BACKEND,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_QUANTIZATION,
    DEFAULT_QUERY_PREFIX,
    default_onnx_model_dir,
    env_int,
)

Mode = Literal["document", "query"]


class Embedder(Protocol):
    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]: ...


@dataclass
class HashEmbedder:
    dim: int = DEFAULT_EMBEDDING_DIMENSION

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


class OnnxRuntimeEmbedder:
    def __init__(self, model_dir: Path, document_prefix: str, query_prefix: str) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from transformers import AutoTokenizer
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ONNX embedding dependencies are not installed. "
                "Run python ~/.copilot/rag/query/setup.py, then prepare the model."
            ) from exc

        model_path = model_dir / "model.onnx"
        if not model_path.exists():
            raise RuntimeError(
                "ONNX INT8 embedding model is not prepared. "
                "Run python ~/.copilot/rag/query/prepare_onnx_model.py."
            )

        self._np = np
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self.max_length = env_int("EMBED_MAX_LENGTH", 384)
        options = ort.SessionOptions()
        options.intra_op_num_threads = env_int("RAG_ONNX_THREADS", env_int("OMP_NUM_THREADS", 4))
        options.inter_op_num_threads = 1
        print(f"Loading ONNX embedding model: {model_path}", file=sys.stderr)
        self._session = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self._tokenizer = _load_tokenizer(
            AutoTokenizer,
            str(model_dir),
            local_files_only=True,
        )
        self._input_names = {item.name for item in self._session.get_inputs()}
        self._output_names = [item.name for item in self._session.get_outputs()]
        print("ONNX embedding model loaded", file=sys.stderr)

    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]:
        prefix = self.document_prefix if mode == "document" else self.query_prefix
        encoded_texts = [prefix + text for text in texts]
        batch = self._tokenizer(
            encoded_texts,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="np",
        )
        inputs = {}
        for name in self._input_names:
            if name in batch:
                inputs[name] = batch[name]
            elif name == "token_type_ids" and "input_ids" in batch:
                inputs[name] = self._np.zeros_like(batch["input_ids"])
            elif name == "position_ids" and "input_ids" in batch:
                seq_len = batch["input_ids"].shape[1]
                positions = self._np.arange(seq_len, dtype=batch["input_ids"].dtype)
                inputs[name] = self._np.tile(positions, (batch["input_ids"].shape[0], 1))
        outputs = self._session.run(None, inputs)
        vectors = self._select_embedding_output(outputs, batch.get("attention_mask"))
        norms = self._np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / self._np.clip(norms, 1e-12, None)
        return vectors.astype("float32").tolist()

    def _select_embedding_output(self, outputs: list[object], attention_mask: object | None) -> object:
        for name, output in zip(self._output_names, outputs):
            if name in {"sentence_embedding", "sentence_embeddings", "pooler_output"}:
                return output
        token_embeddings = outputs[0]
        if attention_mask is None:
            return token_embeddings.mean(axis=1)
        mask = attention_mask[..., None].astype("float32")
        summed = (token_embeddings * mask).sum(axis=1)
        counts = self._np.clip(mask.sum(axis=1), 1e-9, None)
        return summed / counts


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, document_prefix: str, query_prefix: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        print(f"Loading embedding model: {model_name}", file=sys.stderr)
        self._model = SentenceTransformer(
            model_name,
            local_files_only=True,
        )
        print("Embedding model loaded", file=sys.stderr)

    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]:
        prefix = self.document_prefix if mode == "document" else self.query_prefix
        encoded_texts = [prefix + text for text in texts]
        vecs = self._model.encode(encoded_texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]


_EMBEDDER_CACHE: dict[tuple[str, str, int, str, str, str], Embedder] = {}
_EMBEDDER_CACHE_LOCK = threading.Lock()


def get_embedder() -> Embedder:
    model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    backend = os.getenv("EMBEDDING_BACKEND", DEFAULT_EMBEDDING_BACKEND).strip().lower()
    dimension = embedding_dimension()
    document_prefix = os.getenv("EMBED_DOCUMENT_PREFIX", DEFAULT_DOCUMENT_PREFIX)
    query_prefix = os.getenv("EMBED_QUERY_PREFIX", DEFAULT_QUERY_PREFIX)
    model_dir = Path(os.getenv("EMBEDDING_ONNX_DIR", str(default_onnx_model_dir()))).expanduser().resolve()
    key = (model, backend, dimension, str(model_dir), document_prefix, query_prefix)
    with _EMBEDDER_CACHE_LOCK:
        cached = _EMBEDDER_CACHE.get(key)
        if cached is not None:
            return cached
    if model == "__hash__":
        embedder: Embedder = HashEmbedder(dim=dimension)
    elif backend in {"onnx", "onnx-int8", "onnxruntime"}:
        embedder = OnnxRuntimeEmbedder(
            model_dir=model_dir,
            document_prefix=document_prefix,
            query_prefix=query_prefix,
        )
    elif backend in {"pytorch", "sentence-transformers", "sentence_transformers"}:
        embedder = SentenceTransformerEmbedder(
            model_name=model,
            document_prefix=document_prefix,
            query_prefix=query_prefix,
        )
    else:
        raise RuntimeError(f"Unsupported EMBEDDING_BACKEND: {backend}")
    with _EMBEDDER_CACHE_LOCK:
        _EMBEDDER_CACHE[key] = embedder
    return embedder


def _load_tokenizer(
    auto_tokenizer: object,
    model: str,
    *,
    local_files_only: bool = True,
) -> object:
    try:
        return auto_tokenizer.from_pretrained(  # type: ignore[attr-defined]
            model,
            fix_mistral_regex=True,
            local_files_only=local_files_only,
        )
    except TypeError:
        try:
            return auto_tokenizer.from_pretrained(  # type: ignore[attr-defined]
                model,
                fix_mistral_regex=False,
                local_files_only=local_files_only,
            )
        except TypeError:
            return auto_tokenizer.from_pretrained(  # type: ignore[attr-defined]
                model,
                local_files_only=local_files_only,
            )


def embedding_dimension() -> int:
    return env_int("EMBEDDING_DIMENSION", DEFAULT_EMBEDDING_DIMENSION)


def embedding_backend() -> str:
    model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    if model == "__hash__":
        return "hash"
    return os.getenv("EMBEDDING_BACKEND", DEFAULT_EMBEDDING_BACKEND).strip().lower()


def embedding_fingerprint() -> dict[str, str | int]:
    backend = embedding_backend()
    return {
        "embedding_model": os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
        "embedding_dimension": embedding_dimension(),
        "embedding_backend": backend,
        "quantization": os.getenv("EMBEDDING_QUANTIZATION", DEFAULT_QUANTIZATION if "onnx" in backend else "none"),
        "document_prefix": os.getenv("EMBED_DOCUMENT_PREFIX", DEFAULT_DOCUMENT_PREFIX),
        "query_prefix": os.getenv("EMBED_QUERY_PREFIX", DEFAULT_QUERY_PREFIX),
    }
