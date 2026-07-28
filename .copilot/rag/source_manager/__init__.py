"""DB-local Source Manager primitives.

This package deliberately contains no interactive UI and performs no network
access.  It owns portable Source configuration/state while provider executors
consume the bounded plans produced here.
"""

from .checkpoints import (
    REDMINE_BATCH_SIZE,
    REDMINE_RETRY_POLICY,
    RetryPolicy,
    advance_checkpoint,
    complete_run,
    new_run_state,
    redmine_batches,
    record_retry,
)
from .errors import SourceManagerError
from .execution import execute_fetch_plan
from .providers import (
    SUPPORTED_PROVIDERS,
    FetchPlan,
    FetchStep,
    build_fetch_plan,
    resolve_environment_root,
    validate_provider_config,
)
from .security import (
    redact_runtime_path,
    redact_runtime_paths,
    validate_persistable,
)
from .runner import (
    confirm_add_success,
    list_sources,
    register_source,
    update_all_sources,
    update_source,
    update_source_configuration,
)
from .store import (
    SOURCE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    SourcePaths,
    SourceStore,
    StoredJson,
    stable_source_key,
    validate_local_source_key,
)

__all__ = [
    "FetchPlan",
    "FetchStep",
    "REDMINE_BATCH_SIZE",
    "REDMINE_RETRY_POLICY",
    "RetryPolicy",
    "SOURCE_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "SUPPORTED_PROVIDERS",
    "SourceManagerError",
    "SourcePaths",
    "SourceStore",
    "StoredJson",
    "advance_checkpoint",
    "build_fetch_plan",
    "complete_run",
    "confirm_add_success",
    "execute_fetch_plan",
    "list_sources",
    "new_run_state",
    "record_retry",
    "register_source",
    "redact_runtime_path",
    "redact_runtime_paths",
    "redmine_batches",
    "resolve_environment_root",
    "stable_source_key",
    "update_all_sources",
    "update_source",
    "update_source_configuration",
    "validate_local_source_key",
    "validate_persistable",
    "validate_provider_config",
]
