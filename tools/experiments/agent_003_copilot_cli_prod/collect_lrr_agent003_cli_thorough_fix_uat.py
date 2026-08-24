#!/usr/bin/env python3
"""Collect the single formal Agent003 thorough-fix UAT.

This collector intentionally has its own one-case schema.  It reuses the
already-audited event, URL, OTEL, launcher, session-store, and omission helpers
from the production/model-bakeoff collectors, but does not inherit their
multi-case or ordinal assumptions.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import collect_lrr_agent003_cli_model_bakeoff as bakeoff  # noqa: E402
import collect_lrr_agent003_cli_prod_uat as prod  # noqa: E402


AUTHORITY_SCHEMA = "lrr-agent003-cli-thorough-fix-uat-authority-v1"
RUN_SCHEMA = "lrr-agent003-cli-thorough-fix-uat-run-v1"
REPORT_SCHEMA = "lrr-agent003-cli-thorough-fix-uat-report-v1"
EXPECTED_CASE_ID = "LRR-AGENT003-CLI-THOROUGH-FIX-1"
EXPECTED_PROMPT = (
    "フィズバス惑星で、ダム族の集落周辺に調査拠点を設ける可否を検討しています。"
    "集落の場所、接触時に守るべき規則、保護区予算増額案の増額率・内容・現在の扱い、"
    "判断に不足する情報を、確定事項／提案段階／未確認に分けて整理してください。"
    "資料同士に直接の関係が示されているかと、食い違いの有無も確認し、"
    "根拠はクリックできる元資料URLで示してください。"
)
EXPECTED_BASELINE_SHA256 = (
    "07be1ac03414e7b832b84ff8f1bb5aeef2382d3f57f9dd339e2f21b3347f229c"
)
EXPECTED_BASELINE_NANO = 38_075_996_000
EXPECTED_TOTAL_CAP_NANO = 50_000_000_000
EXPECTED_REMAINING_NANO = 11_924_004_000
NANO_AIU_PER_CREDIT = 1_000_000_000

FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$")
INLINE_CODE_RE = re.compile(r"(?s)(?<!`)`{1,2}.*?`{1,2}(?!`)")
PERCENT_ENCODED_RE = re.compile(r"(?i)(?:%[0-9a-f]{2})+")
PERCENT_12_RE = re.compile(r"(?<![\d.])12\s*(?:%|％)(?!\d)")
PERCENT_7_RE = re.compile(r"(?<![\d.])7\s*(?:%|％)(?!\d)")
SECTION_RE = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?(?:[-*+][ \t]+)?"
    r"(確定事項|提案段階|未確認)[ \t]*(?:[:：][ \t]*|$)"
)
PROHIBITION_RE = re.compile(
    r"禁止|不可|認め(?:ない|られない)|許可しない|行わない|実施しない"
)


class CollectionError(ValueError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CollectionError(f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CollectionError(f"unreadable JSONL file {path}: {exc}") from exc
    result: list[dict[str, Any]] = []
    for ordinal, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CollectionError(f"invalid JSONL {path}:{ordinal}: {exc}") from exc
        if not isinstance(value, dict):
            raise CollectionError(f"JSON object required at {path}:{ordinal}")
        result.append(value)
    if not result:
        raise CollectionError(f"empty JSONL evidence: {path}")
    return result


def _require_exact_int(value: Any, expected: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise CollectionError(f"{name} must be exactly {expected}")


def load_authority(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        authority = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid authority: {exc}") from exc
    if not isinstance(authority, dict):
        raise CollectionError("authority must be an object")
    if authority.get("schema_version") != AUTHORITY_SCHEMA:
        raise CollectionError("authority schema mismatch")
    _require_exact_int(authority.get("formal_session_count"), 1, "formal_session_count")
    _require_exact_int(authority.get("retry_count"), 0, "retry_count")
    case = authority.get("case")
    if not isinstance(case, dict):
        raise CollectionError("authority case missing")
    exact_case = {
        "id": EXPECTED_CASE_ID,
        "tier": "thorough",
        "requested_model": "auto",
        "expected_agent": "local-rag-agent003-thorough",
        "prompt": EXPECTED_PROMPT,
        "expected_database": "fizzbuzz-planet-rag",
    }
    for key, expected in exact_case.items():
        if case.get(key) != expected:
            raise CollectionError(f"canonical case mismatch: {key}")
    if case.get("required_sections") != ["確定事項", "提案段階", "未確認"]:
        raise CollectionError("required section authority mismatch")
    tools = case.get("tool_contract")
    if not isinstance(tools, dict):
        raise CollectionError("tool contract missing")
    for key, expected in {
        "expected_routing_search_calls": 1,
        "minimum_selected_database_search_calls": 3,
        "maximum_evidence_calls": 2,
        "maximum_total_tool_calls": 7,
    }.items():
        _require_exact_int(tools.get(key), expected, key)
    if tools.get("omitted_evidence_relevance_terms") != ["予算", "増額", "確定"]:
        raise CollectionError("omitted evidence relevance authority mismatch")
    if tools.get("narrow_follow_up_query_terms") != ["確定", "執行", "7%"]:
        raise CollectionError("narrow follow-up query authority mismatch")
    for key in (
        "forbid_duplicate_selected_database_queries",
        "require_omitted_inspectable_evidence_follow_up",
        "forbid_foreign_tools",
        "forbid_ask_user",
        "forbid_permission_prompt",
    ):
        if tools.get(key) is not True:
            raise CollectionError(f"required tool gate missing: {key}")
    if set(tools.get("allowed_tools") or []) != set(prod.ALLOWED_RUNTIME_TOOLS):
        raise CollectionError("allowed tool boundary mismatch")
    credit = authority.get("credit_authority")
    if not isinstance(credit, dict):
        raise CollectionError("credit authority missing")
    if "baseline_report_path" in credit:
        raise CollectionError("tracked authority must not contain a local baseline path")
    if credit.get("baseline_report_sha256") != EXPECTED_BASELINE_SHA256:
        raise CollectionError("baseline report SHA mismatch")
    for key, expected in {
        "baseline_total_nano_aiu": EXPECTED_BASELINE_NANO,
        "maximum_total_nano_aiu": EXPECTED_TOTAL_CAP_NANO,
        "maximum_additional_nano_aiu": EXPECTED_REMAINING_NANO,
        "cli_max_ai_credits": 30,
    }.items():
        _require_exact_int(credit.get(key), expected, key)
    if credit.get("cli_floor_is_not_spend_authority") is not True:
        raise CollectionError("CLI floor disclaimer missing")
    execution = authority.get("execution_contract")
    if not isinstance(execution, dict):
        raise CollectionError("execution contract missing")
    for key in (
        "fresh_copilot_home",
        "fresh_workspace",
        "fresh_session",
        "no_ask_user",
        "no_remote",
        "no_remote_export",
        "no_auto_update",
    ):
        if execution.get(key) is not True:
            raise CollectionError(f"execution gate missing: {key}")
    _require_exact_int(execution.get("formal_prompt_send_count"), 1, "formal_prompt_send_count")
    _require_exact_int(execution.get("retry_count"), 0, "execution retry_count")
    if execution.get("reuse_session") is not False:
        raise CollectionError("session reuse must be forbidden")
    return authority, _sha256_bytes(raw)


def visible_answer_prose(response: str) -> str:
    """Remove non-prose locations before checking answer facts.

    Markdown destinations/bare URLs are removed by the audited bakeoff helper.
    Fenced code, inline code, and percent-encoded byte sequences are removed here.
    Consequently ``7%`` in prose counts, while ``%37``, ``7%25``, a URL, or code
    cannot satisfy a fact assertion.
    """

    prose = bakeoff._answer_prose_without_link_destinations(response)
    prose = FENCE_RE.sub("\n", prose)
    prose = INLINE_CODE_RE.sub("", prose)
    prose = PERCENT_ENCODED_RE.sub("", prose)
    return prose


def _visible_response_urls(response: str) -> tuple[set[str], set[str]]:
    without_code = bakeoff._without_markdown_code(response)
    return (
        prod._response_https_urls(without_code),
        prod._markdown_source_urls(without_code),
    )


def _section_bodies(prose: str) -> tuple[dict[str, str], list[str]]:
    matches = list(SECTION_RE.finditer(prose))
    bodies: dict[str, str] = {}
    failures: list[str] = []
    for index, match in enumerate(matches):
        name = match.group(1)
        if name in bodies:
            failures.append(f"section_duplicate:{name}")
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prose)
        bodies[name] = prose[match.end() : end]
    for name in ("確定事項", "提案段階", "未確認"):
        if not bodies.get(name, "").strip():
            failures.append(f"section_missing_or_empty:{name}")
    return bodies, failures


def _term_has_prohibition(prose: str, alternatives: Iterable[str]) -> bool:
    for term in alternatives:
        for match in re.finditer(re.escape(term), prose, re.IGNORECASE):
            # A disposition in some later bullet must not accidentally qualify a
            # bare mention.  Limit the check to the containing sentence/line.
            left_boundaries = [
                prose.rfind(marker, 0, match.start()) for marker in ("。", "！", "？", "\n")
            ]
            left = max(left_boundaries) + 1
            right_candidates = [
                position
                for marker in ("。", "！", "？", "\n")
                if (position := prose.find(marker, match.end())) >= 0
            ]
            right = min(right_candidates) if right_candidates else len(prose)
            clause = prose[left:right]
            negated = re.search(
                r"禁止(?:では|じゃ)?ない|不可(?:では|じゃ)?ない|"
                r"認めないわけではない|禁止.{0,8}(?:解除|撤回)|許可(?:する|される)",
                clause,
            )
            if negated is None and PROHIBITION_RE.search(clause):
                return True
    return False


def _affirmed_term_in_clause(
    prose: str,
    alternatives: Iterable[str],
    *,
    required_context: Iterable[str] = (),
    contradiction: str = r"(?:では|には|で|に|は)?ない|ではなく|でなく|存在しない|所在しない|位置しない",
) -> bool:
    for term in alternatives:
        for match in re.finditer(re.escape(term), prose, re.IGNORECASE):
            left = max(prose.rfind(marker, 0, match.start()) for marker in ("。", "！", "？", "\n")) + 1
            right_positions = [
                position
                for marker in ("。", "！", "？", "\n")
                if (position := prose.find(marker, match.end())) >= 0
            ]
            right = min(right_positions) if right_positions else len(prose)
            clause = prose[left:right]
            if required_context and not any(item in clause for item in required_context):
                continue
            if re.search(contradiction, clause, re.IGNORECASE) is None:
                return True
    return False


def score_response(case: dict[str, Any], response: str) -> dict[str, Any]:
    failures: list[str] = []
    prose = visible_answer_prose(response)
    sections, section_failures = _section_bodies(prose)
    failures.extend(section_failures)

    confirmed = sections.get("確定事項", "")
    proposed = sections.get("提案段階", "")
    if PERCENT_7_RE.search(confirmed) is None:
        failures.append("confirmed_7_percent_missing_from_confirmed_section")
    if PERCENT_12_RE.search(proposed) is None:
        failures.append("requested_12_percent_missing_from_proposed_section")
    if PERCENT_12_RE.search(confirmed) is not None:
        failures.append("requested_12_percent_misclassified_as_confirmed")
    if PERCENT_7_RE.search(proposed) is not None:
        failures.append("confirmed_7_percent_misclassified_as_proposed")

    facts = case["required_facts"]
    if not _affirmed_term_in_clause(
        confirmed,
        facts["settlement_location"],
        required_context=("集落", "ダム族"),
    ):
        failures.append("settlement_location_missing")
    if not _affirmed_term_in_clause(
        confirmed,
        facts["contact_rule"],
        contradiction=r"ではない|ではなく|解除|許可(?:する|される)",
    ):
        failures.append("contact_rule_missing")
    if not _term_has_prohibition(confirmed, facts["technology_provision"]):
        failures.append("technology_provision_prohibition_missing")
    if not _term_has_prohibition(confirmed, facts["resource_extraction"]):
        failures.append("resource_extraction_prohibition_missing")
    if not _affirmed_term_in_clause(
        proposed,
        facts["issue_state"],
        contradiction=r"ではない|ではなく|でなく|解決済み|closed|クローズ",
    ):
        failures.append("issue_current_state_missing")

    relationship_ok = re.search(
        r"(?:直接(?:的な)?(?:の)?関係).{0,48}"
        r"(?:示されていない|示されていません|明示されていない|明示されていません|"
        r"確認できない|確認できません|確認されない|確認されません|関係なし|不明)",
        prose,
        re.DOTALL,
    ) is not None
    if not relationship_ok:
        failures.append("direct_relationship_disposition_missing")
    discrepancy_ok = re.search(
        r"(?:食い違い|相違|差異|矛盾)(?:が|は|を)?"
        r"(?:ある|あります|認められる|確認できる|確認された|生じている|存在する|不一致)",
        prose,
    )
    if discrepancy_ok is None or PERCENT_12_RE.search(prose) is None or PERCENT_7_RE.search(prose) is None:
        failures.append("discrepancy_disposition_missing")

    seven_match = PERCENT_7_RE.search(prose)
    seven_location = None
    if seven_match is not None:
        seven_location = {
            "character_offset": seven_match.start(),
            "line_number": prose.count("\n", 0, seven_match.start()) + 1,
            "section": "確定事項" if PERCENT_7_RE.search(confirmed) else None,
        }
    return {
        "failures": sorted(set(failures)),
        "visible_prose_sha256": _sha256_text(prose),
        "visible_prose_bytes": len(prose.encode("utf-8")),
        "seven_percent_location": seven_location,
        "section_names": sorted(sections),
    }


def _is_narrow_confirmed_budget_query(value: str) -> bool:
    normalized = bakeoff._normalized_search_query(value)
    if PERCENT_7_RE.search(normalized):
        return True
    return (
        any(term in normalized for term in ("確定", "執行", "決定済", "実施済"))
        and any(term in normalized for term in ("予算", "増額", "増額率"))
    )


def _strict_omission_follow_up(evidence: dict[str, Any]) -> dict[str, bool]:
    starts = evidence["starts"]
    arguments = evidence["start_arguments"]
    order = evidence["call_order"]
    positions = {call_id: index for index, call_id in enumerate(order)}
    result: dict[str, bool] = {}
    for call_id, omitted_values in evidence["omitted_inspectable_ids_by_call"].items():
        omitted = set(omitted_values)
        source_args = arguments.get(call_id, {})
        source_db = str(source_args.get("database") or "").strip()
        source_query = bakeoff._normalized_search_query(source_args.get("question"))
        satisfied = False
        for later_id in order[positions[call_id] + 1 :]:
            later_name = starts.get(later_id)
            later_args = arguments.get(later_id, {})
            if later_name == prod.EVIDENCE_TOOL:
                requested = {
                    str(item)
                    for item in later_args.get("evidence_ids") or []
                    if isinstance(item, str) and item
                }
                if omitted & requested:
                    satisfied = True
                    break
            elif later_name == prod.SEARCH_TOOL:
                later_db = str(later_args.get("database") or "").strip()
                later_query = bakeoff._normalized_search_query(later_args.get("question"))
                if (
                    later_db == source_db
                    and later_query
                    and later_query != source_query
                    and _is_narrow_confirmed_budget_query(later_query)
                ):
                    satisfied = True
                    break
        result[call_id] = satisfied
    return result


def score_tools(case: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    contract = case["tool_contract"]
    evidence = bakeoff._event_tool_evidence(
        events,
        omitted_relevance_terms=tuple(
            str(item) for item in contract["omitted_evidence_relevance_terms"]
        ),
        narrow_follow_up_query_terms=tuple(
            str(item) for item in contract["narrow_follow_up_query_terms"]
        ),
    )
    failures = list(evidence["failures"])
    strict_follow_up = _strict_omission_follow_up(evidence)
    if evidence["routing_search_calls"] != contract["expected_routing_search_calls"]:
        failures.append("routing_search_call_count_mismatch")
    if evidence["selected_database_search_calls"] < contract["minimum_selected_database_search_calls"]:
        failures.append("selected_database_search_call_count_below_minimum")
    if evidence["evidence_calls"] > contract["maximum_evidence_calls"]:
        failures.append("evidence_call_count_above_maximum")
    if evidence["total_tool_calls"] > contract["maximum_total_tool_calls"]:
        failures.append("total_tool_call_count_above_maximum")
    if any(value != case["expected_database"] for value in evidence["selected_databases"]):
        failures.append("selected_database_mismatch")
    if evidence["duplicate_selected_database_queries"]:
        failures.append("duplicate_selected_database_query")
    if any(value is False for value in strict_follow_up.values()):
        failures.append("omitted_inspectable_evidence_not_followed_up")
    for call_id, name in evidence["starts"].items():
        if name != prod.EVIDENCE_TOOL:
            continue
        arguments = evidence["start_arguments"].get(call_id, {})
        ids = arguments.get("evidence_ids")
        if (
            not isinstance(arguments.get("result_token"), str)
            or not arguments["result_token"].strip()
            or not isinstance(ids, list)
            or not 1 <= len(ids) <= 3
        ):
            failures.append("evidence_detail_arguments_invalid")
    if bakeoff._interaction_observed(events):
        failures.append("permission_or_user_input_event_observed")
    for event in events:
        event_type = prod._event_type(event)
        data = prod._event_data(event)
        if event_type == "tool.execution_start":
            name = prod._tool_name(data)
            if re.search(r"ask[_-]?user", name, re.IGNORECASE):
                failures.append("ask_user_tool_observed")
    return {
        "failures": sorted(set(failures)),
        "routing_search_calls": evidence["routing_search_calls"],
        "selected_database_search_calls": evidence["selected_database_search_calls"],
        "selected_databases": evidence["selected_databases"],
        "selected_database_queries": evidence["selected_database_queries"],
        "duplicate_selected_database_queries": evidence["duplicate_selected_database_queries"],
        "evidence_calls": evidence["evidence_calls"],
        "total_tool_calls": evidence["total_tool_calls"],
        "omitted_inspectable_ids_by_call": evidence["omitted_inspectable_ids_by_call"],
        "omitted_inspectable_follow_up": strict_follow_up,
        "all_evidence_urls": sorted(evidence["all_evidence_urls"]),
        "search_evidence_urls": sorted(evidence["search_evidence_urls"]),
        "maximum_tool_result_bytes": max(evidence["result_bytes"], default=0),
    }


def _baseline_total_from_report(report: dict[str, Any]) -> int | None:
    direct = report.get("true_total_nano_aiu")
    if isinstance(direct, int) and not isinstance(direct, bool):
        return direct
    value = report.get("true_total_ai_credits")
    try:
        parsed = Decimal(str(value)) * NANO_AIU_PER_CREDIT
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _event_models_and_usage(
    run: dict[str, Any], events: list[dict[str, Any]], otel: list[dict[str, Any]], copilot_home: Path | None
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    session_id, premium, result_failures = prod._result_contract(events, run.get("exit_code"))
    failures.extend(result_failures)
    usage: dict[str, Any] = {
        "row_count": 0,
        "models": [],
        "total_nano_aiu": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    if copilot_home is None or session_id is None:
        failures.append("session_usage_unavailable")
    else:
        usage, usage_failures = prod._read_session_usage(copilot_home, session_id)
        failures.extend(usage_failures)
    otel_evidence = prod._otel_evidence(otel)
    usage_models = set(usage.get("models") or [])
    response_models = set(otel_evidence["response_models"])
    if not usage_models:
        failures.append("resolved_model_missing")
    if "auto" in usage_models or "auto" in response_models:
        failures.append("auto_not_resolved")
    if not response_models:
        failures.append("otel_response_model_missing")
    elif usage_models != response_models:
        failures.append("session_otel_response_model_mismatch")
    expected_agent = "local-rag-agent003-thorough"
    otel_agents = set(otel_evidence["agent_ids"]) | set(otel_evidence["agent_names"])
    if expected_agent not in otel_agents:
        failures.append("otel_expected_agent_missing")
    if otel_evidence["invalid_tokens"]:
        failures.append("otel_token_evidence_invalid")
    if (
        usage.get("input_tokens") != otel_evidence["input_tokens"]
        or usage.get("output_tokens") != otel_evidence["output_tokens"]
    ):
        failures.append("session_otel_token_mismatch")
    explicit_fallback = any(
        _explicit_fallback_observed(event) for event in events + otel
    )
    return {
        "session_id": session_id,
        "result_premium_requests": premium,
        "resolved_models": sorted(usage_models),
        "otel_requested_models": sorted(otel_evidence["requested_models"]),
        "otel_response_models": sorted(response_models),
        "session_store_total_nano_aiu": usage.get("total_nano_aiu"),
        "session_store_input_tokens": usage.get("input_tokens"),
        "session_store_output_tokens": usage.get("output_tokens"),
        "fallback_observed": explicit_fallback,
        "fallback_directly_observable": explicit_fallback,
    }, sorted(set(failures))


def _explicit_fallback_observed(value: Any, *, fallback_context: bool = False) -> bool:
    """Return true only for an explicit, positive fallback signal.

    Field names such as ``fallback: false`` or ``fallback_reason: null`` are
    common telemetry schema, not evidence that a fallback occurred.  Concrete
    auto resolution is likewise not treated as fallback.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            key_context = fallback_context or "fallback" in str(key).casefold()
            if _explicit_fallback_observed(item, fallback_context=key_context):
                return True
        return False
    if isinstance(value, list):
        return any(
            _explicit_fallback_observed(item, fallback_context=fallback_context)
            for item in value
        )
    if not fallback_context or value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        return normalized not in {"", "0", "false", "no", "none", "null", "not_observed"}
    return False


