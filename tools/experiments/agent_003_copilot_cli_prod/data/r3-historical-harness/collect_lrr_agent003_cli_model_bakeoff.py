#!/usr/bin/env python3
"""Evidence collector for the Agent003 seven-model Copilot CLI bakeoff.

The collector is intentionally separate from the accepted five-case UAT collector.
It consumes immutable per-run evidence, never launches Copilot, and treats unknown
Credit evidence as a hard stop.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_PROD_COLLECTOR_PATH = Path(__file__).with_name(
    "collect_lrr_agent003_cli_prod_uat.py"
)
_PROD_SPEC = importlib.util.spec_from_file_location(
    "lrr_agent003_cli_prod_uat_collector", _PROD_COLLECTOR_PATH
)
if _PROD_SPEC is None or _PROD_SPEC.loader is None:
    raise RuntimeError(f"cannot load accepted UAT collector: {_PROD_COLLECTOR_PATH}")
prod = importlib.util.module_from_spec(_PROD_SPEC)
_PROD_SPEC.loader.exec_module(prod)


AUTHORITY_SCHEMA = "lrr-agent003-cli-model-bakeoff-authority-v1"
RUN_SCHEMA = "lrr-agent003-cli-model-bakeoff-run-v1"
REPORT_SCHEMA = "lrr-agent003-cli-model-bakeoff-report-v1"
SNAPSHOT_SCHEMA = "lrr-agent003-cli-session-usage-snapshot-v1"
EXPECTED_CANDIDATES = [
    "claude-haiku-4.5",
    "gpt-5-mini",
    "gpt-5.4-mini",
    "gpt-5.6-luna",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "mai-code-1-flash-picker",
]
AUTHORITATIVE_URL = "https://github.com/harilos/fizzbuzz-planet-docs/issues/1"
POLICY_ERROR_RE = re.compile(
    r"(?:model.{0,100}(?:not available|unavailable|not enabled|unsupported|"
    r"requires? (?:enablement|enabling|organization)))|(?:organi[sz]ation.{0,100}"
    r"policy)|(?:policy.{0,100}(?:reject|den(?:y|ied)|block))",
    re.IGNORECASE | re.DOTALL,
)
NANO_AIU_PER_CREDIT = 1_000_000_000


class BakeoffError(ValueError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BakeoffError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BakeoffError(f"JSON root is not an object: {path}")
    return value


def _load_jsonl(path: Path, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BakeoffError(f"cannot read JSONL: {path}: {exc}") from exc
    values: list[dict[str, Any]] = []
    for ordinal, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BakeoffError(f"invalid JSONL: {path}:{ordinal}: {exc}") from exc
        if not isinstance(value, dict):
            raise BakeoffError(f"JSONL object expected: {path}:{ordinal}")
        values.append(value)
    if not values and not allow_empty:
        raise BakeoffError(f"JSONL is empty: {path}")
    return values


def _require_case(value: Any, name: str, *, expected_tier: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BakeoffError(f"{name} must be an object")
    if value.get("tier") != expected_tier:
        raise BakeoffError(f"{name}.tier is not canonical")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or prompt != prompt.strip():
        raise BakeoffError(f"{name}.prompt is invalid")
    reserve = value.get("minimum_remaining_credit_before_launch")
    if isinstance(reserve, bool) or not isinstance(reserve, (int, float)) or reserve <= 0:
        raise BakeoffError(f"{name} Credit reserve is invalid")
    return value


def load_authority(path: Path) -> dict[str, Any]:
    authority = _load_json(path)
    if authority.get("schema_version") != AUTHORITY_SCHEMA:
        raise BakeoffError("authority schema is invalid")
    if authority.get("candidate_models") != EXPECTED_CANDIDATES:
        raise BakeoffError("candidate order/list is not canonical")
    if authority.get("fresh_session_repetitions") != 3:
        raise BakeoffError("exactly three fresh sessions are required per candidate")
    cap = authority.get("aggregate_ai_credit_cap")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or float(cap) != 50.0:
        raise BakeoffError("the new UAT epoch must have an exact 50 Credit cap")
    if authority.get("per_session_cli_soft_cap") != 30:
        raise BakeoffError("Copilot CLI per-session soft cap must be 30")
    credit_epoch = authority.get("credit_epoch")
    if not isinstance(credit_epoch, str) or "starts at zero" not in credit_epoch:
        raise BakeoffError("new-UAT Credit epoch is not explicit")
    savings = _require_case(authority.get("savings_case"), "savings_case", expected_tier="savings")
    standard = _require_case(authority.get("standard_case"), "standard_case", expected_tier="standard")
    thorough = _require_case(authority.get("thorough_case"), "thorough_case", expected_tier="thorough")
    boundary = _require_case(authority.get("boundary_case"), "boundary_case", expected_tier="standard")
    if standard.get("requested_model") != "auto" or thorough.get("requested_model") != "auto":
        raise BakeoffError("standard and thorough must use auto")
    if boundary.get("requested_model") != "auto":
        raise BakeoffError("boundary must use auto")
    if savings.get("authoritative_url") != AUTHORITATIVE_URL or standard.get("authoritative_url") != AUTHORITATIVE_URL:
        raise BakeoffError("simple question authoritative URL is not canonical")
    if savings.get("prompt") != standard.get("prompt"):
        raise BakeoffError("savings and standard simple prompts must be byte-identical")
    if boundary.get("minimum_tool_result_bytes", 0) <= 32768:
        raise BakeoffError("boundary must exceed 32 KiB")
    if thorough.get("minimum_markdown_source_urls") != 2:
        raise BakeoffError("thorough cross-document case requires two unique source URLs")
    expected_facts = thorough.get("expected_facts")
    if not isinstance(expected_facts, dict) or expected_facts.get("requested_percent") != 12 or expected_facts.get("confirmed_percent") != 7 or expected_facts.get("issue_state") != "open" or expected_facts.get("settlement_location") != "衛星バズ" or expected_facts.get("contact_rule") != "非接触（直接接触禁止）" or expected_facts.get("decision_topics") != ["技術供与", "採掘"]:
        raise BakeoffError("thorough expected-fact authority is not canonical")
    if thorough.get("required_classification_sections") != ["確定事項", "提案段階", "未確認"]:
        raise BakeoffError("thorough classification-section authority is not canonical")
    if boundary.get("required_response_fragment") != "LRR-CLI-LARGE-OUTPUT-TAIL-7F3C9A21":
        raise BakeoffError("boundary marker is not canonical")
    unavailable = authority.get("unavailable_policy_contract")
    if not isinstance(unavailable, dict) or unavailable.get("forbid_auto_fallback") is not True:
        raise BakeoffError("unavailable-model contract is invalid")
    return authority


def snapshot_session_store(copilot_home: Path) -> dict[str, Any]:
    db_path = copilot_home / "session-store.db"
    snapshot: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "copilot_home": str(copilot_home.resolve()),
        "session_store_path": str(db_path.resolve()),
        "session_store_exists": False,
        "row_count": 0,
        "total_nano_aiu": 0,
        "maximum_usage_event_id": None,
    }
    if not db_path.is_file() or db_path.is_symlink():
        return snapshot
    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            connection.execute("PRAGMA query_only=ON")
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(assistant_usage_events)")}
            required = {"id", "total_nano_aiu"}
            if not required.issubset(columns):
                raise BakeoffError("assistant_usage_events schema is missing required columns")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_nano_aiu), 0), MAX(id) FROM assistant_usage_events"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BakeoffError(f"session store snapshot failed: {exc}") from exc
    count, total, maximum_id = row
    if not isinstance(count, int) or not isinstance(total, int) or count < 0 or total < 0:
        raise BakeoffError("session store snapshot values are invalid")
    snapshot.update(
        {
            "session_store_exists": True,
            "row_count": count,
            "total_nano_aiu": total,
            "maximum_usage_event_id": maximum_id,
        }
    )
    return snapshot


def _snapshot_delta(before: dict[str, Any], after: dict[str, Any]) -> tuple[int | None, int | None, list[str]]:
    failures: list[str] = []
    if before.get("schema_version") != SNAPSHOT_SCHEMA or after.get("schema_version") != SNAPSHOT_SCHEMA:
        return None, None, ["usage_snapshot_schema_invalid"]
    if before.get("copilot_home") != after.get("copilot_home"):
        failures.append("usage_snapshot_home_mismatch")
    row_delta: int | None = None
    nano_delta: int | None = None
    for key, target in (("row_count", "row"), ("total_nano_aiu", "nano")):
        left, right = before.get(key), after.get(key)
        if isinstance(left, bool) or not isinstance(left, int) or isinstance(right, bool) or not isinstance(right, int) or right < left:
            failures.append(f"usage_snapshot_{target}_delta_invalid")
            continue
        if target == "row":
            row_delta = right - left
        else:
            nano_delta = right - left
    return row_delta, nano_delta, failures


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise BakeoffError(f"cannot read text: {path}: {exc}") from exc


def _validate_harness_identity(run: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for prefix in ("runner", "collector", "authority"):
        path_value = run.get(f"{prefix}_path")
        digest_value = run.get(f"{prefix}_sha256")
        if (
            not isinstance(path_value, str)
            or not path_value
            or not Path(path_value).is_absolute()
        ):
            failures.append(f"{prefix}_path_invalid")
            continue
        path = Path(path_value)
        if not path.is_file() or path.is_symlink():
            failures.append(f"{prefix}_file_unreadable")
        elif not isinstance(digest_value, str) or digest_value != prod._sha256_file(path):
            failures.append(f"{prefix}_hash_mismatch")
    return failures


def _event_tool_evidence(events: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    starts: dict[str, str] = {}
    completions: dict[str, dict[str, Any]] = {}
    contents: dict[str, str] = {}
    structured: dict[str, Any] = {}
    for event in events:
        event_type = prod._event_type(event)
        data = prod._event_data(event)
        if event_type == "tool.execution_start":
            call_id = data.get("toolCallId")
            name = prod._tool_name(data)
            if not isinstance(call_id, str) or not call_id or call_id in starts:
                failures.append("tool_start_identity_invalid")
                continue
            starts[call_id] = name
            if name not in prod.ALLOWED_RUNTIME_TOOLS:
                failures.append(f"foreign_tool:{name or '<missing>'}")
        elif event_type == "tool.execution_complete":
            call_id = data.get("toolCallId")
            if not isinstance(call_id, str) or not call_id or call_id in completions:
                failures.append("tool_completion_identity_invalid")
                continue
            completions[call_id] = data
            content = prod._tool_result_content(data)
            if isinstance(content, str):
                contents[call_id] = content
            result = data.get("result")
            if isinstance(result, dict):
                structured[call_id] = result.get("structuredContent")
    if set(starts) != set(completions):
        failures.append("tool_completion_mismatch")
    search_urls: set[str] = set()
    all_urls: set[str] = set()
    result_bytes: list[int] = []
    for call_id, completion in completions.items():
        if completion.get("success") is not True or completion.get("error"):
            failures.append(f"tool_failed:{call_id}")
            continue
        content = contents.get(call_id, "")
        result_bytes.append(len(content.encode("utf-8")))
        urls = prod._tool_evidence_urls(content, structured.get(call_id))
        all_urls.update(urls)
        if starts.get(call_id) == prod.SEARCH_TOOL:
            search_urls.update(urls)
    return {
        "failures": failures,
        "starts": starts,
        "search_calls": sum(name == prod.SEARCH_TOOL for name in starts.values()),
        "evidence_calls": sum(name == prod.EVIDENCE_TOOL for name in starts.values()),
        "search_evidence_urls": search_urls,
        "all_evidence_urls": all_urls,
        "result_bytes": result_bytes,
        "contents": list(contents.values()),
    }


def _interaction_observed(events: list[dict[str, Any]]) -> bool:
    return any(
        prod._event_type(event).startswith("permission.")
        or prod._event_type(event).startswith("user_input.")
        for event in events
    )


def _simple_answer_failures(response: str, search_urls: set[str]) -> tuple[list[str], set[str], set[str]]:
    failures: list[str] = []
    if re.search(r"(?<!\d)12\s*(?:%|％)", response) is None:
        failures.append("requested_12_percent_missing")
    if re.search(r"(?<!\d)7\s*(?:%|％)", response) is None:
        failures.append("confirmed_7_percent_missing")
    if re.search(
        r"(?<![A-Za-z])open(?![A-Za-z])|オープン|未解決|未完了",
        response,
        re.IGNORECASE,
    ) is None:
        failures.append("issue_open_state_missing")
    response_urls = prod._response_https_urls(response)
    markdown_urls = prod._markdown_source_urls(response)
    if markdown_urls != {AUTHORITATIVE_URL} or response_urls != {AUTHORITATIVE_URL}:
        failures.append("response_authoritative_markdown_url_not_exact")
    if AUTHORITATIVE_URL not in search_urls:
        failures.append("authoritative_url_missing_from_search_evidence")
    if not response_urls.issubset(search_urls):
        failures.append("response_url_not_from_same_run_search_evidence")
    return failures, response_urls, markdown_urls


def _is_policy_rejection(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    otel: list[dict[str, Any]],
    stderr: str,
    stdout: str,
    row_delta: int | None,
    nano_delta: int | None,
) -> tuple[bool, list[str]]:
    evidence_failures: list[str] = []
    exit_code = run.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code == 0:
        evidence_failures.append("policy_exit_not_nonzero")
    assistant_messages = [event for event in events if prod._event_type(event) == "assistant.message"]
    if assistant_messages:
        evidence_failures.append("policy_assistant_message_observed")
    otel_evidence = prod._otel_evidence(otel)
    if otel_evidence["response_models"]:
        evidence_failures.append("policy_otel_response_model_observed")
    if otel_evidence["input_tokens"] not in (None, 0) or otel_evidence["output_tokens"] not in (None, 0):
        evidence_failures.append("policy_otel_usage_observed")
    if row_delta != 0:
        evidence_failures.append("policy_session_usage_row_delta_nonzero")
    if nano_delta != 0:
        evidence_failures.append("policy_credit_delta_nonzero")
    result_events = [event for event in events if prod._event_type(event) == "result"]
    for result in result_events:
        usage = result.get("usage")
        if not isinstance(usage, dict) or usage.get("premiumRequests") not in (0, 0.0, "0", "0.0"):
            evidence_failures.append("policy_result_usage_nonzero_or_unknown")
    if not POLICY_ERROR_RE.search(stderr + "\n" + stdout):
        evidence_failures.append("policy_rejection_message_not_recognized")
    return not evidence_failures, sorted(set(evidence_failures))


def evaluate_run(authority: dict[str, Any], root: Path) -> dict[str, Any]:
    run = _load_json(root / "run.json")
    if run.get("schema_version") != RUN_SCHEMA:
        raise BakeoffError(f"run schema mismatch: {root}")
    result: dict[str, Any] = {
        "plan_ordinal": run.get("plan_ordinal"),
        "run_id": run.get("run_id"),
        "case_kind": run.get("case_kind"),
        "candidate_model": run.get("candidate_model"),
        "attempt": run.get("attempt"),
        "execution_state": run.get("execution_state"),
        "prompt_sha256": run.get("prompt_sha256"),
        "status": "FAIL",
        "failures": [],
        "ai_credits": 0.0,
        "total_nano_aiu": 0,
        "elapsed_seconds": run.get("elapsed_seconds"),
    }
    state = run.get("execution_state")
    result["failures"].extend(_validate_harness_identity(run))
    if state in ("skipped_not_help_listed", "skipped_policy_preinference"):
        if result["failures"]:
            result["status"] = "FAIL"
            return result
        result["status"] = (
            "SKIPPED_NOT_HELP_LISTED"
            if state == "skipped_not_help_listed"
            else "SKIPPED_POLICY_PREINFERENCE"
        )
        result["skip_reason_run_id"] = run.get("skip_reason_run_id")
        return result
    if state != "executed":
        result["failures"] = ["execution_state_invalid"]
        return result

    events = _load_jsonl(root / "copilot.jsonl", allow_empty=True)
    otel = _load_jsonl(root / "otel.jsonl", allow_empty=True)
    stdout = _read_text(root / "copilot.jsonl")
    stderr = _read_text(root / "stderr.log")
    before = _load_json(root / "usage-before.json")
    after = _load_json(root / "usage-after.json")
    row_delta, nano_delta, snapshot_failures = _snapshot_delta(before, after)
    result.update(
        {
            "usage_row_delta": row_delta,
            "usage_nano_aiu_delta": nano_delta,
            "assistant_message_count": sum(prod._event_type(event) == "assistant.message" for event in events),
            "interaction_request_observed": _interaction_observed(events),
        }
    )
    if run.get("help_listed") is not True:
        result["failures"] = ["executed_model_not_help_listed"]
        return result
    policy, policy_failures = _is_policy_rejection(
        run, events, otel, stderr, stdout, row_delta, nano_delta
    )
    if policy:
        if result["failures"]:
            result["status"] = "FAIL"
            return result
        result["status"] = "UNAVAILABLE_POLICY_PREINFERENCE"
        result["policy_zero_usage_evidence"] = {
            "assistant_message_count": 0,
            "otel_response_model_count": 0,
            "session_usage_row_delta": 0,
            "total_nano_aiu_delta": 0,
        }
        return result

    failures = list(result["failures"]) + list(snapshot_failures)
    exit_code = run.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0:
        failures.append("cli_exit_nonzero_not_policy")
        failures.extend(policy_failures)
    if run.get("fresh_session") is not True:
        failures.append("fresh_session_not_asserted")
    if run.get("retry_count") != 0:
        failures.append("retry_observed")
    if run.get("max_ai_credits") != 30:
        failures.append("per_session_soft_cap_mismatch")
    if _interaction_observed(events):
        failures.append("permission_or_user_input_event_observed")

    tool = _event_tool_evidence(events)
    failures.extend(tool["failures"])
    response = prod._assistant_response(events)
    if not response:
        failures.append("final_assistant_response_missing")
    case_kind = run.get("case_kind")
    case = (
        authority["savings_case"]
        if case_kind == "savings"
        else authority["standard_case"]
        if case_kind == "standard"
        else authority["thorough_case"]
        if case_kind == "thorough"
        else authority["boundary_case"]
        if case_kind == "boundary"
        else None
    )
    if case is None:
        failures.append("case_kind_invalid")
        case = {}
    if run.get("prompt_sha256") != _sha256_text(str(case.get("prompt", ""))):
        failures.append("prompt_hash_mismatch")
    minimum_search = case.get("minimum_search_calls")
    maximum_search = case.get("maximum_search_calls")
    maximum_evidence = case.get("maximum_evidence_calls")
    if not isinstance(minimum_search, int) or not isinstance(maximum_search, int) or not minimum_search <= tool["search_calls"] <= maximum_search:
        failures.append("search_call_count_out_of_range")
    if not isinstance(maximum_evidence, int) or not 0 <= tool["evidence_calls"] <= maximum_evidence:
        failures.append("evidence_call_count_out_of_range")

    response_urls: set[str] = set()
    markdown_urls: set[str] = set()
    if case_kind in ("savings", "standard") and response:
        simple_failures, response_urls, markdown_urls = _simple_answer_failures(
            response, tool["search_evidence_urls"]
        )
        failures.extend(simple_failures)
    elif case_kind == "thorough" and response:
        response_urls = prod._response_https_urls(response)
        markdown_urls = prod._markdown_source_urls(response)
        if len(markdown_urls) < int(case.get("minimum_markdown_source_urls", 1)):
            failures.append("thorough_markdown_url_missing")
        if not response_urls.issubset(tool["all_evidence_urls"]):
            failures.append("thorough_response_url_not_from_tool_evidence")
        thorough_patterns = {
            "thorough_requested_12_percent_missing": r"(?<!\d)12\s*(?:%|％)",
            "thorough_confirmed_7_percent_missing": r"(?<!\d)7\s*(?:%|％)",
            "thorough_issue_open_missing": r"(?<![A-Za-z])open(?![A-Za-z])|オープン|未解決|未完了",
            "thorough_satellite_buzz_missing": r"衛星.{0,12}バズ|バズ.{0,12}衛星",
            "thorough_no_direct_contact_missing": r"非接触|直接接触.{0,12}(?:禁止|しない|避け|不可)",
            "thorough_technology_provision_missing": r"技術供与|技術提供",
            "thorough_mining_missing": r"採掘",
        }
        for failure_name, pattern in thorough_patterns.items():
            if re.search(pattern, response, re.IGNORECASE | re.DOTALL) is None:
                failures.append(failure_name)
        for section in case.get("required_classification_sections", []):
            if section not in response:
                failures.append(f"thorough_section_missing:{section}")
    elif case_kind == "boundary" and response:
        marker = str(case.get("required_response_fragment", ""))
        normalized = response.replace("\r\n", "\n").replace("\r", "\n").strip()
        header = re.search(r"(?m)^## References[ \t]*$", normalized)
        if header is None or normalized[: header.start()].strip() != marker:
            failures.append("boundary_primary_or_references_contract_invalid")
        qualifying = [content for content in tool["contents"] if len(content.encode("utf-8")) >= int(case.get("minimum_tool_result_bytes", 0))]
        if not qualifying:
            failures.append("boundary_over_32k_result_missing")
        elif not any(marker.encode("utf-8") in content.encode("utf-8")[-int(case.get("tool_result_tail_window_bytes", 256)) :] for content in qualifying):
            failures.append("boundary_tail_marker_missing")

    session_id, result_premium, result_failures = prod._result_contract(events, exit_code)
    failures.extend(result_failures)
    session_usage: dict[str, Any] = {}
    if isinstance(session_id, str) and session_id:
        session_usage, usage_failures = prod._read_session_usage(Path(str(run.get("copilot_home"))), session_id)
        failures.extend(usage_failures)
    else:
        failures.append("session_usage_unavailable")
    nano_aiu = session_usage.get("total_nano_aiu")
    if isinstance(nano_aiu, bool) or not isinstance(nano_aiu, int) or nano_aiu < 0:
        failures.append("credit_unknown")
        nano_aiu = None
    elif nano_delta != nano_aiu:
        failures.append("session_credit_snapshot_delta_mismatch")
    if nano_aiu is not None:
        result["total_nano_aiu"] = nano_aiu
        result["ai_credits"] = nano_aiu / NANO_AIU_PER_CREDIT

    otel_evidence = prod._otel_evidence(otel)
    usage_models = set(session_usage.get("models", []))
    response_models = set(otel_evidence["response_models"])
    requested_model = run.get("requested_model")
    if requested_model == "auto":
        if not usage_models or "auto" in usage_models or usage_models != response_models:
            failures.append("auto_model_resolution_invalid")
    elif not isinstance(requested_model, str) or usage_models != {requested_model} or response_models != {requested_model}:
        failures.append("candidate_model_exact_or_fallback_invalid")
    expected_agent = case.get("expected_agent")
    observed_agents = set(otel_evidence["agent_ids"]) | set(otel_evidence["agent_names"])
    if expected_agent not in observed_agents:
        failures.append("expected_agent_missing")
    if otel_evidence["input_tokens"] is None or otel_evidence["output_tokens"] is None:
        failures.append("otel_token_usage_unknown")

    mutation = _load_json(root / "temporary-model-mutation.json")
    if mutation.get("schema_version") != "lrr-agent003-cli-model-mutation-audit-v1" or mutation.get("requested_model") != requested_model:
        failures.append("temporary_model_mutation_audit_invalid")
    elif mutation.get("production_artifacts_modified") is not False:
        failures.append("production_artifact_mutation_claim_invalid")

    result.update(
        {
            "status": "PASS" if not failures else "FAIL",
            "failures": sorted(set(failures)),
            "session_id": session_id,
            "requested_model": requested_model,
            "resolved_models": sorted(usage_models),
            "otel_response_models": sorted(response_models),
            "result_premium_requests": result_premium,
            "search_calls": tool["search_calls"],
            "evidence_calls": tool["evidence_calls"],
            "search_evidence_urls": sorted(tool["search_evidence_urls"]),
            "response_urls": sorted(response_urls),
            "markdown_urls": sorted(markdown_urls),
            "maximum_tool_result_bytes": max(tool["result_bytes"], default=0),
            "assistant_response_sha256": _sha256_text(response) if response else None,
        }
    )
    return result


def _candidate_summaries(authority: dict[str, Any], runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    summaries: list[dict[str, Any]] = []
    ranking: list[dict[str, Any]] = []
    for authority_ordinal, model in enumerate(authority["candidate_models"], 1):
        model_runs = [run for run in runs if run.get("case_kind") == "savings" and run.get("candidate_model") == model]
        pass_runs = [run for run in model_runs if run.get("status") == "PASS"]
        unavailable_runs = [run for run in model_runs if run.get("status") in ("UNAVAILABLE_POLICY_PREINFERENCE", "SKIPPED_POLICY_PREINFERENCE", "SKIPPED_NOT_HELP_LISTED")]
        credits = [float(run["ai_credits"]) for run in pass_runs if isinstance(run.get("ai_credits"), (int, float))]
        elapsed = [float(run["elapsed_seconds"]) for run in pass_runs if isinstance(run.get("elapsed_seconds"), (int, float))]
        eligible = len(model_runs) == 3 and len(pass_runs) == 3 and len(credits) == 3 and len(elapsed) == 3
        summary = {
            "authority_ordinal": authority_ordinal,
            "model": model,
            "planned_runs": 3,
            "pass_runs": len(pass_runs),
            "unavailable_or_skipped_runs": len(unavailable_runs),
            "eligible": eligible,
            "mean_ai_credits": statistics.fmean(credits) if eligible else None,
            "median_elapsed_seconds": statistics.median(elapsed) if eligible else None,
            "run_statuses": [run.get("status") for run in model_runs],
        }
        summaries.append(summary)
        if eligible:
            ranking.append(summary.copy())
    ranking.sort(key=lambda item: (item["mean_ai_credits"], item["median_elapsed_seconds"], item["authority_ordinal"]))
    for rank, item in enumerate(ranking, 1):
        item["rank"] = rank
    winner = ranking[0]["model"] if ranking else None
    return summaries, ranking, winner


def _global_run_invariants(
    authority: dict[str, Any], runs: list[dict[str, Any]]
) -> list[str]:
    failures: list[str] = []
    expected_savings_prompt = _sha256_text(authority["savings_case"]["prompt"])
    savings_runs = [run for run in runs if run.get("case_kind") == "savings"]
    if any(run.get("prompt_sha256") != expected_savings_prompt for run in savings_runs):
        failures.append("savings_prompt_hash_not_identical_to_authority")
    session_ids = [
        run.get("session_id")
        for run in runs
        if isinstance(run.get("session_id"), str) and run.get("session_id")
    ]
    if len(session_ids) != len(set(session_ids)):
        failures.append("duplicate_session_id_fresh_session_violation")
    return failures


def collect(authority_path: Path, raw_root: Path) -> dict[str, Any]:
    authority = load_authority(authority_path)
    runs_root = raw_root / "runs"
    if not runs_root.is_dir():
        raise BakeoffError(f"runs directory is missing: {runs_root}")
    run_roots = sorted(
        (path for path in runs_root.iterdir() if path.is_dir() and (path / "run.json").is_file()),
        key=lambda path: int(_load_json(path / "run.json").get("plan_ordinal", 10**9)),
    )
    runs = [evaluate_run(authority, root) for root in run_roots]
    ordinals = [run.get("plan_ordinal") for run in runs]
    failures: list[str] = []
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ordinals) or ordinals != sorted(set(ordinals)):
        failures.append("plan_ordinals_invalid")
    failures.extend(_global_run_invariants(authority, runs))
    credit_unknown = any("credit_unknown" in run.get("failures", []) for run in runs)
    aggregate_nano = sum(int(run.get("total_nano_aiu", 0)) for run in runs if isinstance(run.get("total_nano_aiu"), int))
    aggregate_credits = aggregate_nano / NANO_AIU_PER_CREDIT
    if aggregate_credits > float(authority["aggregate_ai_credit_cap"]):
        failures.append("aggregate_credit_cap_exceeded")
    if credit_unknown:
        failures.append("aggregate_credit_unknown")
    summaries, ranking, winner = _candidate_summaries(authority, runs)
    savings_complete = all(len([run for run in runs if run.get("case_kind") == "savings" and run.get("candidate_model") == model]) == 3 for model in authority["candidate_models"])
    all_unavailable = savings_complete and all(summary["unavailable_or_skipped_runs"] == 3 for summary in summaries)
    auxiliary_status = {
        kind: [run.get("status") for run in runs if run.get("case_kind") == kind]
        for kind in ("standard", "thorough", "boundary")
    }
    auxiliary_has_failure = any(
        status == "FAIL"
        for statuses in auxiliary_status.values()
        for status in statuses
    )
    all_aux_pass = all(auxiliary_status[kind] == ["PASS"] for kind in auxiliary_status)
    if failures:
        overall = "STOP_CREDIT_OR_EVIDENCE"
    elif all_unavailable:
        overall = "STOP_ALL_SAVINGS_CANDIDATES_UNAVAILABLE"
    elif savings_complete and winner is None:
        overall = "STOP_NO_ELIGIBLE_SAVINGS_CANDIDATE"
    elif auxiliary_has_failure:
        overall = "FAIL"
    elif winner is not None and all_aux_pass:
        overall = "PASS"
    else:
        overall = "IN_PROGRESS"
    return {
        "schema_version": REPORT_SCHEMA,
        "authority_path": str(authority_path.resolve()),
        "authority_sha256": prod._sha256_file(authority_path),
        "credit_epoch": authority["credit_epoch"],
        "aggregate_ai_credit_cap": authority["aggregate_ai_credit_cap"],
        "aggregate_total_nano_aiu": aggregate_nano,
        "aggregate_ai_credits": aggregate_credits,
        "credit_observable": not credit_unknown,
        "overall_status": overall,
        "stop_required": overall.startswith("STOP_") or overall == "FAIL",
        "failures": sorted(set(failures)),
        "all_savings_candidates_unavailable": all_unavailable,
        "forbid_auto_fallback": True,
        "candidate_summaries": summaries,
        "ranking_contract": [
            "eligible only when exactly 3/3 runs PASS",
            "ascending mean observed AI Credits",
            "ascending median elapsed seconds",
            "ascending authority ordinal deterministic tie-break",
        ],
        "ranking": ranking,
        "winner": winner,
        "auxiliary_status": auxiliary_status,
        "runs": runs,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def self_test(authority_path: Path) -> int:
    authority = load_authority(authority_path)
    assert authority["candidate_models"] == EXPECTED_CANDIDATES
    sample = (
        "要求は12%、確定済みは7%、issueはopenです。\n\n"
        "## References\n- [元資料](https://github.com/harilos/fizzbuzz-planet-docs/issues/1)"
    )
    failures, response_urls, markdown_urls = _simple_answer_failures(sample, {AUTHORITATIVE_URL})
    if failures:
        raise BakeoffError(f"valid simple-answer fixture failed: {failures}")
    if response_urls != {AUTHORITATIVE_URL}:
        raise BakeoffError(f"response URL parser mismatch: {sorted(response_urls)}")
    if markdown_urls != {AUTHORITATIVE_URL}:
        raise BakeoffError(f"Markdown URL parser mismatch: {sorted(markdown_urls)}")
    bad_failures, _, _ = _simple_answer_failures(sample.replace("12%", "11%"), {AUTHORITATIVE_URL})
    if "requested_12_percent_missing" not in bad_failures:
        raise BakeoffError(f"negative fact fixture was not rejected: {bad_failures}")
    before = {"schema_version": SNAPSHOT_SCHEMA, "copilot_home": "X", "row_count": 9, "total_nano_aiu": 100}
    after = {"schema_version": SNAPSHOT_SCHEMA, "copilot_home": "X", "row_count": 9, "total_nano_aiu": 100}
    snapshot_result = _snapshot_delta(before, after)
    if snapshot_result != (0, 0, []):
        raise BakeoffError(f"zero-usage snapshot mismatch: {snapshot_result}")
    synthetic_runs = []
    for ordinal, model in enumerate(EXPECTED_CANDIDATES, 1):
        for attempt in range(1, 4):
            synthetic_runs.append(
                {
                    "case_kind": "savings",
                    "candidate_model": model,
                    "status": "PASS" if ordinal == 1 else "SKIPPED_NOT_HELP_LISTED",
                    "ai_credits": 1.0,
                    "elapsed_seconds": float(5 + attempt),
                    "prompt_sha256": _sha256_text(authority["savings_case"]["prompt"]),
                    "session_id": f"session-{ordinal}-{attempt}" if ordinal == 1 else None,
                }
            )
    summaries, ranking, winner = _candidate_summaries(authority, synthetic_runs)
    if summaries[0]["eligible"] is not True:
        raise BakeoffError(f"eligibility self-test mismatch: {summaries[0]}")
    if not ranking or ranking[0]["model"] != EXPECTED_CANDIDATES[0]:
        raise BakeoffError(f"ranking self-test mismatch: {ranking}")
    if winner != EXPECTED_CANDIDATES[0]:
        raise BakeoffError(f"winner self-test mismatch: {winner}")
    if _global_run_invariants(authority, synthetic_runs):
        raise BakeoffError("valid global run invariants were rejected")
    duplicate_runs = [dict(value) for value in synthetic_runs]
    duplicate_runs[1]["session_id"] = duplicate_runs[0]["session_id"]
    duplicate_failures = _global_run_invariants(authority, duplicate_runs)
    if "duplicate_session_id_fresh_session_violation" not in duplicate_failures:
        raise BakeoffError(
            f"duplicate session self-test was not rejected: {duplicate_failures}"
        )
    print("PASS: model bakeoff collector self-test")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot-copilot-home", type=Path)
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.snapshot_copilot_home is not None or args.snapshot_output is not None:
            if args.snapshot_copilot_home is None or args.snapshot_output is None:
                raise BakeoffError("snapshot mode requires both snapshot arguments")
            _write_json(args.snapshot_output, snapshot_session_store(args.snapshot_copilot_home))
            return 0
        if args.authority is None:
            raise BakeoffError("--authority is required")
        if args.self_test:
            return self_test(args.authority)
        if args.raw_root is None or args.output is None:
            raise BakeoffError("collection requires --raw-root and --output")
        report = collect(args.authority, args.raw_root)
        _write_json(args.output, report)
        return 0 if report["overall_status"] not in ("STOP_CREDIT_OR_EVIDENCE", "FAIL") else 2
    except (BakeoffError, prod.EvidenceError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
