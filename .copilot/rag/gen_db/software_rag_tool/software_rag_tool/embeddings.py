from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .config import (
    DEFAULT_DOCUMENT_PREFIX,
    DEFAULT_EMBEDDING_BACKEND,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MAX_TOKENS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_QUANTIZATION,
    DEFAULT_QUERY_PREFIX,
    default_onnx_model_dir,
    env_int,
)

Mode = Literal["document", "query"]


class DocumentEmbeddingTokenLimitError(RuntimeError):
    """Raised instead of silently truncating a document embedding input."""


@dataclass(frozen=True)
class DocumentTokenBudget:
    tokenizer: Any
    document_prefix: str
    tokenizer_name: str
    target_tokens: int = 320
    max_tokens: int = DEFAULT_EMBEDDING_MAX_TOKENS

    def document_input(self, path: str, title: str, text: str) -> str:
        return self.document_prefix + f"{path}\n{title}\n{text}"

    def count_document(self, path: str, title: str, text: str) -> int:
        return _token_count(
            self.tokenizer,
            self.document_input(path, title, text),
            add_special_tokens=True,
        )

    def count_embedding_text(self, text: str) -> int:
        return _token_count(
            self.tokenizer,
            self.document_prefix + text,
            add_special_tokens=True,
        )

    def count_body(self, text: str) -> int:
        return _token_count(
            self.tokenizer,
            text,
            add_special_tokens=False,
        )


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
        self.max_length = document_embedding_max_tokens()
        options = ort.SessionOptions()
        options.intra_op_num_threads = env_int("RAG_ONNX_THREADS", env_int("OMP_NUM_THREADS", 4))
        options.inter_op_num_threads = 1
        print(f"Loading ONNX embedding model: {model_path}", file=sys.stderr)
        self._session = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self._tokenizer = get_embedding_tokenizer()
        self._input_names = {item.name for item in self._session.get_inputs()}
        self._output_names = [item.name for item in self._session.get_outputs()]
        print("ONNX embedding model loaded", file=sys.stderr)

    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]:
        prefix = self.document_prefix if mode == "document" else self.query_prefix
        encoded_texts = [prefix + text for text in texts]
        if mode == "document":
            _validate_document_inputs(
                self._tokenizer,
                encoded_texts,
                max_tokens=self.max_length,
            )
            batch = self._tokenizer(
                encoded_texts,
                padding=True,
                truncation=False,
                return_tensors="np",
            )
        else:
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
        tokenizer = get_embedding_tokenizer()
        self._model.tokenizer = tokenizer
        self._tokenizer = tokenizer
        self.max_length = document_embedding_max_tokens()
        self._model.max_seq_length = min(
            int(self._model.max_seq_length),
            self.max_length,
        )
        print("Embedding model loaded", file=sys.stderr)

    def encode(self, texts: list[str], mode: Mode) -> list[list[float]]:
        prefix = self.document_prefix if mode == "document" else self.query_prefix
        encoded_texts = [prefix + text for text in texts]
        if mode == "document":
            _validate_document_inputs(
                self._tokenizer,
                encoded_texts,
                max_tokens=self.max_length,
            )
        vecs = self._model.encode(encoded_texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]


_EMBEDDER_CACHE: dict[tuple[str, str, int, str, str, str], Embedder] = {}
_EMBEDDER_CACHE_LOCK = threading.Lock()
_TOKENIZER_CACHE: dict[tuple[str, str, str], Any] = {}
_TOKENIZER_CACHE_LOCK = threading.Lock()


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


def get_embedding_tokenizer() -> Any:
    model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    backend = os.getenv("EMBEDDING_BACKEND", DEFAULT_EMBEDDING_BACKEND).strip().lower()
    model_dir = Path(
        os.getenv("EMBEDDING_ONNX_DIR", str(default_onnx_model_dir()))
    ).expanduser().resolve()
    if model == "__hash__":
        raise RuntimeError(
            "Hash embeddings do not provide a production tokenizer. "
            "Inject a DocumentTokenBudget explicitly in tests."
        )
    tokenizer_source = str(model_dir) if backend in {"onnx", "onnx-int8", "onnxruntime"} else model
    key = (model, backend, tokenizer_source)
    with _TOKENIZER_CACHE_LOCK:
        cached = _TOKENIZER_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        from transformers import AutoTokenizer
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The embedding tokenizer is required for token-safe ingestion. "
            "Run python ~/.copilot/rag/query/setup.py."
        ) from exc
    tokenizer = _load_tokenizer(
        AutoTokenizer,
        tokenizer_source,
        local_files_only=True,
    )
    with _TOKENIZER_CACHE_LOCK:
        existing = _TOKENIZER_CACHE.setdefault(key, tokenizer)
    return existing


def get_document_token_budget(*, tokenizer: Any | None = None) -> DocumentTokenBudget:
    max_tokens = document_embedding_max_tokens()
    target_tokens = min(320, max_tokens)
    model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    backend = os.getenv("EMBEDDING_BACKEND", DEFAULT_EMBEDDING_BACKEND).strip().lower()
    if tokenizer is None:
        tokenizer = get_embedding_tokenizer()
    return DocumentTokenBudget(
        tokenizer=tokenizer,
        document_prefix=os.getenv("EMBED_DOCUMENT_PREFIX", DEFAULT_DOCUMENT_PREFIX),
        tokenizer_name=f"{model}:{backend}",
        target_tokens=target_tokens,
        max_tokens=max_tokens,
    )


def document_embedding_max_tokens() -> int:
    configured = env_int("EMBED_MAX_LENGTH", DEFAULT_EMBEDDING_MAX_TOKENS)
    if configured <= 0:
        raise ValueError("EMBED_MAX_LENGTH must be positive")
    return min(configured, DEFAULT_EMBEDDING_MAX_TOKENS)


def _token_count(tokenizer: Any, text: str, *, add_special_tokens: bool) -> int:
    encoded = tokenizer(
        text,
        add_special_tokens=add_special_tokens,
        truncation=False,
        padding=False,
    )
    input_ids = encoded["input_ids"]
    if input_ids and isinstance(input_ids[0], (list, tuple)):
        if len(input_ids) != 1:
            raise ValueError("token count expects exactly one input")
        input_ids = input_ids[0]
    return len(input_ids)


def _validate_document_inputs(
    tokenizer: Any,
    texts: list[str],
    *,
    max_tokens: int,
) -> None:
    oversized = [
        (index, _token_count(tokenizer, text, add_special_tokens=True))
        for index, text in enumerate(texts)
    ]
    oversized = [item for item in oversized if item[1] > max_tokens]
    if oversized:
        details = ", ".join(
            f"index={index} tokens={count}"
            for index, count in oversized[:5]
        )
        raise DocumentEmbeddingTokenLimitError(
            "document embedding input exceeds the hard token limit; "
            "silent truncation is disabled: "
            f"limit={max_tokens} {details}"
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
