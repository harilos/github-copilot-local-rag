from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


CASE_SCHEMA = "lrr-agent003-cli-prod-uat-case-v1"
RUN_SCHEMA = "lrr-agent003-cli-prod-uat-run-v1"
REPORT_SCHEMA = "lrr-agent003-cli-prod-uat-report-v1"
EXPECTED_CASE_COUNT = 5
NANO_AIU_PER_CREDIT = 1_000_000_000
DEFAULT_AGGREGATE_CREDIT_CAP = 50
MAX_AGGREGATE_CREDIT_CAP = 80
SUPPORTED_COPILOT_CLI_VERSION = "1.0.77"
PASS_WITH_RESIDUAL = "PASS_WITH_RESIDUAL"
APPROVAL_OBSERVATION = "NO_OBSERVED_PROMPT"

TIER_CONTRACTS = {
    "savings": ("local-rag-agent003-savings", "claude-haiku-4.5"),
    "standard": ("local-rag-agent003-standard", "auto"),
    "thorough": ("local-rag-agent003-thorough", "gpt-5.3-codex"),
}
SEARCH_TOOL = "localragagent003-local_rag_search"
EVIDENCE_TOOL = "localragagent003-local_rag_get_evidence"
ALLOWED_RUNTIME_TOOLS = frozenset((SEARCH_TOOL, EVIDENCE_TOOL))
ALLOWED_AGENT_TOOLS = frozenset(
    (
        "localragagent003/local_rag_search",
        "localragagent003/local_rag_get_evidence",
    )
)
EXPECTED_AVAILABLE_TOOLS = (
    "localragagent003-local_rag_search",
    "localragagent003-local_rag_get_evidence",
)
EXPECTED_ALLOWED_TOOLS = (
    "localragagent003(local_rag_search)",
    "localragagent003(local_rag_get_evidence)",
)
EXPECTED_PERMISSION_CONTRACT = {
    "available_tools": list(EXPECTED_AVAILABLE_TOOLS),
    "allow_tools": list(EXPECTED_ALLOWED_TOOLS),
    "no_custom_instructions": True,
    "no_ask_user": True,
    "output_format": "json",
    "stream": "off",
    "no_auto_update": True,
    "no_remote": True,
    "no_remote_export": True,
    "approval_observation": APPROVAL_OBSERVATION,
    "approval_prompt_count_directly_observable": False,
}
FORBIDDEN_EVENT_TYPES = frozenset(
    (
        "permission.requested",
        "permission.request",
        "user_input.requested",
        "user_input.request",
        "subagent.started",
    )
)
CREDIT_GATE_FAILURES = frozenset(("session_usage_total_nano_aiu_missing",))
FORBIDDEN_TOOL_RE = re.compile(
    r"(?:^|[-_/])(shell|powershell|bash|cmd|terminal|file|read|write|edit|"
    r"delete|web|fetch|browser|ask[_-]?user)(?:$|[-_/])",
    re.IGNORECASE,
)
HTTPS_URL_RE = re.compile(r"https://[^\s<>()\]]+")
URL_TRAILING_PUNCTUATION = ".,;:!?\"'。．、，；：！？」』】〕〉》"


class EvidenceError(ValueError):
    pass


class CreditStop(EvidenceError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"JSON root is not an object: {path}")
    return value


def _load_jsonl(
    path: Path, *, allow_empty: bool = False
) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EvidenceError(f"cannot read JSONL: {path}: {exc}") from exc
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceError(
                f"invalid JSONL at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise EvidenceError(
                f"JSONL value is not an object at {path}:{line_number}"
            )
        values.append(value)
    if not values and not allow_empty:
        raise EvidenceError(f"JSONL is empty: {path}")
    return values


def load_cases(path: Path) -> list[dict[str, Any]]:
    values = _load_jsonl(path)
    if len(values) != EXPECTED_CASE_COUNT:
        raise EvidenceError(
            f"case authority must contain exactly {EXPECTED_CASE_COUNT} cases"
        )
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, value in enumerate(values, 1):
        if value.get("schema_version") != CASE_SCHEMA:
            raise EvidenceError(f"case {ordinal}: invalid schema_version")
        case_id = value.get("id")
        if not isinstance(case_id, str) or not re.fullmatch(
            r"LRR-AGENT003-CLI-PROD-[1-5]", case_id
        ):
            raise EvidenceError(f"case {ordinal}: invalid id")
        if case_id in seen or case_id != f"LRR-AGENT003-CLI-PROD-{ordinal}":
            raise EvidenceError(f"case {ordinal}: id/order is not canonical")
        seen.add(case_id)
        tier = value.get("tier")
        if tier not in TIER_CONTRACTS:
            raise EvidenceError(f"{case_id}: invalid tier")
        expected_agent, requested_model = TIER_CONTRACTS[tier]
        if value.get("expected_agent") != expected_agent:
            raise EvidenceError(f"{case_id}: expected_agent is not canonical")
        if value.get("requested_model") != requested_model:
            raise EvidenceError(f"{case_id}: requested_model is not canonical")
        prompt = value.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or prompt != prompt.strip():
            raise EvidenceError(f"{case_id}: exact prompt is invalid")
        for key in (
            "minimum_search_calls",
            "maximum_search_calls",
            "minimum_evidence_calls",
            "maximum_evidence_calls",
        ):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise EvidenceError(f"{case_id}: {key} is invalid")
        if value["minimum_search_calls"] < 1:
            raise EvidenceError(f"{case_id}: at least one search is required")
        if value["minimum_search_calls"] > value["maximum_search_calls"]:
            raise EvidenceError(f"{case_id}: search range is invalid")
        if value["minimum_evidence_calls"] > value["maximum_evidence_calls"]:
            raise EvidenceError(f"{case_id}: evidence range is invalid")
        scope = value.get("launcher_scope")
        expected_scope = (
            "temporary_boundary_fixture"
            if ordinal == EXPECTED_CASE_COUNT
            else "installed_product"
        )
        if scope != expected_scope:
            raise EvidenceError(f"{case_id}: launcher_scope is not canonical")
        has_url_contract = "minimum_markdown_source_urls" in value
        if ordinal in (3, 4):
            minimum_urls = value.get("minimum_markdown_source_urls")
            if (
                isinstance(minimum_urls, bool)
                or not isinstance(minimum_urls, int)
                or minimum_urls < 1
                or value.get("require_all_response_urls_from_tool_evidence")
                is not True
            ):
                raise EvidenceError(f"{case_id}: source URL contract is invalid")
        elif has_url_contract or "require_all_response_urls_from_tool_evidence" in value:
            raise EvidenceError(f"{case_id}: unexpected source URL contract")
        if scope == "temporary_boundary_fixture":
            minimum_bytes = value.get("minimum_tool_result_bytes")
            tail_window = value.get("tool_result_tail_window_bytes")
            required_fragment = value.get("required_response_fragment")
            revision = value.get("compatibility_revision")
            if (
                isinstance(minimum_bytes, bool)
                or not isinstance(minimum_bytes, int)
                or minimum_bytes <= 32768
                or isinstance(tail_window, bool)
                or not isinstance(tail_window, int)
                or not 1 <= tail_window <= 4096
                or not isinstance(required_fragment, str)
                or not required_fragment
                or not isinstance(revision, dict)
                or revision.get("fixture_schema")
                != "lrr-agent003-cli-prod-large-output-fixture-v1"
            ):
                raise EvidenceError(f"{case_id}: boundary fixture contract is invalid")
        cases.append(value)
    return cases


