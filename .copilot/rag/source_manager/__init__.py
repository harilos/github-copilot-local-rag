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
from .gitlab_issues import (
    GITLAB_ISSUE_IDS_STATE_KEY,
    GITLAB_ISSUES_BATCH_SIZE,
    GITLAB_ISSUES_CUTOFF_STATE_KEY,
    GITLAB_PROJECT_ID_STATE_KEY,
    GitLabIssueInventoryItem,
    GitLabProject,
    fetch_gitlab_issues,
    generated_gitlab_issues_link,
    gitlab_connection_id,
    gitlab_issues_updated_after,
    gitlab_token_env,
    parse_gitlab_project,
    repair_generated_gitlab_issues_link,
)
from .gitlab_issue_fixes import (
    install_gitlab_issue_fixes,
    parse_gitlab_api_project_web_url,
)
from .gitlab_wiki import (
    GitLabWikiInventoryItem,
    decode_gitlab_wiki_page_relative_path,
    fetch_gitlab_wiki,
    generated_gitlab_wiki_link,
    gitlab_wiki_page_relative_path,
    gitlab_wiki_page_url,
    repair_generated_gitlab_wiki_link,
    validate_gitlab_wiki_work_tree,
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
    GitLabProjectCheck,
    GitLabProjectLocation,
    GitLabRegistration,
    RedmineRegistration,
    SharePointRootStatus,
    clear_sharepoint_root,
    configured_sharepoint_root,
    connection_config_path,
    connection_secret_path,
    check_gitlab_project,
    gitlab_project_location,
    has_stored_gitlab_token,
    has_stored_redmine_api_key,
    install_machine_connection_runtime,
    list_gitlab_registrations,
    list_redmine_registrations,
    redmine_api_key_env,
    redmine_connection_id,
    register_gitlab_token,
    register_redmine_api_key,
    resolve_gitlab_token,
    resolve_redmine_api_key,
    set_sharepoint_root,
    sharepoint_root_status,
    source_runtime_environment,
)
from .database_copy import (
    DatabaseCopyError,
    copy_database,
    install_database_copy_runtime,
)
from .copy_only_packages import install_copy_only_package_runtime
from .document_filter import (
    FILE_SELECTION_ALL,
    FILE_SELECTION_DOCUMENTS,
    FILE_SELECTION_KEY,
    install_document_filter_runtime,
)
from .document_filter_counts import install_document_filter_count_runtime
from .document_filter_packages import install_document_filter_package_contract
from .gitlab_wiki_runtime import install_gitlab_wiki_runtime
from .manager_connections import install_manage_custom_hook
from .provisional_source_merge import install_provisional_source_merge_runtime
from .source_preflight import install_source_preflight_runtime
from .teams_source import install_teams_source_runtime


def _remove_default_source_operation_timeout() -> None:
    """Allow Source operations to run until completion by default."""

    keyword_defaults = dict(run_streaming_process.__kwdefaults__ or {})
    keyword_defaults["timeout"] = None
    run_streaming_process.__kwdefaults__ = keyword_defaults


_remove_default_source_operation_timeout()
install_gitlab_issue_fixes()
install_redmine_incremental_refresh()
install_machine_connection_runtime()
install_source_preflight_runtime()
install_provisional_source_merge_runtime()
install_database_copy_runtime()
install_teams_source_runtime()
install_document_filter_runtime()
install_document_filter_count_runtime()
install_document_filter_package_contract()
install_copy_only_package_runtime()
install_gitlab_wiki_runtime()
install_manage_custom_hook()

# Runtime installers wrap several module functions. Keep package-level exports
# aligned with the final implementations rather than stale pre-install aliases.
from . import execution as _execution
from . import machine_connections as _machine_connections
from . import providers as _providers
from . import runner as _runner

SUPPORTED_PROVIDERS = _providers.SUPPORTED_PROVIDERS
build_fetch_plan = _providers.build_fetch_plan
execute_fetch_plan = _execution.execute_fetch_plan
register_source = _runner.register_source
resolve_environment_root = _providers.resolve_environment_root
source_runtime_environment = _machine_connections.source_runtime_environment
update_all_sources = _runner.update_all_sources
update_source = _runner.update_source
update_source_configuration = _runner.update_source_configuration
validate_provider_config = _providers.validate_provider_config

__all__ = [
    "CONNECTION_SCHEMA_VERSION",
    "DatabaseCopyError",
    "FILE_SELECTION_ALL",
    "FILE_SELECTION_DOCUMENTS",
    "FILE_SELECTION_KEY",
    "FetchPlan",
    "FetchStep",
    "GITLAB_ISSUE_IDS_STATE_KEY",
    "GITLAB_ISSUES_BATCH_SIZE",
    "GITLAB_ISSUES_CUTOFF_STATE_KEY",
    "GITLAB_PROJECT_ID_STATE_KEY",
    "GitLabIssueInventoryItem",
    "GitLabProject",
    "GitLabProjectCheck",
    "GitLabProjectLocation",
    "GitLabRegistration",
    "GitLabWikiInventoryItem",
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
    "check_gitlab_project",
    "complete_run",
    "configured_sharepoint_root",
    "confirm_add_success",
    "connection_config_path",
    "connection_secret_path",
    "copy_database",
    "decode_gitlab_wiki_page_relative_path",
    "execute_fetch_plan",
    "extract_json_result",
    "fetch_gitlab_issues",
    "fetch_gitlab_wiki",
    "generated_gitlab_issues_link",
    "generated_gitlab_wiki_link",
    "generated_redmine_link",
    "gitlab_connection_id",
    "gitlab_issues_updated_after",
    "gitlab_project_location",
    "gitlab_token_env",
    "gitlab_wiki_page_relative_path",
    "gitlab_wiki_page_url",
    "has_stored_gitlab_token",
    "has_stored_redmine_api_key",
    "install_copy_only_package_runtime",
    "install_document_filter_count_runtime",
    "install_document_filter_package_contract",
    "install_document_filter_runtime",
    "install_gitlab_issue_fixes",
    "install_gitlab_wiki_runtime",
    "install_teams_source_runtime",
    "list_gitlab_registrations",
    "list_redmine_registrations",
    "list_sources",
    "new_run_state",
    "parse_gitlab_api_project_web_url",
    "parse_gitlab_project",
    "parse_redmine_project_url",
    "record_retry",
    "redact_runtime_path",
    "redact_runtime_paths",
    "redmine_api_key_env",
    "redmine_batches",
    "redmine_connection_id",
    "redmine_updated_on_cutoff",
    "register_gitlab_token",
    "register_redmine_api_key",
    "register_source",
    "remove_source_metadata",
    "repair_generated_gitlab_issues_link",
    "repair_generated_gitlab_wiki_link",
    "repair_generated_redmine_link",
    "resolve_environment_root",
    "resolve_gitlab_token",
    "resolve_redmine_api_key",
    "run_streaming_process",
    "set_sharepoint_root",
    "sharepoint_root_status",
    "source_runtime_environment",
    "stable_source_key",
    "update_all_sources",
    "update_source",
    "update_source_configuration",
    "validate_gitlab_wiki_work_tree",
    "validate_local_source_key",
    "validate_persistable",
    "validate_provider_config",
]
