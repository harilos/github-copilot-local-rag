from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def _wrapper_handoff_environment(enabled: bool):
    """Expose the private handoff bit only while one worker request runs."""
    previous = os.environ.get("LOCAL_RAG_WRAPPER_INTERNAL")
    if enabled:
        os.environ["LOCAL_RAG_WRAPPER_INTERNAL"] = "1"
    else:
        os.environ.pop("LOCAL_RAG_WRAPPER_INTERNAL", None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LOCAL_RAG_WRAPPER_INTERNAL", None)
        else:
            os.environ["LOCAL_RAG_WRAPPER_INTERNAL"] = previous


def _final_dense_loaded(
    dense_loaded: bool,
    dense_ready: threading.Event,
) -> bool:
    # Warmup may complete after the request's initial readiness check but
    # before its response snapshot is built. Read the terminal Event again so
    # model readiness can never regress from ready/1 to ready/0.
    return bool(dense_loaded or dense_ready.is_set())


def _dense_warmup_wait_seconds(
    deadline_monotonic: float | None,
    *,
    now_monotonic: float | None = None,
) -> float:
    """Reserve four seconds for result serialization after cold warmup."""
    if deadline_monotonic is None:
        return 0.0
    now = time.monotonic() if now_monotonic is None else now_monotonic
    return max(0.0, deadline_monotonic - now - 4.0)


def worker_main(
    connection: Any,
    status_connection: Any,
    *,
    rag_root: str,
    dbs_root: str,
    worker_generation: str,
) -> None:
    root = Path(rag_root).resolve()
    tool_root = root / "gen_db" / "software_rag_tool"
    sys.path.insert(0, str(tool_root))
    os.environ["RAG_DBS_ROOT"] = str(Path(dbs_root).resolve())
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    from rag_manager import _current_process_metrics

    state_lock = threading.Lock()
    state_revision = 1
    state = {
        "worker_pid": os.getpid(),
        "worker_generation": worker_generation,
        "model_load_count": 0,
        "open_database_count": 0,
        "handled_request_count": 0,
        "dense_warmup_state": "starting",
        "state_revision": state_revision,
        **_current_process_metrics(),
    }

    def snapshot_state() -> dict[str, Any]:
        with state_lock:
            return dict(state)

    def update_state(**updates: Any) -> dict[str, Any]:
        nonlocal state_revision
        with state_lock:
            state.update(updates)
            state_revision += 1
            state["state_revision"] = state_revision
            return dict(state)

    connection.send(
        {
            "op": "ready",
            "worker_generation": worker_generation,
            "worker_state": snapshot_state(),
        }
    )

    # Heavy native/runtime imports are intentionally child-only.
    from software_rag_tool.search_api import (
        compact_search_contract,
        normalize_search_contract,
        registry,
        run_adaptive_search_payload,
        run_search_payload,
    )

    dense_loaded = False
    dense_ready = threading.Event()
    dense_warmup_finished = threading.Event()

    def warm_dense_runtime() -> None:
        try:
            from software_rag_tool.embeddings import get_embedder

            get_embedder().encode(["Local RAG warmup"], mode="query")
        except Exception as exc:
            terminal_state = update_state(
                dense_warmup_state=f"error:{type(exc).__name__}",
                model_load_count=0,
            )
        else:
            # Publish the Event before creating the terminal snapshot. A
            # concurrent search response re-reads this Event, so it cannot
            # create a newer ready/model_load_count=0 revision between these
            # operations.
            dense_ready.set()
            terminal_state = update_state(
                dense_warmup_state="ready",
                model_load_count=1,
            )
        finally:
            dense_warmup_finished.set()
        try:
            # This dedicated one-way channel has exactly one writer: the
            # background warmup thread. Search responses continue to use the
            # request/response Pipe only from the worker main thread.
            status_connection.send(
                {
                    "op": "worker_status",
                    "worker_generation": worker_generation,
                    "state_revision": terminal_state["state_revision"],
                    "worker_state": terminal_state,
                }
            )
        except (BrokenPipeError, EOFError, OSError, ValueError):
            pass
    threading.Thread(
        target=warm_dense_runtime,
        name="rag-dense-warmup",
        daemon=True,
    ).start()

    while True:
        try:
            message = connection.recv()
        except (EOFError, OSError):
            registry().close()
            return
        operation = str(message.get("op") or "")
        if operation == "shutdown":
            registry().close()
            return
        if operation != "search":
            continue
        request_id = str(message.get("request_id") or "")
        client_id = str(message.get("client_id") or "")
        db_name = str(message.get("db") or "")
        payload = dict(message.get("payload") or {})
        if dense_ready.is_set() and not dense_loaded:
            dense_loaded = True
        remaining_deadline_ms = max(
            0,
            int(message.get("remaining_deadline_ms") or 0),
        )
        try:
            deadline_monotonic = float(message["deadline_monotonic"])
        except (KeyError, TypeError, ValueError):
            deadline_monotonic = (
                time.monotonic() + (remaining_deadline_ms / 1000.0)
                if remaining_deadline_ms
                else None
            )
        if (
            not dense_warmup_finished.is_set()
            and deadline_monotonic is not None
        ):
            # The first request owns the caller's full remaining deadline.
            # Wait for the generation's one background model load instead of
            # falling into lexical-only after an arbitrary platform cap.  A
            # small reserve remains for retrieval serialization and output.
            dense_warmup_finished.wait(
                _dense_warmup_wait_seconds(deadline_monotonic)
            )
        if dense_ready.is_set() and not dense_loaded:
            dense_loaded = True
        started = time.monotonic()
        try:
            with _wrapper_handoff_environment(
                bool(payload.get("_wrapper_private_handoff"))
            ):
                result = _execute_search_payload(
                    payload,
                    run_adaptive_search_payload=run_adaptive_search_payload,
                    run_search_payload=run_search_payload,
                    deadline_monotonic=deadline_monotonic,
                    dense_runtime_ready=dense_loaded,
                )
            result = normalize_search_contract(result)
            if payload.get("compact_json"):
                result = compact_search_contract(
                    result,
                    explain=bool(payload.get("explain")),
                )
        except Exception as exc:
            result = normalize_search_contract(
                {
                    "schema": "local-rag.search.v1",
                    "status": "error",
                    "db": db_name,
                    "query": str(payload.get("question") or ""),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        coverage = result.get("coverage") or {}
        if (
            result.get("dense_used") is True
            or coverage.get("dense_discovery_used") is True
        ) and not dense_loaded:
            dense_loaded = True
        dense_loaded = _final_dense_loaded(
            dense_loaded,
            dense_ready,
        )
        try:
            open_database_count = registry().cached_count
        except Exception:
            open_database_count = 0
        with state_lock:
            handled_request_count = int(
                state.get("handled_request_count") or 0
            ) + 1
        response_state = update_state(
            handled_request_count=handled_request_count,
            model_load_count=1 if dense_loaded else 0,
            open_database_count=open_database_count,
            last_request_seconds=round(
                time.monotonic() - started,
                6,
            ),
            **_current_process_metrics(),
        )
        try:
            connection.send(
                {
                    "op": "response",
                    "request_id": request_id,
                    "client_id": client_id,
                    "db": db_name,
                    "result": result,
                    "worker_state": response_state,
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            registry().close()
            return


def _execute_search_payload(
    payload: dict[str, Any],
    *,
    run_adaptive_search_payload: Any,
    run_search_payload: Any,
    deadline_monotonic: float | None,
    dense_runtime_ready: bool,
) -> dict[str, Any]:
    retrieval_mode = str(payload.get("retrieval_mode") or "hybrid")
    common = {
        "db_name": str(payload["db"]),
        "question": str(payload["question"]),
        "top_k": int(payload.get("top_k") or 8),
        "source": str(payload.get("source") or "any"),
        "max_chars": int(payload.get("max_chars") or 900),
        "budget_tokens": (
            int(payload["budget_tokens"])
            if payload.get("budget_tokens")
            else None
        ),
        "explain": bool(payload.get("explain")),
        "include_db_hint": bool(payload.get("include_db_hint")),
        "identifier_diagnostics": bool(
            payload.get("identifier_diagnostics", True)
        ),
        "search_request": payload.get("search_request"),
        "deadline_monotonic": deadline_monotonic,
        "dense_runtime_ready": dense_runtime_ready,
    }
    if retrieval_mode in {"hybrid", "dense"} and not dense_runtime_ready:
        result = run_search_payload(
            **common,
            retrieval_mode="lexical",
        )
        warnings = list(result.get("warnings") or [])
        warnings.append("dense_discovery_unavailable_within_deadline")
        result["warnings"] = sorted(set(warnings))
        result["retrieval_mode"] = retrieval_mode
        result["retrieval_route"] = (
            "persistent_lexical_while_dense_warming"
        )
        result["dense_used"] = False
        result["dense_skipped_reason"] = (
            "background_dense_warmup_incomplete"
        )
        lane_health = dict(result.get("lane_health") or {})
        lane_health["dense"] = {
            "attempted": False,
            "succeeded": False,
            "error": None,
            "skipped_reason": "background_dense_warmup_incomplete",
        }
        result["lane_health"] = lane_health
        return result
    if (
        bool(payload.get("adaptive_hybrid"))
        and retrieval_mode == "hybrid"
    ):
        return run_adaptive_search_payload(**common)
    return run_search_payload(
        **common,
        retrieval_mode=retrieval_mode,
    )
