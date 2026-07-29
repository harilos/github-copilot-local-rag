from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit


SCHEMA_VERSION = "local-rag.manage-custom.v1"
CUSTOM_FILE_NAME = "manage-custom.json"
EXAMPLE_FILE_NAME = "manage-custom.example.json"
ENV_CONFIG_PATH = "LOCAL_RAG_MANAGE_CUSTOM_CONFIG"
_MAX_FILE_BYTES = 256 * 1024
_MAX_EXAMPLES_PER_KEY = 8
_MAX_EXAMPLE_CHARS = 1024
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?:authorization|cookie|credential|password|passwd|secret|"
    r"token|private[_-]?key|proxy)\s*[:=]"
)
_PRIVATE_KEY_MARKER = re.compile(
    r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|OPENSSH PRIVATE KEY)-----"
)
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "auth",
        "authorization",
        "code",
        "cookie",
        "credential",
        "key",
        "oauth",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)

# These are display-example identities, not their organization-specific
# values. Values live in the tracked example file or the untracked custom
# file, so manage.py never embeds organization examples.
EXAMPLE_KEYS = frozenset(
    {
        "admin_transfer_output",
        "azure_repository_web_url",
        "database_name",
        "database_query_hint",
        "database_query_hint_edit",
        "database_title",
        "database_title_edit",
        "distribution_output",
        "generic_home_url",
        "generic_web_root",
        "git_commit",
        "git_ref",
        "git_repository_path_prefix",
        "github_repository_clone_url",
        "github_repository_web_url",
        "github_source_display_name",
        "gitlab_repository_web_url",
        "ingestion_root",
        "new_source_id",
        "other_input_path",
        "other_source_display_name",
        "package_input",
        "redmine_days",
        "redmine_project_url",
        "redmine_source_display_name",
        "regex_pattern",
        "regex_url_template",
        "scan_subdirectory",
        "search_question",
        "sharepoint_browser_url",
        "sharepoint_link_root",
        "sharepoint_relative_path",
        "sharepoint_source_display_name",
        "source_display_name",
        "source_id",
        "svn_link_repository_url",
        "svn_link_web_root",
        "svn_repository_path_prefix",
        "svn_repository_url",
        "svn_revision",
        "svn_source_display_name",
    }
)


@dataclass(frozen=True)
class ManageCustomWarning:
    code: str
    source: str
    key: str | None = None
    line: int | None = None
    column: int | None = None
    offset: int | None = None
    path: str | None = None

    def render(self) -> str:
        fields = [self.code, f"source={self.source}"]
        if self.path:
            fields.append(f"path={self.path}")
        if self.key:
            fields.append(f"key={self.key}")
        if self.line is not None:
            fields.append(f"line={self.line}")
        if self.column is not None:
            fields.append(f"column={self.column}")
        if self.offset is not None:
            fields.append(f"offset={self.offset}")
        return ":".join(fields)


@dataclass(frozen=True)
class ManageCustom:
    examples: Mapping[str, tuple[str, ...]]
    warnings: tuple[ManageCustomWarning, ...]

    def values(self, key: str) -> tuple[str, ...]:
        return tuple(self.examples.get(key, ()))


def load_manage_custom(
    rag_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    example_path: Path | None = None,
    custom_path: Path | None = None,
) -> ManageCustom:
    """Resolve examples with env-file > custom > tracked-example priority."""
    environment = dict(os.environ if environ is None else environ)
    bundled_example = (
        Path(example_path)
        if example_path is not None
        else Path(__file__).resolve().parents[1]
        / "config"
        / EXAMPLE_FILE_NAME
    )
    local_custom = (
        Path(custom_path)
        if custom_path is not None
        else Path(rag_root) / "config" / CUSTOM_FILE_NAME
    )
    warnings: list[ManageCustomWarning] = []
    example_values = _read_layer(
        bundled_example,
        source="example",
        optional=False,
        warnings=warnings,
    )
    custom_values = _read_layer(
        local_custom,
        source="custom",
        optional=True,
        warnings=warnings,
    )
    environment_values: dict[str, tuple[str, ...]] = {}
    configured_path = str(environment.get(ENV_CONFIG_PATH) or "").strip()
    if configured_path:
        environment_path = Path(configured_path).expanduser()
        if not environment_path.is_absolute():
            warnings.append(
                ManageCustomWarning(
                    "manage_custom_environment_path_invalid",
                    "environment",
                    key=ENV_CONFIG_PATH,
                )
            )
        else:
            environment_values = _read_layer(
                environment_path,
                source="environment",
                optional=False,
                warnings=warnings,
            )
    resolved: dict[str, tuple[str, ...]] = {}
    for key in sorted(EXAMPLE_KEYS):
        value = example_values.get(key, ())
        if key in custom_values:
            value = custom_values[key]
        if key in environment_values:
            value = environment_values[key]
        resolved[key] = tuple(value)
    return ManageCustom(
        examples=resolved,
        warnings=tuple(warnings),
    )


