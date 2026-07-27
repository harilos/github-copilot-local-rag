from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


CASE_SCHEMA = "copilot-compliance-case-v1"
REPORT_SCHEMA = "copilot-compliance-report-v1"
REQUIRED_PROFILES = ("auto", "mini", "standard")
PHASE_B_REPETITIONS = {"auto": 4, "mini": 2, "standard": 2}
PHASE_B_FOCUSED_CASES = 8
SCRIPT_NAMES = ("list_dbs.py", "search.py", "result_detail.py")
POINTER_SCHEMAS = {
    "rag-result-pointer-v1": "result_pointers",
    "rag-detail-pointer-v1": "detail_pointers",
}
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
DIRECT_VENV_RE = re.compile(
    r"[\\/]\.copilot[\\/]rag[\\/]query"
    r"[\\/]\.venv[\\/](?:bin[\\/]python|Scripts[\\/]python\.exe)",
    re.IGNORECASE,
)
FORBIDDEN_COMMAND_PATTERNS = {
    "path_python_probe": re.compile(
        r"(?:^|[;&|]\s*|\s)(?:python3?|py)(?:\.exe)?\s+"
        r"[^;\r\n]*(?:list_dbs|search|result_detail)\.py",
        re.IGNORECASE,
    ),
    "cmd_wrapper": re.compile(r"\bcmd(?:\.exe)?\s+/c\b", re.IGNORECASE),
    "start_process": re.compile(r"\bStart-Process\b", re.IGNORECASE),
    "batch_wrapper": re.compile(r"\.(?:bat|cmd)(?:[\"'\s]|$)", re.IGNORECASE),
    "no_daemon": re.compile(r"(?<![\w-])--no-daemon(?:\s|[\"']|$)"),
    "python_auto_db": re.compile(r"(?<![\w-])--auto(?:\s|[\"']|$)"),
    "retrieval_mode": re.compile(
        r"(?<![\w-])--retrieval-mode(?:\s|=|[\"']|$)"
    ),
    "json_stdin": re.compile(r"(?<![\w-])--request-json(?:\s|[\"']|$)"),
    "post_process": re.compile(
        r"(?:^|[;&|]\s*)(?:jq|grep|head|tail)\b", re.IGNORECASE
    ),
}


def _load_jsonl(path: Path) -> tuple[list[Any], int]:
    values: list[Any] = []
    invalid = 0
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except FileNotFoundError:
        return [], 1
    except UnicodeError:
        return [], 1
    for line in lines:
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            invalid += 1
    return values, invalid


EXPECTED_COUNT_KEYS = (
    "list_dbs_calls",
    "search_calls",
    "result_detail_calls",
    "summary_file_reads",
    "detail_file_reads",
    "requested_file_reads",
    "approved_file_writes",
    "management_calls",
    "manifest_reads",
    "raw_item_reads",
    "result_pointers",
    "detail_pointers",
    "subagent_calls",
    "automatic_retries",
    "workspace_changes",
)


def load_cases(path: Path) -> list[dict[str, Any]]:
    raw, invalid = _load_jsonl(path)
    if invalid:
        raise ValueError(f"{path} contains {invalid} invalid JSONL line(s)")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, dict) or value.get("schema_version") != CASE_SCHEMA:
            raise ValueError("every case must use copilot-compliance-case-v1")
        case_id = str(value.get("id") or "")
        if not re.fullmatch(r"CPL-\d{3}", case_id) or case_id in seen:
            raise ValueError(f"invalid or duplicate case id: {case_id!r}")
        seen.add(case_id)
        profiles = tuple(value.get("profiles") or ())
        if profiles != REQUIRED_PROFILES:
            raise ValueError(
                f"{case_id}: profiles must be {list(REQUIRED_PROFILES)}"
            )
        turns = value.get("turns")
        expected = value.get("expected")
        if not isinstance(turns, list) or not turns or len(turns) > 2:
            raise ValueError(f"{case_id}: one or two turns are required")
        if not isinstance(expected, dict):
            raise ValueError(f"{case_id}: expected object is required")
        for key in EXPECTED_COUNT_KEYS:
            if not isinstance(expected.get(key), int) or expected[key] < 0:
                raise ValueError(f"{case_id}: expected.{key} is invalid")
        assertions = value.get("assertions")
        if assertions is not None and not isinstance(assertions, dict):
            raise ValueError(f"{case_id}: assertions must be an object")
        required_skill_calls = (
            assertions.get("required_skill_calls")
            if isinstance(assertions, dict)
            else None
        )
        if required_skill_calls is not None:
            allowed_skills = value.get("allowed_skills")
            if (
                not isinstance(required_skill_calls, dict)
                or not required_skill_calls
                or not isinstance(allowed_skills, list)
                or any(
                    not isinstance(skill_name, str)
                    or skill_name not in allowed_skills
                    or isinstance(call_count, bool)
                    or not isinstance(call_count, int)
                    or call_count < 0
                    for skill_name, call_count
                    in required_skill_calls.items()
                )
            ):
                raise ValueError(
                    f"{case_id}: assertions.required_skill_calls is invalid"
                )
        phase_b = value.get("phase_b_repetitions")
        if phase_b is not None and phase_b != PHASE_B_REPETITIONS:
            raise ValueError(
                f"{case_id}: phase_b_repetitions must be "
                f"{PHASE_B_REPETITIONS}"
            )
        cases.append(value)
    if len(cases) != 16:
        raise ValueError(f"exactly 16 cases are required; found {len(cases)}")
    expected_ids = [f"CPL-{index:03d}" for index in range(1, 17)]
    if [case["id"] for case in cases] != expected_ids:
        raise ValueError("cases must be ordered CPL-001 through CPL-016")
    focused = [
        case for case in cases if case.get("phase_b_repetitions") is not None
    ]
    if len(focused) != PHASE_B_FOCUSED_CASES:
        raise ValueError(
            "exactly eight cases must define phase_b_repetitions"
        )
    phase_b_total = sum(
        sum(int(value) for value in case["phase_b_repetitions"].values())
        for case in focused
    )
    if phase_b_total != 64:
        raise ValueError(
            f"Phase B must define exactly 64 executions; found {phase_b_total}"
        )
    return cases


