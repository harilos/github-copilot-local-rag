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
FORBIDDEN_EVENT_TYPES = frozenset(
    (
        "permission.requested",
        "permission.request",
        "user_input.requested",
        "user_input.request",
        "subagent.started",
    )
)
FORBIDDEN_TOOL_RE = re.compile(
    r"(?:^|[-_/])(shell|powershell|bash|cmd|terminal|file|read|write|edit|"
    r"delete|web|fetch|browser|ask[_-]?user)(?:$|[-_/])",
    re.IGNORECASE,
)


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


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
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
    if not values:
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
        if scope == "temporary_boundary_fixture":
            minimum_bytes = value.get("minimum_tool_result_bytes")
            required_fragment = value.get("required_response_fragment")
            revision = value.get("compatibility_revision")
            if (
                isinstance(minimum_bytes, bool)
                or not isinstance(minimum_bytes, int)
                or minimum_bytes <= 32768
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
        # Copilot CLI 1.0.75 compatibility: phase-less, explicitly terminal,
        # empty toolRequests and a matching immediate turn_end.
        if phase is not None or tool_requests != []:
            continue
        turn_id = data.get("turnId")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if index + 1 >= len(events):
            continue
        following = events[index + 1]
        if _event_type(following) != "assistant.turn_end":
            continue
        if _event_data(following).get("turnId") == turn_id:
            terminal = content
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
    token_values: dict[str, list[int]] = {
        "input": [],
        "prompt": [],
        "output": [],
        "completion": [],
    }
    invalid_tokens = False
    for event in otel:
        for key, value in _otel_pairs(event):
            normalized = key.lower().replace("-", "_")
            if normalized.endswith("gen_ai.request.model"):
                if isinstance(value, str) and value:
                    requested.add(value)
            elif normalized.endswith("gen_ai.response.model"):
                if isinstance(value, str) and value:
                    responded.add(value)
            elif normalized.endswith("gen_ai.agent.id"):
                if isinstance(value, str) and value:
                    agent_ids.add(value)
            elif normalized.endswith("gen_ai.agent.name"):
                if isinstance(value, str) and value:
                    agent_names.add(value)
            else:
                token_kind = None
                for candidate, suffix in (
                    ("input", "gen_ai.usage.input_tokens"),
                    ("prompt", "gen_ai.usage.prompt_tokens"),
                    ("output", "gen_ai.usage.output_tokens"),
                    ("completion", "gen_ai.usage.completion_tokens"),
                ):
                    if normalized.endswith(suffix):
                        token_kind = candidate
                        break
                if token_kind is not None:
                    parsed = _as_nonnegative_int(value)
                    if parsed is None:
                        invalid_tokens = True
                    else:
                        token_values[token_kind].append(parsed)
    input_values = token_values["input"] or token_values["prompt"]
    output_values = token_values["output"] or token_values["completion"]
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
            "launcher_sha256": live_hash,
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
    if run.get("max_ai_credits") != 30:
        failures.append("per_case_credit_cap_mismatch")
    if run.get("launcher_scope") != case["launcher_scope"]:
        failures.append("launcher_scope_mismatch")
    if run.get("fresh_session") is not True:
        failures.append("fresh_session_not_asserted")
    if run.get("retry_count") != 0:
        failures.append("retry_observed")

    event_types = [_event_type(event) for event in events]
    for event_type in event_types:
        if event_type in FORBIDDEN_EVENT_TYPES:
            failures.append(f"forbidden_event:{event_type}")

    manifest_home, launcher_evidence, manifest_failures = _validate_launcher_manifest(run)
    failures.extend(manifest_failures)
    session_id, result_premium_requests, result_failures = _result_contract(
        events, exit_code
    )
    failures.extend(result_failures)

    mcp_event_seen = False
    owned_mcp_listed = False
    mcp_state_ready = False
    for event in events:
        if _event_type(event) != "session.mcp_servers_loaded":
            continue
        mcp_event_seen = True
        servers = _event_data(event).get("servers")
        if not isinstance(servers, list):
            continue
        for server in servers:
            if not isinstance(server, dict):
                continue
            name = server.get("name") or server.get("serverName")
            state = str(server.get("status") or server.get("state") or "").lower()
            if name == "localragagent003":
                owned_mcp_listed = True
                if state in ("connected", "loaded", "ready", "running"):
                    mcp_state_ready = True
    if not mcp_event_seen:
        failures.append("mcp_servers_loaded_missing")
    elif not owned_mcp_listed:
        failures.append("owned_mcp_not_loaded")

    starts: dict[str, str] = {}
    completions: dict[str, dict[str, Any]] = {}
    result_bytes: list[int] = []
    result_texts: list[str] = []
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
            if content is None:
                failures.append("tool_result_content_missing")
            else:
                result_bytes.append(len(content.encode("utf-8")))
                result_texts.append(content)
    if set(starts) != set(completions):
        failures.append("tool_completion_mismatch")
    successful_owned_tool = False
    for call_id, completion in completions.items():
        if completion.get("success") is not True or completion.get("error"):
            failures.append(f"tool_failed:{call_id}")
        elif starts.get(call_id) in ALLOWED_RUNTIME_TOOLS:
            successful_owned_tool = True
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
    if isinstance(required_fragment, str) and required_fragment not in response:
        failures.append("required_response_fragment_missing")
    if isinstance(required_fragment, str) and not any(
        required_fragment in rendered for rendered in result_texts
    ):
        failures.append("required_tool_result_fragment_missing")
    minimum_result_bytes = case.get("minimum_tool_result_bytes")
    if isinstance(minimum_result_bytes, int) and max(result_bytes, default=0) < minimum_result_bytes:
        failures.append("tool_result_boundary_not_observed")

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
    result = {
        "case_id": case_id,
        "tier": case["tier"],
        "status": "PASS" if not failures else "FAIL",
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
        "result_premium_requests": result_premium_requests,
        "launcher_identity": launcher_evidence,
        "credit_observable": credit_observable,
        "total_nano_aiu": nano_aiu if credit_observable else None,
        "ai_credits": nano_aiu / NANO_AIU_PER_CREDIT if credit_observable else None,
        "permission_evidence": "fixed_launcher_manifest_and_exact_successful_tool_calls",
        "residuals": [
            "permission_and_user_input_events_not_exposed_by_copilot_cli_1.0.77"
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
    if aggregate_credit_cap != DEFAULT_AGGREGATE_CREDIT_CAP:
        raise EvidenceError("aggregate credit cap is fixed at 50")
    results: list[dict[str, Any]] = []
    total_nano = 0
    credit_stop = False
    for ordinal, case in enumerate(cases[:completed_count], 1):
        run_root = raw_root / f"{ordinal:02d}-{case['id']}"
        run = _load_json(run_root / "run.json")
        events = _load_jsonl(run_root / "copilot.jsonl")
        otel = _load_jsonl(run_root / "otel.jsonl")
        result = evaluate_case(case, run, events, otel)
        results.append(result)
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
    overall = "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL"
    if credit_stop:
        overall = "STOP_CREDIT_GATE"
    return {
        "schema_version": REPORT_SCHEMA,
        "overall_status": overall,
        "completed_count": completed_count,
        "canonical_case_count": len(cases),
        "aggregate_credit_cap": aggregate_credit_cap,
        "aggregate_total_nano_aiu": total_nano,
        "aggregate_ai_credits": total_nano / NANO_AIU_PER_CREDIT,
        "stop_required": credit_stop,
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
    for ordinal, case in enumerate(cases, 1):
        run_root = raw_root / f"{ordinal:02d}-{case['id']}"
        actual_model = resolved[case["tier"]]
        session_id = f"synthetic-session-{ordinal}"
        input_tokens = 100 + ordinal
        output_tokens = 20 + ordinal
        install_root = raw_root / f"_synthetic-install-{ordinal}"
        copilot_home = raw_root / f"_synthetic-copilot-home-{ordinal}"
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
        database = sqlite3.connect(copilot_home / "session-store.db")
        try:
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
                "launcher_sha256": launcher_hash,
                "launcher_manifest_schema": 1,
                "launcher_manifest_path": str(manifest_path.resolve()),
                "launcher_manifest_sha256": _sha256_file(manifest_path),
                "copilot_home": str(copilot_home.resolve()),
                "prompt_sha256": _sha256_text(case["prompt"]),
                "max_ai_credits": 30,
                "fresh_session": True,
                "retry_count": 0,
                "exit_code": 0,
            },
        )
        synthetic_content = '{"status":"ok"}'
        synthetic_answer = "Synthetic answer."
        if case.get("launcher_scope") == "temporary_boundary_fixture":
            synthetic_content = "X" * 33000 + str(case["required_response_fragment"])
            synthetic_answer = str(case["required_response_fragment"])
        tool_events: list[dict[str, Any]] = []
        for call_index in range(case["minimum_search_calls"]):
            search_id = f"search-{ordinal}-{call_index + 1}"
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
                            "result": {
                                "content": [
                                    {"type": "text", "text": synthetic_content}
                                ]
                            },
                        },
                    },
                ]
            )
        events = [
            {
                "type": "session.mcp_servers_loaded",
                "data": {
                    "servers": [
                        {
                            "name": "localragagent003",
                            ("state" if ordinal % 2 == 0 else "status"): (
                                "loaded" if ordinal % 2 == 0 else "connected"
                            ),
                        }
                    ]
                },
            },
            *tool_events,
            {
                "type": "assistant.message",
                "data": {"phase": "final_answer", "content": synthetic_answer},
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
                    "attributes": [
                        {
                            "key": "gen_ai.request.model",
                            "value": {"stringValue": request_model},
                        },
                        {
                            "key": "gen_ai.response.model",
                            "value": {"stringValue": actual_model},
                        },
                        {
                            "key": agent_key,
                            "value": {"stringValue": case["expected_agent"]},
                        },
                        {
                            "key": "gen_ai.usage.input_tokens",
                            "value": {"intValue": input_tokens},
                        },
                        {
                            "key": "gen_ai.usage.output_tokens",
                            "value": {"intValue": output_tokens},
                        },
                    ]
                }
            ],
        )