def _read_layer(
    path: Path,
    *,
    source: str,
    optional: bool,
    warnings: list[ManageCustomWarning],
) -> dict[str, tuple[str, ...]]:
    candidate = Path(path)
    if candidate.is_symlink():
        warnings.append(
            ManageCustomWarning(
                "manage_custom_file_invalid", source, path=str(candidate)
            )
        )
        return {}
    if not candidate.exists():
        if not optional:
            warnings.append(
                ManageCustomWarning(
                    "manage_custom_file_missing",
                    source,
                    path=str(candidate),
                )
            )
        return {}
    try:
        if not candidate.is_file() or candidate.stat().st_size > _MAX_FILE_BYTES:
            raise OSError("invalid file")
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.append(
            ManageCustomWarning(
                "manage_custom_invalid_json",
                source,
                line=exc.lineno,
                column=exc.colno,
                offset=exc.pos,
                path=str(candidate),
            )
        )
        return {}
    except (OSError, UnicodeError):
        warnings.append(
            ManageCustomWarning(
                "manage_custom_file_invalid", source, path=str(candidate)
            )
        )
        return {}
    if not isinstance(payload, dict):
        warnings.append(
            ManageCustomWarning(
                "manage_custom_invalid_type",
                source,
                key="<root>",
                path=str(candidate),
            )
        )
        return {}
    for key in payload:
        if key not in {"schema_version", "examples"}:
            warnings.append(
                ManageCustomWarning(
                    "manage_custom_unknown_key",
                    source,
                    key=key,
                    path=str(candidate),
                )
            )
    if payload.get("schema_version") != SCHEMA_VERSION:
        warnings.append(
            ManageCustomWarning(
                "manage_custom_invalid_schema",
                source,
                key="schema_version",
                path=str(candidate),
            )
        )
        return {}
    raw_examples = payload.get("examples")
    if not isinstance(raw_examples, dict):
        warnings.append(
            ManageCustomWarning(
                "manage_custom_invalid_type",
                source,
                key="examples",
                path=str(candidate),
            )
        )
        return {}
    values: dict[str, tuple[str, ...]] = {}
    for key, raw in raw_examples.items():
        field = f"examples.{key}"
        if key not in EXAMPLE_KEYS:
            warnings.append(
                ManageCustomWarning(
                    "manage_custom_unknown_key",
                    source,
                    key=field,
                    path=str(candidate),
                )
            )
            continue
        decoded = _validate_values(
            raw,
            source=source,
            key=field,
            warnings=warnings,
            path=candidate,
        )
        if decoded is not None:
            values[key] = decoded
    return values


def _validate_values(
    raw: Any,
    *,
    source: str,
    key: str,
    warnings: list[ManageCustomWarning],
    path: Path,
) -> tuple[str, ...] | None:
    if (
        not isinstance(raw, list)
        or len(raw) > _MAX_EXAMPLES_PER_KEY
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > _MAX_EXAMPLE_CHARS
            for value in raw
        )
    ):
        warnings.append(
            ManageCustomWarning(
                "manage_custom_invalid_type",
                source,
                key=key,
                path=str(path),
            )
        )
        return None
    values = tuple(value.strip() for value in raw)
    if any(_contains_secret(value) for value in values):
        warnings.append(
            ManageCustomWarning(
                "manage_custom_secret_rejected",
                source,
                key=key,
                path=str(path),
            )
        )
        return None
    return values


def _contains_secret(value: str) -> bool:
    if _PRIVATE_KEY_MARKER.search(value) or _SENSITIVE_ASSIGNMENT.search(value):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    for key, _item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in _SENSITIVE_QUERY_NAMES:
            return True
    fragment = parsed.fragment
    return bool(
        fragment
        and (
            _SENSITIVE_ASSIGNMENT.search(fragment)
            or any(
                key.casefold() in _SENSITIVE_QUERY_NAMES
                for key, _item in parse_qsl(
                    fragment,
                    keep_blank_values=True,
                )
            )
        )
    )
