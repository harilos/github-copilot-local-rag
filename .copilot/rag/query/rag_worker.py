from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


def worker_main(
    connection: Any,
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

    state = {
        "worker_pid": os.getpid(),
        "worker_generation": worker_generation,
        "model_load_count": 0,
        "open_database_count": 0,
        "handled_request_count": 0,
        "dense_warmup_state": "not_started",
        **_current_process_metrics(),
    }
    connection.send(
        {
            "op": "ready",
            "worker_generation": worker_generation,
            "worker_state": dict(state),
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
            state["dense_warmup_state"] = (
                f"error:{type(exc).__name__}"
            )
        else:
            state["dense_warmup_state"] = "ready"
            dense_ready.set()
        finally:
            dense_warmup_finished.set()

    state["dense_warmup_state"] = "starting"
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
            state["model_load_count"] = 1
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
            # Give the persistent generation one bounded chance to finish its
            # single background model load, while preserving time for lexical
            # retrieval, serialization, and client output.
            dense_warmup_finished.wait(
                max(
                    0.0,
                    min(
                        8.0 if os.name == "nt" else 6.0,
                        deadline_monotonic - time.monotonic() - 4.0,
                    ),
                )
            )
        if dense_ready.is_set() and not dense_loaded:
            dense_loaded = True
            state["model_load_count"] = 1
        started = time.monotonic()
        try:
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
        state["handled_request_count"] += 1
        coverage = result.get("coverage") or {}
        if (
            result.get("dense_used") is True
            or coverage.get("dense_discovery_used") is True
        ) and not dense_loaded:
            dense_loaded = True
            state["model_load_count"] = 1
        try:
            state["open_database_count"] = registry().cached_count
        except Exception:
            state["open_database_count"] = 0
        state["last_request_seconds"] = round(
            time.monotonic() - started,
            6,
        )
        state.update(_current_process_metrics())
        try:
            connection.send(
                {
                    "op": "response",
                    "request_id": request_id,
                    "client_id": client_id,
                    "db": db_name,
                    "result": result,
                    "worker_state": dict(state),
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
