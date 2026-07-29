"""DB-local Source Manager primitives and Manager connection extensions.

Portable Source configuration and checkpoints remain DB-local. Machine-only
connection roots and credentials are kept outside databases and are never
included in transfer packages.
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
from .machine_connections import (
    CONNECTION_SCHEMA_VERSION,
    LEGACY_REDMINE_API_KEY_ENV,
    SHAREPOINT_ROOT_ENV,
    RedmineRegistration,
    SharePointRootStatus,
    clear_sharepoint_root,
    configured_sharepoint_root,
    connection_config_path,
    connection_secret_path,
    has_stored_redmine_api_key,
    install_machine_connection_runtime,
    list_redmine_registrations,
    redmine_api_key_env,
    redmine_connection_id,
    register_redmine_api_key,
    resolve_redmine_api_key,
    set_sharepoint_root,
    sharepoint_root_status,
    source_runtime_environment,
)
from .manager_connections import install_manage_custom_hook


def _remove_default_source_operation_timeout() -> None:
    """Allow Source operations to run until completion by default."""

    keyword_defaults = dict(run_streaming_process.__kwdefaults__ or {})
    keyword_defaults["timeout"] = None
    run_streaming_process.__kwdefaults__ = keyword_defaults


_remove_default_source_operation_timeout()
install_redmine_incremental_refresh()
install_machine_connection_runtime()
install_manage_custom_hook()

# Runtime installation replaces runner.update_source. Keep the package-level
# convenience export aligned with the patched function.
from . import runner as _runner

update_source = _runner.update_source

__all__ = [
    "CONNECTION_SCHEMA_VERSION",
    "FetchPlan",
    "FetchStep",
    "LEGACY_REDMINE_API_KEY_ENV",
    "PROGRESS_FRAME",
    "REDMINE_BATCH_SIZE",
    "REDMINE_CUTOFF_STATE_KEY",
    "REDMINE_RETRY_POLICY",
    "RESULT_FRAME",
    "RedmineProject",
    "RedmineRegistration",
    "ResultExtractionError",
    "RetryPolicy",
    "SHAREPOINT_ROOT_ENV",
    "SOURCE_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "SUPPORTED_PROVIDERS",
    "SharePointRootStatus",
    "SourceManagerError",
    "SourcePaths",
    "SourceStore",
    "StoredJson",
    "StreamingProcessResult",
    "advance_checkpoint",
    "build_fetch_plan",
    "clear_sharepoint_root",
    "complete_run",
    "configured_sharepoint_root",
    "confirm_add_success",
    "connection_config_path",
    "connection_secret_path",
    "execute_fetch_plan",
    "extract_json_result",
    "generated_redmine_link",
    "has_stored_redmine_api_key",
    "list_redmine_registrations",
    "list_sources",
    "new_run_state",
    "parse_redmine_project_url",
    "record_retry",
    "redact_runtime_path",
    "redact_runtime_paths",
    "redmine_api_key_env",
    "redmine_batches",
    "redmine_connection_id",
    "redmine_updated_on_cutoff",
    "register_redmine_api_key",
    "register_source",
    "remove_source_metadata",
    "repair_generated_redmine_link",
    "resolve_environment_root",
    "resolve_redmine_api_key",
    "run_streaming_process",
    "set_sharepoint_root",
    "sharepoint_root_status",
    "source_runtime_environment",
    "stable_source_key",
    "update_all_sources",
    "update_source",
    "update_source_configuration",
    "validate_local_source_key",
    "validate_persistable",
    "validate_provider_config",
]