def _user_prompt_evidence(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    contents = [
        prod._event_data(event).get("content")
        for event in events
        if prod._event_type(event) == "user.message"
    ]
    failures: list[str] = []
    if len(contents) != 1:
        failures.append("user_message_count_not_exactly_one")
    if contents != [EXPECTED_PROMPT]:
        failures.append("user_message_content_not_exact_prompt")
    return {
        "user_message_count": len(contents),
        "user_message_content_sha256": (
            _sha256_text(contents[0]) if len(contents) == 1 and isinstance(contents[0], str) else None
        ),
    }, failures


def collect(authority_path: Path, raw_root: Path) -> dict[str, Any]:
    authority, authority_sha = load_authority(authority_path)
    case = authority["case"]
    run_root = raw_root / "run-001"
    if not run_root.is_dir() or any((raw_root / name).exists() for name in ("run-002", "run-003")):
        raise CollectionError("exactly run-001 must exist")
    run = _load_json(run_root / "run.json")
    events = _load_jsonl(run_root / "copilot.jsonl")
    otel = _load_jsonl(run_root / "otel.jsonl")
    failures: list[str] = []
    if run.get("schema_version") != RUN_SCHEMA:
        failures.append("run_schema_mismatch")
    exact_run_values = {
        "case_id": EXPECTED_CASE_ID,
        "authority_sha256": authority_sha,
        "tier": "thorough",
        "requested_model": "auto",
        "prompt_sha256": _sha256_text(EXPECTED_PROMPT),
        "formal_prompt_send_count": 1,
        "retry_count": 0,
        "fresh_session": True,
        "fresh_copilot_home": True,
        "fresh_workspace": True,
        "max_ai_credits": 30,
        "logical_remaining_nano_aiu": EXPECTED_REMAINING_NANO,
        "baseline_total_nano_aiu": EXPECTED_BASELINE_NANO,
        "maximum_total_nano_aiu": EXPECTED_TOTAL_CAP_NANO,
    }
    for key, expected in exact_run_values.items():
        if run.get(key) != expected:
            failures.append(f"run_contract_mismatch:{key}")
    if run.get("timed_out") is not False or run.get("process_tree_terminated") is not False:
        failures.append("timeout_or_process_tree_termination_observed")
    if run.get("exit_code") != 0:
        failures.append("cli_exit_nonzero")

    prompt_evidence, prompt_failures = _user_prompt_evidence(events)
    failures.extend(prompt_failures)

    baseline_path_value = run.get("baseline_report_path")
    baseline_path = Path(baseline_path_value) if isinstance(baseline_path_value, str) else Path()
    if not isinstance(baseline_path_value, str) or not baseline_path.is_absolute():
        failures.append("baseline_report_path_not_absolute_runtime_evidence")
    try:
        baseline_hash = _sha256_file(baseline_path)
        baseline_report = _load_json(baseline_path)
    except (OSError, CollectionError) as exc:
        failures.append(f"baseline_report_unreadable:{type(exc).__name__}")
        baseline_hash = None
        baseline_report = {}
    if baseline_hash != EXPECTED_BASELINE_SHA256:
        failures.append("baseline_report_hash_mismatch")
    if _baseline_total_from_report(baseline_report) != EXPECTED_BASELINE_NANO:
        failures.append("baseline_report_total_mismatch")

    before = _load_json(run_root / "usage-before.json")
    after = _load_json(run_root / "usage-after.json")
    row_delta, nano_delta, snapshot_failures = bakeoff._snapshot_delta(before, after)
    failures.extend(snapshot_failures)
    if before.get("row_count") != 0 or before.get("total_nano_aiu") != 0:
        failures.append("copilot_home_not_fresh")
    if row_delta is None or row_delta < 1:
        failures.append("fresh_session_usage_row_missing")

    copilot_home, launcher_evidence, manifest_failures = prod._validate_launcher_manifest(run)
    failures.extend(manifest_failures)
    cli_evidence, cli_failures = prod._validate_cli_identity(run)
    failures.extend(cli_failures)

    response = prod._assistant_response(events)
    if not response:
        failures.append("final_assistant_response_missing")
    response_score = score_response(case, response)
    failures.extend(response_score["failures"])
    tool_score = score_tools(case, events)
    failures.extend(tool_score["failures"])

    response_urls, markdown_urls = _visible_response_urls(response)
    tool_urls = set(tool_score["all_evidence_urls"])
    minimum_urls = case["url_contract"]["minimum_clickable_markdown_urls"]
    if len(markdown_urls) < minimum_urls:
        failures.append("clickable_markdown_url_count_below_minimum")
    if not response_urls.issubset(tool_urls):
        failures.append("response_url_not_from_tool_evidence")

    model_evidence, model_failures = _event_models_and_usage(
        run, events, otel, copilot_home
    )
    failures.extend(model_failures)
    session_nano = model_evidence["session_store_total_nano_aiu"]
    if not isinstance(nano_delta, int) or not isinstance(session_nano, int):
        failures.append("actual_credit_not_observable")
    else:
        if nano_delta != session_nano:
            failures.append("snapshot_session_credit_mismatch")
        if nano_delta > EXPECTED_REMAINING_NANO:
            failures.append("logical_remaining_credit_exceeded")
        if EXPECTED_BASELINE_NANO + nano_delta > EXPECTED_TOTAL_CAP_NANO:
            failures.append("aggregate_credit_cap_exceeded")

    report = {
        "schema_version": REPORT_SCHEMA,
        "status": "PASS" if not failures else "FAIL",
        "failures": sorted(set(failures)),
        "authority_path": str(authority_path.resolve()),
        "authority_sha256": authority_sha,
        "case_id": EXPECTED_CASE_ID,
        "formal_session_count": 1,
        "retry_count": 0,
        **prompt_evidence,
        "requested_model": "auto",
        **model_evidence,
        "routing_search_calls": tool_score["routing_search_calls"],
        "selected_database_search_calls": tool_score["selected_database_search_calls"],
        "selected_databases": tool_score["selected_databases"],
        "selected_database_queries": tool_score["selected_database_queries"],
        "evidence_calls": tool_score["evidence_calls"],
        "total_tool_calls": tool_score["total_tool_calls"],
        "omitted_inspectable_ids_by_call": tool_score["omitted_inspectable_ids_by_call"],
        "omitted_inspectable_follow_up": tool_score["omitted_inspectable_follow_up"],
        "seven_percent_location": response_score["seven_percent_location"],
        "response_sha256": _sha256_text(response) if response else None,
        "response_markdown_url_count": len(markdown_urls),
        "response_markdown_urls": sorted(markdown_urls),
        "response_url_count": len(response_urls),
        "tool_evidence_urls": sorted(tool_urls),
        "url_validation_passed": response_urls.issubset(tool_urls),
        "baseline_report_sha256": baseline_hash,
        "baseline_total_nano_aiu": EXPECTED_BASELINE_NANO,
        "actual_nano_aiu": nano_delta,
        "actual_ai_credits": (
            nano_delta / NANO_AIU_PER_CREDIT if isinstance(nano_delta, int) else None
        ),
        "remaining_authority_nano_aiu": EXPECTED_REMAINING_NANO,
        "aggregate_nano_aiu": (
            EXPECTED_BASELINE_NANO + nano_delta if isinstance(nano_delta, int) else None
        ),
        "maximum_total_nano_aiu": EXPECTED_TOTAL_CAP_NANO,
        "cli_max_ai_credits": 30,
        "cli_floor_is_not_spend_authority": True,
        "launcher_identity": launcher_evidence,
        "cli_identity": cli_evidence,
        "approval_prompt_observed": bakeoff._interaction_observed(events),
        "foreign_tool_observed": any(
            item.startswith("foreign_tool:") for item in tool_score["failures"]
        ),
        "residuals": [
            "auto_to_concrete_resolution_is_not_counted_as_fallback",
            "fallback_is_reported_only_when_explicitly_observable_in_events_or_otel",
            "cli_30_credit_floor_is_a_soft_cli_setting;_11.924004_is_postchecked_from_session_store",
        ],
    }
    return report


def _tool_pair(
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    structured: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool.execution_start",
            "data": {"toolCallId": call_id, "toolName": name, "arguments": arguments},
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": call_id,
                "success": True,
                "result": {
                    "content": json.dumps(structured, ensure_ascii=False),
                    "structuredContent": structured,
                },
            },
        },
    ]


