from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, TypeVar

from .errors import SourceManagerError
from .providers import REDMINE_BATCH_SIZE, FetchPlan
from .security import validate_persistable


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    retry_statuses: frozenset[int]
    retry_error_kinds: frozenset[str]

    def should_retry(
        self,
        *,
        attempt: int,
        status_code: int | None = None,
        error_kind: str | None = None,
    ) -> bool:
        if attempt >= self.max_attempts:
            return False
        return (
            status_code in self.retry_statuses
            or str(error_kind or "") in self.retry_error_kinds
        )


REDMINE_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    retry_statuses=frozenset({429, 502, 503, 504}),
    retry_error_kinds=frozenset(
        {"connection_error", "connection_timeout", "read_timeout"}
    ),
)

_T = TypeVar("_T")


def redmine_batches(values: Sequence[_T] | Iterable[_T]) -> list[list[_T]]:
    items = list(values)
    return [
        items[index : index + REDMINE_BATCH_SIZE]
        for index in range(0, len(items), REDMINE_BATCH_SIZE)
    ]


def new_run_state(
    plan: FetchPlan,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    now = _now()
    state = {
        "schema_version": "local-rag-source-state-v1",
        "local_source_key": plan.source_key,
        "provider": plan.provider,
        "run_id": run_id or str(uuid.uuid4()),
        "plan_etag": plan.plan_etag,
        "status": "planned",
        "operation": "update",
        "phase": "fetch",
        "started_at": now,
        "last_completed_item": None,
        "fetched_count": 0,
        "indexed_confirmed_count": 0,
        "pending_count": 0,
        "can_resume": True,
        "metadata_sync_pending": False,
        "last_error": None,
        "checkpoints": {
            step.step_id: {
                "status": "pending",
                "cursor": None,
                "completed_count": 0,
                "attempt": 0,
            }
            for step in plan.steps
        },
        "updated_at": now,
    }
    validate_persistable(state, field="state")
    return state


def advance_checkpoint(
    state: dict[str, Any],
    step_id: str,
    *,
    cursor: str | int | None,
    completed_count: int,
) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    checkpoint = _checkpoint(updated, step_id)
    previous = int(checkpoint.get("completed_count") or 0)
    if completed_count < previous:
        raise SourceManagerError("checkpoint cannot move backwards")
    checkpoint.update(
        {
            "status": "running",
            "cursor": cursor,
            "completed_count": int(completed_count),
            "attempt": 0,
        }
    )
    updated["status"] = "running"
    updated["updated_at"] = _now()
    validate_persistable(updated, field="state")
    return updated


def record_retry(
    state: dict[str, Any],
    step_id: str,
    *,
    error_kind: str,
    status_code: int | None = None,
) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    checkpoint = _checkpoint(updated, step_id)
    attempt = int(checkpoint.get("attempt") or 0) + 1
    if not REDMINE_RETRY_POLICY.should_retry(
        attempt=attempt,
        status_code=status_code,
        error_kind=error_kind,
    ):
        checkpoint["status"] = "failed"
        updated["status"] = "failed"
    else:
        checkpoint["status"] = "retry_pending"
        updated["status"] = "running"
    checkpoint["attempt"] = attempt
    checkpoint["last_error_kind"] = str(error_kind)[:100]
    if status_code is not None:
        checkpoint["last_status_code"] = int(status_code)
    updated["updated_at"] = _now()
    validate_persistable(updated, field="state")
    return updated


def complete_run(state: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    for checkpoint in updated.get("checkpoints", {}).values():
        if checkpoint.get("status") == "failed":
            raise SourceManagerError("failed checkpoint cannot complete")
        checkpoint["status"] = "complete"
        checkpoint["attempt"] = 0
    updated["status"] = "complete"
    updated["phase"] = "complete"
    updated["can_resume"] = False
    updated["pending_count"] = 0
    updated["metadata_sync_pending"] = False
    updated["last_error"] = None
    updated["updated_at"] = _now()
    validate_persistable(updated, field="state")
    return updated


def _checkpoint(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    checkpoints = state.get("checkpoints")
    if not isinstance(checkpoints, dict):
        raise SourceManagerError("state checkpoints are invalid")
    checkpoint = checkpoints.get(step_id)
    if not isinstance(checkpoint, dict):
        raise SourceManagerError("unknown checkpoint step")
    return checkpoint


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
