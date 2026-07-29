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
from .metadata import remove_source_metadata
from .providers import (
    SUPPORTED_PROVIDERS,
    FetchPlan,
    FetchStep,
    build_fetch_plan,
    resolve_environment_root,
    validate_provider_config,
)
from .redmine import (
    REDMINE_CUTOFF_STATE_KEY,
    RedmineProject,
    generated_redmine_link,
    parse_redmine_project_url,
    redmine_updated_on_cutoff,
    repair_generated_redmine_link,
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
from .subprocess_stream import (
    PROGRESS_FRAME,
    RESULT_FRAME,
    ResultExtractionError,
    StreamingProcessResult,
    extract_json_result,
    run_streaming_process,
)
from .redmine_incremental import install_redmine_incremental_refresh


def _remove_default_source_operation_timeout() -> None:
    """Allow Source operations to run until completion by default."""

    keyword_defaults = dict(run_streaming_process.__kwdefaults__ or {})
    keyword_defaults["timeout"] = None
    run_streaming_process.__kwdefaults__ = keyword_defaults


_remove_default_source_operation_timeout()
install_redmine_incremental_refresh()

__all__ = [
    "FetchPlan",
    "FetchStep",
    "PROGRESS_FRAME",
    "REDMINE_BATCH_SIZE",
    "REDMINE_CUTOFF_STATE_KEY",
    "REDMINE_RETRY_POLICY",
    "RESULT_FRAME",
    "RedmineProject",
    "ResultExtractionError",
    "RetryPolicy",
    "SOURCE_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "SUPPORTED_PROVIDERS",
    "SourceManagerError",
    "SourcePaths",
    "SourceStore",
    "StoredJson",
    "StreamingProcessResult",
    "advance_checkpoint",
    "build_fetch_plan",
    "complete_run",
    "confirm_add_success",
    "execute_fetch_plan",
    "extract_json_result",
    "generated_redmine_link",
    "list_sources",
    "new_run_state",
    "parse_redmine_project_url",
    "record_retry",
    "remove_source_metadata",
    "register_source",
    "redact_runtime_path",
    "redact_runtime_paths",
    "redmine_batches",
    "redmine_updated_on_cutoff",
    "repair_generated_redmine_link",
    "resolve_environment_root",
    "run_streaming_process",
    "stable_source_key",
    "update_all_sources",
    "update_source",
    "update_source_configuration",
    "validate_local_source_key",
    "validate_persistable",
    "validate_provider_config",
]