def _synthetic_events(*, follow_up: bool = True, duplicate: bool = False) -> list[dict[str, Any]]:
    packet = {
        "evidence": [
            {
                "id": "E1",
                "text": "予算の執行確定値に関するIssue抜粋…",
                "url": "https://example.test/issues/1",
            }
        ],
        "inspectable_evidence_ids": ["E1"],
    }
    events: list[dict[str, Any]] = []
    events += _tool_pair("r", prod.SEARCH_TOOL, {"question": "route"}, {"databases": ["fizzbuzz-planet-rag"]})
    events += _tool_pair("s1", prod.SEARCH_TOOL, {"database": "fizzbuzz-planet-rag", "question": "集落と接触規則"}, {"evidence": [{"id": "E0", "text": "衛星バズ", "url": "https://example.test/issues/0"}]})
    second_query = "集落と接触規則" if duplicate else "予算要求12%"
    events += _tool_pair("s2", prod.SEARCH_TOOL, {"database": "fizzbuzz-planet-rag", "question": second_query}, {"evidence": [{"id": "E2", "text": "12%", "url": "https://example.test/issues/2"}]})
    events += _tool_pair("s3", prod.SEARCH_TOOL, {"database": "fizzbuzz-planet-rag", "question": "執行確定7%"}, packet)
    if follow_up:
        events += _tool_pair(
            "e1",
            prod.EVIDENCE_TOOL,
            {"result_token": "synthetic-token", "evidence_ids": ["E1"]},
            {"evidence": [{"id": "E1", "text": "全文", "url": "https://example.test/issues/1"}]},
        )
    return events