def _synthetic_db(raw_root: Path, case: dict[str, Any], ordinal: int) -> Path:
    run_root = raw_root / f"{ordinal:02d}-{case['id']}"
    run = _load_json(run_root / "run.json")
    return Path(str(run["copilot_home"])) / "session-store.db"


def self_test(cases_path: Path) -> int:
    cases = load_cases(cases_path)
    with tempfile.TemporaryDirectory(prefix="lrr-agent003-cli-prod-collector-") as tmp:
        raw_root = Path(tmp) / "raw"
        _synthetic_case_files(raw_root, cases)
        report = collect(cases_path, raw_root, EXPECTED_CASE_COUNT)
        if report["overall_status"] != "PASS":
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

        first = raw_root / f"01-{cases[0]['id']}" / "copilot.jsonl"
        original = first.read_text(encoding="utf-8")
        original_values = [json.loads(line) for line in original.splitlines() if line]
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

        for event_type in sorted(FORBIDDEN_EVENT_TYPES):
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
            ("claude-haiku-4.5", 51 * NANO_AIU_PER_CREDIT),
        )
        database.commit()
        database.close()
        report = collect(cases_path, raw_root, 1)
        if report["overall_status"] != "STOP_CREDIT_GATE":
            raise AssertionError("aggregate credit gate did not stop")

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
        "OTel model/token/agent, launcher, tool and aggregate Credit gates are "
        "fail-closed. No prompt was sent."
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
        return 0 if report["overall_status"] == "PASS" else 1
    except (OSError, EvidenceError) as exc:
        print(f"collection failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
