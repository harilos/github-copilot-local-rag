"""Small dependency-free Redmine batch contract shared by fetch layers."""

from __future__ import annotations


REDMINE_ADD_BATCH_SIZE = 50
REDMINE_STATE_CHECKPOINT_SIZE = 5


def is_redmine_state_checkpoint(completed_count: int) -> bool:
    """Return whether one fetched Issue closes a durable checkpoint."""

    completed = int(completed_count)
    return completed > 0 and completed % REDMINE_STATE_CHECKPOINT_SIZE == 0