def _valid_response() -> str:
    return """# 確定事項
- 集落は衛星バズにある。接触規則は非接触（直接接触禁止）。
- 技術供与は禁止として扱い、資源採掘も禁止として扱う。
- 執行確定値は7%。[根拠1](https://example.test/issues/1)

# 提案段階
- 保護区予算の要求増額率は12%で、Issueはopen（未解決）。[根拠2](https://example.test/issues/2)

# 未確認
- 調査拠点の具体位置は未確認。
- 資料間の直接の関係は明示されていない。要求12%と確定7%には食い違いがある。
"""


def _expect_failure(result: dict[str, Any], token: str, name: str) -> None:
    if not any(token in item for item in result["failures"]):
        raise CollectionError(f"self-test failed ({name}): {result['failures']}")


def self_test(authority_path: Path) -> int:
    authority, _ = load_authority(authority_path)
    case = authority["case"]
    valid = score_response(case, _valid_response())
    if valid["failures"]:
        raise CollectionError(f"valid response self-test failed: {valid['failures']}")
    tools_valid = score_tools(case, _synthetic_events())
    if tools_valid["failures"]:
        raise CollectionError(f"valid tool sequence self-test failed: {tools_valid['failures']}")

    for name, injected in (
        ("markdown destination", "[hidden](https://example.test/7%25)"),
        ("fenced code", "\n```text\n7%\n```\n"),
        ("inline code", "`7%`"),
        ("standalone percent encoding", "%37%25"),
    ):
        value = _valid_response().replace("執行確定値は7%。", f"執行確定値は未記載。{injected}")
        _expect_failure(score_response(case, value), "confirmed_7_percent_missing", name)

    swapped = _valid_response().replace("執行確定値は7%", "要求値は12%").replace(
        "要求増額率は12%", "執行確定値は7%"
    )
    swapped_result = score_response(case, swapped)
    _expect_failure(swapped_result, "misclassified_as_confirmed", "12/7 swapped")
    _expect_failure(swapped_result, "misclassified_as_proposed", "12/7 swapped")

    no_treatment = _valid_response().replace(
        "技術供与は禁止として扱い、資源採掘も禁止として扱う。",
        "技術供与と資源採掘という語が資料にある。",
    )
    treatment = score_response(case, no_treatment)
    _expect_failure(treatment, "technology_provision_prohibition_missing", "technology treatment")
    _expect_failure(treatment, "resource_extraction_prohibition_missing", "mining treatment")
    wrong_polarity = _valid_response().replace(
        "技術供与は禁止として扱い、資源採掘も禁止として扱う。",
        "技術供与を許可し、資源採掘も許可する。",
    )
    polarity = score_response(case, wrong_polarity)
    _expect_failure(polarity, "technology_provision_prohibition_missing", "technology polarity")
    _expect_failure(polarity, "resource_extraction_prohibition_missing", "mining polarity")
    negated_prohibition = _valid_response().replace(
        "技術供与は禁止として扱い、資源採掘も禁止として扱う。",
        "技術供与は禁止ではない。資源採掘も禁止ではない。",
    )
    negated_prohibition_result = score_response(case, negated_prohibition)
    _expect_failure(
        negated_prohibition_result,
        "technology_provision_prohibition_missing",
        "negated technology prohibition",
    )
    _expect_failure(
        negated_prohibition_result,
        "resource_extraction_prohibition_missing",
        "negated mining prohibition",
    )
    negated_core_facts = (
        _valid_response()
        .replace("集落は衛星バズにある。", "集落は衛星バズにはない。")
        .replace("接触規則は非接触（直接接触禁止）。", "接触規則は非接触ではなく、直接接触禁止ではない。")
        .replace("Issueはopen（未解決）。", "Issueはopenではなく解決済み。")
    )
    negated_facts = score_response(case, negated_core_facts)
    for token in (
        "settlement_location_missing",
        "contact_rule_missing",
        "issue_current_state_missing",
    ):
        _expect_failure(negated_facts, token, "negated core fact")

    no_relation = _valid_response().replace(
        "資料間の直接の関係は明示されていない。要求12%と確定7%には食い違いがある。",
        "資料を確認した。要求12%と確定7%を記載する。",
    )
    relation = score_response(case, no_relation)
    _expect_failure(relation, "direct_relationship_disposition_missing", "direct relationship")
    _expect_failure(relation, "discrepancy_disposition_missing", "discrepancy")
    neutral_discrepancy = _valid_response().replace(
        "要求12%と確定7%には食い違いがある。",
        "要求12%と確定7%の食い違いについて記載する。",
    )
    _expect_failure(
        score_response(case, neutral_discrepancy),
        "discrepancy_disposition_missing",
        "discrepancy without disposition",
    )
    wrong_relation_polarity = (
        _valid_response()
        .replace("資料間の直接の関係は明示されていない。", "資料間の直接の関係がある。")
        .replace("要求12%と確定7%には食い違いがある。", "要求12%と確定7%に食い違いはない。")
    )
    relation_polarity = score_response(case, wrong_relation_polarity)
    _expect_failure(
        relation_polarity,
        "direct_relationship_disposition_missing",
        "direct relationship polarity",
    )
    _expect_failure(
        relation_polarity,
        "discrepancy_disposition_missing",
        "discrepancy polarity",
    )

    omitted = score_tools(case, _synthetic_events(follow_up=False))
    _expect_failure(omitted, "omitted_inspectable_evidence_not_followed_up", "omitted follow-up")
    narrow_follow_up_events = _synthetic_events(follow_up=False) + _tool_pair(
        "s4",
        prod.SEARCH_TOOL,
        {"database": "fizzbuzz-planet-rag", "question": "Issue 1 執行済み増額率だけ"},
        {
            "evidence": [
                {
                    "id": "E4",
                    "text": "執行確定値7%",
                    "url": "https://example.test/issues/4",
                }
            ]
        },
    )
    narrow_follow_up = score_tools(case, narrow_follow_up_events)
    if narrow_follow_up["failures"]:
        raise CollectionError(
            f"narrow-search omission follow-up self-test failed: {narrow_follow_up['failures']}"
        )
    unrelated_follow_up_events = _synthetic_events(follow_up=False) + _tool_pair(
        "s4",
        prod.SEARCH_TOOL,
        {"database": "fizzbuzz-planet-rag", "question": "予算資料との直接関係"},
        {"evidence": [{"id": "E4", "text": "関係", "url": "https://example.test/issues/4"}]},
    )
    _expect_failure(
        score_tools(case, unrelated_follow_up_events),
        "omitted_inspectable_evidence_not_followed_up",
        "unrelated budget follow-up",
    )
    duplicate = score_tools(case, _synthetic_events(duplicate=True))
    _expect_failure(duplicate, "duplicate_selected_database_query", "duplicate query")
    foreign_events = _synthetic_events() + _tool_pair(
        "ask", "ask_user", {"question": "approve"}, {"ok": True}
    )
    foreign = score_tools(case, foreign_events)
    _expect_failure(foreign, "foreign_tool", "foreign tool")
    _expect_failure(foreign, "ask_user_tool_observed", "ask_user")
    permission = score_tools(
        case, _synthetic_events() + [{"type": "permission.requested", "data": {}}]
    )
    _expect_failure(permission, "permission_or_user_input_event_observed", "permission")

    if any(
        _explicit_fallback_observed(value)
        for value in (
            {"fallback": False},
            {"fallback_reason": None},
            {"fallback": "not_observed"},
            {"model": "auto", "resolved_model": "gpt-5-mini"},
        )
    ):
        raise CollectionError("negative fallback telemetry self-test failed")
    if not _explicit_fallback_observed({"fallback": True}):
        raise CollectionError("positive fallback telemetry self-test failed")

    exact_prompt_event = {
        "type": "user.message",
        "data": {"content": EXPECTED_PROMPT},
    }
    prompt_evidence, prompt_failures = _user_prompt_evidence([exact_prompt_event])
    if prompt_failures or prompt_evidence["user_message_count"] != 1:
        raise CollectionError("exact user-message evidence self-test failed")
    _, repeated_prompt_failures = _user_prompt_evidence(
        [exact_prompt_event, exact_prompt_event]
    )
    if "user_message_count_not_exactly_one" not in repeated_prompt_failures:
        raise CollectionError("repeated user-message evidence self-test failed")

    visible_urls, visible_markdown_urls = _visible_response_urls(_valid_response())
    if len(visible_urls) != 2 or len(visible_markdown_urls) != 2:
        raise CollectionError("visible Markdown URL self-test failed")
    code_only_links = (
        "```markdown\n"
        "[root](https://example.test/issues/1)\n"
        "[root2](https://example.test/issues/2)\n"
        "```"
    )
    code_urls, code_markdown_urls = _visible_response_urls(code_only_links)
    if code_urls or code_markdown_urls:
        raise CollectionError("code-only Markdown URLs were counted as clickable evidence")

    # The pinned nano arithmetic is intentionally exact and never float-derived.
    if EXPECTED_BASELINE_NANO + EXPECTED_REMAINING_NANO != EXPECTED_TOTAL_CAP_NANO:
        raise CollectionError("nano credit pin arithmetic failed")
    print("self-test: PASS (response, code/URL exclusions, classification, tool follow-up, nano pin)")
    return 0


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


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
                raise CollectionError("snapshot mode requires both snapshot arguments")
            _write_json(
                args.snapshot_output,
                bakeoff.snapshot_session_store(args.snapshot_copilot_home),
            )
            return 0
        if args.authority is None:
            raise CollectionError("--authority is required")
        if args.self_test:
            return self_test(args.authority)
        if args.raw_root is None or args.output is None:
            raise CollectionError("collection requires --raw-root and --output")
        report = collect(args.authority, args.raw_root)
        _write_json(args.output, report)
        print(json.dumps({"status": report["status"], "failures": report["failures"]}, ensure_ascii=False))
        return 0 if report["status"] == "PASS" else 1
    except (CollectionError, OSError) as exc:
        print(f"collector error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
