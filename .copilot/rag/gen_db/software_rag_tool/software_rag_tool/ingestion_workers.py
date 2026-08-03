from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .structured_extraction import (
    ExtractionResult,
    _get_docling_converter,
    docling_available,
    docling_pdf_artifacts_path,
    extract_document_structure,
)


DOCLING_WORKER_EXTENSIONS = frozenset({".pdf", ".docx", ".pptx"})
_WORKER_INIT_ERROR = ""
DEFAULT_FILE_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class WorkerPlan:
    workers: int
    threads_per_worker: int
    logical_cpus: int
    available_memory_bytes: int
    reason: str


def choose_worker_plan(
    candidate_count: int,
    *,
    requested_workers: int | None = None,
) -> WorkerPlan:
    logical = max(1, os.cpu_count() or 1)
    available_memory = _available_memory_bytes()
    requested = requested_workers
    if requested is None:
        raw = str(os.getenv("RAG_INGEST_WORKERS") or "").strip()
        requested = int(raw) if raw else None
    if requested is not None and requested <= 0:
        raise ValueError("ingestion worker count must be positive")
    if candidate_count < 2 or not docling_available():
        workers = 1
        reason = (
            "docling_unavailable"
            if not docling_available()
            else "single_candidate"
        )
    elif requested is not None:
        workers = min(requested, candidate_count, 4)
        reason = "explicit"
    else:
        cpu_limit = max(1, logical // 4)
        memory_limit = max(1, available_memory // (3 * 1024**3))
        workers = min(2, candidate_count, cpu_limit, memory_limit, 4)
        reason = "safe_auto"
    threads = max(1, logical // max(1, workers))
    return WorkerPlan(
        workers=max(1, workers),
        threads_per_worker=threads,
        logical_cpus=logical,
        available_memory_bytes=available_memory,
        reason=reason,
    )


class DoclingExtractionPool:
    """A bounded spawn pool whose children never receive production stores."""

    def __init__(
        self,
        plan: WorkerPlan,
        *,
        allow_docling: bool = True,
        file_timeout_seconds: float | None = None,
    ) -> None:
        self.plan = plan
        self.allow_docling = allow_docling
        raw_timeout = (
            file_timeout_seconds
            if file_timeout_seconds is not None
            else float(
                os.getenv(
                    "RAG_DOCLING_FILE_TIMEOUT_SECONDS",
                    str(DEFAULT_FILE_TIMEOUT_SECONDS),
                )
            )
        )
        if raw_timeout <= 0:
            raise ValueError("Docling file timeout must be positive")
        self.file_timeout_seconds = raw_timeout
        self._executor: ProcessPoolExecutor | None = None
        self._failed_reason = ""

    @property
    def enabled(self) -> bool:
        # Keep Docling/Torch native libraries out of the parent that owns
        # ONNX, Chroma, and SQLite even for a single Office document.
        return (
            self.allow_docling
            and self.plan.workers >= 1
            and docling_available()
        )

    @property
    def group_size(self) -> int:
        return max(1, self.plan.workers * 2)

    def __enter__(self) -> "DoclingExtractionPool":
        if self.enabled:
            context = multiprocessing.get_context("spawn")
            self._executor = ProcessPoolExecutor(
                max_workers=self.plan.workers,
                mp_context=context,
                initializer=_initialize_worker,
                initargs=(self.plan.threads_per_worker,),
            )
        return self

    def extract(
        self,
        paths: Iterable[Path],
        *,
        backend_policy: str = "auto",
    ) -> dict[Path, ExtractionResult]:
        values = [Path(path) for path in paths]
        if not values:
            return {}
        if self._failed_reason:
            return {
                path: _worker_failure_result(path, self._failed_reason)
                for path in values
            }
        if self._executor is None:
            return {
                path: extract_document_structure(
                    path,
                    backend_policy=backend_policy,
                    docling_threads=self.plan.threads_per_worker,
                )
                for path in values
            }
        futures = [
            self._executor.submit(
                _extract_worker,
                str(path),
                backend_policy,
            )
            for path in values
        ]
        # Preserve canonical input order even when workers finish out of order.
        output: dict[Path, ExtractionResult] = {}
        for index, (path, future) in enumerate(zip(values, futures)):
            try:
                output[path] = future.result(
                    timeout=self.file_timeout_seconds
                )
            except FutureTimeoutError:
                reason = "docling_worker_timeout"
                self._abort_executor(reason=reason)
                for pending_path in values[index:]:
                    output[pending_path] = _worker_failure_result(
                        pending_path,
                        reason,
                    )
                break
            except Exception as exc:
                reason = f"docling_worker_{type(exc).__name__}"
                self._abort_executor(reason=reason)
                for pending_path in values[index:]:
                    output[pending_path] = _worker_failure_result(
                        pending_path,
                        reason,
                    )
                break
        return output

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc, traceback
        if self._executor is not None:
            if exc_type is None:
                self._executor.shutdown(wait=True, cancel_futures=True)
                self._executor = None
            else:
                self._abort_executor(reason="docling_worker_interrupted")

    def _abort_executor(self, *, reason: str) -> None:
        self._failed_reason = reason
        executor = self._executor
        if executor is None:
            return
        processes = list(
            (getattr(executor, "_processes", None) or {}).values()
        )
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        executor.shutdown(wait=False, cancel_futures=True)
        self._executor = None


def is_docling_worker_candidate(path: Path) -> bool:
    extension = Path(path).suffix.lower()
    if extension == ".pdf" and docling_pdf_artifacts_path() is None:
        return False
    return extension in DOCLING_WORKER_EXTENSIONS


def _initialize_worker(threads: int) -> None:
    global _WORKER_INIT_ERROR
    os.environ["OMP_NUM_THREADS"] = str(max(1, threads))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        _get_docling_converter(threads=max(1, threads))
    except Exception as exc:
        _WORKER_INIT_ERROR = f"worker_init_{type(exc).__name__}"


def _extract_worker(path: str, backend_policy: str) -> ExtractionResult:
    if _WORKER_INIT_ERROR:
        if backend_policy == "auto":
            fallback = extract_document_structure(
                Path(path),
                backend_policy="legacy",
            )
            if fallback.is_indexed:
                return replace(
                    fallback,
                    structure_origin="legacy_fallback",
                    fallback_reason=_WORKER_INIT_ERROR,
                )
        return ExtractionResult(
            status="extract_error",
            backend="docling",
            backend_version="unavailable",
            source_format=Path(path).suffix.lower().lstrip("."),
            structure_origin="docling_tree",
            retryable=True,
            reason=_WORKER_INIT_ERROR,
        )
    return extract_document_structure(
        Path(path),
        backend_policy=backend_policy,
        docling_threads=max(1, int(os.getenv("OMP_NUM_THREADS", "1"))),
    )


def _worker_failure_result(path: Path, reason: str) -> ExtractionResult:
    return ExtractionResult(
        status="extract_error",
        backend="docling",
        backend_version="unavailable",
        source_format=path.suffix.lower().lstrip("."),
        structure_origin="docling_tree",
        retryable=True,
        reason=reason,
    )


def _available_memory_bytes() -> int:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        return max(0, page_size * pages)
    except (AttributeError, OSError, TypeError, ValueError):
        # Conservative Windows/no-sysconf fallback; the auto plan remains at 1.
        return 3 * 1024**3
