"""Small dependency-free Redmine batch contract shared by fetch layers."""

from __future__ import annotations


REDMINE_ADD_BATCH_SIZE = 50
REDMINE_STATE_CHECKPOINT_SIZE = 5


def is_redmine_state_checkpoint(
    completed_count: int,
    total_count: int | None,
) -> bool:
    """Return whether fetched Issue state must be persisted now.

    The exact tail is always durable before ADD. Between tails, persisting
    every five Issues bounds a restart after a failed checkpoint write to at
    most five repeated detail requests.
    """

    completed = int(completed_count)
    total = int(total_count) if total_count is not None else None
    return completed > 0 and (
        completed % REDMINE_STATE_CHECKPOINT_SIZE == 0
        or (total is not None and completed == total)
    )