def _event_type(event: dict[str, Any]) -> str:
    value = event.get("type")
    return value if isinstance(value, str) else ""


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def _tool_name(data: dict[str, Any]) -> str:
    for key in ("toolName", "name"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _assistant_response(events: list[dict[str, Any]]) -> str:
    terminal = ""
    for index, event in enumerate(events):
        if _event_type(event) != "assistant.message":
            continue
        data = _event_data(event)
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        phase = data.get("phase")
        tool_requests = data.get("toolRequests")
        if phase in ("final", "final_answer") and tool_requests in (None, []):
            terminal = content
            continue
        # Copilot CLI 1.0.77 emits a phase-less final message followed by
        # optional reasoning/message bookkeeping before the same turn_end.
        if phase is not None or tool_requests != []:
            continue
        turn_id = data.get("turnId")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        for following in events[index + 1 :]:
            following_type = _event_type(following)
            if following_type == "assistant.turn_start":
                break
            if (
                following_type == "assistant.turn_end"
                and _event_data(following).get("turnId") == turn_id
            ):
                terminal = content
                break
    return terminal


def _normalize_agent_tool(name: str) -> str:
    if name in ALLOWED_AGENT_TOOLS:
        return name
    if name == SEARCH_TOOL:
        return "localragagent003/local_rag_search"
    if name == EVIDENCE_TOOL:
        return "localragagent003/local_rag_get_evidence"
    return name


def _otel_pairs(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        key = value.get("key")
        wrapped = value.get("value")
        if isinstance(key, str) and isinstance(wrapped, dict):
            for value_key in (
                "stringValue",
                "intValue",
                "doubleValue",
                "boolValue",
            ):
                if value_key in wrapped:
                    yield key, wrapped[value_key]
        for nested_key, nested in value.items():
            if nested_key not in ("key", "value"):
                if isinstance(nested, (str, int, float, bool)):
                    yield nested_key, nested
                yield from _otel_pairs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _otel_pairs(nested)


def _as_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _otel_evidence(otel: list[dict[str, Any]]) -> dict[str, Any]:
    requested: set[str] = set()
    responded: set[str] = set()
    agent_ids: set[str] = set()
    agent_names: set[str] = set()
    input_values: list[int] = []
    output_values: list[int] = []
    invalid_tokens = False
    parent_span_ids = {
        event.get("parentSpanId")
        for event in otel
        if isinstance(event.get("parentSpanId"), str)
        and event.get("parentSpanId")
    }
    for event in otel:
        for key, value in _otel_pairs(event):
            normalized = key.lower().replace("-", "_")
            if normalized.endswith("gen_ai.agent.id"):
                if isinstance(value, str) and value:
                    agent_ids.add(value)
            elif normalized.endswith("gen_ai.agent.name"):
                if isinstance(value, str) and value:
                    agent_names.add(value)
        attributes = event.get("attributes")
        if not isinstance(attributes, dict):
            continue
        response_model = attributes.get("gen_ai.response.model")
        operation = attributes.get("gen_ai.operation.name")
        span_name = event.get("name")
        span_id = event.get("spanId")
        is_leaf_chat = (
            isinstance(response_model, str)
            and bool(response_model)
            and isinstance(span_id, str)
            and bool(span_id)
            and span_id not in parent_span_ids
            and (
                operation == "chat"
                or (isinstance(span_name, str) and span_name.startswith("chat "))
            )
        )
        if not is_leaf_chat:
            continue
        responded.add(response_model)
        request_model = attributes.get("gen_ai.request.model")
        if isinstance(request_model, str) and request_model:
            requested.add(request_model)
        input_value = attributes.get(
            "gen_ai.usage.input_tokens",
            attributes.get("gen_ai.usage.prompt_tokens"),
        )
        output_value = attributes.get(
            "gen_ai.usage.output_tokens",
            attributes.get("gen_ai.usage.completion_tokens"),
        )
        parsed_input = _as_nonnegative_int(input_value)
        parsed_output = _as_nonnegative_int(output_value)
        if parsed_input is None or parsed_output is None:
            invalid_tokens = True
        else:
            input_values.append(parsed_input)
            output_values.append(parsed_output)
    return {
        "requested_models": requested,
        "response_models": responded,
        "agent_ids": agent_ids,
        "agent_names": agent_names,
        "input_tokens": sum(input_values) if input_values else None,
        "output_tokens": sum(output_values) if output_values else None,
        "invalid_tokens": invalid_tokens,
    }


def _tool_result_content(data: dict[str, Any]) -> str | None:
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        texts.append(text)
    return "".join(texts)


def _normalize_https_url(value: str) -> str | None:
    normalized = value.strip().rstrip(URL_TRAILING_PUNCTUATION)
    if re.fullmatch(r"https://[^\s<>()]+", normalized):
        return normalized
    return None


def _response_https_urls(response: str) -> set[str]:
    values: set[str] = set()
    for match in HTTPS_URL_RE.finditer(response):
        normalized = _normalize_https_url(match.group(0))
        if normalized is not None:
            values.add(normalized)
    return values


def _markdown_source_urls(response: str) -> set[str]:
    def escaped(position: int) -> bool:
        backslashes = 0
        position -= 1
        while position >= 0 and response[position] == "\\":
            backslashes += 1
            position -= 1
        return backslashes % 2 == 1

    values: set[str] = set()
    index = 0
    while index < len(response):
        if response[index] != "[" or escaped(index):
            index += 1
            continue
        is_image = (
            index > 0
            and response[index - 1] == "!"
            and not escaped(index - 1)
        )
        depth = 1
        cursor = index + 1
        while cursor < len(response) and response[cursor] not in "\r\n":
            if response[cursor] == "\\":
                cursor += 2
                continue
            if response[cursor] == "[":
                depth += 1
            elif response[cursor] == "]":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if (
            depth != 0
            or is_image
            or cursor + 2 >= len(response)
            or response[cursor + 1] != "("
            or not response.startswith("https://", cursor + 2)
        ):
            index = max(cursor + 1, index + 1)
            continue
        url_start = cursor + 2
        url_end = url_start
        while (
            url_end < len(response)
            and response[url_end] not in "\r\n\t <>()"
        ):
            url_end += 1
        if url_end < len(response) and response[url_end] == ")":
            normalized = _normalize_https_url(response[url_start:url_end])
            if normalized is not None:
                values.add(normalized)
            index = url_end + 1
        else:
            index += 1
    return values


def _packet_evidence_urls(parsed: Any) -> set[str]:
    if not isinstance(parsed, dict):
        return set()
    evidence = parsed.get("evidence")
    if not isinstance(evidence, list):
        return set()
    values: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str):
            normalized = _normalize_https_url(url)
            if normalized is not None:
                values.add(normalized)
    return values


def _tool_evidence_urls(content: str, structured_content: Any) -> set[str]:
    values = _packet_evidence_urls(structured_content)
    if values:
        return values
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return set()
    return _packet_evidence_urls(parsed)


def _url_set_sha256(values: set[str]) -> str | None:
    if not values:
        return None
    return _sha256_text("\n".join(sorted(values)))


def _result_contract(
    events: list[dict[str, Any]], run_exit_code: Any
) -> tuple[str | None, float | None, list[str]]:
    failures: list[str] = []
    results = [event for event in events if _event_type(event) == "result"]
    if len(results) != 1:
        return None, None, ["result_event_count_invalid"]
    result = results[0]
    session_id = result.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        failures.append("result_session_id_missing")
        session_id = None
    result_exit = result.get("exitCode")
    if isinstance(result_exit, bool) or not isinstance(result_exit, int):
        failures.append("result_exit_code_invalid")
    elif result_exit != run_exit_code or result_exit != 0:
        failures.append("result_run_exit_code_mismatch")
    usage = result.get("usage")
    premium: float | None = None
    if not isinstance(usage, dict) or "premiumRequests" not in usage:
        failures.append("result_usage_missing")
    else:
        try:
            parsed = Decimal(str(usage["premiumRequests"]))
        except (InvalidOperation, ValueError):
            failures.append("result_premium_requests_invalid")
        else:
            if not parsed.is_finite() or parsed < 0:
                failures.append("result_premium_requests_invalid")
            else:
                premium = float(parsed)
    return session_id, premium, failures


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_cli_identity(
    run: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    path_value = run.get("cli_path")
    version_path_value = run.get("cli_version_evidence_path")
    if (
        not isinstance(path_value, str)
        or not path_value
        or not Path(path_value).is_absolute()
    ):
        return evidence, ["cli_path_invalid"]
    cli_path = Path(path_value)
    if not cli_path.is_file() or cli_path.is_symlink():
        return evidence, ["cli_file_unreadable"]
    cli_path = cli_path.resolve()
    live_hash = _sha256_file(cli_path)
    if run.get("cli_sha256") != live_hash:
        failures.append("cli_file_hash_mismatch")
    if run.get("cli_version") != SUPPORTED_COPILOT_CLI_VERSION:
        failures.append("cli_version_unsupported")
    if (
        not isinstance(version_path_value, str)
        or not version_path_value
        or not Path(version_path_value).is_absolute()
    ):
        failures.append("cli_version_evidence_path_invalid")
    else:
        version_path = Path(version_path_value)
        if not version_path.is_file() or version_path.is_symlink():
            failures.append("cli_version_evidence_unreadable")
        else:
            version_path = version_path.resolve()
            version_hash = _sha256_file(version_path)
            if run.get("cli_version_evidence_sha256") != version_hash:
                failures.append("cli_version_evidence_hash_mismatch")
            try:
                version_output = version_path.read_text(
                    encoding="utf-8-sig", errors="strict"
                ).strip()
            except (OSError, UnicodeError):
                failures.append("cli_version_evidence_unreadable")
            else:
                lines = version_output.splitlines()
                version_match = (
                    re.fullmatch(
                        r"GitHub Copilot CLI ([0-9]+\.[0-9]+\.[0-9]+)\.?",
                        lines[0],
                    )
                    if lines
                    else None
                )
                if (
                    version_match is None
                    or version_match.group(1) != SUPPORTED_COPILOT_CLI_VERSION
                ):
                    failures.append("cli_version_evidence_mismatch")
    evidence = {
        "path": str(cli_path),
        "sha256": live_hash,
        "version": run.get("cli_version"),
    }
    return evidence, sorted(set(failures))


def _validate_launcher_manifest(
    run: dict[str, Any]
) -> tuple[Path | None, dict[str, Any], list[str]]:
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    home_value = run.get("copilot_home")
    manifest_value = run.get("launcher_manifest_path")
    if not isinstance(home_value, str) or not home_value or not Path(home_value).is_absolute():
        failures.append("copilot_home_invalid")
        return None, evidence, failures
    copilot_home = Path(home_value).resolve()
    if not isinstance(manifest_value, str) or not manifest_value or not Path(manifest_value).is_absolute():
        failures.append("launcher_manifest_path_invalid")
        return copilot_home, evidence, failures
    manifest_path = Path(manifest_value).resolve()
    try:
        manifest = _load_json(manifest_path)
        manifest_hash = _sha256_file(manifest_path)
    except (OSError, EvidenceError) as exc:
        failures.append(f"launcher_manifest_unreadable:{type(exc).__name__}")
        return copilot_home, evidence, failures
    if run.get("launcher_manifest_schema") != 1 or manifest.get("schema") != 1:
        failures.append("launcher_manifest_schema_mismatch")
    if run.get("launcher_manifest_sha256") != manifest_hash:
        failures.append("launcher_manifest_hash_mismatch")
    manifest_home = manifest.get("copilot_home")
    install_root = manifest.get("install_root")
    if not isinstance(manifest_home, str) or Path(manifest_home).resolve() != copilot_home:
        failures.append("launcher_manifest_copilot_home_mismatch")
    if not isinstance(install_root, str) or not Path(install_root).is_absolute():
        failures.append("launcher_manifest_install_root_invalid")
        return copilot_home, evidence, failures
    install_path = Path(install_root).resolve()
    expected_manifest = install_path / "copilot-cli" / "owned-manifest.json"
    launcher_path = install_path / "copilot-cli" / "local-rag-agent003.ps1"
    run_install_root = run.get("launcher_install_root")
    if (
        not isinstance(run_install_root, str)
        or not Path(run_install_root).is_absolute()
        or Path(run_install_root).resolve() != install_path
    ):
        failures.append("launcher_install_root_mismatch")
    run_launcher_path = run.get("launcher_path")
    if (
        not isinstance(run_launcher_path, str)
        or not Path(run_launcher_path).is_absolute()
        or Path(run_launcher_path).resolve() != launcher_path.resolve()
    ):
        failures.append("launcher_path_mismatch")
    candidate_root = run.get("candidate_runtime_root")
    if not isinstance(candidate_root, str) or not Path(candidate_root).is_absolute():
        failures.append("candidate_runtime_root_invalid")
    elif (
        run.get("launcher_scope") == "installed_product"
        and Path(candidate_root).resolve() != install_path
    ):
        failures.append("installed_launcher_outside_candidate_runtime")
    if manifest_path != expected_manifest.resolve():
        failures.append("launcher_manifest_location_mismatch")
    entries = manifest.get("artifacts")
    matches = []
    if isinstance(entries, list):
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("root") == "install_root"
            and str(entry.get("path", "")).replace("\\", "/")
            == "copilot-cli/local-rag-agent003.ps1"
        ]
    if len(matches) != 1:
        failures.append("launcher_manifest_artifact_missing")
    elif not launcher_path.is_file() or launcher_path.is_symlink():
        failures.append("launcher_artifact_unreadable")
    else:
        live_hash = _sha256_file(launcher_path)
        if (
            run.get("launcher_sha256") != live_hash
            or matches[0].get("sha256") != live_hash
            or matches[0].get("bytes") != launcher_path.stat().st_size
        ):
            failures.append("launcher_artifact_hash_mismatch")
        evidence = {
            "launcher_manifest_sha256": manifest_hash,
            "launcher_manifest_path": str(manifest_path),
            "launcher_sha256": live_hash,
            "launcher_path": str(launcher_path.resolve()),
            "install_root": str(install_path),
            "copilot_home": str(copilot_home),
        }
    return copilot_home, evidence, failures


def _read_session_usage(
    copilot_home: Path, session_id: str
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    evidence: dict[str, Any] = {
        "row_count": 0,
        "agent_ids": [],
        "agent_id_null_count": 0,
        "models": [],
        "input_tokens": None,
        "output_tokens": None,
        "total_nano_aiu": None,
    }
    db_path = copilot_home / "session-store.db"
    if not db_path.is_file() or db_path.is_symlink():
        return evidence, ["session_store_missing"]
    required_columns = {
        "id", "session_id", "turn_index", "agent_id", "model",
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens", "total_nano_aiu",
    }
    try:
        connection = sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(assistant_usage_events)"
                ).fetchall()
            }
            if not required_columns.issubset(columns):
                return evidence, ["session_usage_schema_invalid"]
            session_count = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
            if session_count != 1:
                failures.append("session_row_count_invalid")
            rows = connection.execute(
                "SELECT id, turn_index, agent_id, model, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, "
                "reasoning_tokens, total_nano_aiu "
                "FROM assistant_usage_events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return evidence, ["session_store_read_failed"]
    evidence["row_count"] = len(rows)
    if not rows:
        return evidence, failures + ["session_usage_missing"]
    models: set[str] = set()
    agent_ids: set[str] = set()
    null_agents = 0
    input_total = 0
    output_total = 0
    nano_total = 0
    credit_observable = True
    for row in rows:
        agent_id, model = row[2], row[3]
        if agent_id is None:
            null_agents += 1
        elif isinstance(agent_id, str) and agent_id:
            agent_ids.add(agent_id)
        else:
            failures.append("session_usage_agent_id_invalid")
        if not isinstance(model, str) or not model:
            failures.append("session_usage_model_invalid")
        else:
            models.add(model)
        input_tokens = _as_nonnegative_int(row[4])
        output_tokens = _as_nonnegative_int(row[5])
        if input_tokens is None or output_tokens is None:
            failures.append("session_usage_tokens_invalid")
        else:
            input_total += input_tokens
            output_total += output_tokens
        nano = _as_nonnegative_int(row[9])
        if nano is None:
            credit_observable = False
        else:
            nano_total += nano
    if not credit_observable:
        failures.append("session_usage_total_nano_aiu_missing")
    evidence.update(
        {
            "agent_ids": sorted(agent_ids),
            "agent_id_null_count": null_agents,
            "models": sorted(models),
            "input_tokens": input_total,
            "output_tokens": output_total,
            "total_nano_aiu": nano_total if credit_observable else None,
        }
    )
    return evidence, sorted(set(failures))


def evaluate_case(
    case: dict[str, Any],
    run: dict[str, Any],
    events: list[dict[str, Any]],
    otel: list[dict[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    case_id = case["id"]
    if run.get("schema_version") != RUN_SCHEMA or run.get("case_id") != case_id:
        failures.append("run_metadata_mismatch")
    if run.get("tier") != case["tier"]:
        failures.append("run_tier_mismatch")
    if run.get("prompt_sha256") != _sha256_text(case["prompt"]):
        failures.append("prompt_hash_mismatch")
    exit_code = run.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0:
        failures.append("cli_exit_nonzero")
    per_case_credit_cap = run.get("max_ai_credits")
    valid_per_case_credit_cap = not (
        isinstance(per_case_credit_cap, bool)
        or not isinstance(per_case_credit_cap, int)
        or per_case_credit_cap != 30
    )
    if not valid_per_case_credit_cap:
        failures.append("per_case_credit_cap_mismatch")
    if run.get("launcher_scope") != case["launcher_scope"]:
        failures.append("launcher_scope_mismatch")
    if run.get("fresh_session") is not True:
        failures.append("fresh_session_not_asserted")
    if run.get("retry_count") != 0:
        failures.append("retry_observed")
    timeout_seconds = run.get("timeout_seconds")
    if isinstance(timeout_seconds, bool) or timeout_seconds != 900:
        failures.append("case_timeout_contract_invalid")
    timed_out = run.get("timed_out")
    process_tree_terminated = run.get("process_tree_terminated")
    if not isinstance(timed_out, bool) or not isinstance(
        process_tree_terminated, bool
    ):
        failures.append("case_timeout_metadata_invalid")
    elif timed_out:
        failures.append("case_timeout_observed")
        if not process_tree_terminated:
            failures.append("timeout_process_tree_not_terminated")
    elif process_tree_terminated:
        failures.append("unexpected_process_tree_termination")
    if run.get("noninteractive_permission_contract") != EXPECTED_PERMISSION_CONTRACT:
        failures.append("noninteractive_permission_contract_mismatch")

    event_types = [_event_type(event) for event in events]
    interaction_request_observed = any(
        event_type.startswith("permission.")
        or event_type.startswith("user_input.")
        for event_type in event_types
    )
    for event_type in event_types:
        if (
            event_type in FORBIDDEN_EVENT_TYPES
            or event_type.startswith("permission.")
            or event_type.startswith("user_input.")
        ):
            failures.append(f"forbidden_event:{event_type}")

    manifest_home, launcher_evidence, manifest_failures = _validate_launcher_manifest(run)
    failures.extend(manifest_failures)
    cli_evidence, cli_failures = _validate_cli_identity(run)
    failures.extend(cli_failures)
    session_id, result_premium_requests, result_failures = _result_contract(
        events, exit_code
    )
    failures.extend(result_failures)

    mcp_event_seen = False
    owned_mcp_listed = False
    mcp_state_ready = False
    for event in events:
        event_type = _event_type(event)
        data = _event_data(event)
        if event_type == "session.mcp_servers_loaded":
            mcp_event_seen = True
            servers = data.get("servers")
            if not isinstance(servers, list):
                continue
            for server in servers:
                if not isinstance(server, dict):
                    continue
                name = server.get("name") or server.get("serverName")
                state = str(
                    server.get("status") or server.get("state") or ""
                ).lower()
                if name == "localragagent003":
                    owned_mcp_listed = True
                    if state in ("connected", "loaded", "ready", "running"):
                        mcp_state_ready = True
        elif event_type == "session.mcp_server_status_changed":
            mcp_event_seen = True
            name = data.get("serverName") or data.get("name")
            state = str(data.get("status") or data.get("state") or "").lower()
            if name == "localragagent003":
                owned_mcp_listed = True
                if state in ("connected", "loaded", "ready", "running"):
                    mcp_state_ready = True
    if not mcp_event_seen:
        failures.append("mcp_status_evidence_missing")
    elif not owned_mcp_listed:
        failures.append("owned_mcp_not_loaded")

    starts: dict[str, str] = {}
    completions: dict[str, dict[str, Any]] = {}
    completion_contents: dict[str, str] = {}
    completion_structured: dict[str, Any] = {}
    for event in events:
        event_type = _event_type(event)
        data = _event_data(event)
        if event_type == "tool.execution_start":
            call_id = data.get("toolCallId")
            name = _tool_name(data)
            if not isinstance(call_id, str) or not call_id or call_id in starts:
                failures.append("tool_start_identity_invalid")
                continue
            starts[call_id] = name
            if name not in ALLOWED_RUNTIME_TOOLS:
                failures.append(f"unknown_tool:{name or '<missing>'}")
            if FORBIDDEN_TOOL_RE.search(name):
                failures.append(f"forbidden_tool:{name}")
        elif event_type == "tool.execution_complete":
            call_id = data.get("toolCallId")
            if not isinstance(call_id, str) or not call_id or call_id in completions:
                failures.append("tool_completion_identity_invalid")
                continue
            completions[call_id] = data
            content = _tool_result_content(data)
            if data.get("success") is True and content is None:
                failures.append("tool_result_content_missing")
            elif content is not None:
                completion_contents[call_id] = content
            result_value = data.get("result")
            if isinstance(result_value, dict) and "structuredContent" in result_value:
                completion_structured[call_id] = result_value["structuredContent"]
    if set(starts) != set(completions):
        failures.append("tool_completion_mismatch")
    successful_owned_tool = False
    result_texts: list[str] = []
    successful_call_ids: list[str] = []
    for call_id, completion in completions.items():
        if completion.get("success") is not True or completion.get("error"):
            failures.append(f"tool_failed:{call_id}")
        elif starts.get(call_id) in ALLOWED_RUNTIME_TOOLS:
            successful_owned_tool = True
            successful_call_ids.append(call_id)
            content = completion_contents.get(call_id)
            if content is not None:
                result_texts.append(content)
    result_bytes = [len(content.encode("utf-8")) for content in result_texts]
    if owned_mcp_listed and not (mcp_state_ready or successful_owned_tool):
        failures.append("owned_mcp_not_operational")
    search_count = sum(name == SEARCH_TOOL for name in starts.values())
    evidence_count = sum(name == EVIDENCE_TOOL for name in starts.values())
    if not case["minimum_search_calls"] <= search_count <= case["maximum_search_calls"]:
        failures.append("search_call_count_out_of_range")
    if not case["minimum_evidence_calls"] <= evidence_count <= case["maximum_evidence_calls"]:
        failures.append("evidence_call_count_out_of_range")

    response = _assistant_response(events)
    if not response:
        failures.append("final_assistant_response_missing")
    required_fragment = case.get("required_response_fragment")
    minimum_result_bytes = case.get("minimum_tool_result_bytes")
    tail_window = case.get("tool_result_tail_window_bytes")
    if isinstance(required_fragment, str):
        if response.strip() != required_fragment:
            failures.append("required_response_exact_mismatch")
        qualifying_results = [
            content
            for content in result_texts
            if isinstance(minimum_result_bytes, int)
            and len(content.encode("utf-8")) >= minimum_result_bytes
        ]
        if not qualifying_results:
            failures.append("tool_result_boundary_not_observed")
        elif not any(
            required_fragment.encode("utf-8")
            in content.encode("utf-8")[-int(tail_window) :]
            for content in qualifying_results
        ):
            failures.append("required_tool_result_tail_fragment_missing")

    response_urls = _response_https_urls(response)
    markdown_urls = _markdown_source_urls(response)
    evidence_urls: set[str] = set()
    for call_id in successful_call_ids:
        content = completion_contents.get(call_id, "")
        evidence_urls.update(
            _tool_evidence_urls(content, completion_structured.get(call_id))
        )
    minimum_source_urls = case.get("minimum_markdown_source_urls")
    if isinstance(minimum_source_urls, int):
        if len(markdown_urls) < minimum_source_urls:
            failures.append("source_markdown_url_count_below_minimum")
        if not response_urls.issubset(evidence_urls):
            failures.append("response_url_not_from_tool_evidence")

    session_usage: dict[str, Any] = {
        "row_count": 0,
        "agent_ids": [],
        "agent_id_null_count": 0,
        "models": [],
        "input_tokens": None,
        "output_tokens": None,
        "total_nano_aiu": None,
    }
    if manifest_home is not None and session_id is not None:
        session_usage, usage_failures = _read_session_usage(manifest_home, session_id)
        failures.extend(usage_failures)
    else:
        failures.append("session_usage_unavailable")

    otel_evidence = _otel_evidence(otel)
    requested_models = otel_evidence["requested_models"]
    response_models = otel_evidence["response_models"]
    usage_models = set(session_usage["models"])
    if not usage_models:
        failures.append("session_usage_model_missing")
    if not response_models:
        failures.append("otel_response_model_missing")
    elif usage_models != response_models:
        failures.append("session_otel_response_model_mismatch")
    if otel_evidence["invalid_tokens"]:
        failures.append("otel_token_evidence_invalid")
    if otel_evidence["input_tokens"] is None or otel_evidence["output_tokens"] is None:
        failures.append("otel_token_evidence_missing")
    elif (
        session_usage["input_tokens"] != otel_evidence["input_tokens"]
        or session_usage["output_tokens"] != otel_evidence["output_tokens"]
    ):
        failures.append("session_otel_token_mismatch")
    otel_agents = set(otel_evidence["agent_ids"]) | set(otel_evidence["agent_names"])
    if case["expected_agent"] not in otel_agents:
        failures.append("otel_expected_agent_missing")
    requested_model = case["requested_model"]
    if requested_model == "auto":
        if "auto" in usage_models or "auto" in response_models:
            failures.append("auto_not_resolved")
    else:
        if usage_models != {requested_model}:
            failures.append("actual_model_mismatch")
        if requested_models and requested_models != {requested_model}:
            failures.append("otel_requested_model_mismatch")

    nano_aiu = session_usage["total_nano_aiu"]
    credit_observable = isinstance(nano_aiu, int) and nano_aiu >= 0
    if (
        credit_observable
        and valid_per_case_credit_cap
        and nano_aiu > per_case_credit_cap * NANO_AIU_PER_CREDIT
    ):
        failures.append("per_case_credit_cap_exceeded")
    result = {
        "case_id": case_id,
        "tier": case["tier"],
        "status": PASS_WITH_RESIDUAL if not failures else "FAIL",
        "failures": sorted(set(failures)),
        "expected_agent": case["expected_agent"],
        "otel_agent_ids": sorted(otel_evidence["agent_ids"]),
        "otel_agent_names": sorted(otel_evidence["agent_names"]),
        "session_store_agent_ids": session_usage["agent_ids"],
        "session_store_agent_id_null_count": session_usage["agent_id_null_count"],
        "session_id": session_id,
        "requested_model": requested_model,
        "resolved_models": sorted(usage_models),
        "otel_requested_models": sorted(requested_models),
        "otel_response_models": sorted(response_models),
        "session_store_input_tokens": session_usage["input_tokens"],
        "session_store_output_tokens": session_usage["output_tokens"],
        "otel_input_tokens": otel_evidence["input_tokens"],
        "otel_output_tokens": otel_evidence["output_tokens"],
        "search_calls": search_count,
        "evidence_calls": evidence_count,
        "tool_result_count": len(result_bytes),
        "maximum_tool_result_content_bytes": max(result_bytes, default=0),
        "assistant_response_bytes": len(response.encode("utf-8")),
        "assistant_response_sha256": _sha256_text(response) if response else None,
        "response_source_url_count": len(response_urls),
        "response_source_url_sha256": _url_set_sha256(response_urls),
        "markdown_source_url_count": len(markdown_urls),
        "markdown_source_url_sha256": _url_set_sha256(markdown_urls),
        "tool_evidence_url_count": len(evidence_urls),
        "tool_evidence_url_sha256": _url_set_sha256(evidence_urls),
        "result_premium_requests": result_premium_requests,
        "max_ai_credits": per_case_credit_cap,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "process_tree_terminated": process_tree_terminated,
        "launcher_identity": launcher_evidence,
        "cli_identity": cli_evidence,
        "credit_observable": credit_observable,
        "total_nano_aiu": nano_aiu if credit_observable else None,
        "ai_credits": nano_aiu / NANO_AIU_PER_CREDIT if credit_observable else None,
        "permission_evidence": EXPECTED_PERMISSION_CONTRACT,
        "approval_observation": (
            "INTERACTION_REQUEST_OBSERVED"
            if interaction_request_observed
            else APPROVAL_OBSERVATION
        ),
        "residuals": [
            "approval_prompt_count_not_directly_observable_in_copilot_cli_1.0.77"
        ],
    }
    return result


def collect(
    cases_path: Path,
    raw_root: Path,
    completed_count: int,
    aggregate_credit_cap: int = DEFAULT_AGGREGATE_CREDIT_CAP,
) -> dict[str, Any]:
    cases = load_cases(cases_path)
    if completed_count < 1 or completed_count > len(cases):
        raise EvidenceError("completed_count is outside the canonical case range")
    if (
        isinstance(aggregate_credit_cap, bool)
        or not isinstance(aggregate_credit_cap, int)
        or aggregate_credit_cap < 1
        or aggregate_credit_cap > MAX_AGGREGATE_CREDIT_CAP
    ):
        raise EvidenceError("aggregate credit cap must be an integer from 1 through 80")
    results: list[dict[str, Any]] = []
    total_nano = 0
    credit_stop = False
    for ordinal, case in enumerate(cases[:completed_count], 1):
        run_root = raw_root / f"{ordinal:02d}-{case['id']}"
        run = _load_json(run_root / "run.json")
        allow_empty = run.get("timed_out") is True
        event_path = run_root / "copilot.jsonl"
        otel_path = run_root / "otel.jsonl"
        events = (
            _load_jsonl(event_path, allow_empty=allow_empty)
            if event_path.exists()
            else ([] if allow_empty else _load_jsonl(event_path))
        )
        otel = (
            _load_jsonl(otel_path, allow_empty=allow_empty)
            if otel_path.exists()
            else ([] if allow_empty else _load_jsonl(otel_path))
        )
        result = evaluate_case(case, run, events, otel)
        results.append(result)
        if "per_case_credit_cap_exceeded" in result["failures"]:
            credit_stop = True
        if not result["credit_observable"]:
            credit_stop = True
        else:
            total_nano += int(result["total_nano_aiu"])
        if total_nano > aggregate_credit_cap * NANO_AIU_PER_CREDIT:
            credit_stop = True
    session_owners: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        session_id = result.get("session_id")
        if isinstance(session_id, str) and session_id:
            session_owners.setdefault(session_id, []).append(result)
    for owners in session_owners.values():
        if len(owners) > 1:
            for owner in owners:
                owner["failures"] = sorted(
                    set(owner["failures"]) | {"fresh_session_id_reused"}
                )
                owner["status"] = "FAIL"
    installed_results = results[: min(4, len(results))]
    installed_identity_keys = {
        (
            str(item.get("launcher_identity", {}).get("install_root", "")),
            str(item.get("launcher_identity", {}).get("copilot_home", "")),
            str(item.get("launcher_identity", {}).get("launcher_path", "")),
            str(
                item.get("launcher_identity", {}).get(
                    "launcher_manifest_path", ""
                )
            ),
            str(item.get("launcher_identity", {}).get("launcher_sha256", "")),
            str(
                item.get("launcher_identity", {}).get(
                    "launcher_manifest_sha256", ""
                )
            ),
        )
        for item in installed_results
    }
    if len(installed_identity_keys) != 1:
        for item in installed_results:
            item["failures"] = sorted(
                set(item["failures"]) | {"installed_identity_changed_across_cases"}
            )
            item["status"] = "FAIL"
    if len(results) == EXPECTED_CASE_COUNT:
        boundary_identity = results[4].get("launcher_identity", {})
        expected_boundary_root = (
            raw_root / "boundary-fixture" / "install"
        ).resolve()
        boundary_root_value = boundary_identity.get("install_root")
        installed_root_value = (
            installed_results[0].get("launcher_identity", {}).get("install_root")
            if installed_results
            else None
        )
        if (
            not isinstance(boundary_root_value, str)
            or Path(boundary_root_value).resolve() != expected_boundary_root
            or boundary_root_value == installed_root_value
        ):
            results[4]["failures"] = sorted(
                set(results[4]["failures"])
                | {"boundary_fixture_launcher_scope_invalid"}
            )
            results[4]["status"] = "FAIL"
    cli_identity_keys = {
        (
            str(item.get("cli_identity", {}).get("path", "")),
            str(item.get("cli_identity", {}).get("sha256", "")),
            str(item.get("cli_identity", {}).get("version", "")),
        )
        for item in results
    }
    if len(cli_identity_keys) != 1:
        for item in results:
            item["failures"] = sorted(
                set(item["failures"]) | {"cli_identity_changed_across_cases"}
            )
            item["status"] = "FAIL"
    overall = (
        PASS_WITH_RESIDUAL
        if all(item["status"] == PASS_WITH_RESIDUAL for item in results)
        else "FAIL"
    )
    hard_failures = any(
        any(failure not in CREDIT_GATE_FAILURES for failure in item["failures"])
        for item in results
    )
    if credit_stop and not hard_failures:
        overall = "STOP_CREDIT_GATE"
    aggregate_approval_observation = (
        APPROVAL_OBSERVATION
        if all(
            item.get("approval_observation") == APPROVAL_OBSERVATION
            for item in results
        )
        else "INTERACTION_REQUEST_OBSERVED"
    )
    return {
        "schema_version": REPORT_SCHEMA,
        "overall_status": overall,
        "completed_count": completed_count,
        "canonical_case_count": len(cases),
        "aggregate_credit_cap": aggregate_credit_cap,
        "aggregate_total_nano_aiu": total_nano,
        "aggregate_ai_credits": total_nano / NANO_AIU_PER_CREDIT,
        "stop_required": credit_stop,
        "approval_observation": aggregate_approval_observation,
        "noninteractive_permission_contract": EXPECTED_PERMISSION_CONTRACT,
        "cli_identities": [
            {"path": path, "sha256": sha256, "version": version}
            for path, sha256, version in sorted(cli_identity_keys)
        ],
        "case_timeout_seconds": sorted(
            {
                int(item["timeout_seconds"])
                for item in results
                if isinstance(item.get("timeout_seconds"), int)
                and not isinstance(item.get("timeout_seconds"), bool)
            }
        ),
        "residuals": [
            "approval_prompt_count_not_directly_observable_in_copilot_cli_1.0.77"
        ],
        "compatibility_revisions": [
            {
                "case_id": case["id"],
                "fixture_schema": case["compatibility_revision"]["fixture_schema"],
                "reason": case["compatibility_revision"]["reason"],
            }
            for case in cases
            if isinstance(case.get("compatibility_revision"), dict)
        ],
        "cases": results,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _synthetic_case_files(raw_root: Path, cases: list[dict[str, Any]]) -> None:
    resolved = {
        "savings": "claude-haiku-4.5",
        "standard": "gpt-5-mini",
        "thorough": "gpt-5.3-codex",
    }
    synthetic_cli = raw_root / "_synthetic-cli" / "copilot.cmd"
    synthetic_version = raw_root / "_synthetic-cli" / "version.stdout.log"
    synthetic_cli.parent.mkdir(parents=True, exist_ok=True)
    synthetic_cli.write_text("@echo off\r\n", encoding="utf-8")
    synthetic_version.write_text(
        f"GitHub Copilot CLI {SUPPORTED_COPILOT_CLI_VERSION}.\n"
        "Run 'copilot update' to check for updates.\n",
        encoding="utf-8",
    )
    cli_hash = _sha256_file(synthetic_cli)
    version_hash = _sha256_file(synthetic_version)
    shared_install_root = raw_root / "_synthetic-candidate-runtime"
    shared_copilot_home = raw_root / "_synthetic-copilot-home"
    for ordinal, case in enumerate(cases, 1):
        run_root = raw_root / f"{ordinal:02d}-{case['id']}"
        actual_model = resolved[case["tier"]]
        session_id = f"synthetic-session-{ordinal}"
        input_tokens = 100 + ordinal
        output_tokens = 20 + ordinal
        is_boundary = case["launcher_scope"] == "temporary_boundary_fixture"
        install_root = (
            raw_root / "boundary-fixture" / "install"
            if is_boundary
            else shared_install_root
        )
        copilot_home = (
            raw_root / "boundary-fixture" / "copilot-home"
            if is_boundary
            else shared_copilot_home
        )
        launcher = install_root / "copilot-cli" / "local-rag-agent003.ps1"
        manifest_path = install_root / "copilot-cli" / "owned-manifest.json"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        copilot_home.mkdir(parents=True, exist_ok=True)
        launcher.write_text("# synthetic fixed launcher\n", encoding="utf-8")
        launcher_hash = _sha256_file(launcher)
        _write_json(
            manifest_path,
            {
                "schema": 1,
                "server": "localragagent003",
                "copilot_home": str(copilot_home.resolve()),
                "install_root": str(install_root.resolve()),
                "artifacts": [
                    {
                        "root": "install_root",
                        "path": "copilot-cli/local-rag-agent003.ps1",
                        "bytes": launcher.stat().st_size,
                        "sha256": launcher_hash,
                    }
                ],
            },
        )
        database_path = copilot_home / "session-store.db"
        database_exists = database_path.exists()
        database = sqlite3.connect(database_path)
        try:
            if not database_exists:
                database.executescript(
                    "CREATE TABLE sessions (id TEXT PRIMARY KEY);"
                    "CREATE TABLE assistant_usage_events ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                    "turn_index INTEGER, agent_id TEXT, model TEXT NOT NULL, "
                    "input_tokens INTEGER, output_tokens INTEGER, "
                    "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                    "reasoning_tokens INTEGER, total_nano_aiu INTEGER);"
                )
            database.execute("INSERT INTO sessions(id) VALUES (?)", (session_id,))
            database.execute(
                "INSERT INTO assistant_usage_events("
                "session_id, turn_index, agent_id, model, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, "
                "reasoning_tokens, total_nano_aiu) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    1,
                    None if ordinal % 2 == 0 else case["expected_agent"],
                    actual_model,
                    input_tokens,
                    output_tokens,
                    0,
                    0,
                    0,
                    NANO_AIU_PER_CREDIT,
                ),
            )
            database.commit()
        finally:
            database.close()
        _write_json(
            run_root / "run.json",
            {
                "schema_version": RUN_SCHEMA,
                "case_id": case["id"],
                "tier": case["tier"],
                "launcher_scope": case["launcher_scope"],
                "candidate_runtime_root": str(shared_install_root.resolve()),
                "launcher_path": str(launcher.resolve()),
                "launcher_install_root": str(install_root.resolve()),
                "launcher_sha256": launcher_hash,
                "launcher_manifest_schema": 1,
                "launcher_manifest_path": str(manifest_path.resolve()),
                "launcher_manifest_sha256": _sha256_file(manifest_path),
                "copilot_home": str(copilot_home.resolve()),
                "cli_path": str(synthetic_cli.resolve()),
                "cli_sha256": cli_hash,
                "cli_version": SUPPORTED_COPILOT_CLI_VERSION,
                "cli_version_evidence_path": str(synthetic_version.resolve()),
                "cli_version_evidence_sha256": version_hash,
                "noninteractive_permission_contract": EXPECTED_PERMISSION_CONTRACT,
                "prompt_sha256": _sha256_text(case["prompt"]),
                "max_ai_credits": 30,
                "fresh_session": True,
                "retry_count": 0,
                "exit_code": 0,
                "timeout_seconds": 900,
                "timed_out": False,
                "process_tree_terminated": False,
            },
        )
        synthetic_content = '{"status":"ok"}'
        synthetic_structured: dict[str, Any] | None = None
        synthetic_answer = "Synthetic answer."
        if isinstance(case.get("minimum_markdown_source_urls"), int):
            synthetic_url = f"https://example.invalid/evidence/{ordinal}"
            synthetic_content = (
                "Local RAG synthetic status=ok; use structuredContent."
            )
            synthetic_structured = {
                "status": "ok",
                "evidence": [{"id": "E1", "url": synthetic_url}],
            }
            synthetic_answer = (
                f"Synthetic answer [source [nested label]]({synthetic_url})."
            )
        if case.get("launcher_scope") == "temporary_boundary_fixture":
            synthetic_content = "X" * 33000 + str(case["required_response_fragment"])
            synthetic_answer = str(case["required_response_fragment"])
        tool_events: list[dict[str, Any]] = []
        for call_index in range(case["minimum_search_calls"]):
            search_id = f"search-{ordinal}-{call_index + 1}"
            result_value: dict[str, Any] = {
                "content": [{"type": "text", "text": synthetic_content}]
            }
            if synthetic_structured is not None:
                result_value["structuredContent"] = synthetic_structured
            tool_events.extend(
                [
                    {
                        "type": "tool.execution_start",
                        "data": {
                            "toolCallId": search_id,
                            "toolName": SEARCH_TOOL,
                        },
                    },
                    {
                        "type": "tool.execution_complete",
                        "data": {
                            "toolCallId": search_id,
                            "success": True,
                            "result": result_value,
                        },
                    },
                ]
            )
        events = [
            {
                "type": "session.mcp_server_status_changed",
                "data": {"serverName": "localragagent003", "status": "pending"},
            },
            {
                "type": "session.mcp_server_status_changed",
                "data": {"serverName": "localragagent003", "status": "connected"},
            },
            *tool_events,
            {
                "type": "assistant.turn_start",
                "data": {"turnId": f"final-turn-{ordinal}"},
            },
            {
                "type": "assistant.message",
                "data": {
                    "turnId": f"final-turn-{ordinal}",
                    "toolRequests": [],
                    "content": synthetic_answer,
                },
            },
            {
                "type": "assistant.reasoning",
                "data": {"turnId": f"final-turn-{ordinal}"},
            },
            {
                "type": "assistant.turn_end",
                "data": {"turnId": f"final-turn-{ordinal}"},
            },
            {
                "type": "result",
                "timestamp": "2026-08-23T00:00:00Z",
                "sessionId": session_id,
                "exitCode": 0,
                "usage": {
                    "premiumRequests": 1.0,
                    "totalApiDurationMs": 1,
                    "sessionDurationMs": 1,
                    "codeChanges": {"linesAdded": 0, "linesRemoved": 0},
                },
            },
        ]
        _write_jsonl(run_root / "copilot.jsonl", events)
        request_model = (
            actual_model if case["requested_model"] == "auto" else case["requested_model"]
        )
        agent_key = "gen_ai.agent.name" if ordinal % 2 == 0 else "gen_ai.agent.id"
        _write_jsonl(
            run_root / "otel.jsonl",
            [
                {
                    "type": "span",
                    "spanId": f"root-{ordinal}",
                    "name": f"invoke_agent {case['expected_agent']}",
                    "attributes": {
                        "gen_ai.operation.name": "invoke_agent",
                        agent_key: case["expected_agent"],
                        # Aggregate/root usage must not be double-counted.
                        "gen_ai.usage.input_tokens": input_tokens,
                        "gen_ai.usage.output_tokens": output_tokens,
                    },
                },
                {
                    "type": "span",
                    "spanId": f"chat-{ordinal}",
                    "parentSpanId": f"root-{ordinal}",
                    "name": f"chat {actual_model}",
                    "attributes": {
                        "gen_ai.operation.name": "chat",
                        "gen_ai.request.model": request_model,
                        "gen_ai.response.model": actual_model,
                        "gen_ai.usage.input_tokens": input_tokens,
                        "gen_ai.usage.output_tokens": output_tokens,
                    },
                },
            ],
        )


def _synthetic_db(raw_root: Path, case: dict[str, Any], ordinal: int) -> Path:
    run_root = raw_root / f"{ordinal:02d}-{case['id']}"
    run = _load_json(run_root / "run.json")
    return Path(str(run["copilot_home"])) / "session-store.db"


def self_test(cases_path: Path) -> int:
    cases = load_cases(cases_path)
    parser_url = "https://example.invalid/evidence/nested"
    if _markdown_source_urls(
        f"[outer [nested label]]({parser_url})"
    ) != {parser_url}:
        raise AssertionError("nested Markdown link label was not recognized")
    for malformed_link in (
        f"bare {parser_url}",
        f"![image]({parser_url})",
        f"\\[escaped]({parser_url})",
        f"[missing close]({parser_url}",
        f"![outer [inner]({parser_url})]",
        f"[outer [inner]({parser_url})",
    ):
        if _markdown_source_urls(malformed_link):
            raise AssertionError(
                f"malformed or non-link Markdown was accepted: {malformed_link}"
            )
    with tempfile.TemporaryDirectory(prefix="lrr-agent003-cli-prod-collector-") as tmp:
        raw_root = Path(tmp) / "raw"
        _synthetic_case_files(raw_root, cases)
        report = collect(cases_path, raw_root, EXPECTED_CASE_COUNT)
        if (
            report["overall_status"] != PASS_WITH_RESIDUAL
            or report["approval_observation"] != APPROVAL_OBSERVATION
            or len(report["cli_identities"]) != 1
            or any(
                report["cases"][index]["markdown_source_url_count"] < 1
                for index in (2, 3)
            )
            or "https://example.invalid" in json.dumps(report)
        ):
            raise AssertionError(report)
        for ordinal, case in enumerate(cases, 1):
            values = _load_jsonl(
                raw_root / f"{ordinal:02d}-{case['id']}" / "copilot.jsonl"
            )
            forbidden_synthetic = {
                "assistant.usage",
                "session.custom_agents_updated",
                "subagent.selected",
            }
            if forbidden_synthetic & {_event_type(value) for value in values}:
                raise AssertionError("synthetic stdout does not match CLI 1.0.77")

        source_case = cases[2]
        source_events_path = (
            raw_root / f"03-{source_case['id']}" / "copilot.jsonl"
        )
        source_original = source_events_path.read_text(encoding="utf-8")
        source_values = [
            json.loads(line) for line in source_original.splitlines() if line
        ]
        for value in source_values:
            if value.get("type") == "assistant.message":
                value["data"]["content"] = (
                    "Synthetic source https://example.invalid/evidence/3"
                )
        _write_jsonl(source_events_path, source_values)
        report = collect(cases_path, raw_root, 3)
        if (
            report["overall_status"] != "FAIL"
            or "source_markdown_url_count_below_minimum"
            not in report["cases"][2]["failures"]
        ):
            raise AssertionError("missing Markdown source URL did not fail")

        source_values = [
            json.loads(line) for line in source_original.splitlines() if line
        ]
        for value in source_values:
            if value.get("type") == "assistant.message":
                value["data"]["content"] = (
                    "Synthetic [source](https://example.invalid/not-returned)."
                )
        _write_jsonl(source_events_path, source_values)
        report = collect(cases_path, raw_root, 3)
        if (
            report["overall_status"] != "FAIL"
            or "response_url_not_from_tool_evidence"
            not in report["cases"][2]["failures"]
        ):
            raise AssertionError("invented source URL did not fail")

        source_values = [
            json.loads(line) for line in source_original.splitlines() if line
        ]
        for value in source_values:
            if value.get("type") == "tool.execution_complete":
                value["data"]["result"]["structuredContent"] = {
                    "metadata": {"url": "https://example.invalid/evidence/3"}
                }
        _write_jsonl(source_events_path, source_values)
        report = collect(cases_path, raw_root, 3)
        if (
            report["overall_status"] != "FAIL"
            or "response_url_not_from_tool_evidence"
            not in report["cases"][2]["failures"]
        ):
            raise AssertionError("non-evidence URL field authorized a source")
        source_events_path.write_text(source_original, encoding="utf-8")

        boundary_case = cases[4]
        boundary_events_path = (
            raw_root / f"05-{boundary_case['id']}" / "copilot.jsonl"
        )
        boundary_original = boundary_events_path.read_text(encoding="utf-8")
        boundary_values = [
            json.loads(line) for line in boundary_original.splitlines() if line
        ]
        for value in boundary_values:
            if value.get("type") == "tool.execution_complete":
                value["data"]["result"]["content"][0]["text"] = (
                    str(boundary_case["required_response_fragment"]) + "X" * 33000
                )
        _write_jsonl(boundary_events_path, boundary_values)
        report = collect(cases_path, raw_root, EXPECTED_CASE_COUNT)
        if (
            report["overall_status"] != "FAIL"
            or "required_tool_result_tail_fragment_missing"
            not in report["cases"][4]["failures"]
        ):
            raise AssertionError("non-tail boundary marker did not fail")

        boundary_values = [
            json.loads(line) for line in boundary_original.splitlines() if line
        ]
        marker = str(boundary_case["required_response_fragment"])
        exact_boundary = "X" * (32768 - len(marker.encode("utf-8"))) + marker
        for value in boundary_values:
            if value.get("type") == "tool.execution_complete":
                value["data"]["result"]["content"][0]["text"] = exact_boundary
        _write_jsonl(boundary_events_path, boundary_values)
        report = collect(cases_path, raw_root, EXPECTED_CASE_COUNT)
        if (
            report["overall_status"] != "FAIL"
            or "tool_result_boundary_not_observed"
            not in report["cases"][4]["failures"]
        ):
            raise AssertionError("exactly 32 KiB tool result did not fail")

        boundary_values = [
            json.loads(line) for line in boundary_original.splitlines() if line
        ]
        completion_index = 0
        for value in boundary_values:
            if value.get("type") == "tool.execution_complete":
                completion_index += 1
                value["data"]["result"]["content"][0]["text"] = (
                    "X" * 33000 if completion_index == 1 else marker
                )
        _write_jsonl(boundary_events_path, boundary_values)
        report = collect(cases_path, raw_root, EXPECTED_CASE_COUNT)
        if (
            report["overall_status"] != "FAIL"
            or "required_tool_result_tail_fragment_missing"
            not in report["cases"][4]["failures"]
        ):
            raise AssertionError("marker from a separate small result was accepted")

        boundary_values = [
            json.loads(line) for line in boundary_original.splitlines() if line
        ]
        for value in boundary_values:
            if value.get("type") == "assistant.message":
                value["data"]["content"] = (
                    str(boundary_case["required_response_fragment"]) + " extra"
                )
        _write_jsonl(boundary_events_path, boundary_values)
        report = collect(cases_path, raw_root, EXPECTED_CASE_COUNT)
        if (
            report["overall_status"] != "FAIL"
            or "required_response_exact_mismatch"
            not in report["cases"][4]["failures"]
        ):
            raise AssertionError("non-exact boundary response did not fail")
        boundary_events_path.write_text(boundary_original, encoding="utf-8")

        first = raw_root / f"01-{cases[0]['id']}" / "copilot.jsonl"
        original = first.read_text(encoding="utf-8")
        original_values = [json.loads(line) for line in original.splitlines() if line]
        first_run_path = raw_root / f"01-{cases[0]['id']}" / "run.json"
        first_run_original = first_run_path.read_text(encoding="utf-8")
        first_otel = raw_root / f"01-{cases[0]['id']}" / "otel.jsonl"
        first_otel_original = first_otel.read_text(encoding="utf-8")

        timeout_run = json.loads(first_run_original)
        timeout_run["timed_out"] = True
        timeout_run["process_tree_terminated"] = True
        timeout_run["exit_code"] = None
        _write_json(first_run_path, timeout_run)
        first.write_text("", encoding="utf-8")
        first_otel.write_text("", encoding="utf-8")
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or "case_timeout_observed" not in report["cases"][0]["failures"]
        ):
            raise AssertionError("timed-out case did not fail")
        first_run_path.write_text(first_run_original, encoding="utf-8")
        first.write_text(original, encoding="utf-8")
        first_otel.write_text(first_otel_original, encoding="utf-8")

        invalid_run = json.loads(first_run_original)
        invalid_run["timeout_seconds"] = 899
        _write_json(first_run_path, invalid_run)
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or "case_timeout_contract_invalid"
            not in report["cases"][0]["failures"]
        ):
            raise AssertionError("non-900-second timeout contract did not fail")

        invalid_run = json.loads(first_run_original)
        invalid_run["candidate_runtime_root"] = str((raw_root / "elsewhere").resolve())
        _write_json(first_run_path, invalid_run)
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or "installed_launcher_outside_candidate_runtime"
            not in report["cases"][0]["failures"]
        ):
            raise AssertionError("installed launcher scope mismatch did not fail")

        invalid_run = json.loads(first_run_original)
        invalid_run["cli_version"] = "1.0.78"
        _write_json(first_run_path, invalid_run)
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or "cli_version_unsupported" not in report["cases"][0]["failures"]
        ):
            raise AssertionError("unsupported CLI version did not fail")

        invalid_run = json.loads(first_run_original)
        invalid_run["noninteractive_permission_contract"]["no_ask_user"] = False
        _write_json(first_run_path, invalid_run)
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or "noninteractive_permission_contract_mismatch"
            not in report["cases"][0]["failures"]
        ):
            raise AssertionError("noninteractive permission mismatch did not fail")
        first_run_path.write_text(first_run_original, encoding="utf-8")

        second_run_path = raw_root / f"02-{cases[1]['id']}" / "run.json"
        second_run_original = second_run_path.read_text(encoding="utf-8")
        second_run = json.loads(second_run_original)
        boundary_run = _load_json(
            raw_root / f"05-{cases[4]['id']}" / "run.json"
        )
        for key in (
            "candidate_runtime_root",
            "launcher_path",
            "launcher_install_root",
            "launcher_sha256",
            "launcher_manifest_path",
            "launcher_manifest_sha256",
            "copilot_home",
        ):
            second_run[key] = boundary_run[key]
        _write_json(second_run_path, second_run)
        report = collect(cases_path, raw_root, 2)
        if (
            report["overall_status"] != "FAIL"
            or "installed_identity_changed_across_cases"
            not in report["cases"][1]["failures"]
        ):
            raise AssertionError("cross-case installed identity change did not fail")
        second_run_path.write_text(second_run_original, encoding="utf-8")

        boundary_run_path = raw_root / f"05-{cases[4]['id']}" / "run.json"
        boundary_run_original = boundary_run_path.read_text(encoding="utf-8")
        relocated_boundary = json.loads(boundary_run_original)
        installed_run = json.loads(first_run_original)
        for key in (
            "launcher_path",
            "launcher_install_root",
            "launcher_sha256",
            "launcher_manifest_path",
            "launcher_manifest_sha256",
            "copilot_home",
        ):
            relocated_boundary[key] = installed_run[key]
        _write_json(boundary_run_path, relocated_boundary)
        report = collect(cases_path, raw_root, EXPECTED_CASE_COUNT)
        if (
            report["overall_status"] != "FAIL"
            or "boundary_fixture_launcher_scope_invalid"
            not in report["cases"][4]["failures"]
        ):
            raise AssertionError("relocated boundary launcher did not fail")
        boundary_run_path.write_text(boundary_run_original, encoding="utf-8")
        for tool_name in ("shell", "read_file", "web_fetch", "ask_user"):
            values = json.loads(json.dumps(original_values))
            values.insert(
                -1,
                {
                    "type": "tool.execution_start",
                    "data": {"toolCallId": "forbidden", "toolName": tool_name},
                },
            )
            values.insert(
                -1,
                {
                    "type": "tool.execution_complete",
                    "data": {"toolCallId": "forbidden", "success": True},
                },
            )
            _write_jsonl(first, values)
            report = collect(cases_path, raw_root, 1)
            if report["overall_status"] != "FAIL" or not any(
                failure.startswith("unknown_tool:")
                for failure in report["cases"][0]["failures"]
            ):
                raise AssertionError(f"unknown tool gate did not fail: {tool_name}")

        for event_type in sorted(
            FORBIDDEN_EVENT_TYPES | {"permission.unexpected_schema"}
        ):
            values = json.loads(json.dumps(original_values))
            values.insert(-1, {"type": event_type, "data": {}})
            _write_jsonl(first, values)
            report = collect(cases_path, raw_root, 1)
            if (
                report["overall_status"] != "FAIL"
                or f"forbidden_event:{event_type}"
                not in report["cases"][0]["failures"]
            ):
                raise AssertionError(
                    f"permission/user-input/subagent gate did not fail: {event_type}"
                )

        values = json.loads(json.dumps(original_values))
        failed_call_id = None
        for value in values:
            if value.get("type") == "tool.execution_complete":
                failed_call_id = value["data"]["toolCallId"]
                value["data"]["success"] = False
                value["data"]["error"] = "sanitized synthetic failure"
                value["data"].pop("result", None)
                break
        _write_jsonl(first, values)
        report = collect(cases_path, raw_root, 1)
        failures = report["cases"][0]["failures"]
        if (
            report["overall_status"] != "FAIL"
            or f"tool_failed:{failed_call_id}" not in failures
            or "tool_result_content_missing" in failures
        ):
            raise AssertionError("failed tool content compatibility gate is invalid")

        first_db = _synthetic_db(raw_root, cases[0], 1)
        database = sqlite3.connect(first_db)
        database.execute(
            "UPDATE assistant_usage_events SET model = ?",
            ("unexpected-model",),
        )
        database.commit()
        database.close()
        first.write_text(original, encoding="utf-8")
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or "actual_model_mismatch" not in report["cases"][0]["failures"]
        ):
            raise AssertionError("model mismatch gate did not fail")

        database = sqlite3.connect(first_db)
        database.execute(
            "UPDATE assistant_usage_events SET model = ?, total_nano_aiu = ?",
            ("claude-haiku-4.5", 31 * NANO_AIU_PER_CREDIT),
        )
        database.commit()
        database.close()
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or report["stop_required"] is not True
            or "per_case_credit_cap_exceeded"
            not in report["cases"][0]["failures"]
        ):
            raise AssertionError("isolated per-case credit gate did not stop")

        database = sqlite3.connect(first_db)
        database.execute(
            "UPDATE assistant_usage_events SET total_nano_aiu = NULL"
        )
        database.commit()
        database.close()
        report = collect(cases_path, raw_root, 1)
        if report["overall_status"] != "STOP_CREDIT_GATE":
            raise AssertionError("missing credit evidence did not stop")

        database = sqlite3.connect(first_db)
        database.execute(
            "UPDATE assistant_usage_events SET total_nano_aiu = ?",
            (NANO_AIU_PER_CREDIT,),
        )
        database.commit()
        database.close()

        reduced_cap_report = collect(
            cases_path,
            raw_root,
            1,
            aggregate_credit_cap=48,
        )
        if (
            reduced_cap_report["overall_status"] != PASS_WITH_RESIDUAL
            or reduced_cap_report["aggregate_credit_cap"] != 48
        ):
            raise AssertionError("reduced aggregate credit cap was not preserved")
        for invalid_cap in (0, 81):
            try:
                collect(
                    cases_path,
                    raw_root,
                    1,
                    aggregate_credit_cap=invalid_cap,
                )
            except EvidenceError:
                pass
            else:
                raise AssertionError(
                    f"invalid aggregate credit cap was accepted: {invalid_cap}"
                )

        values = json.loads(json.dumps(original_values))
        for value in values:
            if value.get("type") == "result":
                value["exitCode"] = 1
        _write_jsonl(first, values)
        report = collect(cases_path, raw_root, 1)
        if (
            report["overall_status"] != "FAIL"
            or "result_run_exit_code_mismatch" not in report["cases"][0]["failures"]
        ):
            raise AssertionError("result/run exit cross-check did not fail")

        first.write_text("{not-json}\n", encoding="utf-8")
        try:
            collect(cases_path, raw_root, 1)
        except EvidenceError:
            pass
        else:
            raise AssertionError("malformed JSONL did not fail closed")

    print(
        "SELF-TEST OK: CLI 1.0.77 JSONL, read-only session-store contract, "
        "OTel model/token/agent, URL provenance, tail boundary, timeout, "
        "launcher scope, CLI identity, permission and aggregate Credit gates "
        "are fail-closed. No prompt was sent."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--completed-count", type=int)
    parser.add_argument("--aggregate-credit-cap", type=int, default=50)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        cases_path = args.cases.resolve()
        if args.self_test:
            return self_test(cases_path)
        if args.raw_root is None or args.output is None or args.completed_count is None:
            parser.error("--raw-root, --output and --completed-count are required")
        report = collect(
            cases_path,
            args.raw_root.resolve(),
            args.completed_count,
            args.aggregate_credit_cap,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "overall_status": report["overall_status"],
                    "completed_count": report["completed_count"],
                    "aggregate_ai_credits": report["aggregate_ai_credits"],
                    "stop_required": report["stop_required"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
            )
        )
        if report["overall_status"] == "STOP_CREDIT_GATE":
            return 3
        return 0 if report["overall_status"] in {
            "PASS",
            PASS_WITH_RESIDUAL,
        } else 1
    except (OSError, EvidenceError) as exc:
        print(f"collection failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