def load_variables(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid variables JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("variables JSON must contain one object")
    return value


def render_case(
    case: dict[str, Any],
    variables: dict[str, Any],
    *,
    execution_tag: str,
) -> dict[str, Any]:
    replacements = {
        str(key): str(value)
        for key, value in variables.items()
        if isinstance(value, (str, int, float, bool))
    }
    replacements["EXECUTION_TAG"] = execution_tag

    def render(value: Any) -> Any:
        if isinstance(value, str):
            output = value
            for key, replacement in replacements.items():
                output = output.replace("{{" + key + "}}", replacement)
            return output
        if isinstance(value, list):
            return [render(item) for item in value]
        if isinstance(value, dict):
            return {str(key): render(item) for key, item in value.items()}
        return value

    rendered = render(case)
    unresolved = [
        value
        for value in _flatten_strings(rendered)
        if PLACEHOLDER_RE.search(value)
    ]
    if unresolved:
        raise ValueError(
            f"{case['id']}: unresolved fixture placeholder"
        )
    return rendered


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _decoded_walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _decoded_walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _decoded_walk(child)
    elif isinstance(value, str):
        stripped = value.strip()
        if (
            len(stripped) >= 2
            and stripped[0] in "[{"
            and stripped[-1] in "]}"
        ):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return
            yield from _decoded_walk(decoded)


def _flatten_strings(value: Any) -> list[str]:
    return [item for item in _walk(value) if isinstance(item, str)]


def _event_kind(record: dict[str, Any]) -> str:
    values: list[str] = []
    for node in _walk(record):
        if not isinstance(node, dict):
            continue
        for key in (
            "type",
            "event",
            "event_type",
            "kind",
            "name",
            "tool_name",
            "toolName",
        ):
            value = node.get(key)
            if isinstance(value, str):
                values.append(value)
    return " ".join(values).casefold()


def _call_id(record: dict[str, Any]) -> str:
    nodes = [record]
    data = record.get("data")
    if isinstance(data, dict):
        nodes.append(data)
    for node in nodes:
        for key in ("toolCallId", "tool_call_id", "call_id"):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _direct_event_type(record: dict[str, Any]) -> str:
    for key in ("type", "event_type", "event", "kind"):
        value = record.get(key)
        if isinstance(value, str):
            return value.casefold()
    return ""


def _is_tool_delta_event(record: dict[str, Any]) -> bool:
    return _direct_event_type(record) in {
        "assistant.tool_call_delta",
        "tool.call_delta",
        "tool_call_delta",
    }


def _is_tool_completion_event(record: dict[str, Any]) -> bool:
    return _direct_event_type(record) in {
        "tool.execution_complete",
        "tool.execution_completed",
        "tool.execution_result",
        "tool.call_complete",
        "tool.call_completed",
        "tool.call_result",
    }


def _is_tool_non_call_lifecycle_event(record: dict[str, Any]) -> bool:
    return _direct_event_type(record) in {
        "tool.execution_partial_result",
    }


def _tool_records(cli_events: Iterable[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_calls: dict[str, set[str]] = {}
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if _is_tool_delta_event(value):
                return
            if _is_tool_non_call_lifecycle_event(value):
                return
            if str(value.get("type") or "").casefold() == "session.tools_updated":
                return
            request_keys = ("toolRequests", "tool_requests")
            has_requests = False
            for request_key in request_keys:
                requests = value.get(request_key)
                if isinstance(requests, list):
                    has_requests = True
                    for request in requests:
                        visit(request)
            if has_requests:
                for key, child in value.items():
                    if key not in request_keys:
                        visit(child)
                return
            keys = {str(key).casefold() for key in value}
            direct_type = _direct_event_type(value)
            is_tool = (
                direct_type.startswith("tool.")
                or direct_type.startswith("assistant.tool_")
                or bool(
                    keys
                    & {
                        "tool",
                        "toolname",
                        "tool_name",
                        "toolcallid",
                        "tool_call_id",
                    }
                )
            )
            if is_tool:
                if not _is_tool_completion_event(value):
                    call_id = _call_id(value)
                    tool_name, arguments = _direct_tool_name_and_arguments(
                        value
                    )
                    signature = json.dumps(
                        {
                            "tool_name": tool_name.casefold(),
                            "arguments": arguments,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    seen_signatures = seen_calls.setdefault(call_id, set())
                    if not call_id or signature not in seen_signatures:
                        if call_id:
                            seen_signatures.add(signature)
                        records.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for root in cli_events:
        visit(root)
    return records


def _direct_tool_name_and_arguments(
    record: dict[str, Any],
) -> tuple[str, Any]:
    candidates = [record]
    data = record.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    for node in candidates:
        tool_name = (
            node.get("toolName")
            or node.get("tool_name")
            or node.get("name")
        )
        if isinstance(tool_name, str):
            return tool_name, node.get("arguments")
    return "", None


def _record_text(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def _record_content(record: dict[str, Any]) -> str:
    return "\n".join(_flatten_strings(record))


def _values_for_keys(record: dict[str, Any], keys: set[str]) -> list[Any]:
    values: list[Any] = []
    for node in _walk(record):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if str(key).casefold() in keys:
                values.append(value)
    return values


def _consume_command_token(command: str) -> tuple[str, str] | None:
    value = command.lstrip()
    if not value:
        return None
    if value[0] in {"\"", "'"}:
        quote = value[0]
        end = value.find(quote, 1)
        if end < 0:
            return None
        return value[1:end], value[end + 1 :]
    match = re.match(r"([^\s]+)(.*)", value, re.DOTALL)
    return (match.group(1), match.group(2)) if match else None


def _has_unquoted_shell_operator(
    value: str,
    *,
    powershell: bool,
) -> bool:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if powershell and character == "`":
            escaped = True
            continue
        if not powershell and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"\"", "'"}:
            quote = character
            continue
        if not powershell and character == "`":
            return True
        if character in {";", "|", "&", ">", "<", "\r", "\n"}:
            return True
        if character == "$" and value[index : index + 2] == "$(":
            return True
    return bool(quote or escaped)


def _direct_script_invocation_details(
    record: dict[str, Any],
) -> tuple[str, list[str]] | None:
    tool_name, arguments = _direct_tool_name_and_arguments(record)
    if not isinstance(arguments, dict):
        return None
    argv = arguments.get("argv")
    command_values = [
        arguments.get(key)
        for key in ("command", "cmd")
        if isinstance(arguments.get(key), str)
    ]
    if (
        isinstance(argv, list) and command_values
    ) or len(command_values) > 1:
        return None
    if isinstance(argv, list) and all(
        isinstance(value, str) for value in argv
    ):
        if tool_name.casefold() not in {"powershell", "bash", "shell"}:
            return None
        tokens = list(argv)
    else:
        command = command_values[0] if command_values else None
        if not isinstance(command, str):
            return None
        shell = tool_name.casefold()
        if shell == "powershell":
            value = re.sub(r"`\r?\n[ \t]*", " ", command.strip())
            if not value.startswith("&"):
                return None
            value = value[1:].lstrip()
            powershell = True
        elif shell in {"bash", "shell"}:
            value = re.sub(r"\\\r?\n[ \t]*", " ", command.strip())
            if value.startswith("&"):
                return None
            powershell = False
        else:
            return None
        if (
            "\n" in value
            or "\r" in value
            or "$(" in value
            or "`" in value
            or (powershell and r"\#" in value)
            or _has_unquoted_shell_comment(value)
            or _has_unquoted_shell_operator(
                value, powershell=powershell
            )
        ):
            return None
        tokens = _static_command_tokens(
            value,
            shell="powershell" if powershell else "git-bash",
        )
        if tokens is None:
            return None
    if len(tokens) < 3:
        return None
    script = _validated_script_paths(tokens[0], tokens[1])
    return (script, tokens) if script else None


def _direct_script_invocation(record: dict[str, Any]) -> str | None:
    details = _direct_script_invocation_details(record)
    return details[0] if details else None


def _validated_script_paths(
    python_path: str,
    script_path: str,
) -> str | None:
    normalized_python = python_path.replace("\\", "/")
    normalized_script = script_path.replace("\\", "/")
    if not re.search(
        r"/\.copilot/rag/query/\.venv/"
        r"(?:bin/python|Scripts/python\.exe)$",
        normalized_python,
        re.IGNORECASE,
    ):
        return None
    match = re.search(
        r"/\.copilot/rag/query/"
        r"(list_dbs\.py|search\.py|result_detail\.py)$",
        normalized_script,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _option_values(argv: list[str], option: str) -> list[str | None]:
    return [
        argv[index + 1] if index + 1 < len(argv) else None
        for index, value in enumerate(argv)
        if value == option
    ]


def _search_argv_is_structured(argv: list[str]) -> bool:
    if len(argv) < 3 or not argv[-1] or argv[-1].startswith("--"):
        return False
    flag_options = {
        "--include-db-hint",
        "--compact-json",
    }
    value_options = {
        "--db",
        "--result-delivery",
        "--format",
        "--answer-goal",
        "--literal-identifier",
        "--entity",
        "--facet",
        "--semantic-hypothesis",
    }
    index = 2
    limit = len(argv) - 1
    while index < limit:
        option = argv[index]
        if option in flag_options:
            index += 1
            continue
        if option not in value_options or index + 1 >= limit:
            return False
        value = argv[index + 1]
        if not value or value.startswith("--"):
            return False
        if option == "--answer-goal" and value not in {
            "definition",
            "evidence",
            "comparison",
            "procedure",
            "history",
            "survey",
        }:
            return False
        index += 2
    return index == limit


def _script_records(
    tool_records: list[dict[str, Any]],
    script_name: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in tool_records
        if _direct_script_invocation(record) == script_name
    ]


def _tool_completion_records(cli_events: Iterable[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for root in cli_events:
        for value in _walk(root):
            if not isinstance(value, dict):
                continue
            if _is_tool_completion_event(value):
                output.append(value)
                break
    return output


def _normalize_audit_path(value: str) -> str:
    normalized = value.strip().strip("\"'").replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", normalized):
        return normalized.casefold()
    return normalized


def _pointer_observations(
    cli_events: list[Any],
    script_records: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, int], set[str], set[str], dict[str, set[str]]]:
    identities: dict[str, set[str]] = {key: set() for key in POINTER_SCHEMAS}
    anonymous: dict[str, int] = {key: 0 for key in POINTER_SCHEMAS}
    summary_paths: set[str] = set()
    detail_paths: set[str] = set()
    allowed_calls: dict[str, str] = {}
    for script, records in script_records.items():
        if script not in {"search.py", "result_detail.py"}:
            continue
        for record in records:
            call_id = _call_id(record)
            if call_id:
                allowed_calls[call_id] = script
    linked_completions = [
        record
        for record in _tool_completion_records(cli_events)
        if _call_id(record) in allowed_calls
    ]
    for root in linked_completions:
        for value in _decoded_walk(root):
            if not isinstance(value, dict):
                continue
            schema = str(value.get("schema_version") or "")
            if schema not in POINTER_SCHEMAS:
                continue
            if schema == "rag-result-pointer-v1":
                path = str(value.get("summary_file") or "")
                if path:
                    summary_paths.add(_normalize_audit_path(path))
            elif schema == "rag-detail-pointer-v1":
                path = str(value.get("detail_file") or "")
                if path:
                    detail_paths.add(_normalize_audit_path(path))
            result_id = str(value.get("result_set_id") or "")
            if result_id:
                identities[schema].add(result_id)
            else:
                anonymous[schema] += 1
    for text in _flatten_strings(linked_completions):
        for schema in POINTER_SCHEMAS:
            if schema not in text:
                continue
            match = re.search(
                r"result_set_id\\?[\"']\s*:\s*\\?[\"']"
                r"([^\\\"']+)",
                text,
            )
            if match:
                identities[schema].add(match.group(1))
            elif not identities[schema]:
                anonymous[schema] = max(anonymous[schema], 1)
    counts = {
        POINTER_SCHEMAS[schema]: len(identities[schema]) + anonymous[schema]
        for schema in POINTER_SCHEMAS
    }
    return counts, summary_paths, detail_paths, identities


def _attribute_pairs(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("key"), str) and "value" in value:
            raw = value["value"]
            if isinstance(raw, dict) and len(raw) == 1:
                raw = next(iter(raw.values()))
            yield value["key"], raw
        for key, child in value.items():
            if not isinstance(child, (dict, list)):
                yield str(key), child
            yield from _attribute_pairs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _attribute_pairs(child)


def _models_from_telemetry(
    otel_events: list[Any],
) -> tuple[list[str], list[str]]:
    requested: list[str] = []
    selected: list[str] = []
    for key, value in _attribute_pairs(otel_events):
        lowered = key.casefold()
        if not (
            lowered.startswith("gen_ai.")
            and isinstance(value, str)
            and value.strip()
            and len(value) < 200
        ):
            continue
        if lowered in {
            "gen_ai.request.model",
            "gen_ai.request.model_name",
        }:
            requested.append(value.strip())
        elif lowered in {
            "gen_ai.response.model",
            "gen_ai.response.model_name",
        }:
            selected.append(value.strip())
    chat_models: list[str] = []
    for value in _walk(otel_events):
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        if isinstance(name, str) and name.casefold().startswith("chat "):
            chat_models.append(name[5:].strip())
    requested = list(dict.fromkeys(requested))
    selected = list(dict.fromkeys(selected or chat_models))
    return requested, selected


def _numeric_telemetry(otel_events: list[Any]) -> dict[str, float]:
    output = {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cache_read_tokens": 0.0,
        "cache_creation_tokens": 0.0,
        "ai_credits": 0.0,
        "llm_round_trips": 0.0,
    }
    for key, value in _attribute_pairs(otel_events):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        lowered = key.casefold()
        if "input" in lowered and "token" in lowered:
            output["input_tokens"] += float(value)
        elif "output" in lowered and "token" in lowered:
            output["output_tokens"] += float(value)
        elif "cache" in lowered and "read" in lowered and "token" in lowered:
            output["cache_read_tokens"] += float(value)
        elif "cache" in lowered and "creation" in lowered and "token" in lowered:
            output["cache_creation_tokens"] += float(value)
        elif "credit" in lowered:
            output["ai_credits"] += float(value)
        elif "turn" in lowered and ("count" in lowered or "value" in lowered):
            output["llm_round_trips"] += float(value)
    return output


def _strict_missing_probe_target(
    record: dict[str, Any],
) -> str | None:
    tool_name, arguments = _direct_tool_name_and_arguments(record)
    if (
        tool_name.casefold() != "powershell"
        or not isinstance(arguments, dict)
        or any(key in arguments for key in ("argv", "args"))
    ):
        return None
    command_values = [
        value
        for key in ("command", "cmd")
        for value in [arguments.get(key)]
        if isinstance(value, str)
    ]
    if len(command_values) != 1:
        return None
    if "\r" in command_values[0] or "\n" in command_values[0]:
        return None
    match = re.fullmatch(
        r"\s*\$path\s*=\s*'(?P<path>[^'\r\n]+)'\s*;\s*"
        r"if\s*\(\s*Test-Path\s+-LiteralPath\s+\$path\s*\)\s*"
        r"\{\s*Get-Content\s+-LiteralPath\s+\$path\s+-Raw\s*\}\s*"
        r"else\s*\{\s*Write-Output\s+'__MISSING__'\s*\}\s*",
        command_values[0],
        re.IGNORECASE,
    )
    if match is None:
        return None
    path = match.group("path")
    if "$(" in path or "`" in path:
        return None
    return path


def _strict_read_target(record: dict[str, Any]) -> str | None:
    missing_probe = _strict_missing_probe_target(record)
    if missing_probe is not None:
        return missing_probe
    tool_name, arguments = _direct_tool_name_and_arguments(record)
    if not isinstance(arguments, dict):
        return None
    command_values = [
        value
        for key in ("command", "cmd")
        for value in [arguments.get(key)]
        if isinstance(value, str)
    ]
    if tool_name.casefold() in {
        "read",
        "view",
        "open",
        "read_file",
        "view_file",
        "open_file",
    }:
        paths = [
            str(arguments[key])
            for key in ("path", "file_path", "filepath")
            if isinstance(arguments.get(key), str)
            and arguments.get(key)
        ]
        if len(paths) == 1 and not command_values:
            return paths[0]
    if tool_name.casefold() not in {"powershell", "bash", "shell"}:
        return None
    token = r"(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))"
    patterns = (
        re.compile(
            rf"^\s*Get-Content\s+(?:-Raw\s+)?(?:-LiteralPath\s+)?"
            rf"{token}(?:\s+-Raw)?\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^\s*(?:\[System\.)?\[?IO\.File\]?::ReadAllText\(\s*"
            rf"{token}\s*\)\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^\s*cat\s+(?:--\s+)?{token}\s*$",
            re.IGNORECASE,
        ),
    )
    matches: list[str] = []
    for command in command_values:
        for pattern in patterns:
            match = pattern.fullmatch(command)
            if match:
                matches.append(next(value for value in match.groups() if value))
                break
    return matches[0] if len(matches) == 1 else None


def _strict_write_target(record: dict[str, Any]) -> str | None:
    tool_name, arguments = _direct_tool_name_and_arguments(record)
    if tool_name.casefold() not in {
        "write",
        "edit",
        "create",
        "write_file",
        "edit_file",
        "create_file",
        "apply_patch",
    }:
        return None
    if isinstance(arguments, dict) and any(
        isinstance(arguments.get(key), str)
        for key in ("command", "cmd")
    ):
        return None
    if (
        tool_name.casefold() == "apply_patch"
        and isinstance(arguments, str)
    ):
        lines = arguments.replace("\r\n", "\n").splitlines()
        if (
            len(lines) < 4
            or lines[0] != "*** Begin Patch"
            or lines[-1] != "*** End Patch"
            or lines.count("*** Begin Patch") != 1
            or lines.count("*** End Patch") != 1
            or not lines[1].startswith("*** Add File: ")
        ):
            return None
        path = lines[1].removeprefix("*** Add File: ")
        if not path or path != path.strip():
            return None
        added_lines = lines[2:-1]
        if not added_lines or any(
            not line.startswith("+") for line in added_lines
        ):
            return None
        if any(
            re.match(
                r"^\+\*\*\* (?:Begin|End|Add|Update|Delete|Move)(?: |$)",
                line,
            )
            for line in added_lines
        ):
            return None
        return path
    if not isinstance(arguments, dict):
        return None
    paths = [
        str(arguments[key])
        for key in ("path", "file_path", "filepath")
        if isinstance(arguments.get(key), str)
        and arguments.get(key)
    ]
    return paths[0] if len(paths) == 1 else None


def _matches_case_path(
    target: str,
    relative: str,
    *,
    fixture_workspace: Path | None = None,
) -> bool:
    normalized_target = _normalize_audit_path(target).rstrip("/")
    normalized_relative = _normalize_audit_path(relative).strip("/")
    if (
        not normalized_relative
        or normalized_relative.startswith("../")
        or "/../" in normalized_relative
    ):
        return False
    if normalized_target == normalized_relative:
        return True
    if fixture_workspace is None:
        return False
    expected = _normalize_audit_path(
        str(
            fixture_workspace
            / Path(*normalized_relative.split("/"))
        )
    ).rstrip("/")
    return normalized_target == expected


def _read_observations(
    tool_records: list[dict[str, Any]],
    summary_paths: set[str],
    detail_paths: set[str],
    *,
    allowed_reads: list[str],
    allowed_writes: list[str],
    allowed_skills: list[str],
    fixture_workspace: Path | None,
) -> tuple[dict[str, int], set[int]]:
    counts = {
        "summary_file_reads": 0,
        "detail_file_reads": 0,
        "requested_file_reads": 0,
        "approved_file_writes": 0,
        "manifest_reads": 0,
        "raw_item_reads": 0,
        "file_read_tool_calls": 0,
        "unexpected_file_read_calls": 0,
        "file_write_tool_calls": 0,
        "unexpected_file_write_calls": 0,
    }
    approved: set[int] = set()
    for record in tool_records:
        target = _strict_read_target(record)
        if target is not None:
            counts["file_read_tool_calls"] += 1
            normalized = _normalize_audit_path(target)
            lowered = normalized.casefold()
            if normalized in summary_paths:
                counts["summary_file_reads"] += 1
                approved.add(id(record))
            elif normalized in detail_paths:
                counts["detail_file_reads"] += 1
                approved.add(id(record))
            elif lowered.endswith("/manifest.json"):
                counts["manifest_reads"] += 1
            elif "/items/" in lowered and lowered.endswith(".json"):
                counts["raw_item_reads"] += 1
            elif any(
                _matches_case_path(
                    target,
                    relative,
                    fixture_workspace=fixture_workspace,
                )
                for relative in allowed_reads
            ):
                counts["requested_file_reads"] += 1
                approved.add(id(record))
            elif (
                (
                    "local-rag" in allowed_skills
                    and
                    lowered.endswith(
                        "/.copilot/skills/local-rag/skill.md"
                    )
                )
                or (
                    bool(allowed_skills)
                    and lowered.endswith(
                        "/.copilot/instructions/rag.instructions.md"
                    )
                )
            ):
                approved.add(id(record))
            elif (
                "local-rag-admin" in allowed_skills
                and lowered.endswith(
                    "/.copilot/skills/local-rag-admin/skill.md"
                )
            ):
                approved.add(id(record))
            else:
                counts["unexpected_file_read_calls"] += 1
        write_target = _strict_write_target(record)
        kind = _event_kind(record)
        if re.search(
            r"(?:^|\s)(?:write|edit|create|delete)(?:[_ ]file)?"
            r"(?:\s|$)|apply[_ ]patch",
            kind,
        ):
            counts["file_write_tool_calls"] += 1
            if write_target is not None and any(
                _matches_case_path(
                    write_target,
                    relative,
                    fixture_workspace=fixture_workspace,
                )
                for relative in allowed_writes
            ):
                counts["approved_file_writes"] += 1
                approved.add(id(record))
            else:
                counts["unexpected_file_write_calls"] += 1
    return counts, approved


def _unapproved_tool_calls(
    tool_records: list[dict[str, Any]],
    scripts: dict[str, list[dict[str, Any]]],
    approved_read_ids: set[int],
    *,
    allowed_skills: list[str],
) -> int:
    approved_ids = {
        id(record)
        for records in scripts.values()
        for record in records
    }
    unapproved = 0
    for record in tool_records:
        if id(record) in approved_ids:
            continue
        if id(record) in approved_read_ids:
            continue
        kind = _event_kind(record)
        if re.search(r"(?:^|\s)skill(?:\s|$)", kind):
            tool_name, arguments = _direct_tool_name_and_arguments(record)
            if tool_name.casefold() == "skill":
                skill_name = (
                    arguments.get("skill")
                    if isinstance(arguments, dict)
                    else None
                )
                if (
                    isinstance(skill_name, str)
                    and skill_name in allowed_skills
                ):
                    continue
                unapproved += 1
                continue
        unapproved += 1
    return unapproved


def _skill_call_counts(
    tool_records: list[dict[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in tool_records:
        kind = _event_kind(record)
        if not re.search(r"(?:^|\s)skill(?:\s|$)", kind):
            continue
        tool_name, arguments = _direct_tool_name_and_arguments(record)
        if tool_name.casefold() != "skill" or not isinstance(arguments, dict):
            continue
        skill_name = arguments.get("skill")
        if not isinstance(skill_name, str) or not skill_name:
            continue
        counts[skill_name] = counts.get(skill_name, 0) + 1
    return counts


def _management_call_count(tool_records: list[dict[str, Any]]) -> int:
    pattern = re.compile(
        r"(?:^|[\\/])(?:manage\.py|build_db\.py|add_data\.py|"
        r"create_db\.py|delete_db\.py)(?:$|[\"'\s])",
        re.IGNORECASE,
    )
    return sum(
        bool(pattern.search(_record_content(record)))
        for record in tool_records
    )


def _subagent_count(tool_records: list[dict[str, Any]]) -> int:
    pattern = re.compile(
        r"(?:subagent|coding[_ -]?agent|delegate[_ -]?task|"
        r"planner[_ -]?agent|spawn[_ -]?agent|(?:^|\s)(?:task|delegate)(?:\s|$))",
        re.IGNORECASE,
    )
    return sum(bool(pattern.search(_event_kind(record))) for record in tool_records)


def _workspace_change_count(run_meta: dict[str, Any]) -> int:
    changes = run_meta.get("workspace_changes") or []
    return len(changes) if isinstance(changes, list) else 1


def _assistant_text(cli_events: list[Any]) -> str:
    values: list[str] = []
    for index, root in enumerate(cli_events):
        if (
            not isinstance(root, dict)
            or _direct_event_type(root) != "assistant.message"
        ):
            continue
        data = root.get("data")
        if not isinstance(data, dict):
            continue
        phase = str(data.get("phase") or "")
        tool_requests = data.get("toolRequests")
        if phase == "final_answer" and (
            tool_requests is not None
            and (
                not isinstance(tool_requests, list)
                or bool(tool_requests)
            )
        ):
            continue
        turn_id = str(data.get("turnId") or "")
        terminal_marker_observed = False
        if not phase and turn_id:
            for later in cli_events[index + 1 :]:
                if not isinstance(later, dict):
                    continue
                later_type = _direct_event_type(later)
                later_data = later.get("data")
                later_turn = (
                    str(later_data.get("turnId") or "")
                    if isinstance(later_data, dict)
                    else ""
                )
                if later_type == "assistant.turn_end":
                    terminal_marker_observed = later_turn == turn_id
                    break
                if (
                    later_type == "assistant.message"
                    or later_type.startswith("tool.")
                    or later_type.startswith("assistant.tool_")
                ):
                    break
        terminal_without_phase = (
            not phase
            and terminal_marker_observed
            and isinstance(tool_requests, list)
            and not tool_requests
        )
        if phase != "final_answer" and not terminal_without_phase:
            continue
        content = data.get("content")
        if isinstance(content, str) and content:
            values.append(content)
    return "\n".join(dict.fromkeys(values))


def _static_command_contract_failures(
    assertions: dict[str, Any],
    assistant: str,
) -> list[str]:
    shell = str(assertions.get("assistant_command_shell") or "")
    if not shell:
        return []
    blocks = re.findall(
        r"```[^\r\n]*\r?\n(.*?)```",
        assistant,
        re.DOTALL,
    )
    if len(blocks) != 1:
        return ["static_command_requires_one_code_block"]
    command = blocks[0].strip()
    if shell == "powershell":
        command = re.sub(r"`\r?\n[ \t]*", " ", command)
    elif shell == "git-bash":
        command = re.sub(r"\\\r?\n[ \t]*", " ", command)
    else:
        return ["static_command_shell_invalid"]
    if "\n" in command or "\r" in command:
        return ["static_command_contains_multiple_commands"]
    if "$(" in command or "`" in command:
        return ["static_command_substitution_not_allowed"]
    if shell == "powershell" and r"\#" in command:
        return ["static_command_comment_not_allowed"]
    parsed = command.lstrip()
    if shell == "powershell":
        if not parsed.startswith("&"):
            return ["static_powershell_call_operator_missing"]
        parsed = parsed[1:].lstrip()
    elif parsed.startswith("&"):
        return ["static_git_bash_uses_powershell_syntax"]
    if _has_unquoted_shell_comment(parsed):
        return ["static_command_comment_not_allowed"]
    tokens = _static_command_tokens(parsed, shell=shell)
    if tokens is None:
        return ["static_command_argv_invalid"]
    required = [
        str(value)
        for value in assertions.get("assistant_contains") or []
    ]
    if len(required) < 2:
        return ["static_command_contract_invalid"]
    if len(tokens) < 3 or tokens[:2] != required[:2]:
        return ["static_command_argv_mismatch"]
    if tokens[-1] != required[-1]:
        return ["static_command_argv_mismatch"]
    required_db = next(
        (
            value.split(maxsplit=1)[1]
            for value in required
            if value.startswith("--db ")
        ),
        "",
    )
    required_delivery = next(
        (
            value.split(maxsplit=1)[1]
            for value in required
            if value.startswith("--result-delivery ")
        ),
        "",
    )
    counts = {
        "--db": 0,
        "--include-db-hint": 0,
        "--compact-json": 0,
        "--result-delivery": 0,
        "--answer-goal": 0,
        "--literal-identifier": 0,
        "--entity": 0,
        "--facet": 0,
        "--semantic-hypothesis": 0,
    }
    value_options = {
        "--db",
        "--result-delivery",
        "--answer-goal",
        "--literal-identifier",
        "--entity",
        "--facet",
        "--semantic-hypothesis",
    }
    argv = tokens[2:-1]
    index = 0
    while index < len(argv):
        option = argv[index]
        if option not in counts:
            return ["static_command_argv_mismatch"]
        counts[option] += 1
        if option in value_options:
            index += 1
            if index >= len(argv) or not argv[index]:
                return ["static_command_argv_mismatch"]
            value = argv[index]
            if option == "--db" and value != required_db:
                return ["static_command_argv_mismatch"]
            if (
                option == "--result-delivery"
                and value != required_delivery
            ):
                return ["static_command_argv_mismatch"]
            if (
                option == "--answer-goal"
                and value not in {
                    "definition",
                    "evidence",
                    "comparison",
                    "procedure",
                    "history",
                    "survey",
                }
            ):
                return ["static_command_argv_mismatch"]
        index += 1
    if any(
        counts[option] != 1
        for option in (
            "--db",
            "--include-db-hint",
            "--compact-json",
            "--result-delivery",
        )
    ):
        return ["static_command_argv_mismatch"]
    if (
        counts["--answer-goal"] > 1
        or counts["--literal-identifier"] > 3
        or counts["--entity"] > 5
        or counts["--facet"] > 4
        or counts["--semantic-hypothesis"] > 3
    ):
        return ["static_command_argv_mismatch"]
    return []


def _has_unquoted_shell_comment(value: str) -> bool:
    quote = ""
    escaped = False
    for character in value:
        if escaped:
            escaped = False
            continue
        if quote:
            if character == quote:
                quote = ""
            elif character in {"`", "\\"}:
                escaped = True
            continue
        if character in {"\"", "'"}:
            quote = character
        elif character in {"`", "\\"}:
            escaped = True
        elif character == "#":
            return True
    return False


def _static_command_tokens(
    value: str,
    *,
    shell: str,
) -> list[str] | None:
    if _has_unquoted_shell_operator(
        value,
        powershell=shell == "powershell",
    ):
        return None
    if shell == "git-bash":
        try:
            return shlex.split(value, comments=False, posix=True)
        except ValueError:
            return None
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    token_started = False
    for character in value:
        if escaped:
            current.append(character)
            token_started = True
            escaped = False
            continue
        if quote:
            if character == quote:
                quote = ""
            elif character == "`":
                escaped = True
            else:
                current.append(character)
            token_started = True
            continue
        if character in {"\"", "'"}:
            quote = character
            token_started = True
        elif character == "`":
            escaped = True
        elif character.isspace():
            if token_started:
                tokens.append("".join(current))
                current = []
                token_started = False
        else:
            current.append(character)
            token_started = True
    if quote or escaped:
        return None
    if token_started:
        tokens.append("".join(current))
    return tokens


def _linked_script_completions(
    cli_events: list[Any],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    call_ids = {
        _call_id(record)
        for record in records
        if _call_id(record)
    }
    return [
        record
        for record in _tool_completion_records(cli_events)
        if _call_id(record) in call_ids
    ]


def _completion_explicitly_failed(record: dict[str, Any]) -> bool:
    data = record.get("data")
    if not isinstance(data, dict):
        return False
    result = data.get("result")
    has_shell_exit, valid_shell_exit, _ = _validated_shell_exit_preview(
        data,
        result,
    )
    if has_shell_exit:
        return not valid_shell_exit
    if data.get("success") is False:
        return True
    if not isinstance(result, dict):
        return False
    content = result.get("content")
    if not isinstance(content, str):
        return False
    suffix = re.search(
        r"<shellId:\s*[^\r\n<>]+?\s+completed with "
        r"exit code\s+(-?[0-9]+)>\s*$",
        content,
    )
    return suffix is not None and int(suffix.group(1)) != 0


def _validated_shell_exit_preview(
    data: dict[str, Any],
    result: Any,
) -> tuple[bool, bool, str | None]:
    if not isinstance(result, dict) or "contents" not in result:
        return False, False, None
    contents = result.get("contents")
    content = result.get("content")
    if (
        data.get("success") is not True
        or not isinstance(contents, list)
        or len(contents) != 1
        or not isinstance(contents[0], dict)
    ):
        return True, False, None
    item = contents[0]
    if (
        item.get("type") != "shell_exit"
        or not isinstance(item.get("shellId"), str)
        or not item["shellId"]
        or not isinstance(item.get("exitCode"), int)
        or isinstance(item["exitCode"], bool)
        or item["exitCode"] != 0
        or item.get("outputTruncated") is not False
        or not isinstance(item.get("outputPreview"), str)
        or not isinstance(content, str)
    ):
        return True, False, None
    suffix = re.search(
        r"<shellId:\s*([^\r\n<>]+?)\s+completed with "
        r"exit code\s+(-?[0-9]+)>\s*$",
        content,
    )
    if (
        suffix is None
        or suffix.group(1) != item["shellId"]
        or int(suffix.group(2)) != 0
    ):
        return True, False, None
    return True, True, item["outputPreview"]


def _search_stdout_payload(
    completions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates: dict[str, dict[str, Any]] = {}
    for completion in completions:
        data = completion.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        has_shell_exit_schema, valid_shell_exit, shell_preview = (
            _validated_shell_exit_preview(
                data if isinstance(data, dict) else {},
                result,
            )
        )
        stdout_values: list[Any] = []
        if not has_shell_exit_schema:
            stdout_values.extend(
                completion.get(key)
                for key in ("stdout", "standard_output", "output")
                if isinstance(completion.get(key), (str, dict))
            )
        if isinstance(result, dict):
            if has_shell_exit_schema:
                if valid_shell_exit and shell_preview is not None:
                    stdout_values.append(shell_preview)
            else:
                content = result.get("content")
                if isinstance(content, (str, dict)):
                    stdout_values.append(content)
        for value in stdout_values:
            decoded: Any = value
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    continue
            if (
                isinstance(decoded, dict)
                and isinstance(decoded.get("status"), str)
                and (
                    "evidence" in decoded
                    or "related_context" in decoded
                    or "document_results" in decoded
                )
            ):
                key = json.dumps(
                    decoded,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                candidates[key] = decoded
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _search_stderr_text(
    completions: list[dict[str, Any]],
) -> str:
    values: list[str] = []
    for completion in completions:
        for value in _values_for_keys(
            completion, {"stderr", "standard_error"}
        ):
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return "\n".join(values)


def _validate_workspace_outputs(
    case: dict[str, Any],
    run_meta: dict[str, Any],
    fixture_workspace: Path,
) -> list[str]:
    failures: list[str] = []
    changes = run_meta.get("workspace_changes")
    allowed_writes = list(
        (case.get("io_contract") or {}).get("allowed_writes") or []
    )
    if not isinstance(changes, list):
        return ["workspace_change_metadata_invalid"]
    for change in changes:
        if not isinstance(change, dict):
            failures.append("workspace_change_metadata_invalid")
            continue
        path = str(change.get("path") or "")
        normalized = _normalize_audit_path(path).strip("/")
        if normalized not in {
            _normalize_audit_path(relative).strip("/")
            for relative in allowed_writes
        }:
            failures.append("workspace_change_outside_allowed_output")
    marker = str((case.get("io_contract") or {}).get("utf8_marker") or "")
    if marker:
        if len(allowed_writes) != 1:
            failures.append("utf8_output_contract_invalid")
        else:
            target = (
                fixture_workspace
                / Path(*allowed_writes[0].split("/"))
            )
            try:
                resolved = target.resolve(strict=True)
                resolved.relative_to(fixture_workspace.resolve(strict=True))
                text = resolved.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeError, ValueError):
                failures.append("utf8_output_missing_or_invalid")
            else:
                if marker not in text:
                    failures.append("utf8_output_marker_missing")
    return failures


def evaluate_run(
    case: dict[str, Any],
    profile: str,
    run_meta: dict[str, Any],
    cli_events: list[Any],
    otel_events: list[Any],
    *,
    cli_invalid_lines: int,
    otel_invalid_lines: int,
    fixture_workspace: Path | None = None,
) -> dict[str, Any]:
    expected = case["expected"]
    io_contract = case.get("io_contract") or {}
    allowed_reads = list(io_contract.get("allowed_reads") or [])
    allowed_writes = list(io_contract.get("allowed_writes") or [])
    allowed_skills = [
        str(value) for value in case.get("allowed_skills") or []
    ]
    if any(
        int(expected.get(key) or 0) > 0
        for key in (
            "list_dbs_calls",
            "search_calls",
            "result_detail_calls",
        )
    ) and "local-rag" not in allowed_skills:
        allowed_skills.append("local-rag")
    tools = _tool_records(cli_events)
    scripts = {
        name: _script_records(tools, name)
        for name in SCRIPT_NAMES
    }
    pointers, summary_paths, detail_paths, pointer_ids = _pointer_observations(
        cli_events, scripts
    )
    read_observed, approved_read_ids = _read_observations(
        tools,
        summary_paths,
        detail_paths,
        allowed_reads=allowed_reads,
        allowed_writes=allowed_writes,
        allowed_skills=allowed_skills,
        fixture_workspace=fixture_workspace,
    )
    observed: dict[str, Any] = {
        "list_dbs_calls": len(scripts["list_dbs.py"]),
        "search_calls": len(scripts["search.py"]),
        "result_detail_calls": len(scripts["result_detail.py"]),
        **read_observed,
        **pointers,
        "subagent_calls": _subagent_count(tools),
        "management_calls": _management_call_count(tools),
        "unapproved_tool_calls": _unapproved_tool_calls(
            tools,
            scripts,
            approved_read_ids,
            allowed_skills=allowed_skills,
        ),
        "skill_calls": _skill_call_counts(tools),
        "workspace_changes": _workspace_change_count(run_meta),
        "cli_invalid_jsonl_lines": cli_invalid_lines,
        "otel_invalid_jsonl_lines": otel_invalid_lines,
    }
    observed["automatic_retries"] = max(
        0, observed["search_calls"] - int(expected["search_calls"])
    )
    requested_models, selected_models = _models_from_telemetry(otel_events)
    requested = str(run_meta.get("requested_model") or "")
    observed["otel_requested_models"] = requested_models
    observed["selected_models"] = selected_models
    observed["requested_model"] = requested
    observed.update(_numeric_telemetry(otel_events))

    failures: list[str] = []
    for key, expected_value in expected.items():
        if key in observed and observed[key] != expected_value:
            failures.append(
                f"{key}: expected {expected_value}, observed {observed[key]}"
            )
    if not tools and any(expected[key] for key in (
        "list_dbs_calls", "search_calls", "result_detail_calls",
        "summary_file_reads", "detail_file_reads",
    )):
        failures.append("tool_call_telemetry_not_observed")
    if not selected_models:
        failures.append("selected_model_not_observed")
    elif profile != "auto" and len(selected_models) != 1:
        failures.append("multiple_or_conflicting_selected_models_observed")
    if profile == "auto":
        if requested.casefold() != "auto":
            failures.append("auto_profile_did_not_request_auto")
    elif (
        not requested
        or requested.casefold() == "auto"
        or requested_models != [requested]
    ):
        failures.append(
            "selected_model_does_not_match_runtime_requested_model"
        )
    exit_codes = run_meta.get("exit_codes")
    rendered_turns = run_meta.get("rendered_turns")
    if (
        not isinstance(exit_codes, list)
        or not isinstance(rendered_turns, list)
        or len(exit_codes) != len(rendered_turns)
        or len(exit_codes) == 0
        or any(code != 0 for code in exit_codes)
    ):
        failures.append("one_or_more_copilot_turns_failed")
    if not cli_events:
        failures.append("copilot_jsonl_event_stream_empty")
    if observed["unexpected_file_read_calls"]:
        failures.append("unexpected_file_read_tool_call")
    if observed["file_write_tool_calls"]:
        if observed["unexpected_file_write_calls"]:
            failures.append("unexpected_file_write_tool_call")
    if observed["unapproved_tool_calls"]:
        failures.append("unapproved_tool_call_observed")
    for script_name, records in scripts.items():
        if any(
            _completion_explicitly_failed(completion)
            for completion in _linked_script_completions(
                cli_events,
                records,
            )
        ):
            failures.append(
                f"{script_name.removesuffix('.py')}_process_failed"
            )
    if cli_invalid_lines:
        failures.append("copilot_jsonl_parse_error")
    if otel_invalid_lines:
        failures.append("otel_jsonl_parse_error")
    if fixture_workspace is not None:
        failures.extend(
            _validate_workspace_outputs(
                case, run_meta, fixture_workspace
            )
        )

    command_records = [
        record
        for name in SCRIPT_NAMES
        for record in scripts[name]
    ]
    for record in command_records:
        details = _direct_script_invocation_details(record)
        text = "\n".join(details[1] if details else [])
        if not DIRECT_VENV_RE.search(text):
            failures.append("rag_command_did_not_use_direct_venv_python")
        for label, pattern in FORBIDDEN_COMMAND_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"forbidden_command_pattern:{label}")
    for record in scripts["list_dbs.py"]:
        details = _direct_script_invocation_details(record)
        argv = details[1] if details else []
        if _option_values(argv, "--format") != ["json"]:
            failures.append("list_dbs_missing_json_format")
    for record in scripts["search.py"]:
        details = _direct_script_invocation_details(record)
        argv = details[1] if details else []
        if not _search_argv_is_structured(argv):
            failures.append("search_argv_invalid")
        for option in ("--compact-json", "--include-db-hint"):
            if argv.count(option) != 1:
                failures.append(f"search_missing_required_option:{option}")
        expected_delivery = str(
            (case.get("assertions") or {}).get(
                "search_result_delivery", "file"
            )
        )
        if _option_values(
            argv, "--result-delivery"
        ) != [expected_delivery]:
            failures.append(
                f"search_result_delivery_not_{expected_delivery}"
            )
        if (
            expected_delivery.casefold() == "stdout"
            and _option_values(argv, "--format") != ["json"]
        ):
            failures.append("search_stdout_missing_json_format")
        expected_db = str(
            (case.get("assertions") or {}).get("search_db") or ""
        )
        if not expected_db:
            failures.append("search_db_contract_missing")
        elif _option_values(argv, "--db") != [expected_db]:
            failures.append("search_db_mismatch")
    for record in scripts["result_detail.py"]:
        details = _direct_script_invocation_details(record)
        argv = details[1] if details else []
        expected_item = str(
            (case.get("assertions") or {}).get("detail_item_id") or ""
        )
        search_ids = sorted(pointer_ids["rag-result-pointer-v1"])
        if (
            len(search_ids) != 1
            or _option_values(argv, "--result-set-id") != search_ids
        ):
            failures.append("detail_result_set_mismatch")
        if _option_values(argv, "--detail-level") != ["expanded"]:
            failures.append("detail_level_mismatch")
        if _option_values(argv, "--result-delivery") != ["file"]:
            failures.append("detail_delivery_mismatch")
        if (
            not expected_item
            or _option_values(argv, "--item-id") != [expected_item]
        ):
            failures.append("detail_item_mismatch")

    rendered_turns = (
        run_meta.get("rendered_turns")
        if isinstance(run_meta.get("rendered_turns"), list)
        else []
    )
    search_prompts = rendered_turns[: int(expected["search_calls"])]
    if int(expected["search_calls"]) == 1 and rendered_turns:
        search_prompts = rendered_turns[:1]
    for prompt in search_prompts:
        if not any(
            (
                (details := _direct_script_invocation_details(record))
                is not None
                and details[1][-1] == str(prompt)
            )
            for record in scripts["search.py"]
        ):
            failures.append("complete_original_question_not_observed_in_search_call")
    assertions = case.get("assertions") or {}
    required_skill_calls = assertions.get("required_skill_calls")
    if isinstance(required_skill_calls, dict):
        for skill_name, expected_calls in required_skill_calls.items():
            if (
                not isinstance(skill_name, str)
                or isinstance(expected_calls, bool)
                or not isinstance(expected_calls, int)
                or expected_calls < 0
                or observed["skill_calls"].get(skill_name, 0)
                != expected_calls
            ):
                failures.append("required_skill_call_count_mismatch")
    assistant = _assistant_text(cli_events)
    failures.extend(
        _static_command_contract_failures(assertions, assistant)
    )
    for value in assertions.get("assistant_contains") or []:
        if str(value) not in assistant:
            failures.append("required_assistant_text_not_observed")
    for value in assertions.get("assistant_not_contains") or []:
        if str(value).casefold() in assistant.casefold():
            failures.append("forbidden_assistant_text_observed")
    stdout_contract = assertions.get("search_stdout_contract")
    if isinstance(stdout_contract, dict):
        completions = _linked_script_completions(
            cli_events, scripts["search.py"]
        )
        payload = _search_stdout_payload(completions)
        if payload is None:
            failures.append("search_stdout_json_not_observed")
        else:
            if payload.get("status") != stdout_contract.get("status"):
                failures.append("search_stdout_status_mismatch")
            if stdout_contract.get("evidence_empty") is True and list(
                payload.get("evidence") or []
            ):
                failures.append("search_stdout_evidence_not_empty")
            if (
                stdout_contract.get("related_context_nonempty") is True
                and not (
                    list(payload.get("related_context") or [])
                    or list(payload.get("document_results") or [])
                )
            ):
                failures.append("search_stdout_related_context_empty")
        if assertions.get("search_stderr_warning") is True:
            stderr_text = _search_stderr_text(completions)
            if not re.search(r"\bwarn(?:ing)?\b", stderr_text, re.IGNORECASE):
                failures.append("search_stderr_warning_not_observed")
    return {
        "case_id": case["id"],
        "profile": profile,
        "status": "PASS" if not failures else "FAIL",
        "failures": list(dict.fromkeys(failures)),
        "observed": observed,
    }


def _run_metadata_failures(
    case: dict[str, Any],
    profile: str,
    run_meta: Any,
    *,
    phase: str,
    repetition: int,
) -> list[str]:
    if not isinstance(run_meta, dict):
        return ["run_metadata_is_not_an_object"]
    failures: list[str] = []
    if run_meta.get("schema_version") != "copilot-compliance-run-v1":
        failures.append("invalid_run_metadata_schema")
    if run_meta.get("case_id") != case["id"]:
        failures.append("run_metadata_case_mismatch")
    if run_meta.get("profile") != profile:
        failures.append("run_metadata_profile_mismatch")
    session_id = str(run_meta.get("session_id") or "")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        session_id,
        re.IGNORECASE,
    ):
        failures.append("invalid_session_id")
    metadata_phase = str(run_meta.get("phase") or "A").upper()
    if metadata_phase != phase.upper():
        failures.append("run_metadata_phase_mismatch")
    metadata_repetition = run_meta.get("repetition", 1)
    if metadata_repetition != repetition:
        failures.append("run_metadata_repetition_mismatch")
    rendered_turns = run_meta.get("rendered_turns")
    if (
        not isinstance(rendered_turns, list)
        or len(rendered_turns) != len(case["turns"])
        or not all(
            isinstance(value, str) and value
            for value in rendered_turns
        )
    ):
        failures.append("invalid_rendered_turns")
    elif rendered_turns != [
        str(turn["prompt"]) for turn in case["turns"]
    ]:
        failures.append("rendered_turns_do_not_match_case")
    return failures


def _apply_profile_integrity(results: list[dict[str, Any]]) -> None:
    requested_by_profile: dict[str, set[str]] = {
        profile: {
            str(result.get("observed", {}).get("requested_model") or "")
            for result in results
            if result.get("profile") == profile
        }
        for profile in REQUIRED_PROFILES
    }
    mini = requested_by_profile["mini"] - {""}
    standard = requested_by_profile["standard"] - {""}
    if len(mini) != 1 or len(standard) != 1 or mini == standard:
        for result in results:
            if result.get("profile") in {"mini", "standard"}:
                result["failures"] = list(
                    dict.fromkeys(
                        [
                            *result.get("failures", []),
                            "mini_standard_runtime_models_not_distinct",
                        ]
                    )
                )
                result["status"] = "FAIL"


def _execution_matrix(
    cases: list[dict[str, Any]],
    phase: str,
) -> list[tuple[dict[str, Any], str, int]]:
    executions: list[tuple[dict[str, Any], str, int]] = []
    if phase == "a":
        for case in cases:
            for profile in REQUIRED_PROFILES:
                executions.append((case, profile, 1))
        return executions
    for case in cases:
        repetitions = case.get("phase_b_repetitions")
        if not isinstance(repetitions, dict):
            continue
        for profile in REQUIRED_PROFILES:
            for repetition in range(
                1, int(repetitions[profile]) + 1
            ):
                executions.append((case, profile, repetition))
    return executions


def collect(
    cases_path: Path,
    raw_root: Path,
    *,
    variables_path: Path,
    fixture_workspace: Path,
    phase: str = "a",
) -> dict[str, Any]:
    cases = load_cases(cases_path)
    variables = load_variables(variables_path)
    results: list[dict[str, Any]] = []
    executions = _execution_matrix(cases, phase)
    expected_count = 48 if phase == "a" else 64
    if len(executions) != expected_count:
        raise ValueError(
            f"Phase {phase.upper()} execution matrix must contain "
            f"{expected_count} rows; found {len(executions)}"
        )
    for canonical_case, profile, repetition in executions:
        phase_prefix = Path() if phase == "a" else Path("phase-b")
        run_dir = (
            raw_root
            / phase_prefix
            / profile
            / canonical_case["id"]
        )
        if phase == "b":
            run_dir = run_dir / f"repeat-{repetition:02d}"
        try:
            run_meta = json.loads(
                (run_dir / "run.json").read_text(encoding="utf-8-sig")
            )
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
            results.append(
                {
                    "case_id": canonical_case["id"],
                    "profile": profile,
                    "repetition": repetition,
                    "status": "FAIL",
                    "failures": ["missing_or_invalid_run_metadata"],
                    "observed": {},
                }
            )
            continue
        execution_tag = str(
            run_meta.get("session_id") or ""
        ) if isinstance(run_meta, dict) else ""
        try:
            case = render_case(
                canonical_case,
                variables,
                execution_tag=execution_tag,
            )
        except ValueError:
            case = canonical_case
        metadata_failures = _run_metadata_failures(
            case,
            profile,
            run_meta,
            phase=phase,
            repetition=repetition,
        )
        if not isinstance(run_meta, dict):
            run_meta = {}
        cli, cli_invalid = _load_jsonl(run_dir / "copilot.jsonl")
        otel, otel_invalid = _load_jsonl(run_dir / "otel.jsonl")
        result = evaluate_run(
            case,
            profile,
            run_meta,
            cli,
            otel,
            cli_invalid_lines=cli_invalid,
            otel_invalid_lines=otel_invalid,
            fixture_workspace=fixture_workspace,
        )
        result["repetition"] = repetition
        if metadata_failures:
            result["failures"] = list(
                dict.fromkeys([*result["failures"], *metadata_failures])
            )
            result["status"] = "FAIL"
        results.append(result)
    _apply_profile_integrity(results)
    passed = sum(result["status"] == "PASS" for result in results)
    return {
        "schema_version": REPORT_SCHEMA,
        "phase": phase.upper(),
        "case_definition_count": len(cases),
        "execution_count": len(results),
        "expected_execution_count": expected_count,
        "required_profiles": list(REQUIRED_PROFILES),
        "passed": passed,
        "failed": len(results) - passed,
        "overall_status": "PASS" if passed == len(results) else "FAIL",
        "results": results,
    }


def self_test(cases_path: Path | None = None) -> int:
    case = {
        "id": "CPL-000",
        "turns": [{"prompt": "Synthetic compliance question"}],
        "assertions": {"search_db": "synthetic-rag"},
        "expected": {
            "list_dbs_calls": 0,
            "search_calls": 1,
            "result_detail_calls": 0,
            "summary_file_reads": 1,
            "detail_file_reads": 0,
            "requested_file_reads": 0,
            "approved_file_writes": 0,
            "management_calls": 0,
            "manifest_reads": 0,
            "raw_item_reads": 0,
            "result_pointers": 1,
            "detail_pointers": 0,
            "subagent_calls": 0,
            "automatic_retries": 0,
            "workspace_changes": 0,
        },
    }
    prompt = "Synthetic compliance question"
    venv = r"X:\.copilot\rag\query\.venv\Scripts\python.exe"
    search = {
        "type": "tool.execution_start",
        "toolCallId": "call-1",
        "toolName": "powershell",
        "arguments": {
            "command": (
                f'& "{venv}" "X:\\.copilot\\rag\\query\\search.py" '
                "--db synthetic-rag --include-db-hint --compact-json "
                f'--result-delivery file --format json "{prompt}"'
            )
        },
    }
    read = {
        "type": "tool.execution_start",
        "toolCallId": "call-2",
        "toolName": "read_file",
        "arguments": {"path": "TMP/GitHubCopilotLocalRAG/results/id/summary.json"},
    }
    pointer = {
        "type": "tool.execution_complete",
        "toolCallId": "call-1",
        "output": json.dumps(
            {
                "schema_version": "rag-result-pointer-v1",
                "result_set_id": "synthetic-result-id",
                "summary_file": "TMP/GitHubCopilotLocalRAG/results/id/summary.json",
            }
        ),
    }
    otel = [
        {
            "name": "chat runtime-mini-placeholder",
            "attributes": [
                {
                    "key": "gen_ai.request.model",
                    "value": {"stringValue": "runtime-mini-placeholder"},
                }
            ],
        }
    ]
    meta = {
        "schema_version": "copilot-compliance-run-v1",
        "case_id": "CPL-000",
        "profile": "mini",
        "phase": "A",
        "repetition": 1,
        "requested_model": "runtime-mini-placeholder",
        "session_id": "12345678-1234-4234-8234-1234567890ab",
        "exit_codes": [0],
        "rendered_turns": [prompt],
        "workspace_changes": [],
        "rendered_case": {
            "expected": {"search_calls": 0}
        },
    }
    result = evaluate_run(
        case,
        "mini",
        meta,
        [search, read, pointer],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if result["status"] != "PASS":
        raise AssertionError(result)
    detail_case = json.loads(json.dumps(case))
    detail_case["turns"].append({"prompt": "Expand E1"})
    detail_case["assertions"]["detail_item_id"] = "E1"
    detail_case["expected"]["result_detail_calls"] = 1
    detail_case["expected"]["detail_file_reads"] = 1
    detail_case["expected"]["detail_pointers"] = 1
    detail_meta = {
        **meta,
        "rendered_turns": [prompt, "Expand E1"],
        "exit_codes": [0, 0],
    }
    detail = {
        "type": "tool.execution_start",
        "toolCallId": "detail-call",
        "toolName": "powershell",
        "arguments": {"command": (
            f'& "{venv}" "X:\\.copilot\\rag\\query\\result_detail.py" '
            "--result-set-id synthetic-result-id --item-id E1 "
            "--detail-level expanded "
            "--result-delivery file"
        )},
    }
    detail_pointer = {
        "type": "tool.execution_complete",
        "toolCallId": "detail-call",
        "output": json.dumps({
            "schema_version": "rag-detail-pointer-v1",
            "result_set_id": "synthetic-result-id",
            "detail_file": "TMP/GitHubCopilotLocalRAG/results/id/detail.json",
        }),
    }
    detail_read = {
        "type": "tool.execution_start",
        "toolCallId": "detail-read",
        "toolName": "view",
        "arguments": {
            "path": "TMP/GitHubCopilotLocalRAG/results/id/detail.json"
        },
    }
    detail_events = [search, read, pointer, detail, detail_pointer, detail_read]
    detail_ok = evaluate_run(
        detail_case, "mini", detail_meta, detail_events, otel,
        cli_invalid_lines=0, otel_invalid_lines=0,
    )
    if detail_ok["status"] != "PASS":
        raise AssertionError(detail_ok)
    for label, old, new in (
        ("wrong_result", "--result-set-id synthetic-result-id", "--result-set-id other"),
        ("missing_result", "--result-set-id synthetic-result-id ", ""),
        ("duplicate_result", "--result-set-id synthetic-result-id", "--result-set-id synthetic-result-id --result-set-id other"),
        ("wrong_level", "--detail-level expanded", "--detail-level deep"),
        ("wrong_delivery", "--result-delivery file", "--result-delivery stdout"),
        ("wrong_item", "--item-id E1", "--item-id D1"),
    ):
        changed = json.loads(json.dumps(detail))
        changed["arguments"]["command"] = changed["arguments"]["command"].replace(old, new)
        bad = evaluate_run(
            detail_case, "mini", detail_meta,
            [search, read, pointer, changed, detail_pointer, detail_read],
            otel, cli_invalid_lines=0, otel_invalid_lines=0,
        )
        if bad["status"] != "FAIL":
            raise AssertionError(f"detail {label} accepted")
    for label, replacement in (
        ("prefix", "prefix " + prompt),
        ("suffix", prompt + " suffix"),
        ("rewrite", "Synthetic rewritten question"),
    ):
        changed = json.loads(json.dumps(search))
        changed["arguments"]["command"] = changed["arguments"][
            "command"
        ].replace(f'"{prompt}"', f'"{replacement}"')
        changed_result = evaluate_run(
            case,
            "mini",
            meta,
            [changed],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
        )
        if (
            changed_result["status"] != "FAIL"
            or "complete_original_question_not_observed_in_search_call"
            not in changed_result["failures"]
        ):
            raise AssertionError(f"final query {label} accepted")
    nonfinal_prompt = json.loads(json.dumps(search))
    nonfinal_prompt["arguments"]["command"] = nonfinal_prompt[
        "arguments"
    ]["command"].replace(
        f'--format json "{prompt}"',
        f'--format json --facet "{prompt}" "rewritten"',
    )
    nonfinal_result = evaluate_run(
        case,
        "mini",
        meta,
        [nonfinal_prompt],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        nonfinal_result["status"] != "FAIL"
        or "complete_original_question_not_observed_in_search_call"
        not in nonfinal_result["failures"]
    ):
        raise AssertionError("non-final full prompt accepted")
    nested_argv = {
        "type": "tool.execution_start",
        "toolName": "powershell",
        "arguments": {
            "command": "Write-Output evil",
            "metadata": {
                "argv": [
                    venv,
                    r"X:\.copilot\rag\query\search.py",
                    prompt,
                ]
            },
        },
    }
    nested_argv_result = evaluate_run(
        case,
        "mini",
        meta,
        [nested_argv],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        nested_argv_result["status"] != "FAIL"
        or nested_argv_result["observed"]["search_calls"] != 0
    ):
        raise AssertionError("nested argv spoof accepted")
    evil_top_argv = {
        "type": "tool.execution_start",
        "toolName": "evil_tool",
        "arguments": {
            "argv": [
                venv,
                r"X:\.copilot\rag\query\search.py",
                prompt,
            ]
        },
    }
    evil_top_argv_result = evaluate_run(
        case, "mini", meta, [evil_top_argv], otel,
        cli_invalid_lines=0, otel_invalid_lines=0,
    )
    if evil_top_argv_result["observed"]["search_calls"] != 0:
        raise AssertionError("evil-tool top-level argv accepted")
    ambiguous_args = json.loads(json.dumps(search))
    ambiguous_args["arguments"]["argv"] = [
        venv,
        r"X:\.copilot\rag\query\search.py",
        prompt,
    ]
    ambiguous_result = evaluate_run(
        case, "mini", meta, [ambiguous_args], otel,
        cli_invalid_lines=0, otel_invalid_lines=0,
    )
    if ambiguous_result["observed"]["search_calls"] != 0:
        raise AssertionError("argv plus command ambiguity accepted")
    dual_command = json.loads(json.dumps(search))
    dual_command["arguments"]["cmd"] = dual_command["arguments"]["command"]
    dual_command_result = evaluate_run(
        case, "mini", meta, [dual_command], otel,
        cli_invalid_lines=0, otel_invalid_lines=0,
    )
    if dual_command_result["observed"]["search_calls"] != 0:
        raise AssertionError("command plus cmd ambiguity accepted")
    split_facet = json.loads(json.dumps(search))
    split_facet["arguments"]["command"] = split_facet["arguments"][
        "command"
    ].replace(
        f'"{prompt}"',
        f'--facet retained table row "{prompt}"',
    )
    split_facet_result = evaluate_run(
        case,
        "mini",
        meta,
        [split_facet, read, pointer],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        split_facet_result["status"] != "FAIL"
        or "search_argv_invalid" not in split_facet_result["failures"]
    ):
        raise AssertionError("split multiword facet was accepted")
    free_text_answer_goal = json.loads(json.dumps(search))
    free_text_answer_goal["arguments"]["command"] = (
        free_text_answer_goal["arguments"]["command"].replace(
            f'"{prompt}"',
            (
                '--answer-goal "evidence about what the row establishes" '
                f'"{prompt}"'
            ),
        )
    )
    free_text_goal_result = evaluate_run(
        case,
        "mini",
        meta,
        [free_text_answer_goal, read, pointer],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        free_text_goal_result["status"] != "FAIL"
        or "search_argv_invalid" not in free_text_goal_result["failures"]
    ):
        raise AssertionError("free-text answer goal was accepted")
    for label, old, new in (
        ("wrong_db", "--db synthetic-rag", "--db other-rag"),
        ("missing_db", "--db synthetic-rag ", ""),
        (
            "duplicate_db",
            "--db synthetic-rag",
            "--db synthetic-rag --db other-rag",
        ),
        (
            "duplicate_delivery",
            "--result-delivery file",
            "--result-delivery file --result-delivery stdout",
        ),
    ):
        changed = json.loads(json.dumps(search))
        changed["arguments"]["command"] = changed["arguments"][
            "command"
        ].replace(old, new)
        changed_result = evaluate_run(
            case, "mini", meta, [changed], otel,
            cli_invalid_lines=0, otel_invalid_lines=0,
        )
        if changed_result["status"] != "FAIL":
            raise AssertionError(f"{label} option spoof accepted")
    delta_before_execution = {
        "type": "assistant.tool_call_delta",
        "data": {
            "toolCallId": "call-1",
            "toolName": "powershell",
            "inputDelta": '{"command":"incomplete',
        },
    }
    delta_result = evaluate_run(
        case,
        "mini",
        meta,
        [delta_before_execution, search, read, pointer],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if delta_result["status"] != "PASS":
        raise AssertionError(
            "tool-call delta hid the complete execution record: "
            f"{delta_result}"
        )
    session_metadata = {
        "type": "session.tools_updated",
        "data": {"model": "runtime-mini-placeholder"},
    }
    skill_request = {
        "type": "assistant.message",
        "toolRequests": [
            {
                "toolCallId": "call-skill",
                "name": "skill",
                "arguments": {"skill": "local-rag"},
            }
        ],
    }
    metadata_and_skill_result = evaluate_run(
        case,
        "mini",
        meta,
        [
            session_metadata,
            skill_request,
            delta_before_execution,
            search,
            read,
            pointer,
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if metadata_and_skill_result["status"] != "PASS":
        raise AssertionError(
            "session metadata or an allowed exact skill was misclassified: "
            f"{metadata_and_skill_result}"
        )
    skill_count_case = json.loads(json.dumps(case))
    skill_count_case.setdefault("assertions", {})[
        "required_skill_calls"
    ] = {"local-rag": 1}
    exact_skill_result = evaluate_run(
        skill_count_case,
        "mini",
        meta,
        [skill_request, search, read, pointer],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if exact_skill_result["status"] != "PASS":
        raise AssertionError(exact_skill_result)
    duplicate_skill_request = {
        "type": "assistant.message",
        "toolRequests": [
            {
                "toolCallId": "call-skill-duplicate",
                "name": "skill",
                "arguments": {"skill": "local-rag"},
            }
        ],
    }
    wrong_skill_request = {
        "type": "assistant.message",
        "toolRequests": [
            {
                "toolCallId": "call-skill-wrong",
                "name": "skill",
                "arguments": {"skill": "local-rag-admin"},
            }
        ],
    }
    nested_required_skill_request = {
        "type": "assistant.message",
        "toolRequests": [
            {
                "toolCallId": "call-skill-nested-required",
                "name": "skill",
                "arguments": {
                    "payload": {"skill": "local-rag"},
                },
            }
        ],
    }
    for label, prefix in {
        "missing": [],
        "duplicate": [skill_request, duplicate_skill_request],
        "wrong": [wrong_skill_request],
        "nested": [nested_required_skill_request],
    }.items():
        skill_count_result = evaluate_run(
            skill_count_case,
            "mini",
            meta,
            [*prefix, search, read, pointer],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
        )
        if (
            skill_count_result["status"] != "FAIL"
            or "required_skill_call_count_mismatch"
            not in skill_count_result["failures"]
        ):
            raise AssertionError(
                f"required skill call spoof accepted: {label}"
            )
    skill_text_only = {
        "type": "tool.execution_start",
        "toolCallId": "call-skill-text",
        "toolName": "other_tool",
        "arguments": {"note": "mention local-rag only"},
    }
    if _unapproved_tool_calls(
        [skill_text_only],
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError(
            "a text-only skill mention bypassed the tool allowlist"
        )
    for spoofed_skill in (
        "local-rag-extra",
        "prefix-local-rag",
    ):
        spoofed_record = {
            "type": "tool.execution_start",
            "toolCallId": f"call-{spoofed_skill}",
            "toolName": "skill",
            "arguments": {"skill": spoofed_skill},
        }
        if _unapproved_tool_calls(
            [spoofed_record],
            {name: [] for name in SCRIPT_NAMES},
            set(),
            allowed_skills=["local-rag"],
        ) != 1:
            raise AssertionError(
                "a prefix or suffix skill name bypassed the allowlist"
            )
    nested_skill_spoof = {
        "type": "tool.execution_start",
        "toolCallId": "call-nested-skill",
        "toolName": "skill",
        "arguments": {"payload": {"skill": "local-rag"}},
    }
    if _unapproved_tool_calls(
        [nested_skill_spoof],
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError("a nested skill value bypassed the allowlist")
    structured_skill_note_spoof = {
        "type": "tool.execution_start",
        "toolCallId": "call-skill-note-spoof",
        "toolName": "skill",
        "arguments": {
            "skill": "evil",
            "note": "/local-rag/SKILL.md",
        },
    }
    if _unapproved_tool_calls(
        [structured_skill_note_spoof],
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError(
            "structured evil skill used a content-path allowlist bypass"
        )
    structured_skill_nested_spoof = {
        "type": "tool.execution_start",
        "toolCallId": "call-skill-nested-spoof",
        "toolName": "skill",
        "arguments": {
            "skill": "evil",
            "nested": {"skill": "local-rag"},
        },
    }
    if _unapproved_tool_calls(
        [structured_skill_nested_spoof],
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError(
            "a nested allowed skill overrode the direct evil skill"
        )
    multiple_requests = {
        "type": "assistant.message",
        "toolRequests": [
            {
                "toolCallId": "call-multi-skill",
                "name": "skill",
                "arguments": {"skill": "local-rag"},
            },
            {
                "toolCallId": "call-multi-other",
                "name": "other_tool",
                "arguments": {"value": "unapproved"},
            },
        ],
    }
    multiple_records = _tool_records([multiple_requests])
    if len(multiple_records) != 2 or _unapproved_tool_calls(
        multiple_records,
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError(
            "multiple tool requests were not audited independently"
        )
    same_id_conflict = {
        "type": "assistant.message",
        "toolRequests": [
            {
                "toolCallId": "call-same-id",
                "name": "skill",
                "arguments": {"skill": "local-rag"},
            },
            {
                "toolCallId": "call-same-id",
                "name": "other_tool",
                "arguments": {"value": "unapproved"},
            },
        ],
    }
    same_id_records = _tool_records([same_id_conflict])
    if len(same_id_records) != 2 or _unapproved_tool_calls(
        same_id_records,
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError(
            "same-ID conflicting tool requests were deduplicated"
        )
    no_id_shell = {
        "type": "tool.execution_start",
        "toolName": "powershell",
        "arguments": {"command": "Write-Output unapproved"},
    }
    no_id_records = _tool_records([no_id_shell])
    if len(no_id_records) != 1 or _unapproved_tool_calls(
        no_id_records,
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError("a no-ID shell execution evaded auditing")
    for tool_name, with_id in (
        ("delta_shell", True),
        ("delta_shell", False),
        ("send_message", True),
        ("result_tool", True),
        ("complete_tool", True),
    ):
        execution = {
            "type": "tool.execution_start",
            "toolName": tool_name,
            "arguments": {"value": "unapproved"},
        }
        if with_id:
            execution["toolCallId"] = f"call-{tool_name}"
        execution_records = _tool_records([execution])
        if len(execution_records) != 1 or _unapproved_tool_calls(
            execution_records,
            {name: [] for name in SCRIPT_NAMES},
            set(),
            allowed_skills=["local-rag"],
        ) != 1:
            raise AssertionError(
                f"tool-name lifecycle substring evaded auditing: {tool_name}"
            )
        if _tool_completion_records([execution]):
            raise AssertionError(
                f"tool name was misclassified as completion: {tool_name}"
            )
    partial_result = {
        "type": "tool.execution_partial_result",
        "data": {
            "toolCallId": "call-partial-result",
            "output": "partial native output",
        },
    }
    if _tool_records([partial_result]):
        raise AssertionError("partial-result lifecycle event became a tool call")
    partial_result_named_tool = {
        "type": "tool.execution_start",
        "toolCallId": "call-partial-named-tool",
        "toolName": "tool.execution_partial_result",
        "arguments": {"value": "unapproved"},
    }
    partial_named_records = _tool_records([partial_result_named_tool])
    if len(partial_named_records) != 1 or _unapproved_tool_calls(
        partial_named_records,
        {name: [] for name in SCRIPT_NAMES},
        set(),
        allowed_skills=["local-rag"],
    ) != 1:
        raise AssertionError(
            "a partial-result-like tool name evaded auditing"
        )
    if _run_metadata_failures(
        case, "mini", meta, phase="a", repetition=1
    ):
        raise AssertionError("valid metadata was rejected")
    invalid_meta = dict(meta)
    invalid_meta["case_id"] = "CPL-999"
    if "run_metadata_case_mismatch" not in _run_metadata_failures(
        case,
        "mini",
        invalid_meta,
        phase="a",
        repetition=1,
    ):
        raise AssertionError("metadata identity mismatch was not detected")
    duplicate = evaluate_run(
        case,
        "mini",
        meta,
        [
            search,
            {**search, "toolCallId": "call-3"},
            read,
            pointer,
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if duplicate["status"] != "FAIL" or not any(
        "search_calls" in failure for failure in duplicate["failures"]
    ):
        raise AssertionError("duplicate-search gate did not fail")
    delegated = evaluate_run(
        case,
        "mini",
        meta,
        [
            search,
            read,
            pointer,
            {
                "type": "tool.execution_start",
                "toolCallId": "call-4",
                "toolName": "spawn_agent",
                "arguments": {},
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if delegated["status"] != "FAIL" or "subagent_calls" not in " ".join(
        delegated["failures"]
    ):
        raise AssertionError("subagent gate did not fail")
    write_attempt = evaluate_run(
        case,
        "mini",
        meta,
        [
            search,
            read,
            pointer,
            {
                "type": "tool.execution_start",
                "toolCallId": "call-5",
                "toolName": "write_file",
                "arguments": {"path": "synthetic.txt"},
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        write_attempt["status"] != "FAIL"
        or "unexpected_file_write_tool_call"
        not in write_attempt["failures"]
    ):
        raise AssertionError("file-write gate did not fail")
    arbitrary_shell = evaluate_run(
        case,
        "mini",
        meta,
        [
            search,
            read,
            pointer,
            {
                "type": "tool.execution_start",
                "toolCallId": "call-6",
                "toolName": "powershell",
                "arguments": {
                    "command": "python -c \"open('source.py').read()\""
                },
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        arbitrary_shell["status"] != "FAIL"
        or "unapproved_tool_call_observed"
        not in arbitrary_shell["failures"]
    ):
        raise AssertionError("fail-closed tool allowlist did not fail")
    spoofed_pointer = evaluate_run(
        case,
        "mini",
        meta,
        [
            search,
            read,
            {
                "type": "assistant.message",
                "content": pointer["output"],
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        spoofed_pointer["status"] != "FAIL"
        or not any(
            "result_pointers" in failure
            for failure in spoofed_pointer["failures"]
        )
    ):
        raise AssertionError("unlinked pointer text satisfied the pointer gate")
    nested_id_search = json.loads(json.dumps(search))
    nested_id_search.pop("toolCallId", None)
    nested_id_search["arguments"]["metadata"] = {"call_id": "call-1"}
    nested_id_result = evaluate_run(
        case,
        "mini",
        meta,
        [nested_id_search, pointer, read],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        nested_id_result["status"] != "FAIL"
        or nested_id_result["observed"]["result_pointers"] != 0
    ):
        raise AssertionError("nested call-id spoof linked a pointer")
    spoofed_read = evaluate_run(
        case,
        "mini",
        meta,
        [
            search,
            pointer,
            {
                "type": "tool.execution_start",
                "toolCallId": "call-7",
                "toolName": "powershell",
                "arguments": {"command": "Write-Output summary.json"},
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        spoofed_read["status"] != "FAIL"
        or spoofed_read["observed"]["summary_file_reads"] != 0
    ):
        raise AssertionError("textual path mention satisfied the read gate")
    fake_search = {
        "type": "tool.execution_start",
        "toolCallId": "call-8",
        "toolName": "powershell",
        "arguments": {
            "command": "Write-Output " + json.dumps(
                _values_for_keys(search, {"command"})[0]
            )
        },
    }
    fake_pointer = {
        **pointer,
        "toolCallId": "call-8",
    }
    combined_spoof = evaluate_run(
        case,
        "mini",
        meta,
        [
            fake_search,
            fake_pointer,
            {
                "type": "tool.execution_start",
                "toolCallId": "call-9",
                "toolName": "powershell",
                "arguments": {
                    "command": (
                        "[IO.File]::ReadAllText('source.py'); "
                        "Write-Output 'TMP/GitHubCopilotLocalRAG/"
                        "results/id/summary.json'"
                    )
                },
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if combined_spoof["status"] != "FAIL" or any(
        combined_spoof["observed"][key]
        for key in ("search_calls", "result_pointers", "summary_file_reads")
    ):
        raise AssertionError("fake command/read combined spoof was accepted")
    compound_search = json.loads(json.dumps(search))
    compound_search["arguments"]["command"] += (
        r" \; $null = Get-Content source.py"
    )
    compound_result = evaluate_run(
        case,
        "mini",
        meta,
        [compound_search],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        compound_result["status"] != "FAIL"
        or compound_result["observed"]["search_calls"] != 0
    ):
        raise AssertionError(
            "PowerShell backslash compound-command spoof was accepted"
        )
    continued_search = json.loads(json.dumps(search))
    continued_search["arguments"]["command"] = (
        continued_search["arguments"]["command"]
        .replace(
            f'"{venv}" "X:\\.copilot\\rag\\query\\search.py"',
            (
                f'"{venv}" `\n'
                '  "X:\\.copilot\\rag\\query\\search.py"'
            ),
        )
        .replace(
            "--db synthetic-rag --include-db-hint",
            "--db synthetic-rag `\n  --include-db-hint",
        )
    )
    continued_result = evaluate_run(
        case,
        "mini",
        meta,
        [continued_search, read, pointer],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if continued_result["status"] != "PASS":
        raise AssertionError(
            "PowerShell line continuation was not recognized"
        )
    trailing_space_continuation = json.loads(json.dumps(search))
    trailing_space_continuation["arguments"]["command"] += (
        "`   \nWrite-Output evil"
    )
    trailing_space_result = evaluate_run(
        case,
        "mini",
        meta,
        [trailing_space_continuation],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        trailing_space_result["status"] != "FAIL"
        or trailing_space_result["observed"]["search_calls"] != 0
    ):
        raise AssertionError(
            "PowerShell trailing-space continuation spoof was accepted"
        )
    git_bash_smuggle = {
        "type": "tool.execution_start",
        "toolName": "bash",
        "arguments": {
            "command": (
                '"$HOME/.copilot/rag/query/.venv/Scripts/python.exe" '
                '"$HOME/.copilot/rag/query/search.py" '
                "--db synthetic-rag --include-db-hint --compact-json "
                f'--result-delivery file --format json "{prompt}" '
                "`echo evil`"
            )
        },
    }
    git_bash_result = evaluate_run(
        case,
        "mini",
        meta,
        [git_bash_smuggle],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        git_bash_result["status"] != "FAIL"
        or git_bash_result["observed"]["search_calls"] != 0
    ):
        raise AssertionError("Git Bash backtick smuggling accepted")
    for label, tool, injected in (
        ("ps_dollar", "powershell", '"$(Write-Output evil)"'),
        ("bash_dollar", "bash", '"$(echo evil)"'),
        ("bash_backtick", "bash", '"`echo evil`"'),
        ("ps_single_backtick", "powershell", "'x`'; Write-Output evil"),
        ("ps_backslash_comment", "powershell", r"\# hidden"),
    ):
        smuggle = json.loads(json.dumps(search))
        smuggle["toolName"] = tool
        if tool == "bash":
            smuggle["arguments"]["command"] = smuggle["arguments"][
                "command"
            ].lstrip("& ")
        smuggle["arguments"]["command"] = smuggle["arguments"][
            "command"
        ].replace(
            f'--format json "{prompt}"',
            f'--format json --facet {injected} "{prompt}"',
        )
        smuggle_result = evaluate_run(
            case, "mini", meta, [smuggle], otel,
            cli_invalid_lines=0, otel_invalid_lines=0,
        )
        if smuggle_result["observed"]["search_calls"] != 0:
            raise AssertionError(f"{label} substitution accepted")
    wrong_pointer_path = evaluate_run(
        case,
        "mini",
        meta,
        [
            search,
            pointer,
            {
                "type": "tool.execution_start",
                "toolCallId": "call-wrong-path",
                "toolName": "read_file",
                "arguments": {
                    "path": (
                        "TMP/GitHubCopilotLocalRAG/results/"
                        "different/summary.json"
                    )
                },
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        wrong_pointer_path["status"] != "FAIL"
        or wrong_pointer_path["observed"]["summary_file_reads"] != 0
        or wrong_pointer_path["observed"][
            "unexpected_file_read_calls"
        ] != 1
    ):
        raise AssertionError("summary read was not bound to pointer path")
    scoped_read_case = {
        "id": "CPL-011",
        "turns": [{"prompt": "Read fixtures/queries.txt"}],
        "io_contract": {
            "allowed_reads": ["fixtures/queries.txt"],
            "allowed_writes": [],
        },
        "expected": {
            key: 0 for key in EXPECTED_COUNT_KEYS
        },
    }
    scoped_read_case["expected"]["requested_file_reads"] = 1
    scoped_meta = {
        **meta,
        "case_id": "CPL-011",
        "rendered_turns": ["Read fixtures/queries.txt"],
    }
    scoped_read = {
        "type": "tool.execution_start",
        "toolCallId": "scoped-read",
        "toolName": "read_file",
        "arguments": {"path": "/fixture/fixtures/queries.txt"},
    }
    scoped_read_result = evaluate_run(
        scoped_read_case,
        "mini",
        scoped_meta,
        [
            scoped_read,
            {
                "type": "assistant.message",
                "content": "synthetic file content",
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
        fixture_workspace=Path("/fixture"),
    )
    if scoped_read_result["status"] != "PASS":
        raise AssertionError(scoped_read_result)
    nested_read_result = evaluate_run(
        scoped_read_case,
        "mini",
        scoped_meta,
        [{
            "type": "tool.execution_start",
            "toolName": "evil_tool",
            "arguments": {
                "payload": {"path": "/fixture/fixtures/queries.txt"}
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
        fixture_workspace=Path("/fixture"),
    )
    if (
        nested_read_result["status"] != "FAIL"
        or nested_read_result["observed"]["requested_file_reads"] != 0
    ):
        raise AssertionError("nested allowed-read path spoof accepted")
    nested_kind_read = {
        "type": "tool.execution_start",
        "toolName": "evil_tool",
        "arguments": {
            "path": "/fixture/fixtures/queries.txt",
            "metadata": {"name": "read_file"},
        },
    }
    nested_kind_read_result = evaluate_run(
        scoped_read_case, "mini", scoped_meta, [nested_kind_read], otel,
        cli_invalid_lines=0, otel_invalid_lines=0,
        fixture_workspace=Path("/fixture"),
    )
    if nested_kind_read_result["observed"]["requested_file_reads"] != 0:
        raise AssertionError("nested read kind spoof accepted")
    suffix_spoof = {
        **scoped_read,
        "arguments": {"path": "/other/fixtures/queries.txt"},
    }
    suffix_spoof_result = evaluate_run(
        scoped_read_case,
        "mini",
        scoped_meta,
        [suffix_spoof],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
        fixture_workspace=Path("/fixture"),
    )
    if (
        suffix_spoof_result["status"] != "FAIL"
        or suffix_spoof_result["observed"]["requested_file_reads"] != 0
    ):
        raise AssertionError("fixture read suffix spoof was accepted")
    missing_probe_case = json.loads(json.dumps(scoped_read_case))
    missing_probe_case["id"] = "CPL-013"
    missing_probe_case["io_contract"]["allowed_reads"] = [
        "fixtures/missing-input.txt"
    ]
    missing_probe_meta = {
        **scoped_meta,
        "case_id": "CPL-013",
        "rendered_turns": ["Read fixtures/missing-input.txt"],
    }
    missing_path = "/fixture/fixtures/missing-input.txt"
    missing_command = (
        f"$path = '{missing_path}'; "
        "if (Test-Path -LiteralPath $path) { "
        "Get-Content -LiteralPath $path -Raw "
        "} else { Write-Output '__MISSING__' }"
    )
    missing_probe = {
        "type": "tool.execution_start",
        "toolCallId": "missing-probe",
        "toolName": "powershell",
        "arguments": {"command": missing_command},
    }
    missing_probe_result = evaluate_run(
        missing_probe_case,
        "mini",
        missing_probe_meta,
        [missing_probe],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
        fixture_workspace=Path("/fixture"),
    )
    if missing_probe_result["status"] != "PASS":
        raise AssertionError(missing_probe_result)
    missing_probe_spoofs = {
        "different-read-path": missing_command.replace(
            "Get-Content -LiteralPath $path",
            "Get-Content -LiteralPath '/fixture/fixtures/other.txt'",
        ),
        "double-quoted": missing_command.replace(
            f"'{missing_path}'",
            f'"{missing_path}"',
        ),
        "unquoted": missing_command.replace(
            f"'{missing_path}'",
            missing_path,
        ),
        "interpolated": missing_command.replace(
            missing_path,
            missing_path + "$(Get-Date)",
        ),
        "glob-test": missing_command.replace(
            "Test-Path -LiteralPath",
            "Test-Path -Path",
        ),
        "changed-sentinel": missing_command.replace(
            "'__MISSING__'",
            "'missing'",
        ),
        "appended-command": missing_command + "; Get-ChildItem",
        "prepended-command": "Get-Location; " + missing_command,
        "pipeline": missing_command + " | Out-String",
        "newline": missing_command.replace("; if", ";\nif"),
    }
    for label, command in missing_probe_spoofs.items():
        spoof = json.loads(json.dumps(missing_probe))
        spoof["arguments"]["command"] = command
        spoof_result = evaluate_run(
            missing_probe_case,
            "mini",
            missing_probe_meta,
            [spoof],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
            fixture_workspace=Path("/fixture"),
        )
        if (
            spoof_result["status"] != "FAIL"
            or spoof_result["observed"]["requested_file_reads"] != 0
        ):
            raise AssertionError(
                f"unsafe missing probe accepted: {label}"
            )
    wrong_tool_missing_probe = json.loads(json.dumps(missing_probe))
    wrong_tool_missing_probe["toolName"] = "bash"
    wrong_tool_result = evaluate_run(
        missing_probe_case,
        "mini",
        missing_probe_meta,
        [wrong_tool_missing_probe],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
        fixture_workspace=Path("/fixture"),
    )
    if wrong_tool_result["observed"]["requested_file_reads"] != 0:
        raise AssertionError("wrong-tool missing probe accepted")
    suffix_missing_probe = json.loads(json.dumps(missing_probe))
    suffix_missing_probe["arguments"]["command"] = missing_command.replace(
        "/fixture/",
        "/other/",
    )
    suffix_missing_result = evaluate_run(
        missing_probe_case,
        "mini",
        missing_probe_meta,
        [suffix_missing_probe],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
        fixture_workspace=Path("/fixture"),
    )
    if suffix_missing_result["observed"]["requested_file_reads"] != 0:
        raise AssertionError("wrong-root missing probe accepted")
    output_case = {
        "id": "CPL-012",
        "turns": [{"prompt": "Write the approved UTF-8 report"}],
        "io_contract": {
            "allowed_reads": [],
            "allowed_writes": [
                "compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md"
            ],
            "utf8_marker": "12345678-1234-4234-8234-1234567890ab",
        },
        "expected": {
            key: 0 for key in EXPECTED_COUNT_KEYS
        },
    }
    output_case["expected"]["approved_file_writes"] = 1
    output_case["expected"]["workspace_changes"] = 1
    output_meta = {
        **meta,
        "case_id": "CPL-012",
        "rendered_turns": ["Write the approved UTF-8 report"],
        "workspace_changes": [
            {
                "path": (
                    "compliance-output/"
                    "12345678-1234-4234-8234-1234567890ab.md"
                ),
                "change": "added",
            }
        ],
    }
    with tempfile.TemporaryDirectory() as output_workspace:
        workspace = Path(output_workspace)
        output_path = (
            workspace
            / "compliance-output"
            / "12345678-1234-4234-8234-1234567890ab.md"
        )
        output_path.parent.mkdir()
        output_path.write_text(
            "日本語 12345678-1234-4234-8234-1234567890ab",
            encoding="utf-8",
        )
        output_write = {
            "type": "tool.execution_start",
            "toolCallId": "approved-write",
            "toolName": "write_file",
            "arguments": {"path": str(output_path)},
        }
        output_result = evaluate_run(
            output_case,
            "mini",
            output_meta,
            [output_write],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
            fixture_workspace=workspace,
        )
        apply_patch_write = {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {
                        "toolCallId": "approved-apply-patch",
                        "name": "apply_patch",
                        "arguments": (
                            "*** Begin Patch\n"
                            "*** Add File: compliance-output/"
                            "12345678-1234-4234-8234-1234567890ab.md\n"
                            "+# Compliance report\n"
                            "+\n"
                            "+12345678-1234-4234-8234-1234567890ab\n"
                            "*** End Patch\n"
                        ),
                    }
                ]
            },
        }
        apply_patch_result = evaluate_run(
            output_case,
            "mini",
            output_meta,
            [apply_patch_write],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
            fixture_workspace=workspace,
        )
        nested_write_result = evaluate_run(
            output_case,
            "mini",
            output_meta,
            [{
                "type": "tool.execution_start",
                "toolName": "evil_write",
                "arguments": {
                    "payload": {"path": str(output_path)}
                },
            }],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
            fixture_workspace=workspace,
        )
        if (
            nested_write_result["status"] != "FAIL"
            or nested_write_result["observed"][
                "approved_file_writes"
            ] != 0
        ):
            raise AssertionError("nested allowed-write path spoof accepted")
        nested_kind_write = evaluate_run(
            output_case,
            "mini",
            output_meta,
            [{
                "type": "tool.execution_start",
                "toolName": "evil_tool",
                "arguments": {
                    "path": str(output_path),
                    "metadata": {"name": "write_file"},
                },
            }],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
            fixture_workspace=workspace,
        )
        if nested_kind_write["observed"]["approved_file_writes"] != 0:
            raise AssertionError("nested write kind spoof accepted")
        invalid_patches = {
            "update": (
                "*** Begin Patch\n"
                "*** Update File: compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md\n"
                "+changed\n"
                "*** End Patch\n"
            ),
            "delete": (
                "*** Begin Patch\n"
                "*** Delete File: compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md\n"
                "*** End Patch\n"
            ),
            "second_add": (
                "*** Begin Patch\n"
                "*** Add File: compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md\n"
                "+first\n"
                "*** Add File: compliance-output/second.md\n"
                "+second\n"
                "*** End Patch\n"
            ),
            "move": (
                "*** Begin Patch\n"
                "*** Add File: compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md\n"
                "*** Move to: compliance-output/second.md\n"
                "+content\n"
                "*** End Patch\n"
            ),
            "missing_begin": (
                "*** Add File: compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md\n"
                "+content\n"
                "*** End Patch\n"
            ),
            "duplicate_end": (
                "*** Begin Patch\n"
                "*** Add File: compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md\n"
                "+content\n"
                "*** End Patch\n"
                "*** End Patch\n"
            ),
            "traversal": (
                "*** Begin Patch\n"
                "*** Add File: ../outside.md\n"
                "+content\n"
                "*** End Patch\n"
            ),
            "outside": (
                "*** Begin Patch\n"
                "*** Add File: compliance-output/not-approved.md\n"
                "+content\n"
                "*** End Patch\n"
            ),
            "fake_header_content": (
                "*** Begin Patch\n"
                "*** Add File: compliance-output/"
                "12345678-1234-4234-8234-1234567890ab.md\n"
                "+*** Add File: compliance-output/second.md\n"
                "*** End Patch\n"
            ),
        }
        for label, patch_text in invalid_patches.items():
            invalid_result = evaluate_run(
                output_case,
                "mini",
                output_meta,
                [
                    {
                        "type": "assistant.message",
                        "data": {
                            "toolRequests": [
                                {
                                    "toolCallId": f"invalid-{label}",
                                    "name": "apply_patch",
                                    "arguments": patch_text,
                                }
                            ]
                        },
                    }
                ],
                otel,
                cli_invalid_lines=0,
                otel_invalid_lines=0,
                fixture_workspace=workspace,
            )
            if invalid_result["status"] != "FAIL":
                raise AssertionError(
                    f"invalid apply_patch accepted: {label}"
                )
    if output_result["status"] != "PASS":
        raise AssertionError(output_result)
    if apply_patch_result["status"] != "PASS":
        raise AssertionError(apply_patch_result)
    empty_case = {
        "id": "CPL-NEG",
        "turns": [{"prompt": "No-tool synthetic turn"}],
        "expected": {
            key: 0 for key in case["expected"]
        },
    }
    empty_result = evaluate_run(
        empty_case,
        "auto",
        {
            "requested_model": "auto",
            "exit_codes": [],
            "rendered_turns": [],
            "workspace_changes": [],
        },
        [],
        [
            {
                "attributes": [
                    {
                        "key": "some_model_label",
                        "value": {"stringValue": "not-a-selected-model"},
                    }
                ]
            }
        ],
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if empty_result["status"] != "FAIL" or not {
        "selected_model_not_observed",
        "one_or_more_copilot_turns_failed",
        "copilot_jsonl_event_stream_empty",
    }.issubset(empty_result["failures"]):
        raise AssertionError("empty-run pseudo-PASS regression")
    bad_auto = dict(meta)
    bad_auto["requested_model"] = "runtime-mini-placeholder"
    bad_auto_result = evaluate_run(
        case,
        "auto",
        bad_auto,
        [search, read, pointer],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if "auto_profile_did_not_request_auto" not in bad_auto_result["failures"]:
        raise AssertionError("Auto selector integrity gate did not fail")
    conflicting_otel = [
        {
            "name": "invoke_agent",
            "attributes": [
                {
                    "key": "gen_ai.request.model",
                    "value": {"stringValue": "runtime-mini-placeholder"},
                },
                {
                    "key": "gen_ai.response.model",
                    "value": {"stringValue": "selected-one"},
                },
                {
                    "key": "gen_ai.response.model",
                    "value": {"stringValue": "selected-two"},
                },
            ],
        }
    ]
    conflict_result = evaluate_run(
        case,
        "mini",
        meta,
        [search, read, pointer],
        conflicting_otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        "multiple_or_conflicting_selected_models_observed"
        not in conflict_result["failures"]
    ):
        raise AssertionError("conflicting selected models were accepted")
    auto_conflict_meta = dict(meta)
    auto_conflict_meta["requested_model"] = "auto"
    auto_conflict_result = evaluate_run(
        case,
        "auto",
        auto_conflict_meta,
        [search, read, pointer],
        conflicting_otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        "multiple_or_conflicting_selected_models_observed"
        in auto_conflict_result["failures"]
    ):
        raise AssertionError(
            "Auto turn-level model changes were misclassified"
        )
    profile_rows = [
        {
            "profile": "auto",
            "status": "PASS",
            "failures": [],
            "observed": {"requested_model": "auto"},
        },
        {
            "profile": "mini",
            "status": "PASS",
            "failures": [],
            "observed": {"requested_model": "same-model"},
        },
        {
            "profile": "standard",
            "status": "PASS",
            "failures": [],
            "observed": {"requested_model": "same-model"},
        },
    ]
    _apply_profile_integrity(profile_rows)
    if any(
        row["status"] != "FAIL"
        for row in profile_rows
        if row["profile"] != "auto"
    ):
        raise AssertionError("Mini/Standard distinctness gate did not fail")
    canonical_cases = load_cases(
        cases_path
        or (
            Path(__file__).resolve().parent
            / "data"
            / "copilot-compliance-cases-v1.jsonl"
        )
    )
    if (
        len(_execution_matrix(canonical_cases, "a")) != 48
        or len(_execution_matrix(canonical_cases, "b")) != 64
    ):
        raise AssertionError("Phase A/B execution matrix regression")
    url_priority_forbidden = canonical_cases[8]["assertions"][
        "assistant_not_contains"
    ]
    if (
        "{{SOURCE_URL}}" not in url_priority_forbidden
        or "{{SOURCE_PATH}}" in url_priority_forbidden
    ):
        raise AssertionError(
            "permalink priority conflicts with path provenance"
        )
    output_prompt = canonical_cases[11]["turns"][0]["prompt"]
    for required_text in (
        "only permitted tool call",
        "placeholder/no-op",
        "Do not call a shell",
    ):
        if required_text not in output_prompt:
            raise AssertionError(
                "output-file tool boundary regressed"
            )
    static_question = "What is the documented purpose of ORBIT-7?"
    static_label = (
        "Lookup question (copy exactly as the final argument):\n"
    )
    for static_case_definition in canonical_cases[13:15]:
        rendered_static = render_case(
            static_case_definition,
            {
                "EXPLICIT_DB": "synthetic-rag",
                "DIRECT_QUESTION": static_question,
            },
            execution_tag="12345678-1234-4234-8234-1234567890ab",
        )
        static_prompt_text = rendered_static["turns"][0]["prompt"]
        if static_label not in static_prompt_text:
            raise AssertionError("static lookup-question label missing")
        embedded_question = static_prompt_text.split(
            static_label,
            1,
        )[1].split("\nFirst load", 1)[0]
        if embedded_question != static_question:
            raise AssertionError(
                "static case changed the embedded lookup question"
            )
    for label, invalid_required_skill_calls in {
        "not-object": "local-rag",
        "bool-count": {"local-rag": True},
        "unlisted-skill": {"local-rag-admin": 1},
    }.items():
        invalid_cases = json.loads(json.dumps(canonical_cases))
        invalid_cases[13]["assertions"][
            "required_skill_calls"
        ] = invalid_required_skill_calls
        with tempfile.TemporaryDirectory() as validation_root:
            invalid_path = Path(validation_root) / "cases.jsonl"
            invalid_path.write_text(
                "\n".join(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    for value in invalid_cases
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                load_cases(invalid_path)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "invalid required skill assertion accepted: "
                    f"{label}"
                )
    rendered_utf8 = render_case(
        canonical_cases[10],
        {"QUERIES_FILE_SENTINEL": "日本語の合図"},
        execution_tag="12345678-1234-4234-8234-1234567890ab",
    )
    if "日本語の合図" not in rendered_utf8["turns"][0]["prompt"]:
        raise AssertionError("UTF-8 variable rendering regression")
    stdout_case = {
        "id": "CPL-016",
        "turns": [{"prompt": prompt}],
        "expected": {
            key: 0 for key in EXPECTED_COUNT_KEYS
        },
        "assertions": {
            "search_db": "synthetic-rag",
            "search_result_delivery": "stdout",
            "search_stdout_contract": {
                "status": "partial",
                "evidence_empty": True,
                "related_context_nonempty": True,
            },
            "search_stderr_warning": True,
        },
    }
    stdout_case["expected"]["search_calls"] = 1
    stdout_search = json.loads(json.dumps(search))
    stdout_search["arguments"]["command"] = stdout_search["arguments"][
        "command"
    ].replace("--result-delivery file", "--result-delivery stdout")
    stdout_completion = {
        "type": "tool.execution_complete",
        "toolCallId": "call-1",
        "stdout": json.dumps(
            {
                "status": "partial",
                "evidence": [],
                "related_context": [{"authoritative": False}],
            }
        ),
        "stderr": "synthetic warning",
    }
    stdout_result = evaluate_run(
        stdout_case,
        "mini",
        meta,
        [stdout_search, stdout_completion],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if stdout_result["status"] != "PASS":
        raise AssertionError(stdout_result)
    cli_1075_stdout_case = json.loads(json.dumps(stdout_case))
    cli_1075_stdout_case["assertions"].pop(
        "search_stderr_warning",
        None,
    )
    cli_1075_payload = json.dumps(
        {
            "status": "partial",
            "evidence": [],
            "document_results": [{"authoritative": False}],
        }
    )
    cli_1075_partial = {
        "type": "tool.execution_partial_result",
        "data": {
            "toolCallId": "call-1",
            "partialOutput": cli_1075_payload,
        },
    }
    cli_1075_complete = {
        "type": "tool.execution_complete",
        "data": {
            "toolCallId": "call-1",
            "success": True,
            "result": {"content": cli_1075_payload},
        },
    }
    cli_1075_stdout_result = evaluate_run(
        cli_1075_stdout_case,
        "mini",
        meta,
        [stdout_search, cli_1075_partial, cli_1075_complete],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if cli_1075_stdout_result["status"] != "PASS":
        raise AssertionError(cli_1075_stdout_result)
    shell_exit_complete = {
        "type": "tool.execution_complete",
        "data": {
            "toolCallId": "call-1",
            "success": True,
            "result": {
                "content": (
                    cli_1075_payload
                    + "\n<shellId: shell-stdout completed with exit code 0>"
                ),
                "contents": [
                    {
                        "type": "shell_exit",
                        "shellId": "shell-stdout",
                        "exitCode": 0,
                        "outputTruncated": False,
                        "cwd": "<TEST_ROOT>",
                        "outputPreview": cli_1075_payload,
                    }
                ],
            },
        },
    }
    shell_exit_stdout_result = evaluate_run(
        cli_1075_stdout_case,
        "mini",
        meta,
        [stdout_search, shell_exit_complete],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if shell_exit_stdout_result["status"] != "PASS":
        raise AssertionError(shell_exit_stdout_result)
    cli_stdout_spoofs = {
        "partial-only": cli_1075_partial,
        "nested-partial-output": {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "call-1",
                "success": True,
                "result": {
                    "content": "not JSON",
                    "nested": {
                        "partialOutput": cli_1075_payload,
                    },
                },
            },
        },
        "nested-result-output": {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "call-1",
                "success": True,
                "result": {
                    "content": "not JSON",
                    "output": cli_1075_payload,
                },
            },
        },
        "data-output": {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "call-1",
                "success": True,
                "output": cli_1075_payload,
                "result": {"content": "not JSON"},
            },
        },
        "nested-metadata-output-content": {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "call-1",
                "success": True,
                "result": {
                    "content": "not JSON",
                    "metadata": {
                        "output": {
                            "content": cli_1075_payload,
                        },
                    },
                },
            },
        },
        "unlinked-shell-exit": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "toolCallId": "different-call",
            },
        },
        "nonzero-shell-exit": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "content": (
                        cli_1075_payload
                        + "\n<shellId: shell-stdout completed with exit code 1>"
                    ),
                    "contents": [
                        {
                            **shell_exit_complete["data"]["result"][
                                "contents"
                            ][0],
                            "exitCode": 1,
                        }
                    ],
                },
            },
        },
        "bool-shell-exit-code": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "contents": [
                        {
                            **shell_exit_complete["data"]["result"][
                                "contents"
                            ][0],
                            "exitCode": False,
                        }
                    ],
                },
            },
        },
        "truncated-shell-exit": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "contents": [
                        {
                            **shell_exit_complete["data"]["result"][
                                "contents"
                            ][0],
                            "outputTruncated": True,
                        }
                    ],
                },
            },
        },
        "empty-shell-id": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "contents": [
                        {
                            **shell_exit_complete["data"]["result"][
                                "contents"
                            ][0],
                            "shellId": "",
                        }
                    ],
                },
            },
        },
        "malformed-shell-contents": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "contents": "not-a-list",
                },
            },
        },
        "multiple-shell-previews": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "contents": [
                        shell_exit_complete["data"]["result"]["contents"][0],
                        {
                            **shell_exit_complete["data"]["result"][
                                "contents"
                            ][0],
                            "shellId": "shell-other",
                            "outputPreview": json.dumps(
                                {
                                    "status": "ok",
                                    "evidence": [{"id": "E-conflict"}],
                                }
                            ),
                        },
                    ],
                },
            },
        },
        "mismatched-shell-id": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "contents": [
                        {
                            **shell_exit_complete["data"]["result"][
                                "contents"
                            ][0],
                            "shellId": "shell-other",
                        }
                    ],
                },
            },
        },
        "success-false": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "success": False,
            },
        },
        "success-missing": {
            **shell_exit_complete,
            "data": {
                key: value
                for key, value in shell_exit_complete["data"].items()
                if key != "success"
            },
        },
        "success-non-bool": {
            **shell_exit_complete,
            "data": {
                **shell_exit_complete["data"],
                "success": 1,
            },
        },
        "invalid-contents-with-valid-root-stdout": {
            **shell_exit_complete,
            "stdout": cli_1075_payload,
            "data": {
                **shell_exit_complete["data"],
                "result": {
                    **shell_exit_complete["data"]["result"],
                    "contents": [],
                },
            },
        },
    }
    for label, spoof in cli_stdout_spoofs.items():
        cli_stdout_spoof_result = evaluate_run(
            cli_1075_stdout_case,
            "mini",
            meta,
            [stdout_search, spoof],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
        )
        if (
            cli_stdout_spoof_result["status"] != "FAIL"
            or "search_stdout_json_not_observed"
            not in cli_stdout_spoof_result["failures"]
        ):
            raise AssertionError(
                f"nested stdout spoof accepted: {label}"
            )
        if label in {
            "nonzero-shell-exit",
            "bool-shell-exit-code",
            "truncated-shell-exit",
            "empty-shell-id",
            "malformed-shell-contents",
            "multiple-shell-previews",
            "mismatched-shell-id",
            "success-false",
            "success-missing",
            "success-non-bool",
            "invalid-contents-with-valid-root-stdout",
        } and "search_process_failed" not in cli_stdout_spoof_result[
            "failures"
        ]:
            raise AssertionError(
                f"failed search process was not reported: {label}"
            )
    legacy_failed_completion = {
        "type": "tool.execution_complete",
        "data": {
            "toolCallId": "call-1",
            "success": False,
            "result": {
                "content": (
                    "argparse failed\n"
                    "<shellId: shell-legacy completed with exit code 2>"
                ),
            },
        },
    }
    legacy_failed_result = evaluate_run(
        cli_1075_stdout_case,
        "mini",
        meta,
        [stdout_search, legacy_failed_completion],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if "search_process_failed" not in legacy_failed_result["failures"]:
        raise AssertionError("legacy nonzero search completion was accepted")
    stderr_spoof_search = json.loads(json.dumps(stdout_search))
    stderr_spoof_search["arguments"]["command"] = re.sub(
        r"--format\s+json",
        "--format human",
        stderr_spoof_search["arguments"]["command"],
    )
    stderr_spoof_completion = {
        **stdout_completion,
        "stdout": "this is not JSON",
        "stderr": json.dumps(
            {
                "status": "partial",
                "evidence": [],
                "related_context": [{"authoritative": False}],
                "warning": "warning",
            }
        ),
    }
    stderr_spoof_result = evaluate_run(
        stdout_case,
        "mini",
        meta,
        [stderr_spoof_search, stderr_spoof_completion],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        stderr_spoof_result["status"] != "FAIL"
        or "search_stdout_json_not_observed"
        not in stderr_spoof_result["failures"]
        or "search_stdout_missing_json_format"
        not in stderr_spoof_result["failures"]
    ):
        raise AssertionError("stderr JSON spoof satisfied stdout contract")
    url_case = json.loads(json.dumps(case))
    url_case["assertions"] = {
        "search_db": "synthetic-rag",
        "assistant_contains": ["https://permalink.invalid/item"],
        "assistant_not_contains": [
            "https://browse.invalid/item",
            "Synthetic Root/item.md",
        ],
    }
    url_result = evaluate_run(
        url_case,
        "mini",
        meta,
        [
            search,
            read,
            pointer,
            {
                "type": "assistant.message",
                "data": {
                    "phase": "final_answer",
                    "content": "Source: https://permalink.invalid/item",
                },
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if url_result["status"] != "PASS":
        raise AssertionError(url_result)
    terminal_without_phase = {
        "type": "assistant.message",
        "data": {
            "turnId": "terminal-turn",
            "content": "Source: https://permalink.invalid/item",
            "toolRequests": [],
        },
    }
    terminal_without_phase_result = evaluate_run(
        url_case,
        "mini",
        meta,
        [
            search,
            read,
            pointer,
            terminal_without_phase,
            {
                "type": "assistant.turn_end",
                "data": {"turnId": "terminal-turn"},
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if terminal_without_phase_result["status"] != "PASS":
        raise AssertionError(terminal_without_phase_result)
    phase_missing_tool_request = json.loads(
        json.dumps(terminal_without_phase)
    )
    phase_missing_tool_request["data"]["toolRequests"] = [
        {
            "toolCallId": "phase-missing-spoof",
            "name": "view",
            "arguments": {"path": "synthetic.txt"},
        }
    ]
    phase_missing_spoof_result = evaluate_run(
        url_case,
        "mini",
        meta,
        [
            search,
            read,
            pointer,
            phase_missing_tool_request,
            {
                "type": "assistant.turn_end",
                "data": {"turnId": "terminal-turn"},
            },
        ],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        phase_missing_spoof_result["status"] != "FAIL"
        or "required_assistant_text_not_observed"
        not in phase_missing_spoof_result["failures"]
    ):
        raise AssertionError(
            "phase-less tool request was accepted as terminal text"
        )
    phase_less_terminal_spoofs = {
        "missing-tool-requests": (
            {
                "type": "assistant.message",
                "data": {
                    "turnId": "terminal-turn",
                    "content": "Source: https://permalink.invalid/item",
                },
            },
            {
                "type": "assistant.turn_end",
                "data": {"turnId": "terminal-turn"},
            },
        ),
        "wrong-turn-id": (
            terminal_without_phase,
            {
                "type": "assistant.turn_end",
                "data": {"turnId": "different-turn"},
            },
        ),
        "intervening-assistant-message": (
            terminal_without_phase,
            {
                "type": "assistant.message",
                "data": {
                    "turnId": "terminal-turn",
                    "content": "intervening",
                    "toolRequests": [],
                },
            },
            {
                "type": "assistant.turn_end",
                "data": {"turnId": "terminal-turn"},
            },
        ),
        "intervening-tool": (
            terminal_without_phase,
            {
                "type": "tool.execution_start",
                "data": {
                    "toolCallId": "intervening-tool",
                    "name": "view",
                },
            },
            {
                "type": "assistant.turn_end",
                "data": {"turnId": "terminal-turn"},
            },
        ),
    }
    for label, suffix in phase_less_terminal_spoofs.items():
        spoof_result = evaluate_run(
            url_case,
            "mini",
            meta,
            [search, read, pointer, *suffix],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
        )
        if (
            spoof_result["status"] != "FAIL"
            or "required_assistant_text_not_observed"
            not in spoof_result["failures"]
        ):
            raise AssertionError(
                f"phase-less terminal spoof accepted: {label}"
            )
    assistant_text_spoofs = {
        "user": {
            "type": "user.message",
            "data": {
                "content": "https://permalink.invalid/item",
            },
        },
        "commentary": {
            "type": "assistant.message",
            "data": {
                "phase": "commentary",
                "content": "https://permalink.invalid/item",
            },
        },
        "tool_request": {
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": "No source link.",
                "toolRequests": [
                    {
                        "toolCallId": "assistant-text-spoof",
                        "name": "view",
                        "arguments": {
                            "path": "https://permalink.invalid/item",
                        },
                    }
                ],
            },
        },
        "malformed_dict_tool_requests": {
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": "Source: https://permalink.invalid/item",
                "toolRequests": {},
            },
        },
        "malformed_string_tool_requests": {
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": "Source: https://permalink.invalid/item",
                "toolRequests": "none",
            },
        },
    }
    for label, spoof in assistant_text_spoofs.items():
        spoof_result = evaluate_run(
            url_case,
            "mini",
            meta,
            [search, read, pointer, spoof],
            otel,
            cli_invalid_lines=0,
            otel_invalid_lines=0,
        )
        if (
            spoof_result["status"] != "FAIL"
            or "required_assistant_text_not_observed"
            not in spoof_result["failures"]
        ):
            raise AssertionError(
                f"assistant text spoof accepted: {label}"
            )
    wrong_url_events = [
        search,
        read,
        pointer,
        {
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": "Source: https://browse.invalid/item",
            },
        },
    ]
    wrong_url_result = evaluate_run(
        url_case,
        "mini",
        meta,
        wrong_url_events,
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if wrong_url_result["status"] != "FAIL":
        raise AssertionError("URL priority output gate did not fail")
    static_prompt = "Show one synthetic command."
    static_case = {
        "id": "CPL-STATIC",
        "turns": [{"prompt": static_prompt}],
        "expected": {
            key: 0
            for key in case["expected"]
        },
        "assertions": {
            "assistant_command_shell": "git-bash",
            "assistant_contains": [
                "$HOME/.copilot/rag/query/.venv/Scripts/python.exe",
                "$HOME/.copilot/rag/query/search.py",
                "--db synthetic-rag",
                "--include-db-hint",
                "--compact-json",
                "--result-delivery file",
                "Synthetic question",
            ],
        },
    }
    static_meta = {
        **meta,
        "case_id": "CPL-STATIC",
        "exit_codes": [0],
        "rendered_turns": [static_prompt],
        "workspace_changes": [],
    }
    static_valid = evaluate_run(
        static_case,
        "mini",
        static_meta,
        [{
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": (
                    "```bash\n"
                    "\"$HOME/.copilot/rag/query/.venv/Scripts/python.exe\" "
                    "\"$HOME/.copilot/rag/query/search.py\" "
                    "--db synthetic-rag --include-db-hint --compact-json "
                    "--result-delivery file \"Synthetic question\"\n"
                    "```"
                ),
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if static_valid["status"] != "PASS":
        raise AssertionError(static_valid)
    static_whole_prompt = evaluate_run(
        static_case,
        "mini",
        static_meta,
        [{
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": (
                    "```bash\n"
                    "\"$HOME/.copilot/rag/query/.venv/Scripts/python.exe\" "
                    "\"$HOME/.copilot/rag/query/search.py\" "
                    "--db synthetic-rag --include-db-hint --compact-json "
                    "--result-delivery file "
                    f"\"{static_prompt}\"\n"
                    "```"
                ),
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        static_whole_prompt["status"] != "FAIL"
        or "static_command_argv_mismatch"
        not in static_whole_prompt["failures"]
    ):
        raise AssertionError(
            "static command copied the surrounding request as its query"
        )
    static_powershell_case = json.loads(json.dumps(static_case))
    static_powershell_case["assertions"] = {
        "assistant_command_shell": "powershell",
        "assistant_contains": [
            "$env:USERPROFILE\\.copilot\\rag\\query\\.venv\\"
            "Scripts\\python.exe",
            "$env:USERPROFILE\\.copilot\\rag\\query\\search.py",
            "--db synthetic-rag",
            "--include-db-hint",
            "--compact-json",
            "--result-delivery file",
            "Synthetic question",
        ],
    }
    static_trailing_space = evaluate_run(
        static_powershell_case,
        "mini",
        static_meta,
        [{
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": (
                    "```powershell\n"
                    '& "$env:USERPROFILE\\.copilot\\rag\\query\\'
                    '.venv\\Scripts\\python.exe" '
                    '"$env:USERPROFILE\\.copilot\\rag\\query\\search.py" '
                    "--db synthetic-rag --include-db-hint --compact-json "
                    '--result-delivery file "Synthetic question" `   \n'
                    'Write-Output evil\n'
                    "```"
                ),
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        static_trailing_space["status"] != "FAIL"
        or "static_command_contains_multiple_commands"
        not in static_trailing_space["failures"]
    ):
        raise AssertionError(
            "static trailing-space continuation spoof was accepted"
        )
    static_git_trailing_space = evaluate_run(
        static_case,
        "mini",
        static_meta,
        [{
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": (
                    "```bash\n"
                    "\"$HOME/.copilot/rag/query/.venv/Scripts/python.exe\" "
                    "\"$HOME/.copilot/rag/query/search.py\" "
                    "--db synthetic-rag --include-db-hint --compact-json "
                    '--result-delivery file "Synthetic question" \\   \n'
                    "echo evil\n"
                    "```"
                ),
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        static_git_trailing_space["status"] != "FAIL"
        or "static_command_contains_multiple_commands"
        not in static_git_trailing_space["failures"]
    ):
        raise AssertionError(
            "Git Bash trailing-space continuation spoof was accepted"
        )
    static_spoof = evaluate_run(
        static_case,
        "mini",
        static_meta,
        [{
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": "\n".join(
                    static_case["assertions"]["assistant_contains"]
                ),
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        static_spoof["status"] != "FAIL"
        or "static_command_requires_one_code_block"
        not in static_spoof["failures"]
    ):
        raise AssertionError(
            "static command prose fragments satisfied command contract"
        )
    static_argv_spoof = evaluate_run(
        static_case,
        "mini",
        static_meta,
        [{
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": (
                    "```bash\n"
                    "\"$HOME/.copilot/rag/query/.venv/Scripts/python.exe\" "
                    "\"$HOME/.copilot/rag/query/search.py\" "
                    "\"--db synthetic-rag --include-db-hint --compact-json "
                    "--result-delivery file Synthetic question\"\n"
                    "```"
                ),
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        static_argv_spoof["status"] != "FAIL"
        or "static_command_argv_mismatch"
        not in static_argv_spoof["failures"]
    ):
        raise AssertionError(
            "quoted argv spoof satisfied static command contract"
        )
    static_comment_spoof = evaluate_run(
        static_case,
        "mini",
        static_meta,
        [{
            "type": "assistant.message",
            "data": {
                "phase": "final_answer",
                "content": (
                    "```bash\n"
                    "\"$HOME/.copilot/rag/query/.venv/Scripts/python.exe\" "
                    "\"$HOME/.copilot/rag/query/search.py\" "
                    "# --db synthetic-rag --include-db-hint --compact-json "
                    "--result-delivery file Synthetic question\n"
                    "```"
                ),
            },
        }],
        otel,
        cli_invalid_lines=0,
        otel_invalid_lines=0,
    )
    if (
        static_comment_spoof["status"] != "FAIL"
        or "static_command_comment_not_allowed"
        not in static_comment_spoof["failures"]
    ):
        raise AssertionError(
            "shell comment spoof satisfied static command contract"
        )
    print(
        "SELF-TEST OK: collector parsing and negative gates work. "
        "This is not a Copilot product compliance result."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--variables", type=Path)
    parser.add_argument("--fixture-workspace", type=Path)
    parser.add_argument(
        "--phase", choices=("a", "b"), default="a"
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test(
            args.cases.resolve() if args.cases is not None else None
        )
    if (
        args.cases is None
        or args.raw_root is None
        or args.output is None
        or args.variables is None
        or args.fixture_workspace is None
    ):
        parser.error(
            "--cases, --raw-root, --output, --variables, and "
            "--fixture-workspace are required"
        )
    try:
        report = collect(
            args.cases.resolve(),
            args.raw_root.resolve(),
            variables_path=args.variables.resolve(),
            fixture_workspace=args.fixture_workspace.resolve(),
            phase=args.phase,
        )
    except (OSError, ValueError) as exc:
        print(f"Compliance collection failed: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "overall_status": report["overall_status"],
                "passed": report["passed"],
                "failed": report["failed"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
