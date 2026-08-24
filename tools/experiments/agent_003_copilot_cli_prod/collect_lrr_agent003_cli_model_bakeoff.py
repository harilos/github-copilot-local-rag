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
import stat
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from pathlib import PurePosixPath
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
RECOVERY_IMPORT_SCHEMA = "lrr-agent003-cli-model-bakeoff-recovery-import-v1"
RECOVERY_IMPORT_SCHEMA_V2 = "lrr-agent003-cli-model-bakeoff-recovery-import-v2"
RECOVERY_IMPORT_SCHEMA_V3 = "lrr-agent003-cli-model-bakeoff-recovery-import-v3"
RECOVERY_IMPORT_SCHEMA_V4 = "lrr-agent003-cli-model-bakeoff-recovery-import-v4"
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
SHA256_RE = re.compile(r"[0-9a-f]{64}")
NANO_AIU_PER_CREDIT = 1_000_000_000
R3_RECOVERY_BEFORE_SHA256 = "24f4a2eae256a6fd1b5695f11bf6b0553f776b8168a5a5ce957c29999e277598"
R3_RECOVERY_AFTER_SHA256 = "e917ee4415b89669e759de35259ec8e951508a1681bda9e4136428027657ac51"
R3_HISTORICAL_HARNESS = {
    "runner": (
        "run_lrr_agent003_cli_model_bakeoff.ps1",
        "f841047aa991b3ac24fa1478754bb166db4441da0421d4e86a6b99049d29b7ea",
    ),
    "collector": (
        "collect_lrr_agent003_cli_model_bakeoff.py",
        "2d61824fdb16bda6b458dc78e9d1ba1f1bedcb147e53a9cd14a676232274e708",
    ),
    "authority": (
        "lrr-agent003-cli-model-bakeoff-v1.json",
        "c3a3dfa6ef96d1362f7763b8391c9d9d0b94560ab82e33edc5597cda90024517",
    ),
}
R5_PREFIX_HISTORICAL_HARNESS = {
    "runner": (
        "run_lrr_agent003_cli_model_bakeoff.ps1",
        "0076aa5dbf87fc9d3cded5c96c11f9a87b7e9adb43b6eac22c293cba0796ac08",
    ),
    "collector": (
        "collect_lrr_agent003_cli_model_bakeoff.py",
        "d41d7c289a67200dd5864eb3f2bf3f6d9d3d8bf4adba5ab2f49d3db40d3206cf",
    ),
    "authority": (
        "lrr-agent003-cli-model-bakeoff-v1.json",
        "c3a3dfa6ef96d1362f7763b8391c9d9d0b94560ab82e33edc5597cda90024517",
    ),
}
R5_PARENT_RECOVERY_MANIFEST_SHA256 = "02d996f662d7c4b5b37238419ab484242a745613fa2cddeea5ee5a6cb7a8c9ac"
R5_REPORT21_SHA256 = "3ac7e7386b60437adc6c43c8f29f022e5d2ab69c2521a170f434126a10062f85"
R5_RECOVERED_REPORT22_SHA256 = "4996053c7142a35d446cc606b6e1e9973111896ce59c682e95a417072f1c4a20"
R5_REPORT22_STDERR_SHA256 = "47a0bf30aa1a722bf4039ad6a59b053c790b3357300571ba6b78a33e7a1c5458"
R6_HISTORICAL_HARNESS = {
    "runner": (
        "run_lrr_agent003_cli_model_bakeoff.ps1",
        "6cc3a92174085100aa0ba98e6df0bdc1f4653c08063dbf4e0595826cbb978065",
    ),
    "collector": (
        "collect_lrr_agent003_cli_model_bakeoff.py",
        "eed4527a4a2f33b9dbbef39913fd0498f0d961f8d05ac697c53bb4560d8f4a88",
    ),
    "authority": (
        "lrr-agent003-cli-model-bakeoff-v1.json",
        "c3a3dfa6ef96d1362f7763b8391c9d9d0b94560ab82e33edc5597cda90024517",
    ),
}
R6_PARENT_RECOVERY_MANIFEST_SHA256 = "9cec0343d9e45b69873f933e8f21184a49f9bdae5c5beb94834c786e80734bcb"
R6_REPORT22_SHA256 = "581e0a15f8b10cfd151f4d788daad92e5bf2b17f0b765d40993d4f7bf9b03ee8"
R6_REPORT23_SHA256 = "6879c12106c6dee0652d23373bb0031cc802b64c8f835b976b7762378d0678fc"
R6_THOROUGH_PROMPT_SHA256 = "928193497e3b9183cf56e704b2bfdc69589e619bc6e1a5ce3b6f16c74189ef1b"
R6_FAILED_THOROUGH_FILES = {
    "copilot-logs/process-1787565711894-28220.log": "1625bff364a9c2d59e150d902c212a432a7bc8ea5eda23e0171c3b6bce85a912",
    "copilot.jsonl": "eafb3cb0950592916815a0adb0850f3cacf95d1da8286f1f50feba89901d452b",
    "otel.jsonl": "f19818297bb53d337323d75065edef261da6eac23adef410a7152b982e5cac94",
    "run.json": "c23d77bd98e36be5e99b3e7ceba5b54b668a988022eafbe6e8f2ffd43c89e7d3",
    "stderr.log": "ac47635ad9fc6466de181f0b7c566eefad3b8120060fcced147374ebfaccb0a5",
    "temporary-model-mutation.json": "bbfbacb6a285a89094b485daf655654713e1b7f62f04a2d65be4e6bac612f677",
    "usage-after.json": "d7c6549757118fcae61646a3c3f4d3d1c6cd9bfe06e40fc3e5aecce89c404cc2",
    "usage-after.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-after.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.json": "4ef46c2015325550652fc7119ebabb164cf75d759391ee081cc6da416f4ccba0",
    "usage-before.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
R7_RECOVERY_MANIFEST_SHA256 = "8634cc4cfe4fc801d862e98cb3c074061bcf17141b075265ba055b9112da4e00"
R7_REPORT23_SHA256 = "253138e55db4aa75eded65a94c46d52092548c60afe09f8e59f326575cbe2371"
R7_REPORT24_SHA256 = "de6b072cdff47bffd1d9542771ed856cd1bc13a248946a1e1aec88f8a38597b6"
R7_RUN24_FILES = {
    "copilot.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "otel.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "run.json": "e8f9b05d0f5e32e00e07e5376b436e2de95d80994862f9de36edea2b7c27d330",
    "stderr.log": "f87411069ba5c0d64bce380b53097a0ed433e76842012944801603d453b16df3",
    "temporary-model-mutation.json": "09a06f5675e5c558b1097caaa63e2bb4ffdfe91f24910791f063f2e2ff77c5bb",
    "usage-after.json": "069e1bb651c6d91ef8b6efc0ef51ac7690b47cea2bc9bcf8fef3a91451ecbd76",
    "usage-after.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-after.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.json": "2cc02561aa27613f928d932ee3c136e046e15b48524c17060c60375fca223140",
    "usage-before.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
R7_RUNNER_SHA256 = "4ec55ea9d9f4b88f003900bd0343295be303bacd2fc90703b4bc10909bef3eda"
R7_COLLECTOR_SHA256 = "062379b5a09fcffc32b09bb8fd8e2614be344e0377b9a7847e75e864ca10db9e"
R7_PREINFERENCE_STDERR = (
    "error: option '--max-ai-credits <credits>' argument '12' is invalid. "
    'Invalid value for --max-ai-credits: "12". Use at least 30 AI credits.'
)
R7_PREINFERENCE_REPORT_FAILURES = sorted(
    [
        "auto_model_resolution_invalid",
        "cli_exit_nonzero_not_policy",
        "credit_unknown",
        "expected_agent_missing",
        "final_assistant_response_missing",
        "otel_token_usage_unknown",
        "per_session_soft_cap_mismatch",
        "policy_rejection_message_not_recognized",
        "result_event_count_invalid",
        "retry_observed",
        "search_call_count_out_of_range",
        "session_usage_unavailable",
    ]
)
R8_BOUNDARY_WRAPPER_REAGGREGATION_MANIFEST_SHA256 = (
    "d9ad2d05bdcef1a83ff4f56b7ee0ae34ece6cbffa270c5f7526e0c869076a632"
)
R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_COLLECTOR_SHA256 = (
    "d37e9af17479db49642548ef503137e1206ae788b2d8bc541c3189c08ab68daf"
)
R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_REPORT_SHA256 = (
    "719ac3e3aa66492e2cafacfdcc0c4778c85e77186a67c029d5227da944901e29"
)
R8_AUDITED_RESCORE_RUN24_FILES = {
    "copilot-logs/process-1787569648916-30952.log": "b10f845a2170d8f41066007a50b2f530be5290bec3043a6ae787ddf3864c0283",
    "copilot.jsonl": "60f307ffd05a61972cc259a889fdfa4012620a6df27b68ff1f3963a7b49ed543",
    "otel.jsonl": "233463d5c318867cb09f41db902b3f8ad9098d951ad518cf569a9cb1c8cdae0b",
    "run.json": "a2448089301e1fd77ba40de514ba279876c9d8e3f0fa421bdd2524706b7073b2",
    "stderr.log": "ac47635ad9fc6466de181f0b7c566eefad3b8120060fcced147374ebfaccb0a5",
    "temporary-model-mutation.json": "53af913e304ada5ff9ea422a284132bd85991b025cc19dc110c40f06326fd4df",
    "usage-after.json": "f3eafb5fd8c79c169f420dd742f1c658b70173f10875624f228772a164bbbffb",
    "usage-after.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-after.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.json": "aef68c6b845a8df8943032e38d236ea40de89f2eceb992eab8d190bfb36867ff",
    "usage-before.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
R8_BOUNDARY_WRAPPER_REAGGREGATION_RUN25_FILES = {
    "copilot-logs/process-1787569868450-26348.log": "c033c44cf0a138a1d23d53a630f040074400228256e8aecf3bb7ed562de3c997",
    "copilot.jsonl": "b682a61fea2f58a2b0430ba7341c30c21175ef56ccea2e0adbddc3280848462f",
    "otel.jsonl": "fc4358f2df967322956723875bc23a8d08284b23d1b5a53f0a6a4d57504706d0",
    "run.json": "3553b9a5e228baadd9a3aef086bb772ce06db379f8179377ef1be979eb9abf35",
    "stderr.log": "893488b17c90a324ae374d9ac5b53fa610134ce8ab4843fbb6172124262d59ed",
    "temporary-model-mutation.json": "eaec24cc1e16230c9e359336be62d5ffa2728d87317685506a9dcb473588b16b",
    "usage-after.json": "73fc975b88197110e6bb3b849ea69077bb97ff63b720e15a5aa3eee65f1a064c",
    "usage-after.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-after.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.json": "14afd013b609569901100e8928c10bbe9db7ab22575201dd74f151be2568245e",
    "usage-before.stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "usage-before.stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


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


def _load_anchored_json(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if not isinstance(expected_sha256, str) or SHA256_RE.fullmatch(expected_sha256) is None:
        raise BakeoffError("recovery import expected SHA-256 is invalid")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BakeoffError(f"cannot read anchored JSON: {path}: {exc}") from exc
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise BakeoffError("recovery import manifest anchor mismatch")
    try:
        value = json.loads(payload.decode("utf-8-sig", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BakeoffError(f"invalid anchored JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BakeoffError(f"JSON root is not an object: {path}")
    return value, actual_sha256


def _require_supported_recovery_schema(value: dict[str, Any]) -> None:
    if value.get("schema_version") not in (
        RECOVERY_IMPORT_SCHEMA,
        RECOVERY_IMPORT_SCHEMA_V2,
        RECOVERY_IMPORT_SCHEMA_V3,
        RECOVERY_IMPORT_SCHEMA_V4,
    ):
        raise BakeoffError("recovery import schema is invalid")


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


def load_authority(
    path: Path,
    *,
    require_final_review_contract: bool = True,
) -> dict[str, Any]:
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
    if require_final_review_contract and (
        thorough.get("expected_database") != "fizzbuzz-planet-rag"
        or thorough.get("expected_routing_search_calls") != 1
        or thorough.get("minimum_selected_database_search_calls") != 3
        or thorough.get("maximum_total_tool_calls") != 7
        or thorough.get("forbid_duplicate_selected_database_queries") is not True
        or thorough.get("require_omitted_inspectable_evidence_follow_up") is not True
        or thorough.get("omitted_evidence_relevance_terms")
        != ["予算", "増額", "確定"]
        or thorough.get("narrow_follow_up_query_terms")
        != ["確定", "執行", "7%"]
    ):
        raise BakeoffError("thorough routing, selected-search, and review authority is not canonical")
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


def _matches_boundary_wrapper_reaggregation_collector(
    path: Path, digest: str, allowed_stale_digest: str | None
) -> bool:
    return (
        allowed_stale_digest
        == R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_COLLECTOR_SHA256
        and digest == allowed_stale_digest
        and path.resolve() == Path(__file__).resolve()
        and path.is_file()
        and not _is_link_or_reparse(path)
    )


def _validate_harness_identity(
    run: dict[str, Any],
    historical_identity: dict[str, Any] | None = None,
    allowed_stale_collector_sha256: str | None = None,
) -> list[str]:
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
        if not isinstance(digest_value, str) or SHA256_RE.fullmatch(digest_value) is None:
            failures.append(f"{prefix}_hash_invalid")
            continue
        live_match = (
            path.is_file()
            and not _is_link_or_reparse(path)
            and digest_value == prod._sha256_file(path)
        )
        reaggregation_match = (
            prefix == "collector"
            and _matches_boundary_wrapper_reaggregation_collector(
                path, digest_value, allowed_stale_collector_sha256
            )
        )
        if not live_match and not reaggregation_match:
            preserved_paths = (
                historical_identity.get("_validated_artifact_paths")
                if isinstance(historical_identity, dict)
                else None
            )
            preserved = (
                Path(preserved_paths[prefix])
                if isinstance(preserved_paths, dict)
                and isinstance(preserved_paths.get(prefix), str)
                else None
            )
            historical_match = (
                isinstance(historical_identity, dict)
                and historical_identity.get(f"{prefix}_path") == path_value
                and historical_identity.get(f"{prefix}_sha256") == digest_value
                and preserved is not None
                and preserved.is_file()
                and not _is_link_or_reparse(preserved)
                and prod._sha256_file(preserved) == digest_value
            )
            if not historical_match:
                failures.append(f"{prefix}_hash_mismatch")
    return failures


def _canonical_run_contract(
    authority: dict[str, Any], ordinal: int, *, retry_plan: bool = False
) -> dict[str, Any]:
    final_ordinal = 25 if retry_plan else 24
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 1 <= ordinal <= final_ordinal
    ):
        raise BakeoffError("formal run ordinal is unknown")
    if ordinal <= 21:
        candidate_ordinal = (ordinal - 1) // 3
        attempt = ((ordinal - 1) % 3) + 1
        run_id = (
            f"LRR-AGENT003-CLI-MODEL-SAVINGS-C{candidate_ordinal + 1:02d}"
            f"-R{attempt}"
        )
        return {
            "run_id": run_id,
            "case_kind": "savings",
            "candidate_model": authority["candidate_models"][candidate_ordinal],
            "requested_model": authority["candidate_models"][candidate_ordinal],
            "tier": "savings",
            "attempt": attempt,
            "leaf": f"{ordinal:02d}-{run_id}",
        }
    if retry_plan and ordinal == 24:
        case = authority["thorough_case"]
        run_id = "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2"
        return {
            "run_id": run_id,
            "case_kind": "thorough",
            "candidate_model": "",
            "requested_model": "auto",
            "tier": case["tier"],
            "attempt": 2,
            "leaf": f"24-{run_id}",
        }
    if retry_plan and ordinal == 25:
        key = "boundary_case"
    else:
        key = {22: "standard_case", 23: "thorough_case", 24: "boundary_case"}[
            ordinal
        ]
    case = authority[key]
    return {
        "run_id": case["id"],
        "case_kind": key.removesuffix("_case"),
        "candidate_model": "",
        "requested_model": "auto",
        "tier": case["tier"],
        "attempt": 1,
        "leaf": f"{ordinal:02d}-{case['id']}",
    }


def _run_matches_canonical_contract(
    run: dict[str, Any], contract: dict[str, Any], ordinal: int
) -> bool:
    return (
        run.get("schema_version") == RUN_SCHEMA
        and run.get("plan_ordinal") == ordinal
        and run.get("run_id") == contract["run_id"]
        and run.get("case_kind") == contract["case_kind"]
        and run.get("candidate_model") == contract["candidate_model"]
        and run.get("requested_model") == contract["requested_model"]
        and run.get("attempt") == contract["attempt"]
    )


def _validate_retry_run_metadata(run: dict[str, Any], ordinal: int) -> None:
    if ordinal == 24:
        if (
            run.get("retry_count") != 1
            or run.get("retry_of_ordinal") != 23
            or run.get("retry_of_run_id")
            != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO"
            or run.get("logical_max_ai_credits") != 12
            or run.get("cli_max_ai_credits") != 30
            or run.get("max_ai_credits") != 30
        ):
            raise BakeoffError("formal thorough retry metadata is invalid")
    elif ordinal == 25:
        if (
            run.get("retry_count") != 0
            or run.get("retry_of_ordinal") is not None
            or run.get("retry_of_run_id") is not None
            or run.get("logical_max_ai_credits") != 8
            or run.get("cli_max_ai_credits") != 30
            or run.get("max_ai_credits") != 30
        ):
            raise BakeoffError("formal boundary retry-plan metadata is invalid")
    else:
        raise BakeoffError("retry metadata ordinal is invalid")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _directory_file_map(root: Path) -> dict[str, tuple[str, int]]:
    if not root.is_dir() or _is_link_or_reparse(root):
        raise BakeoffError(f"evidence directory is invalid: {root}")
    result: dict[str, tuple[str, int]] = {}
    for path in root.rglob("*"):
        if _is_link_or_reparse(path):
            raise BakeoffError(f"evidence directory contains a link/reparse point: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        result[relative] = (prod._sha256_file(path), path.stat().st_size)
    return result


def _validate_exact_file_authority(
    source: Path,
    preserved: Path,
    entries: Any,
    *,
    context: str,
) -> None:
    if not isinstance(entries, list) or not entries:
        raise BakeoffError(f"{context} file authority is empty")
    expected: dict[str, tuple[str, int]] = {}
    casefolded: set[str] = set()
    ordered_paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BakeoffError(f"{context} file entry is invalid")
        relative = entry.get("relative_path")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if (
            not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or relative != PurePosixPath(relative).as_posix()
            or any(part in ("", ".", "..") for part in PurePosixPath(relative).parts)
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or relative in expected
            or relative.casefold() in casefolded
        ):
            raise BakeoffError(f"{context} file entry is invalid")
        expected[relative] = (digest, size)
        casefolded.add(relative.casefold())
        ordered_paths.append(relative)
    if ordered_paths != sorted(ordered_paths, key=lambda value: value.casefold()):
        raise BakeoffError(f"{context} file authority order is not canonical")
    source_map = _directory_file_map(source)
    preserved_map = _directory_file_map(preserved)
    if source_map != expected or preserved_map != expected:
        raise BakeoffError(f"{context} recursive file set/hash mismatch")


def _validate_versioned_harness_archive(
    identity: dict[str, Any],
    archive_root: Path,
    expected: dict[str, tuple[str, str]],
    *,
    context: str,
) -> dict[str, Any]:
    if not archive_root.is_dir() or _is_link_or_reparse(archive_root):
        raise BakeoffError(f"{context} archive is missing or unsafe")
    expected_map: dict[str, tuple[str, int]] = {}
    validated_paths: dict[str, str] = {}
    for kind in ("runner", "collector", "authority"):
        relative, digest = expected[kind]
        original_path = identity.get(f"{kind}_path")
        recorded_digest = identity.get(f"{kind}_sha256")
        artifact = archive_root / relative
        if (
            not isinstance(original_path, str)
            or not Path(original_path).is_absolute()
            or recorded_digest != digest
            or not artifact.is_file()
            or _is_link_or_reparse(artifact)
            or prod._sha256_file(artifact) != digest
        ):
            raise BakeoffError(f"{context} {kind} identity/bytes mismatch")
        expected_map[relative] = (digest, artifact.stat().st_size)
        validated_paths[kind] = str(artifact.resolve())
    if _directory_file_map(archive_root) != expected_map:
        raise BakeoffError(f"{context} archive has extra/missing files")
    load_authority(
        Path(validated_paths["authority"]),
        require_final_review_contract=False,
    )
    result = dict(identity)
    result["_validated_artifact_paths"] = validated_paths
    return result


def _identity_matches_run(run: dict[str, Any], identity: dict[str, Any]) -> bool:
    return all(
        run.get(f"{kind}_path") == identity.get(f"{kind}_path")
        and run.get(f"{kind}_sha256") == identity.get(f"{kind}_sha256")
        for kind in ("runner", "collector", "authority")
    )


def _validate_recovery_snapshots(
    source_report: dict[str, Any],
    source_aborted: Path,
    preserved_aborted: Path,
    aborted: dict[str, Any],
) -> tuple[int, int]:
    before_name = "usage-before.json"
    after_name = "usage-after-recovered.json"
    source_before = source_aborted / before_name
    source_after = source_aborted / after_name
    preserved_before = preserved_aborted / before_name
    preserved_after = preserved_aborted / after_name
    manifest_hashes = {
        before_name: aborted.get("usage_before_sha256"),
        after_name: aborted.get("usage_after_recovered_sha256"),
    }
    if manifest_hashes != {
        before_name: R3_RECOVERY_BEFORE_SHA256,
        after_name: R3_RECOVERY_AFTER_SHA256,
    }:
        raise BakeoffError("recovery snapshot identity is not the observed r3 interruption")
    evidence_files = aborted.get("files")
    if not isinstance(evidence_files, list):
        raise BakeoffError("recovery snapshot file authority is missing")
    evidence_hashes = {
        entry.get("relative_path"): entry.get("sha256")
        for entry in evidence_files
        if isinstance(entry, dict)
    }
    for name, source_path, preserved_path in (
        (before_name, source_before, preserved_before),
        (after_name, source_after, preserved_after),
    ):
        expected_hash = manifest_hashes[name]
        if (
            not isinstance(expected_hash, str)
            or SHA256_RE.fullmatch(expected_hash) is None
            or evidence_hashes.get(name) != expected_hash
            or not source_path.is_file()
            or _is_link_or_reparse(source_path)
            or not preserved_path.is_file()
            or _is_link_or_reparse(preserved_path)
            or prod._sha256_file(source_path) != expected_hash
            or prod._sha256_file(preserved_path) != expected_hash
        ):
            raise BakeoffError("recovery snapshot hash mismatch")
    source_before_value = _load_json(source_before)
    source_after_value = _load_json(source_after)
    preserved_before_value = _load_json(preserved_before)
    preserved_after_value = _load_json(preserved_after)
    if (
        source_before_value != preserved_before_value
        or source_after_value != preserved_after_value
    ):
        raise BakeoffError("source/preserved recovery snapshots differ")
    before, after = source_before_value, source_after_value
    if (
        before.get("schema_version") != SNAPSHOT_SCHEMA
        or after.get("schema_version") != SNAPSHOT_SCHEMA
        or before.get("copilot_home") != after.get("copilot_home")
        or not isinstance(before.get("copilot_home"), str)
        or not before.get("copilot_home")
        or before.get("session_store_path") != after.get("session_store_path")
        or before.get("session_store_exists") is not True
        or after.get("session_store_exists") is not True
    ):
        raise BakeoffError("recovery snapshot schema/home mismatch")
    values: dict[str, int] = {}
    for prefix, snapshot in (("before", before), ("after", after)):
        for field in ("row_count", "total_nano_aiu"):
            value = snapshot.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BakeoffError("recovery snapshot integer value is invalid")
            values[f"{prefix}_{field}"] = value
    if (
        values["after_row_count"] < values["before_row_count"]
        or values["after_total_nano_aiu"] < values["before_total_nano_aiu"]
    ):
        raise BakeoffError("recovery snapshot counters decreased")
    if (
        values["before_row_count"] != 18
        or values["after_row_count"] != 21
        or values["before_total_nano_aiu"] != 6_033_745_000
        or values["after_total_nano_aiu"] != 6_316_900_000
    ):
        raise BakeoffError("recovery snapshot counters are not the observed r3 values")
    source_nano = source_report.get("aggregate_total_nano_aiu")
    if (
        isinstance(source_nano, bool)
        or not isinstance(source_nano, int)
        or values["before_total_nano_aiu"] != source_nano
    ):
        raise BakeoffError("recovery before snapshot does not equal formal source aggregate")
    source_runs = source_report.get("runs")
    source_row_values = (
        [run.get("usage_row_delta") for run in source_runs]
        if isinstance(source_runs, list)
        and all(isinstance(run, dict) for run in source_runs)
        else []
    )
    source_nano_values = (
        [run.get("total_nano_aiu") for run in source_runs]
        if isinstance(source_runs, list)
        and all(isinstance(run, dict) for run in source_runs)
        else []
    )
    if (
        not isinstance(source_runs, list)
        or not source_row_values
        or any(isinstance(item, bool) or not isinstance(item, int) for item in source_row_values)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in source_nano_values)
        or sum(source_row_values) != values["before_row_count"]
        or sum(source_nano_values) != values["before_total_nano_aiu"]
    ):
        raise BakeoffError("recovery source run usage does not reconcile to before snapshot")
    row_delta = values["after_row_count"] - values["before_row_count"]
    nano_delta = values["after_total_nano_aiu"] - values["before_total_nano_aiu"]
    manifest_row_delta = aborted.get("usage_row_delta")
    manifest_nano_delta = aborted.get("recovery_total_nano_aiu")
    manifest_credits = aborted.get("recovery_ai_credits")
    try:
        manifest_credit_nano = Decimal(str(manifest_credits)) * NANO_AIU_PER_CREDIT
    except (InvalidOperation, ValueError):
        manifest_credit_nano = Decimal(-1)
    if (
        manifest_row_delta != row_delta
        or manifest_nano_delta != nano_delta
        or isinstance(manifest_credits, bool)
        or not isinstance(manifest_credits, (int, float))
        or manifest_credit_nano != nano_delta
    ):
        raise BakeoffError("recovery snapshot-derived Credit fields mismatch")
    return row_delta, nano_delta


def _load_recovery_import(
    raw_root: Path,
    authority: dict[str, Any],
    expected_manifest_sha256: str | None,
    *,
    boundary_wrapper_rescore: bool = False,
) -> dict[str, Any] | None:
    path = raw_root / "recovery-import.json"
    if not path.exists():
        if expected_manifest_sha256 is not None:
            raise BakeoffError("expected recovery import manifest is missing")
        return None
    if _is_link_or_reparse(path):
        raise BakeoffError("recovery import manifest is a link/reparse point")
    if expected_manifest_sha256 is None:
        raise BakeoffError("recovery import manifest requires an expected SHA-256 anchor")
    value, manifest_sha256 = _load_anchored_json(path, expected_manifest_sha256)
    if boundary_wrapper_rescore and (
        manifest_sha256
        != R8_BOUNDARY_WRAPPER_REAGGREGATION_MANIFEST_SHA256
        or value.get("schema_version") != RECOVERY_IMPORT_SCHEMA_V4
    ):
        raise BakeoffError("boundary wrapper rescore is not anchored to exact r8")
    if value.get("schema_version") == RECOVERY_IMPORT_SCHEMA_V4:
        return _load_recovery_import_v4_from_value(
            raw_root,
            authority,
            path,
            value,
            manifest_sha256,
            boundary_wrapper_rescore=boundary_wrapper_rescore,
        )
    if value.get("schema_version") == RECOVERY_IMPORT_SCHEMA_V3:
        return _load_recovery_import_v3_from_value(
            raw_root,
            authority,
            path,
            value,
            manifest_sha256,
            boundary_wrapper_rescore=boundary_wrapper_rescore,
        )
    if value.get("schema_version") == RECOVERY_IMPORT_SCHEMA_V2:
        return _load_recovery_import_v2_from_value(
            raw_root,
            authority,
            path,
            value,
            manifest_sha256,
        )
    if value.get("schema_version") != RECOVERY_IMPORT_SCHEMA:
        raise BakeoffError("recovery import schema is invalid")
    output_root_value = value.get("output_root")
    if (
        not isinstance(output_root_value, str)
        or not Path(output_root_value).is_absolute()
        or Path(output_root_value).resolve() != raw_root.resolve()
    ):
        raise BakeoffError("recovery output root is not canonical")
    completed = value.get("resume_completed_ordinal")
    if isinstance(completed, bool) or not isinstance(completed, int) or completed != 5:
        raise BakeoffError("recovery completed ordinal must be exactly 5")
    aborted_contract = _canonical_run_contract(authority, completed + 1)
    source_root_value = value.get("source_evidence_root")
    if (
        not isinstance(source_root_value, str)
        or not Path(source_root_value).is_absolute()
    ):
        raise BakeoffError("recovery source root is invalid")
    source_root = Path(source_root_value).resolve()
    if source_root == raw_root.resolve() or not source_root.is_dir():
        raise BakeoffError("recovery source root is missing or aliases output")
    report_path_value = value.get("source_report_path")
    report_sha = value.get("source_report_sha256")
    if (
        not isinstance(report_path_value, str)
        or not Path(report_path_value).is_absolute()
    ):
        raise BakeoffError("recovery source report path is invalid")
    source_report_path = Path(report_path_value).resolve()
    if (
        source_report_path
        != (source_root / "reports" / f"report-{completed:02d}.json").resolve()
        or
        not source_report_path.is_file()
        or _is_link_or_reparse(source_report_path)
        or not isinstance(report_sha, str)
        or SHA256_RE.fullmatch(report_sha) is None
        or prod._sha256_file(source_report_path) != report_sha
    ):
        raise BakeoffError("recovery source report hash is invalid")
    preserved_report_value = value.get("preserved_source_report_path")
    if (
        not isinstance(preserved_report_value, str)
        or not Path(preserved_report_value).is_absolute()
    ):
        raise BakeoffError("preserved source report path is invalid")
    preserved_report_path = Path(preserved_report_value).resolve()
    if (
        preserved_report_path != (raw_root / "recovery" / f"source-report-{completed:02d}.json").resolve()
        or not preserved_report_path.is_file()
        or _is_link_or_reparse(preserved_report_path)
        or prod._sha256_file(preserved_report_path) != report_sha
        or preserved_report_path.read_bytes() != source_report_path.read_bytes()
    ):
        raise BakeoffError("preserved source report bytes mismatch")
    source_report = _load_json(source_report_path)
    if (
        source_report.get("schema_version") != REPORT_SCHEMA
        or len(source_report.get("runs", [])) != completed
        or [run.get("plan_ordinal") for run in source_report.get("runs", [])]
        != list(range(1, completed + 1))
    ):
        raise BakeoffError("recovery source report is not the exact formal prefix")
    imported_runs = value.get("imported_runs")
    if not isinstance(imported_runs, list) or len(imported_runs) != completed:
        raise BakeoffError("recovery imported run count is invalid")
    imported_ids: set[str] = set()
    imported_run_values: list[dict[str, Any]] = []
    expected_source_leaves: set[str] = set()
    for expected_ordinal, imported in enumerate(imported_runs, 1):
        if not isinstance(imported, dict) or imported.get("ordinal") != expected_ordinal:
            raise BakeoffError("recovery imported run ordinal is invalid")
        run_id = imported.get("run_id")
        source_value = imported.get("source_directory")
        destination_value = imported.get("destination_directory")
        files = imported.get("files")
        if (
            not isinstance(run_id, str)
            or not run_id
            or run_id in imported_ids
            or not isinstance(source_value, str)
            or not Path(source_value).is_absolute()
            or not isinstance(destination_value, str)
            or not Path(destination_value).is_absolute()
            or not isinstance(files, list)
            or not files
        ):
            raise BakeoffError("recovery imported run metadata is invalid")
        imported_ids.add(run_id)
        source_directory = Path(source_value).resolve()
        destination_directory = Path(destination_value).resolve()
        contract = _canonical_run_contract(authority, expected_ordinal)
        expected_source_leaves.add(contract["leaf"])
        if (
            not source_directory.is_dir()
            or not destination_directory.is_dir()
            or _is_link_or_reparse(source_directory)
            or _is_link_or_reparse(destination_directory)
            or source_directory
            != (source_root / "runs" / contract["leaf"]).resolve()
            or destination_directory
            != (raw_root / "runs" / contract["leaf"]).resolve()
        ):
            raise BakeoffError("recovery imported run directory is invalid")
        imported_run = _load_json(destination_directory / "run.json")
        if (
            imported_run.get("plan_ordinal") != expected_ordinal
            or imported_run.get("schema_version") != RUN_SCHEMA
            or imported_run.get("run_id") != run_id
            or run_id != contract["run_id"]
            or imported_run.get("case_kind") != contract["case_kind"]
            or imported_run.get("candidate_model") != contract["candidate_model"]
            or imported_run.get("requested_model") != contract["requested_model"]
            or imported_run.get("attempt") != contract["attempt"]
        ):
            raise BakeoffError("recovery copied run identity is invalid")
        imported_run_values.append(imported_run)
        for file_entry in files:
            if not isinstance(file_entry, dict):
                raise BakeoffError("recovery imported file entry is invalid")
            relative = file_entry.get("relative_path")
            digest = file_entry.get("sha256")
            size = file_entry.get("bytes")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(digest, str)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise BakeoffError("recovery imported file authority is invalid")
            source_file = source_directory / relative
            destination_file = destination_directory / relative
            for candidate in (source_file, destination_file):
                if (
                    not candidate.is_file()
                    or candidate.is_symlink()
                    or candidate.stat().st_size != size
                    or prod._sha256_file(candidate) != digest
                ):
                    raise BakeoffError("recovery imported file hash mismatch")
        _validate_exact_file_authority(
            source_directory,
            destination_directory,
            files,
            context=f"recovery imported run {expected_ordinal}",
        )
    source_runs_root = source_root / "runs"
    if not source_runs_root.is_dir() or _is_link_or_reparse(source_runs_root):
        raise BakeoffError("recovery source runs root is invalid")
    source_children = list(source_runs_root.iterdir())
    if any(not child.is_dir() or _is_link_or_reparse(child) for child in source_children):
        raise BakeoffError("recovery source runs root has unknown entries")
    expected_source_all = expected_source_leaves | {aborted_contract["leaf"]}
    if {child.name for child in source_children} != expected_source_all:
        raise BakeoffError("recovery source contains extra/missing run directories")
    historical = value.get("historical_harness_identity")
    if not isinstance(historical, dict):
        raise BakeoffError("recovery historical harness identity is missing")
    for prefix in ("runner", "collector", "authority"):
        if (
            not isinstance(historical.get(f"{prefix}_path"), str)
            or not Path(historical[f"{prefix}_path"]).is_absolute()
            or not isinstance(historical.get(f"{prefix}_sha256"), str)
            or SHA256_RE.fullmatch(historical[f"{prefix}_sha256"]) is None
            or historical[f"{prefix}_sha256"]
            != R3_HISTORICAL_HARNESS[prefix][1]
        ):
            raise BakeoffError("recovery historical harness identity is invalid")
    for imported_run in imported_run_values:
        for prefix in ("runner", "collector", "authority"):
            if (
                imported_run.get(f"{prefix}_path")
                != historical.get(f"{prefix}_path")
                or imported_run.get(f"{prefix}_sha256")
                != historical.get(f"{prefix}_sha256")
            ):
                raise BakeoffError(
                    "recovery imported run historical identity mismatch"
                )
    historical_source_value = value.get("historical_harness_source_directory")
    historical_preserved_value = value.get("historical_harness_preserved_directory")
    historical_files = value.get("historical_harness_files")
    historical_artifacts = value.get("historical_harness_artifacts")
    if (
        not isinstance(historical_source_value, str)
        or not Path(historical_source_value).is_absolute()
        or not isinstance(historical_preserved_value, str)
        or not Path(historical_preserved_value).is_absolute()
        or not isinstance(historical_files, list)
        or not isinstance(historical_artifacts, list)
        or len(historical_artifacts) != 3
    ):
        raise BakeoffError("historical harness byte authority is invalid")
    historical_source = Path(historical_source_value).resolve()
    historical_preserved = Path(historical_preserved_value).resolve()
    if historical_preserved != (raw_root / "recovery" / "r3-historical-harness").resolve():
        raise BakeoffError("historical harness preserved directory is not canonical")
    _validate_exact_file_authority(
        historical_source,
        historical_preserved,
        historical_files,
        context="historical harness",
    )
    expected_kinds = ["runner", "collector", "authority"]
    if [item.get("kind") for item in historical_artifacts if isinstance(item, dict)] != expected_kinds:
        raise BakeoffError("historical harness artifact kind set/order is invalid")
    expected_names = {
        kind: identity[0] for kind, identity in R3_HISTORICAL_HARNESS.items()
    }
    validated_artifact_paths: dict[str, str] = {}
    for artifact in historical_artifacts:
        kind = artifact["kind"]
        relative = artifact.get("relative_path")
        digest = artifact.get("sha256")
        size = artifact.get("bytes")
        if (
            not isinstance(relative, str)
            or relative != expected_names[kind]
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or historical.get(f"{kind}_sha256") != digest
            or historical.get(f"{kind}_path") != artifact.get("original_path")
        ):
            raise BakeoffError("historical harness artifact identity is invalid")
        for candidate in (
            historical_source / relative,
            historical_preserved / relative,
        ):
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != size
                or prod._sha256_file(candidate) != digest
            ):
                raise BakeoffError("historical harness artifact bytes mismatch")
        validated_artifact_paths[kind] = str((historical_preserved / relative).resolve())
    preserved_authority = Path(validated_artifact_paths["authority"])
    load_authority(
        preserved_authority,
        require_final_review_contract=False,
    )
    if source_report.get("authority_sha256") != historical["authority_sha256"]:
        raise BakeoffError("source report historical authority identity mismatch")
    historical_for_evaluation = dict(historical)
    historical_for_evaluation["_validated_artifact_paths"] = validated_artifact_paths
    resume_identity = value.get("resume_harness_identity")
    if not isinstance(resume_identity, dict):
        raise BakeoffError("resume harness identity is missing")
    resume_live_match = True
    for prefix in ("runner", "collector", "authority"):
        resume_path = resume_identity.get(f"{prefix}_path")
        resume_sha = resume_identity.get(f"{prefix}_sha256")
        if (
            not isinstance(resume_path, str)
            or not Path(resume_path).is_absolute()
            or not Path(resume_path).is_file()
            or Path(resume_path).is_symlink()
            or not isinstance(resume_sha, str)
            or SHA256_RE.fullmatch(resume_sha) is None
            or prod._sha256_file(Path(resume_path)) != resume_sha
        ):
            resume_live_match = False
            break
    resume_historical_identity: dict[str, Any] | None = None
    if not resume_live_match:
        resume_historical_identity = _validate_versioned_harness_archive(
            resume_identity,
            Path(__file__).resolve().parent / "data" / "r5-prefix-historical-harness",
            R5_PREFIX_HISTORICAL_HARNESS,
            context="r5-prefix historical harness",
        )
    production_identity = value.get("production_identity")
    if not isinstance(production_identity, dict):
        raise BakeoffError("resume production identity is missing")
    for prefix in ("launcher", "manifest"):
        production_path = production_identity.get(f"{prefix}_path")
        production_sha = production_identity.get(f"{prefix}_sha256")
        if (
            not isinstance(production_path, str)
            or not Path(production_path).is_absolute()
            or not Path(production_path).is_file()
            or Path(production_path).is_symlink()
            or not isinstance(production_sha, str)
            or SHA256_RE.fullmatch(production_sha) is None
            or prod._sha256_file(Path(production_path)) != production_sha
        ):
            raise BakeoffError("resume production live identity mismatch")
    aborted = value.get("aborted_attempt")
    if (
        not isinstance(aborted, dict)
        or aborted.get("ordinal") != completed + 1
        or aborted.get("run_id") != aborted_contract["run_id"]
    ):
        raise BakeoffError("recovery aborted attempt identity is invalid")
    recovery_nano = aborted.get("recovery_total_nano_aiu")
    recovery_credits = aborted.get("recovery_ai_credits")
    if (
        isinstance(recovery_nano, bool)
        or not isinstance(recovery_nano, int)
        or recovery_nano < 0
        or isinstance(recovery_credits, bool)
        or not isinstance(recovery_credits, (int, float))
        or not math.isclose(
            float(recovery_credits),
            recovery_nano / NANO_AIU_PER_CREDIT,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise BakeoffError("recovery aborted attempt Credit is invalid")
    source_aborted_value = aborted.get("source_directory")
    preserved_aborted_value = aborted.get("preserved_directory")
    absent_files = aborted.get("required_absent_files")
    evidence_files = aborted.get("files")
    if (
        not isinstance(source_aborted_value, str)
        or not Path(source_aborted_value).is_absolute()
        or not isinstance(preserved_aborted_value, str)
        or not Path(preserved_aborted_value).is_absolute()
        or absent_files != ["run.json", "copilot.jsonl"]
        or not isinstance(evidence_files, list)
        or not evidence_files
        or aborted.get("incomplete_session_state") is not True
    ):
        raise BakeoffError("recovery aborted attempt evidence authority is invalid")
    source_aborted = Path(source_aborted_value).resolve()
    preserved_aborted = Path(preserved_aborted_value).resolve()
    if (
        source_aborted
        != (source_root / "runs" / aborted_contract["leaf"]).resolve()
        or preserved_aborted
        != (raw_root / "recovery" / aborted_contract["leaf"]).resolve()
        or not source_aborted.is_dir()
        or not preserved_aborted.is_dir()
        or _is_link_or_reparse(source_aborted)
        or _is_link_or_reparse(preserved_aborted)
    ):
        raise BakeoffError("recovery aborted attempt directory is missing")
    for absent in absent_files:
        if (source_aborted / absent).exists() or (preserved_aborted / absent).exists():
            raise BakeoffError("recovery aborted attempt contains a formal run artifact")
    for file_entry in evidence_files:
        if not isinstance(file_entry, dict):
            raise BakeoffError("recovery aborted file entry is invalid")
        relative = file_entry.get("relative_path")
        digest = file_entry.get("sha256")
        size = file_entry.get("bytes")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(digest, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
        ):
            raise BakeoffError("recovery aborted file authority is invalid")
        for candidate in (source_aborted / relative, preserved_aborted / relative):
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != size
                or prod._sha256_file(candidate) != digest
            ):
                raise BakeoffError("recovery aborted file hash mismatch")
    _validate_exact_file_authority(
        source_aborted,
        preserved_aborted,
        evidence_files,
        context="recovery aborted attempt",
    )
    derived_row_delta, derived_nano_delta = _validate_recovery_snapshots(
        source_report,
        source_aborted,
        preserved_aborted,
        aborted,
    )
    if recovery_nano != derived_nano_delta:
        raise BakeoffError("recovery Credit was not derived from snapshots")
    if derived_row_delta != 3 or derived_nano_delta != 283_155_000:
        raise BakeoffError("recovery attempt does not match the observed r3 interruption")
    if prod._sha256_file(path) != manifest_sha256:
        raise BakeoffError("recovery import manifest changed during validation")
    return {
        "path": str(path.resolve()),
        "sha256": manifest_sha256,
        "source_report": source_report,
        "source_report_path": str(source_report_path),
        "source_report_sha256": report_sha,
        "completed_ordinal": completed,
        "imported_run_ids": imported_ids,
        "historical_harness_identity": historical_for_evaluation,
        "historical_harness_identities": [
            historical_for_evaluation,
            *(
                [resume_historical_identity]
                if resume_historical_identity is not None
                else []
            ),
        ],
        "recovery_total_nano_aiu": derived_nano_delta,
        "recovery_ai_credits": derived_nano_delta / NANO_AIU_PER_CREDIT,
        "recovery_usage_row_delta": derived_row_delta,
        "aborted_attempt": aborted,
    }


def _require_v2_generation_ranges(
    generation_entries: Any,
) -> tuple[tuple[str, int, int, dict[str, tuple[str, str]], str], ...]:
    contracts = (
        ("r3", 1, 5, R3_HISTORICAL_HARNESS, "r3-historical-harness"),
        (
            "r5-prefix",
            6,
            22,
            R5_PREFIX_HISTORICAL_HARNESS,
            "r5-prefix-historical-harness",
        ),
    )
    if not isinstance(generation_entries, list) or len(generation_entries) != 2:
        raise BakeoffError("v2 historical harness generation set is invalid")
    if [
        (entry.get("name"), entry.get("first_ordinal"), entry.get("last_ordinal"))
        if isinstance(entry, dict)
        else None
        for entry in generation_entries
    ] != [(name, first, last) for name, first, last, _, _ in contracts]:
        raise BakeoffError("v2 historical harness generation ranges are not exact")
    return contracts


def _require_v3_generation_ranges(
    generation_entries: Any,
) -> tuple[tuple[str, int, int, dict[str, tuple[str, str]], str], ...]:
    contracts = (
        ("r3", 1, 5, R3_HISTORICAL_HARNESS, "r3-historical-harness"),
        (
            "r5-prefix",
            6,
            22,
            R5_PREFIX_HISTORICAL_HARNESS,
            "r5-prefix-historical-harness",
        ),
        (
            "r6-failed-thorough",
            23,
            23,
            R6_HISTORICAL_HARNESS,
            "r6-thorough-failure-historical-harness",
        ),
    )
    if not isinstance(generation_entries, list) or len(generation_entries) != 3:
        raise BakeoffError("v3 historical harness generation set is invalid")
    if [
        (entry.get("name"), entry.get("first_ordinal"), entry.get("last_ordinal"))
        if isinstance(entry, dict)
        else None
        for entry in generation_entries
    ] != [(name, first, last) for name, first, last, _, _ in contracts]:
        raise BakeoffError("v3 historical harness generation ranges are not exact")
    return contracts


def _load_recovery_import_v2_from_value(
    raw_root: Path,
    authority: dict[str, Any],
    path: Path,
    value: dict[str, Any],
    manifest_sha256: str,
    historical_resume_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed = value.get("resume_completed_ordinal")
    if isinstance(completed, bool) or not isinstance(completed, int) or completed != 22:
        raise BakeoffError("v2 recovery completed ordinal must be exactly 22")
    output_value = value.get("output_root")
    source_value = value.get("source_evidence_root")
    if (
        not isinstance(output_value, str)
        or not Path(output_value).is_absolute()
        or Path(output_value).resolve() != raw_root.resolve()
        or not isinstance(source_value, str)
        or not Path(source_value).is_absolute()
    ):
        raise BakeoffError("v2 recovery source/output root is invalid")
    source_root = Path(source_value).resolve()
    if (
        source_root == raw_root.resolve()
        or not source_root.is_dir()
        or _is_link_or_reparse(source_root)
    ):
        raise BakeoffError("v2 recovery source root is missing or aliases output")

    source_report_value = value.get("source_report_path")
    preserved_report_value = value.get("preserved_source_report_path")
    source_report_sha = value.get("source_report_sha256")
    if (
        not isinstance(source_report_value, str)
        or not Path(source_report_value).is_absolute()
        or not isinstance(preserved_report_value, str)
        or not Path(preserved_report_value).is_absolute()
        or not isinstance(source_report_sha, str)
        or SHA256_RE.fullmatch(source_report_sha) is None
        or source_report_sha != R5_RECOVERED_REPORT22_SHA256
    ):
        raise BakeoffError("v2 source report authority is invalid")
    source_report_path = Path(source_report_value).resolve()
    preserved_report_path = Path(preserved_report_value).resolve()
    if (
        preserved_report_path != (raw_root / "recovery" / "source-report-22.json").resolve()
        or not source_report_path.is_file()
        or _is_link_or_reparse(source_report_path)
        or not preserved_report_path.is_file()
        or _is_link_or_reparse(preserved_report_path)
        or prod._sha256_file(source_report_path) != source_report_sha
        or prod._sha256_file(preserved_report_path) != source_report_sha
        or source_report_path.read_bytes() != preserved_report_path.read_bytes()
    ):
        raise BakeoffError("v2 source report bytes/hash mismatch")
    source_report = _load_json(source_report_path)
    source_runs = source_report.get("runs")
    if (
        source_report.get("schema_version") != REPORT_SCHEMA
        or source_report.get("overall_status") != "IN_PROGRESS"
        or source_report.get("formal_aggregate_total_nano_aiu") != 25_404_921_500
        or source_report.get("recovery_total_nano_aiu") != 283_155_000
        or source_report.get("true_total_nano_aiu") != 25_688_076_500
        or source_report.get("winner") != "claude-haiku-4.5"
        or not isinstance(source_runs, list)
        or len(source_runs) != completed
        or [run.get("plan_ordinal") for run in source_runs]
        != list(range(1, completed + 1))
    ):
        raise BakeoffError("v2 source report is not the exact accepted prefix")

    provenance = value.get("source_failure_provenance")
    if not isinstance(provenance, dict):
        raise BakeoffError("v2 source failure provenance is missing")
    provenance_contract = (
        (
            "report21",
            source_root / "reports" / "report-21.json",
            raw_root / "recovery" / "source-r5-report-21.json",
            R5_REPORT21_SHA256,
        ),
        (
            "report22_stderr",
            source_root / "reports" / "report-22.stderr.log",
            raw_root / "recovery" / "source-r5-report-22.stderr.log",
            R5_REPORT22_STDERR_SHA256,
        ),
        (
            "report22_stdout",
            source_root / "reports" / "report-22.stdout.log",
            raw_root / "recovery" / "source-r5-report-22.stdout.log",
            EMPTY_SHA256,
        ),
    )
    validated_provenance: dict[str, Path] = {}
    for key, expected_source, expected_preserved, expected_sha in provenance_contract:
        item = provenance.get(key)
        if not isinstance(item, dict):
            raise BakeoffError("v2 source failure provenance entry is invalid")
        source_item = Path(str(item.get("source_path", ""))).resolve()
        preserved_item = Path(str(item.get("preserved_path", ""))).resolve()
        if (
            source_item != expected_source.resolve()
            or preserved_item != expected_preserved.resolve()
            or item.get("sha256") != expected_sha
            or not source_item.is_file()
            or _is_link_or_reparse(source_item)
            or not preserved_item.is_file()
            or _is_link_or_reparse(preserved_item)
            or prod._sha256_file(source_item) != expected_sha
            or prod._sha256_file(preserved_item) != expected_sha
            or source_item.read_bytes() != preserved_item.read_bytes()
        ):
            raise BakeoffError("v2 source failure provenance bytes mismatch")
        validated_provenance[key] = source_item
    if (source_root / "reports" / "report-22.json").exists():
        raise BakeoffError("v2 source unexpectedly contains a formal report-22")
    report21 = _load_json(validated_provenance["report21"])
    if (
        report21.get("schema_version") != REPORT_SCHEMA
        or report21.get("formal_aggregate_total_nano_aiu") != 23_967_783_500
        or report21.get("recovery_total_nano_aiu") != 283_155_000
        or report21.get("true_total_nano_aiu") != 24_250_938_500
        or report21.get("winner") != "claude-haiku-4.5"
        or not isinstance(report21.get("runs"), list)
        or len(report21["runs"]) != 21
        or source_runs[:21] != report21["runs"]
    ):
        raise BakeoffError("v2 recovered report-22 does not extend exact report-21")

    parent = value.get("parent_recovery")
    if not isinstance(parent, dict):
        raise BakeoffError("v2 parent recovery authority is missing")
    parent_manifest_value = parent.get("source_manifest_path")
    parent_preserved_manifest_value = parent.get("preserved_manifest_path")
    parent_manifest_sha = parent.get("source_manifest_sha256")
    if (
        not isinstance(parent_manifest_value, str)
        or not Path(parent_manifest_value).is_absolute()
        or Path(parent_manifest_value).resolve()
        != (source_root / "recovery-import.json").resolve()
        or parent_manifest_sha != R5_PARENT_RECOVERY_MANIFEST_SHA256
        or not isinstance(parent_preserved_manifest_value, str)
        or not Path(parent_preserved_manifest_value).is_absolute()
    ):
        raise BakeoffError("v2 parent recovery manifest authority is invalid")
    parent_manifest_path = Path(parent_manifest_value).resolve()
    parent_preserved_manifest = Path(parent_preserved_manifest_value).resolve()
    if (
        parent_preserved_manifest
        != (raw_root / "recovery" / "parent-r5-recovery-import.json").resolve()
        or not parent_manifest_path.is_file()
        or _is_link_or_reparse(parent_manifest_path)
        or not parent_preserved_manifest.is_file()
        or _is_link_or_reparse(parent_preserved_manifest)
        or prod._sha256_file(parent_manifest_path) != parent_manifest_sha
        or prod._sha256_file(parent_preserved_manifest) != parent_manifest_sha
        or parent_manifest_path.read_bytes() != parent_preserved_manifest.read_bytes()
    ):
        raise BakeoffError("v2 parent recovery manifest bytes mismatch")
    parent_recovery = _load_recovery_import(
        source_root, authority, parent_manifest_sha
    )
    try:
        parent_credit_nano = (
            Decimal(str(parent.get("recovery_ai_credits")))
            * NANO_AIU_PER_CREDIT
        )
    except (InvalidOperation, ValueError):
        parent_credit_nano = Decimal(-1)
    if (
        parent_recovery is None
        or parent_recovery["recovery_total_nano_aiu"] != 283_155_000
        or parent.get("recovery_total_nano_aiu") != 283_155_000
        or parent_credit_nano != 283_155_000
    ):
        raise BakeoffError("v2 inherited r3 recovery Credit mismatch")
    parent_source_directory_value = parent.get("source_recovery_directory")
    parent_preserved_directory_value = parent.get("preserved_recovery_directory")
    parent_files = parent.get("files")
    if (
        not isinstance(parent_source_directory_value, str)
        or not Path(parent_source_directory_value).is_absolute()
        or not isinstance(parent_preserved_directory_value, str)
        or not Path(parent_preserved_directory_value).is_absolute()
    ):
        raise BakeoffError("v2 inherited recovery directory authority is invalid")
    parent_source_directory = Path(parent_source_directory_value).resolve()
    parent_preserved_directory = Path(parent_preserved_directory_value).resolve()
    if (
        parent_source_directory != (source_root / "recovery").resolve()
        or parent_preserved_directory
        != (raw_root / "recovery" / "inherited-r5" / "recovery").resolve()
    ):
        raise BakeoffError("v2 inherited recovery directory mapping is invalid")
    _validate_exact_file_authority(
        parent_source_directory,
        parent_preserved_directory,
        parent_files,
        context="v2 inherited r5 recovery",
    )

    generation_entries = value.get("historical_harness_generations")
    generation_contracts = _require_v2_generation_ranges(generation_entries)
    validated_generations: list[tuple[int, int, dict[str, Any]]] = []
    for entry, (name, first, last, expected_harness, leaf) in zip(
        generation_entries, generation_contracts
    ):
        if (
            not isinstance(entry, dict)
            or entry.get("name") != name
            or entry.get("first_ordinal") != first
            or entry.get("last_ordinal") != last
            or not isinstance(entry.get("identity"), dict)
            or not isinstance(entry.get("source_directory"), str)
            or not Path(entry["source_directory"]).is_absolute()
            or not isinstance(entry.get("preserved_directory"), str)
            or not Path(entry["preserved_directory"]).is_absolute()
        ):
            raise BakeoffError("v2 historical harness generation metadata is invalid")
        source_archive = Path(entry["source_directory"]).resolve()
        preserved_archive = Path(entry["preserved_directory"]).resolve()
        if preserved_archive != (raw_root / "recovery" / leaf).resolve():
            raise BakeoffError("v2 historical harness preserved mapping is invalid")
        source_identity = _validate_versioned_harness_archive(
            entry["identity"],
            source_archive,
            expected_harness,
            context=f"v2 {name} source harness",
        )
        preserved_identity = _validate_versioned_harness_archive(
            entry["identity"],
            preserved_archive,
            expected_harness,
            context=f"v2 {name} preserved harness",
        )
        _validate_exact_file_authority(
            source_archive,
            preserved_archive,
            entry.get("files"),
            context=f"v2 {name} harness",
        )
        if source_identity["_validated_artifact_paths"].keys() != preserved_identity[
            "_validated_artifact_paths"
        ].keys():
            raise BakeoffError("v2 historical harness artifact role mismatch")
        validated_generations.append((first, last, preserved_identity))

    imported_runs = value.get("imported_runs")
    if not isinstance(imported_runs, list) or len(imported_runs) != completed:
        raise BakeoffError("v2 imported run count is invalid")
    imported_ids: set[str] = set()
    expected_source_leaves: set[str] = set()
    for ordinal, imported in enumerate(imported_runs, 1):
        if not isinstance(imported, dict) or imported.get("ordinal") != ordinal:
            raise BakeoffError("v2 imported run ordinal is invalid")
        contract = _canonical_run_contract(authority, ordinal)
        source_directory = Path(str(imported.get("source_directory", ""))).resolve()
        destination_directory = Path(
            str(imported.get("destination_directory", ""))
        ).resolve()
        if (
            imported.get("run_id") != contract["run_id"]
            or source_directory
            != (source_root / "runs" / contract["leaf"]).resolve()
            or destination_directory
            != (raw_root / "runs" / contract["leaf"]).resolve()
        ):
            raise BakeoffError("v2 imported run directory/identity is invalid")
        run = _load_json(destination_directory / "run.json")
        if not _run_matches_canonical_contract(run, contract, ordinal):
            raise BakeoffError("v2 imported run canonical mapping is invalid")
        generation = next(
            identity
            for first, last, identity in validated_generations
            if first <= ordinal <= last
        )
        if not _identity_matches_run(run, generation):
            raise BakeoffError("v2 imported run historical generation mismatch")
        run_id = run["run_id"]
        if run_id in imported_ids:
            raise BakeoffError("v2 imported run ID is duplicated")
        imported_ids.add(run_id)
        expected_source_leaves.add(contract["leaf"])
        _validate_exact_file_authority(
            source_directory,
            destination_directory,
            imported.get("files"),
            context=f"v2 imported run {ordinal}",
        )
    source_runs_root = source_root / "runs"
    source_children = list(source_runs_root.iterdir())
    if (
        any(not child.is_dir() or _is_link_or_reparse(child) for child in source_children)
        or {child.name for child in source_children} != expected_source_leaves
    ):
        raise BakeoffError("v2 source formal run set is not exact")

    resume_identity = value.get("resume_harness_identity")
    if not isinstance(resume_identity, dict):
        raise BakeoffError("v2 resume harness identity is missing")
    for kind in ("runner", "collector", "authority"):
        live_path = resume_identity.get(f"{kind}_path")
        live_sha = resume_identity.get(f"{kind}_sha256")
        if historical_resume_identity is None:
            valid_identity = (
                isinstance(live_path, str)
                and Path(live_path).is_absolute()
                and Path(live_path).is_file()
                and not _is_link_or_reparse(Path(live_path))
                and isinstance(live_sha, str)
                and SHA256_RE.fullmatch(live_sha) is not None
                and prod._sha256_file(Path(live_path)) == live_sha
            )
        else:
            valid_identity = (
                isinstance(live_path, str)
                and Path(live_path).is_absolute()
                and live_path == historical_resume_identity.get(f"{kind}_path")
                and isinstance(live_sha, str)
                and live_sha == historical_resume_identity.get(f"{kind}_sha256")
            )
        if not valid_identity:
            raise BakeoffError("v2 resume harness identity mismatch")
    production_identity = value.get("production_identity")
    if not isinstance(production_identity, dict):
        raise BakeoffError("v2 production identity is missing")
    for kind in ("launcher", "manifest"):
        live_path = production_identity.get(f"{kind}_path")
        live_sha = production_identity.get(f"{kind}_sha256")
        if (
            not isinstance(live_path, str)
            or not Path(live_path).is_absolute()
            or not Path(live_path).is_file()
            or _is_link_or_reparse(Path(live_path))
            or not isinstance(live_sha, str)
            or SHA256_RE.fullmatch(live_sha) is None
            or prod._sha256_file(Path(live_path)) != live_sha
        ):
            raise BakeoffError("v2 production live identity mismatch")
    if prod._sha256_file(path) != manifest_sha256:
        raise BakeoffError("v2 recovery import manifest changed during validation")
    historical_identities = [identity for _, _, identity in validated_generations]
    return {
        "path": str(path.resolve()),
        "sha256": manifest_sha256,
        "source_report": source_report,
        "source_report_path": str(source_report_path),
        "source_report_sha256": source_report_sha,
        "completed_ordinal": completed,
        "imported_run_ids": imported_ids,
        "historical_harness_identity": historical_identities[0],
        "historical_harness_identities": historical_identities,
        "recovery_total_nano_aiu": 283_155_000,
        "recovery_ai_credits": 0.283155,
        "recovery_usage_row_delta": parent_recovery["recovery_usage_row_delta"],
        "aborted_attempt": parent_recovery["aborted_attempt"],
    }


def _load_recovery_import_v3_from_value(
    raw_root: Path,
    authority: dict[str, Any],
    path: Path,
    value: dict[str, Any],
    manifest_sha256: str,
    *,
    boundary_wrapper_rescore: bool = False,
) -> dict[str, Any]:
    if value.get("resume_completed_ordinal") != 23:
        raise BakeoffError("v3 recovery completed ordinal must be exactly 23")
    if value.get("retry_plan") != "retry_failed_thorough_once_v1":
        raise BakeoffError("v3 retry plan is invalid")
    output_value = value.get("output_root")
    source_value = value.get("source_evidence_root")
    if (
        not isinstance(output_value, str)
        or not Path(output_value).is_absolute()
        or Path(output_value).resolve() != raw_root.resolve()
        or not isinstance(source_value, str)
        or not Path(source_value).is_absolute()
    ):
        raise BakeoffError("v3 recovery source/output root is invalid")
    source_root = Path(source_value).resolve()
    if (
        source_root == raw_root.resolve()
        or not source_root.is_dir()
        or _is_link_or_reparse(source_root)
    ):
        raise BakeoffError("v3 recovery source root is missing or aliases output")

    source_report_value = value.get("source_report_path")
    preserved_report_value = value.get("preserved_source_report_path")
    source_report_sha = value.get("source_report_sha256")
    if (
        not isinstance(source_report_value, str)
        or not Path(source_report_value).is_absolute()
        or not isinstance(preserved_report_value, str)
        or not Path(preserved_report_value).is_absolute()
        or source_report_sha != R6_REPORT23_SHA256
    ):
        raise BakeoffError("v3 source report authority is invalid")
    source_report_path = Path(source_report_value).resolve()
    preserved_report_path = Path(preserved_report_value).resolve()
    if (
        source_report_path != (source_root / "reports" / "report-23.json").resolve()
        or preserved_report_path
        != (raw_root / "recovery" / "source-report-23.json").resolve()
        or not source_report_path.is_file()
        or _is_link_or_reparse(source_report_path)
        or not preserved_report_path.is_file()
        or _is_link_or_reparse(preserved_report_path)
        or prod._sha256_file(source_report_path) != source_report_sha
        or prod._sha256_file(preserved_report_path) != source_report_sha
        or source_report_path.read_bytes() != preserved_report_path.read_bytes()
    ):
        raise BakeoffError("v3 source report bytes/hash mismatch")
    source_report = _load_json(source_report_path)
    source_runs = source_report.get("runs")
    if (
        source_report.get("schema_version") != REPORT_SCHEMA
        or source_report.get("authority_sha256")
        != R3_HISTORICAL_HARNESS["authority"][1]
        or source_report.get("overall_status") != "FAIL"
        or source_report.get("failures") != []
        or source_report.get("formal_aggregate_total_nano_aiu") != 28_994_283_500
        or source_report.get("recovery_total_nano_aiu") != 283_155_000
        or source_report.get("true_total_nano_aiu") != 29_277_438_500
        or source_report.get("winner") != "claude-haiku-4.5"
        or not isinstance(source_runs, list)
        or len(source_runs) != 23
        or [run.get("plan_ordinal") for run in source_runs] != list(range(1, 24))
    ):
        raise BakeoffError("v3 source report is not the exact failed-thorough prefix")
    failed_run = source_runs[22]
    if (
        failed_run.get("run_id") != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO"
        or failed_run.get("case_kind") != "thorough"
        or failed_run.get("status") != "FAIL"
        or failed_run.get("requested_model") != "auto"
        or failed_run.get("prompt_sha256") != R6_THOROUGH_PROMPT_SHA256
        or failed_run.get("total_nano_aiu") != 3_589_362_000
        or failed_run.get("usage_row_delta") != 3
        or sorted(failed_run.get("failures", []))
        != sorted(
            [
                "thorough_satellite_buzz_missing",
                "thorough_technology_provision_missing",
            ]
        )
    ):
        raise BakeoffError("v3 source thorough failure is not the accepted retry target")

    report22_entry = value.get("source_report22")
    if not isinstance(report22_entry, dict):
        raise BakeoffError("v3 report-22 prefix authority is missing")
    source_report22 = (source_root / "reports" / "report-22.json").resolve()
    preserved_report22 = (raw_root / "recovery" / "source-r6-report-22.json").resolve()
    if (
        Path(str(report22_entry.get("source_path", ""))).resolve()
        != source_report22
        or Path(str(report22_entry.get("preserved_path", ""))).resolve()
        != preserved_report22
        or report22_entry.get("sha256") != R6_REPORT22_SHA256
        or not source_report22.is_file()
        or not preserved_report22.is_file()
        or _is_link_or_reparse(source_report22)
        or _is_link_or_reparse(preserved_report22)
        or prod._sha256_file(source_report22) != R6_REPORT22_SHA256
        or prod._sha256_file(preserved_report22) != R6_REPORT22_SHA256
        or source_report22.read_bytes() != preserved_report22.read_bytes()
    ):
        raise BakeoffError("v3 report-22 prefix bytes/hash mismatch")
    report22 = _load_json(source_report22)
    if (
        not isinstance(report22.get("runs"), list)
        or len(report22["runs"]) != 22
        or report22["runs"] != source_runs[:22]
    ):
        raise BakeoffError("v3 report-23 does not extend exact report-22")

    generation_entries = value.get("historical_harness_generations")
    generation_contracts = _require_v3_generation_ranges(generation_entries)
    validated_generations: list[tuple[int, int, dict[str, Any]]] = []
    for entry, (name, first, last, expected_harness, leaf) in zip(
        generation_entries, generation_contracts
    ):
        if (
            not isinstance(entry, dict)
            or entry.get("name") != name
            or entry.get("first_ordinal") != first
            or entry.get("last_ordinal") != last
            or not isinstance(entry.get("identity"), dict)
            or not isinstance(entry.get("source_directory"), str)
            or not Path(entry["source_directory"]).is_absolute()
            or not isinstance(entry.get("preserved_directory"), str)
            or not Path(entry["preserved_directory"]).is_absolute()
        ):
            raise BakeoffError("v3 historical harness generation metadata is invalid")
        source_archive = Path(entry["source_directory"]).resolve()
        preserved_archive = Path(entry["preserved_directory"]).resolve()
        if preserved_archive != (raw_root / "recovery" / leaf).resolve():
            raise BakeoffError("v3 historical harness preserved mapping is invalid")
        source_identity = _validate_versioned_harness_archive(
            entry["identity"],
            source_archive,
            expected_harness,
            context=f"v3 {name} source harness",
        )
        preserved_identity = _validate_versioned_harness_archive(
            entry["identity"],
            preserved_archive,
            expected_harness,
            context=f"v3 {name} preserved harness",
        )
        _validate_exact_file_authority(
            source_archive,
            preserved_archive,
            entry.get("files"),
            context=f"v3 {name} harness",
        )
        if source_identity["_validated_artifact_paths"].keys() != preserved_identity[
            "_validated_artifact_paths"
        ].keys():
            raise BakeoffError("v3 historical harness artifact role mismatch")
        validated_generations.append((first, last, preserved_identity))

    r6_historical_identity = validated_generations[-1][2]
    parent = value.get("parent_recovery")
    if not isinstance(parent, dict):
        raise BakeoffError("v3 parent recovery authority is missing")
    parent_manifest_path = (source_root / "recovery-import.json").resolve()
    parent_preserved_manifest = (
        raw_root / "recovery" / "parent-r6-recovery-import.json"
    ).resolve()
    if (
        Path(str(parent.get("source_manifest_path", ""))).resolve()
        != parent_manifest_path
        or Path(str(parent.get("preserved_manifest_path", ""))).resolve()
        != parent_preserved_manifest
        or parent.get("source_manifest_sha256")
        != R6_PARENT_RECOVERY_MANIFEST_SHA256
        or not parent_manifest_path.is_file()
        or not parent_preserved_manifest.is_file()
        or _is_link_or_reparse(parent_manifest_path)
        or _is_link_or_reparse(parent_preserved_manifest)
        or prod._sha256_file(parent_manifest_path)
        != R6_PARENT_RECOVERY_MANIFEST_SHA256
        or prod._sha256_file(parent_preserved_manifest)
        != R6_PARENT_RECOVERY_MANIFEST_SHA256
        or parent_manifest_path.read_bytes() != parent_preserved_manifest.read_bytes()
    ):
        raise BakeoffError("v3 parent recovery manifest bytes/hash mismatch")
    parent_value, parent_sha = _load_anchored_json(
        parent_manifest_path, R6_PARENT_RECOVERY_MANIFEST_SHA256
    )
    if parent_value.get("schema_version") != RECOVERY_IMPORT_SCHEMA_V2:
        raise BakeoffError("v3 parent is not the anchored r6 v2 recovery")
    parent_recovery = _load_recovery_import_v2_from_value(
        source_root,
        authority,
        parent_manifest_path,
        parent_value,
        parent_sha,
        historical_resume_identity=r6_historical_identity,
    )
    try:
        parent_credit_nano = (
            Decimal(str(parent.get("recovery_ai_credits")))
            * NANO_AIU_PER_CREDIT
        )
    except (InvalidOperation, ValueError):
        parent_credit_nano = Decimal(-1)
    if (
        parent_recovery["recovery_total_nano_aiu"] != 283_155_000
        or parent.get("recovery_total_nano_aiu") != 283_155_000
        or parent_credit_nano != 283_155_000
    ):
        raise BakeoffError("v3 inherited recovery Credit mismatch")
    parent_source_directory = Path(
        str(parent.get("source_recovery_directory", ""))
    ).resolve()
    parent_preserved_directory = Path(
        str(parent.get("preserved_recovery_directory", ""))
    ).resolve()
    if (
        parent_source_directory != (source_root / "recovery").resolve()
        or parent_preserved_directory
        != (raw_root / "recovery" / "inherited-r6" / "recovery").resolve()
    ):
        raise BakeoffError("v3 inherited recovery directory mapping is invalid")
    _validate_exact_file_authority(
        parent_source_directory,
        parent_preserved_directory,
        parent.get("files"),
        context="v3 inherited r6 recovery",
    )

    imported_runs = value.get("imported_runs")
    if not isinstance(imported_runs, list) or len(imported_runs) != 23:
        raise BakeoffError("v3 imported run count is invalid")
    imported_ids: set[str] = set()
    expected_source_leaves: set[str] = set()
    for ordinal, imported in enumerate(imported_runs, 1):
        if not isinstance(imported, dict) or imported.get("ordinal") != ordinal:
            raise BakeoffError("v3 imported run ordinal is invalid")
        contract = _canonical_run_contract(authority, ordinal, retry_plan=True)
        source_directory = Path(str(imported.get("source_directory", ""))).resolve()
        destination_directory = Path(
            str(imported.get("destination_directory", ""))
        ).resolve()
        if (
            imported.get("run_id") != contract["run_id"]
            or source_directory
            != (source_root / "runs" / contract["leaf"]).resolve()
            or destination_directory
            != (raw_root / "runs" / contract["leaf"]).resolve()
        ):
            raise BakeoffError("v3 imported run directory/identity is invalid")
        run = _load_json(destination_directory / "run.json")
        if not _run_matches_canonical_contract(run, contract, ordinal):
            raise BakeoffError("v3 imported run canonical mapping is invalid")
        generation = next(
            identity
            for first, last, identity in validated_generations
            if first <= ordinal <= last
        )
        if not _identity_matches_run(run, generation):
            raise BakeoffError("v3 imported run historical generation mismatch")
        if run["run_id"] in imported_ids:
            raise BakeoffError("v3 imported run ID is duplicated")
        imported_ids.add(run["run_id"])
        expected_source_leaves.add(contract["leaf"])
        _validate_exact_file_authority(
            source_directory,
            destination_directory,
            imported.get("files"),
            context=f"v3 imported run {ordinal}",
        )
        if ordinal == 23:
            file_authority = imported.get("files")
            if not isinstance(file_authority, list) or {
                entry.get("relative_path"): entry.get("sha256")
                for entry in file_authority
                if isinstance(entry, dict)
            } != R6_FAILED_THOROUGH_FILES:
                raise BakeoffError("v3 failed thorough raw file authority mismatch")
    source_children = list((source_root / "runs").iterdir())
    if (
        any(not child.is_dir() or _is_link_or_reparse(child) for child in source_children)
        or {child.name for child in source_children} != expected_source_leaves
    ):
        raise BakeoffError("v3 source formal run set is not exact")

    superseded = value.get("superseded_auxiliary_attempt")
    if (
        not isinstance(superseded, dict)
        or superseded.get("ordinal") != 23
        or superseded.get("run_id")
        != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO"
        or superseded.get("status") != "FAIL"
        or sorted(superseded.get("failures", []))
        != sorted(failed_run["failures"])
        or superseded.get("prompt_sha256") != R6_THOROUGH_PROMPT_SHA256
        or superseded.get("total_nano_aiu") != 3_589_362_000
        or superseded.get("classification")
        != "SUPERSEDED_ONLY_IF_ORDINAL_24_PASSES"
    ):
        raise BakeoffError("v3 superseded failure contract is invalid")
    if value.get("inherited_aborted_attempt") != parent_recovery["aborted_attempt"]:
        raise BakeoffError("v3 inherited aborted attempt differs from parent")

    resume_identity = value.get("resume_harness_identity")
    if not isinstance(resume_identity, dict):
        raise BakeoffError("v3 resume harness identity is missing")
    for kind in ("runner", "collector", "authority"):
        live_path = resume_identity.get(f"{kind}_path")
        live_sha = resume_identity.get(f"{kind}_sha256")
        stale_collector_match = (
            boundary_wrapper_rescore
            and kind == "collector"
            and isinstance(live_path, str)
            and Path(live_path).is_absolute()
            and isinstance(live_sha, str)
            and _matches_boundary_wrapper_reaggregation_collector(
                Path(live_path), live_sha, live_sha
            )
        )
        if (
            not isinstance(live_path, str)
            or not Path(live_path).is_absolute()
            or not Path(live_path).is_file()
            or _is_link_or_reparse(Path(live_path))
            or not isinstance(live_sha, str)
            or SHA256_RE.fullmatch(live_sha) is None
            or (
                prod._sha256_file(Path(live_path)) != live_sha
                and not stale_collector_match
            )
        ):
            raise BakeoffError("v3 resume harness live identity mismatch")
    production_identity = value.get("production_identity")
    if not isinstance(production_identity, dict):
        raise BakeoffError("v3 production identity is missing")
    for kind in ("launcher", "manifest"):
        live_path = production_identity.get(f"{kind}_path")
        live_sha = production_identity.get(f"{kind}_sha256")
        if (
            not isinstance(live_path, str)
            or not Path(live_path).is_absolute()
            or not Path(live_path).is_file()
            or _is_link_or_reparse(Path(live_path))
            or not isinstance(live_sha, str)
            or SHA256_RE.fullmatch(live_sha) is None
            or prod._sha256_file(Path(live_path)) != live_sha
        ):
            raise BakeoffError("v3 production live identity mismatch")
    if prod._sha256_file(path) != manifest_sha256:
        raise BakeoffError("v3 recovery import manifest changed during validation")
    historical_identities = [identity for _, _, identity in validated_generations]
    return {
        "path": str(path.resolve()),
        "sha256": manifest_sha256,
        "source_report": source_report,
        "source_report_path": str(source_report_path),
        "source_report_sha256": source_report_sha,
        "completed_ordinal": 23,
        "imported_run_ids": imported_ids,
        "historical_harness_identity": historical_identities[0],
        "historical_harness_identities": historical_identities,
        "recovery_total_nano_aiu": 283_155_000,
        "recovery_ai_credits": 0.283155,
        "recovery_usage_row_delta": parent_recovery["recovery_usage_row_delta"],
        "aborted_attempt": parent_recovery["aborted_attempt"],
        "retry_plan": "retry_failed_thorough_once_v1",
        "superseded_auxiliary_attempt": superseded,
    }


def _validate_r7_preinference_semantics(
    raw_run: dict[str, Any],
    evaluated_run: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    mutation: dict[str, Any],
    stderr: str,
    copilot_bytes: bytes,
    otel_bytes: bytes,
) -> None:
    """Prove that r7 stopped in CLI argument parsing before model inference."""
    if (
        raw_run.get("schema_version") != RUN_SCHEMA
        or raw_run.get("plan_ordinal") != 24
        or raw_run.get("run_id")
        != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2"
        or raw_run.get("case_kind") != "thorough"
        or raw_run.get("candidate_model") != ""
        or raw_run.get("requested_model") != "auto"
        or raw_run.get("attempt") != 2
        or raw_run.get("execution_state") != "executed"
        or raw_run.get("help_listed") is not True
        or raw_run.get("prompt_sha256") != R6_THOROUGH_PROMPT_SHA256
        or raw_run.get("fresh_session") is not True
        or raw_run.get("retry_count") != 1
        or raw_run.get("retry_of_ordinal") != 23
        or raw_run.get("retry_of_run_id")
        != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO"
        or raw_run.get("max_ai_credits") != 12
        or raw_run.get("runner_sha256") != R7_RUNNER_SHA256
        or raw_run.get("collector_sha256") != R7_COLLECTOR_SHA256
        or raw_run.get("authority_sha256")
        != R3_HISTORICAL_HARNESS["authority"][1]
        or raw_run.get("exit_code") != 1
        or raw_run.get("timed_out") is not False
        or raw_run.get("process_tree_terminated") is not False
        or raw_run.get("stdout_bytes") != 0
        or raw_run.get("stderr_bytes") != 139
    ):
        raise BakeoffError("r7 pre-inference raw run contract is invalid")
    row_delta, nano_delta, snapshot_failures = _snapshot_delta(before, after)
    if (
        snapshot_failures
        or row_delta != 0
        or nano_delta != 0
        or before.get("session_store_exists") is not False
        or after.get("session_store_exists") is not False
        or before.get("row_count") != 0
        or after.get("row_count") != 0
        or before.get("total_nano_aiu") != 0
        or after.get("total_nano_aiu") != 0
        or before.get("maximum_usage_event_id") is not None
        or after.get("maximum_usage_event_id") is not None
    ):
        raise BakeoffError("r7 pre-inference usage snapshots do not prove zero Credit")
    if copilot_bytes != b"" or otel_bytes != b"":
        raise BakeoffError("r7 pre-inference attempt contains model/event output")
    if stderr.strip() != R7_PREINFERENCE_STDERR:
        raise BakeoffError("r7 pre-inference CLI floor error is not exact")
    if (
        mutation.get("schema_version")
        != "lrr-agent003-cli-model-mutation-audit-v1"
        or mutation.get("tier") != "thorough"
        or mutation.get("requested_model") != "auto"
        or mutation.get("production_artifacts_modified") is not False
    ):
        raise BakeoffError("r7 pre-inference model mutation audit is invalid")
    if (
        evaluated_run.get("plan_ordinal") != 24
        or evaluated_run.get("run_id")
        != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2"
        or evaluated_run.get("case_kind") != "thorough"
        or evaluated_run.get("candidate_model") != ""
        or evaluated_run.get("attempt") != 2
        or evaluated_run.get("execution_state") != "executed"
        or evaluated_run.get("prompt_sha256") != R6_THOROUGH_PROMPT_SHA256
        or evaluated_run.get("status") != "FAIL"
        or sorted(evaluated_run.get("failures", []))
        != R7_PREINFERENCE_REPORT_FAILURES
        or evaluated_run.get("ai_credits") != 0.0
        or evaluated_run.get("total_nano_aiu") != 0
        or evaluated_run.get("usage_row_delta") != 0
        or evaluated_run.get("usage_nano_aiu_delta") != 0
        or evaluated_run.get("assistant_message_count") != 0
        or evaluated_run.get("session_id") is not None
        or evaluated_run.get("requested_model") != "auto"
        or evaluated_run.get("resolved_models") != []
        or evaluated_run.get("otel_response_models") != []
        or evaluated_run.get("search_calls") != 0
        or evaluated_run.get("evidence_calls") != 0
        or evaluated_run.get("maximum_tool_result_bytes") != 0
        or evaluated_run.get("assistant_response_sha256") is not None
    ):
        raise BakeoffError("r7 pre-inference evaluated run contract is invalid")


def _load_recovery_import_v4_from_value(
    raw_root: Path,
    authority: dict[str, Any],
    path: Path,
    value: dict[str, Any],
    manifest_sha256: str,
    *,
    boundary_wrapper_rescore: bool = False,
) -> dict[str, Any]:
    if (
        value.get("resume_completed_ordinal") != 23
        or value.get("retry_plan")
        != "retry_failed_thorough_once_after_preinference_cli_floor_v2"
        or Path(str(value.get("output_root", ""))).resolve()
        != raw_root.resolve()
    ):
        raise BakeoffError("v4 recovery root/retry contract is invalid")

    parent = value.get("parent_v3_recovery")
    if not isinstance(parent, dict):
        raise BakeoffError("v4 parent v3 recovery is missing")
    parent_path = Path(str(parent.get("preserved_manifest_path", ""))).resolve()
    parent_sha = parent.get("manifest_sha256")
    if (
        parent_path
        != (raw_root / "recovery" / "parent-r8-recovery-import-v3.json").resolve()
        or not parent_path.is_file()
        or _is_link_or_reparse(parent_path)
        or not isinstance(parent_sha, str)
        or SHA256_RE.fullmatch(parent_sha) is None
    ):
        raise BakeoffError("v4 parent v3 recovery path/hash metadata is invalid")
    parent_value, validated_parent_sha = _load_anchored_json(parent_path, parent_sha)
    if parent_value.get("schema_version") != RECOVERY_IMPORT_SCHEMA_V3:
        raise BakeoffError("v4 parent is not a v3 recovery manifest")
    parent_recovery = _load_recovery_import_v3_from_value(
        raw_root,
        authority,
        parent_path,
        parent_value,
        validated_parent_sha,
        boundary_wrapper_rescore=boundary_wrapper_rescore,
    )

    budget = value.get("logical_budget_contract")
    if budget != {
        "cli_minimum_max_ai_credits": 30,
        "thorough_logical_max_ai_credits": 12,
        "boundary_logical_max_ai_credits": 8,
        "true_total_before_retry_nano_aiu": 29_277_438_500,
        "maximum_final_true_total_nano_aiu": 49_277_438_500,
        "aggregate_cap_nano_aiu": 50_000_000_000,
        "cli_limit_is_transport_guard_not_logical_budget": True,
    }:
        raise BakeoffError("v4 logical/CLI Credit budget contract is invalid")

    pre = value.get("preinference_attempt")
    if not isinstance(pre, dict):
        raise BakeoffError("v4 r7 pre-inference authority is missing")
    source_root = Path(str(pre.get("source_evidence_root", ""))).resolve()
    if (
        not source_root.is_dir()
        or _is_link_or_reparse(source_root)
        or source_root == raw_root.resolve()
    ):
        raise BakeoffError("v4 r7 pre-inference source root is invalid")
    source_report23 = (source_root / "reports" / "report-23.json").resolve()
    source_report24 = (source_root / "reports" / "report-24.json").resolve()
    source_parent_manifest = (source_root / "recovery-import.json").resolve()
    preserved_report23 = (
        raw_root / "recovery" / "r7-preinference" / "report-23.json"
    ).resolve()
    preserved_report24 = (
        raw_root / "recovery" / "r7-preinference" / "report-24.json"
    ).resolve()
    preserved_parent_manifest = (
        raw_root / "recovery" / "r7-preinference" / "recovery-import.json"
    ).resolve()
    anchored_files = (
        (
            "source_report23_path",
            "preserved_report23_path",
            "report23_sha256",
            source_report23,
            preserved_report23,
            R7_REPORT23_SHA256,
        ),
        (
            "source_report24_path",
            "preserved_report24_path",
            "report24_sha256",
            source_report24,
            preserved_report24,
            R7_REPORT24_SHA256,
        ),
        (
            "source_parent_manifest_path",
            "preserved_parent_manifest_path",
            "parent_manifest_sha256",
            source_parent_manifest,
            preserved_parent_manifest,
            R7_RECOVERY_MANIFEST_SHA256,
        ),
    )
    for source_key, preserved_key, sha_key, source, preserved, expected_sha in anchored_files:
        if (
            Path(str(pre.get(source_key, ""))).resolve() != source
            or Path(str(pre.get(preserved_key, ""))).resolve() != preserved
            or pre.get(sha_key) != expected_sha
            or not source.is_file()
            or not preserved.is_file()
            or _is_link_or_reparse(source)
            or _is_link_or_reparse(preserved)
            or prod._sha256_file(source) != expected_sha
            or prod._sha256_file(preserved) != expected_sha
            or source.read_bytes() != preserved.read_bytes()
        ):
            raise BakeoffError(f"v4 r7 anchored provenance mismatch: {sha_key}")

    report24 = _load_json(source_report24)
    report24_runs = report24.get("runs")
    if (
        report24.get("schema_version") != REPORT_SCHEMA
        or report24.get("authority_sha256")
        != R3_HISTORICAL_HARNESS["authority"][1]
        or report24.get("overall_status") != "STOP_CREDIT_OR_EVIDENCE"
        or report24.get("failures") != ["aggregate_credit_unknown"]
        or report24.get("formal_aggregate_total_nano_aiu") != 28_994_283_500
        or report24.get("recovery_total_nano_aiu") != 283_155_000
        or report24.get("true_total_nano_aiu") != 29_277_438_500
        or report24.get("winner") != "claude-haiku-4.5"
        or report24.get("retry_plan") != "retry_failed_thorough_once_v1"
        or not isinstance(report24_runs, list)
        or len(report24_runs) != 24
        or report24_runs[:23] != parent_recovery["source_report"]["runs"]
        or report24.get("recovery_import", {}).get("sha256")
        != R7_RECOVERY_MANIFEST_SHA256
    ):
        raise BakeoffError("v4 r7 report-24 is not the exact pre-inference failure")

    source_run = (
        source_root
        / "runs"
        / "24-LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2"
    ).resolve()
    preserved_run = (
        raw_root
        / "recovery"
        / "r7-preinference"
        / "24-LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2"
    ).resolve()
    if (
        Path(str(pre.get("source_run_directory", ""))).resolve() != source_run
        or Path(str(pre.get("preserved_run_directory", ""))).resolve()
        != preserved_run
    ):
        raise BakeoffError("v4 r7 pre-inference run mapping is invalid")
    _validate_exact_file_authority(
        source_run,
        preserved_run,
        pre.get("files"),
        context="v4 r7 pre-inference run",
    )
    file_authority = {
        entry.get("relative_path"): entry.get("sha256")
        for entry in pre.get("files", [])
        if isinstance(entry, dict)
    }
    if file_authority != R7_RUN24_FILES:
        raise BakeoffError("v4 r7 pre-inference file hash authority mismatch")
    for root in (source_run, preserved_run):
        directories = [child for child in root.iterdir() if child.is_dir()]
        if (
            [child.name for child in directories] != ["copilot-logs"]
            or list(directories[0].iterdir())
        ):
            raise BakeoffError("v4 r7 pre-inference log directory is not exact/empty")

    source_run_roots = list((source_root / "runs").iterdir())
    expected_source_leaves = {
        _canonical_run_contract(authority, ordinal, retry_plan=True)["leaf"]
        for ordinal in range(1, 25)
    }
    if (
        any(not child.is_dir() or _is_link_or_reparse(child) for child in source_run_roots)
        or {child.name for child in source_run_roots} != expected_source_leaves
    ):
        raise BakeoffError("v4 r7 source run set is not the exact 24-run prefix")

    raw_run = _load_json(source_run / "run.json")
    before = _load_json(source_run / "usage-before.json")
    after = _load_json(source_run / "usage-after.json")
    mutation = _load_json(source_run / "temporary-model-mutation.json")
    _validate_r7_preinference_semantics(
        raw_run,
        report24_runs[23],
        before,
        after,
        mutation,
        _read_text(source_run / "stderr.log"),
        (source_run / "copilot.jsonl").read_bytes(),
        (source_run / "otel.jsonl").read_bytes(),
    )
    if (
        pre.get("classification")
        != "PREINFERENCE_CLI_MINIMUM_VALIDATION_NOT_METERED_NOT_RETRY"
        or pre.get("formal_run_count_delta") != 0
        or pre.get("metered_retry_count_delta") != 0
        or pre.get("total_nano_aiu") != 0
        or pre.get("usage_row_delta") != 0
        or pre.get("prompt_sha256") != R6_THOROUGH_PROMPT_SHA256
        or pre.get("requested_model") != "auto"
        or pre.get("rejected_cli_max_ai_credits") != 12
        or pre.get("observed_cli_minimum_max_ai_credits") != 30
    ):
        raise BakeoffError("v4 r7 pre-inference classification is invalid")
    if prod._sha256_file(path) != manifest_sha256:
        raise BakeoffError("v4 recovery import manifest changed during validation")
    return {
        **parent_recovery,
        "path": str(path.resolve()),
        "sha256": manifest_sha256,
        "retry_plan": "retry_failed_thorough_once_after_preinference_cli_floor_v2",
        "preinference_attempt": pre,
        "logical_budget_contract": budget,
    }


def _normalized_search_query(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _contains_contract_term(value: str, terms: tuple[str, ...]) -> bool:
    normalized = " ".join(value.split()).casefold()
    return any(term.casefold() in normalized for term in terms)


def _contains_narrow_follow_up_target(value: str, terms: tuple[str, ...]) -> bool:
    normalized = " ".join(value.split()).casefold()
    normalized_terms = {term.casefold() for term in terms}
    if normalized_terms == {"確定", "執行", "7%"}:
        return "7%" in normalized or (
            any(term in normalized for term in ("確定", "執行"))
            and any(term in normalized for term in ("予算", "増額"))
        )
    return _contains_contract_term(normalized, terms)


def _omitted_inspectable_ids(
    value: Any,
    *,
    relevance_terms: tuple[str, ...] = (),
) -> set[str]:
    if not isinstance(value, dict):
        return set()
    inspectable = {
        str(item)
        for item in value.get("inspectable_evidence_ids") or []
        if isinstance(item, str) and item
    }
    if not inspectable:
        return set()
    omitted: set[str] = set()
    for item in value.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "")
        if (
            item_id in inspectable
            and re.search(r"(?:…|\.\.\.|\btruncated\b)", text, re.IGNORECASE)
            and (
                not relevance_terms
                or _contains_contract_term(text, relevance_terms)
            )
        ):
            omitted.add(item_id)
    notices = " ".join(str(item) for item in value.get("notices") or [])
    # Bookkeeping notices such as ``locator_hints_omitted`` do not mean that
    # evidence content was omitted.  Only explicit excerpt/content omission
    # notices make every inspectable id require follow-up.
    if re.search(
        r"(?:ellipsis|truncated|evidence[_ -]excerpt[_ -]omitted)",
        notices,
        re.IGNORECASE,
    ):
        if not relevance_terms:
            omitted.update(inspectable)
    return omitted


def _event_tool_evidence(
    events: list[dict[str, Any]],
    *,
    omitted_relevance_terms: tuple[str, ...] = (),
    narrow_follow_up_query_terms: tuple[str, ...] = (),
) -> dict[str, Any]:
    failures: list[str] = []
    starts: dict[str, str] = {}
    start_arguments: dict[str, dict[str, Any]] = {}
    call_order: list[str] = []
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
            call_order.append(call_id)
            arguments = data.get("arguments")
            start_arguments[call_id] = (
                dict(arguments) if isinstance(arguments, dict) else {}
            )
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

    search_call_ids = [
        call_id
        for call_id in call_order
        if starts.get(call_id) == prod.SEARCH_TOOL
    ]
    routing_call_ids: list[str] = []
    selected_call_ids: list[str] = []
    selected_databases: list[str] = []
    selected_queries: list[str] = []
    for call_id in search_call_ids:
        arguments = start_arguments.get(call_id, {})
        database = arguments.get("database")
        if isinstance(database, str) and database.strip():
            selected_call_ids.append(call_id)
            selected_databases.append(database.strip())
            selected_queries.append(
                _normalized_search_query(arguments.get("question"))
            )
        else:
            routing_call_ids.append(call_id)

    seen_queries: set[str] = set()
    duplicate_queries: set[str] = set()
    for query in selected_queries:
        if not query or query in seen_queries:
            duplicate_queries.add(query)
        seen_queries.add(query)

    call_positions = {
        call_id: index for index, call_id in enumerate(call_order)
    }
    omitted_follow_up: dict[str, bool] = {}
    omitted_ids_by_call: dict[str, list[str]] = {}
    for call_id in selected_call_ids:
        omitted_ids = _omitted_inspectable_ids(
            structured.get(call_id),
            relevance_terms=omitted_relevance_terms,
        )
        if not omitted_ids:
            continue
        omitted_ids_by_call[call_id] = sorted(omitted_ids)
        source_query = _normalized_search_query(
            start_arguments.get(call_id, {}).get("question")
        )
        source_database = str(
            start_arguments.get(call_id, {}).get("database") or ""
        ).strip()
        satisfied = False
        for later_call_id in call_order[call_positions[call_id] + 1 :]:
            later_name = starts.get(later_call_id)
            later_arguments = start_arguments.get(later_call_id, {})
            if later_name == prod.EVIDENCE_TOOL:
                requested_ids = {
                    str(item)
                    for item in later_arguments.get("evidence_ids") or []
                    if isinstance(item, str)
                }
                if omitted_ids & requested_ids:
                    satisfied = True
                    break
            elif later_name == prod.SEARCH_TOOL:
                later_database = later_arguments.get("database")
                later_query = _normalized_search_query(
                    later_arguments.get("question")
                )
                if (
                    isinstance(later_database, str)
                    and later_database.strip() == source_database
                    and later_query
                    and later_query != source_query
                    and (
                        not narrow_follow_up_query_terms
                        or _contains_narrow_follow_up_target(
                            later_query,
                            narrow_follow_up_query_terms,
                        )
                    )
                ):
                    satisfied = True
                    break
        omitted_follow_up[call_id] = satisfied
    return {
        "failures": failures,
        "starts": starts,
        "start_arguments": start_arguments,
        "call_order": call_order,
        "search_calls": sum(name == prod.SEARCH_TOOL for name in starts.values()),
        "evidence_calls": sum(name == prod.EVIDENCE_TOOL for name in starts.values()),
        "total_tool_calls": len(starts),
        "routing_search_calls": len(routing_call_ids),
        "selected_database_search_calls": len(selected_call_ids),
        "selected_databases": selected_databases,
        "selected_database_queries": selected_queries,
        "duplicate_selected_database_queries": sorted(duplicate_queries),
        "omitted_inspectable_ids_by_call": omitted_ids_by_call,
        "omitted_inspectable_follow_up": omitted_follow_up,
        "search_evidence_urls": search_urls,
        "all_evidence_urls": all_urls,
        "result_bytes": result_bytes,
        "contents": list(contents.values()),
        "structured_contents": structured,
    }


def _interaction_observed(events: list[dict[str, Any]]) -> bool:
    return any(
        prod._event_type(event).startswith("permission.")
        or prod._event_type(event).startswith("user_input.")
        for event in events
    )


def _without_markdown_code(value: str) -> str:
    """Remove fenced and inline code while preserving surrounding prose."""
    without_fences = re.sub(
        r"(?ms)^[ \t]*(?P<fence>`{3,}|~{3,})[^\r\n]*\r?\n"
        r".*?^[ \t]*(?P=fence)[ \t]*(?:\r?\n|$)",
        "\n",
        value,
    )
    return re.sub(
        r"(?s)(?<!`)``[^`]*?``(?!`)|(?<!`)`[^`]*?`(?!`)",
        "",
        without_fences,
    )


def _answer_prose_without_link_destinations(response: str) -> str:
    """Return visible answer prose while excluding inline-link destinations and URLs.

    Fact checks must not be satisfiable by percent-encoded bytes or words that only
    occur in a locator.  Inline Markdown link text remains available because it is
    visible answer prose; the parenthesized destination is omitted.  Bare HTTPS
    URLs and autolink destinations are removed afterwards.
    """

    response = _without_markdown_code(response)

    def escaped(position: int) -> bool:
        backslashes = 0
        position -= 1
        while position >= 0 and response[position] == "\\":
            backslashes += 1
            position -= 1
        return backslashes % 2 == 1

    visible: list[str] = []
    index = 0
    while index < len(response):
        if response[index] != "[" or escaped(index):
            visible.append(response[index])
            index += 1
            continue

        label_depth = 1
        label_end = index + 1
        while label_end < len(response) and response[label_end] not in "\r\n":
            if response[label_end] == "\\":
                label_end += 2
                continue
            if response[label_end] == "[":
                label_depth += 1
            elif response[label_end] == "]":
                label_depth -= 1
                if label_depth == 0:
                    break
            label_end += 1
        if label_depth != 0:
            visible.append(response[index])
            index += 1
            continue

        destination_open = label_end + 1
        while (
            destination_open < len(response)
            and response[destination_open] in " \t"
        ):
            destination_open += 1
        if (
            destination_open >= len(response)
            or response[destination_open] != "("
            or escaped(destination_open)
        ):
            visible.append(response[index : label_end + 1])
            index = label_end + 1
            continue

        destination_depth = 1
        destination_end = destination_open + 1
        while destination_end < len(response):
            character = response[destination_end]
            if character == "\\":
                destination_end += 2
                continue
            if character == "(":
                destination_depth += 1
            elif character == ")":
                destination_depth -= 1
                if destination_depth == 0:
                    break
            destination_end += 1
        if destination_depth != 0:
            visible.append(response[index])
            index += 1
            continue

        visible.append(response[index + 1 : label_end])
        index = destination_end + 1

    prose = prod.HTTPS_URL_RE.sub("", "".join(visible))
    return re.sub(r"(?:%[0-9A-Fa-f]{2})+", "", prose)


_CLASSIFICATION_LABELS = ("確定事項", "提案段階", "未確認")


def _classification_bucket(prose: str, label: str) -> str:
    """Collect a heading section and table/prose lines carrying one label."""
    collected: list[str] = []
    current = ""
    for line in prose.splitlines():
        stripped = line.strip()
        heading_text = re.sub(r"^[#*_\s]+|[#*_\s]+$", "", stripped)
        heading_text = heading_text.strip("：:| ")
        heading = next(
            (
                candidate
                for candidate in _CLASSIFICATION_LABELS
                if re.fullmatch(
                    re.escape(candidate) + r"(?:[（(][^）)]*[）)])?",
                    heading_text,
                )
            ),
            "",
        )
        if heading:
            current = heading
            if heading == label:
                collected.append(line)
            continue
        labels_in_line = [
            candidate for candidate in _CLASSIFICATION_LABELS if candidate in line
        ]
        if len(labels_in_line) == 1:
            collected.append(line if labels_in_line[0] == label else "")
        elif current == label:
            collected.append(line)
    return "\n".join(item for item in collected if item).strip()


def _patterns_near(
    value: str,
    left: str,
    right: str,
    *,
    distance: int = 48,
) -> bool:
    return re.search(
        rf"(?:{left}).{{0,{distance}}}(?:{right})|"
        rf"(?:{right}).{{0,{distance}}}(?:{left})",
        value,
        re.IGNORECASE | re.DOTALL,
    ) is not None


def _affirmed_pattern_in_clause(
    value: str,
    pattern: str,
    *,
    contradiction: str,
    required_context: str | None = None,
) -> bool:
    for match in re.finditer(pattern, value, re.IGNORECASE):
        left = max(value.rfind(marker, 0, match.start()) for marker in ("。", "！", "？", "\n")) + 1
        right_positions = [
            position
            for marker in ("。", "！", "？", "\n")
            if (position := value.find(marker, match.end())) >= 0
        ]
        right = min(right_positions) if right_positions else len(value)
        clause = value[left:right]
        if required_context and re.search(required_context, clause) is None:
            continue
        if re.search(contradiction, clause, re.IGNORECASE) is None:
            return True
    return False


def _topic_has_affirmed_prohibition(value: str, topic: str) -> bool:
    for match in re.finditer(topic, value, re.IGNORECASE):
        left = max(value.rfind(marker, 0, match.start()) for marker in ("。", "！", "？", "\n")) + 1
        right_positions = [
            position
            for marker in ("。", "！", "？", "\n")
            if (position := value.find(marker, match.end())) >= 0
        ]
        right = min(right_positions) if right_positions else len(value)
        clause = value[left:right]
        if re.search(r"禁止|不可|認めない|禁じ|してはならない", clause) is None:
            continue
        if re.search(
            r"禁止(?:では|じゃ)?ない|不可(?:では|じゃ)?ない|"
            r"認めないわけではない|禁止.{0,8}(?:解除|撤回)|許可(?:する|される)",
            clause,
        ) is None:
            return True
    return False


def _thorough_answer_failures(response: str) -> list[str]:
    prose = _answer_prose_without_link_destinations(response)
    confirmed = _classification_bucket(prose, "確定事項")
    proposed = _classification_bucket(prose, "提案段階")
    unconfirmed = _classification_bucket(prose, "未確認")
    failures: list[str] = []
    if not _patterns_near(
        confirmed,
        r"(?<!\d)7\s*(?:%|％)",
        r"確定|執行|決定|成立",
    ):
        failures.append("thorough_confirmed_7_percent_missing_or_misclassified")
    if not _patterns_near(
        proposed,
        r"(?<!\d)12\s*(?:%|％)",
        r"提案|要求|増額案|討議|審議",
    ):
        failures.append("thorough_requested_12_percent_missing_or_misclassified")
    if not _affirmed_pattern_in_clause(
        proposed,
        r"(?<![A-Za-z])open(?![A-Za-z])|オープン|未解決|未完了",
        contradiction=r"ではない|ではなく|でなく|解決済み|closed|クローズ",
    ):
        failures.append("thorough_issue_open_missing_or_misclassified")
    if not _affirmed_pattern_in_clause(
        confirmed,
        r"衛星.{0,12}バズ|バズ.{0,12}衛星",
        contradiction=r"(?:では|には|で|に|は)?ない|ではなく|でなく|存在しない|所在しない|位置しない",
        required_context=r"集落|ダム族",
    ):
        failures.append("thorough_satellite_buzz_missing_or_misclassified")
    if not _affirmed_pattern_in_clause(
        confirmed,
        r"非接触|直接接触.{0,16}(?:禁止|しない|避け|不可)",
        contradiction=r"ではない|ではなく|解除|許可(?:する|される)",
    ):
        failures.append("thorough_no_direct_contact_missing_or_misclassified")
    if not _topic_has_affirmed_prohibition(confirmed, r"技術供与|技術提供"):
        failures.append("thorough_technology_provision_handling_missing")
    if not _topic_has_affirmed_prohibition(confirmed, r"資源採掘|採掘"):
        failures.append("thorough_mining_handling_missing")
    if not unconfirmed or re.search(r"未確認|不足|不明|判断", unconfirmed) is None:
        failures.append("thorough_unconfirmed_classification_missing")
    if re.search(
        r"(?:資料間.{0,20}直接(?:の)?関係|直接(?:の)?関係).{0,48}"
        r"(?:示されていない|示されていません|明示されていない|明示されていません|"
        r"確認できない|確認できません|確認されない|確認されません|関係なし|不明)",
        prose,
        re.DOTALL,
    ) is None:
        failures.append("thorough_direct_relationship_finding_missing")
    if re.search(
        r"(?:食い違い|相違|差異|矛盾)(?:が|は|を)?.{0,16}"
        r"(?:ある|あります|認められる|確認できる|確認された|生じている|存在する|不一致)",
        prose,
        re.DOTALL,
    ) is None:
        failures.append("thorough_conflict_finding_missing")
    return failures


def _simple_answer_failures(response: str, search_urls: set[str]) -> tuple[list[str], set[str], set[str]]:
    failures: list[str] = []
    answer_prose = _answer_prose_without_link_destinations(response)
    if re.search(r"(?<!\d)12\s*(?:%|％)", answer_prose) is None:
        failures.append("requested_12_percent_missing")
    if re.search(r"(?<!\d)7\s*(?:%|％)", answer_prose) is None:
        failures.append("confirmed_7_percent_missing")
    if re.search(
        r"(?<![A-Za-z])open(?![A-Za-z])|オープン|未解決|未完了",
        answer_prose,
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


def _boundary_primary_matches_marker(primary: str, marker: str) -> bool:
    """Accept the exact marker, optionally in one balanced inline Markdown wrapper."""
    candidate = primary.strip()
    if candidate == marker:
        return True
    for wrapper in ("***", "___", "**", "__", "*", "_", "`"):
        if candidate == f"{wrapper}{marker}{wrapper}":
            return True
    return False


def _boundary_fixture_failures(
    qualifying_contents: list[str], case: dict[str, Any]
) -> list[str]:
    expected_schema = case.get("fixture_schema")
    required_ids = case.get("required_reference_ids")
    payloads: list[dict[str, Any]] = []
    for content in qualifying_contents:
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    schema_payloads = [
        value for value in payloads if value.get("schema_version") == expected_schema
    ]
    failures: list[str] = []
    if not schema_payloads:
        failures.append("boundary_fixture_schema_missing")
    required = (
        set(required_ids)
        if isinstance(required_ids, list)
        and all(isinstance(value, str) and value for value in required_ids)
        else set()
    )
    if not required or not any(
        required.issubset(
            {
                evidence.get("id")
                for evidence in value.get("evidence", [])
                if isinstance(evidence, dict) and isinstance(evidence.get("id"), str)
            }
        )
        for value in schema_payloads
        if isinstance(value.get("evidence"), list)
    ):
        failures.append("boundary_required_evidence_ids_missing")
    return failures


def _boundary_reference_label_failures(
    references: str, required_ids: Any
) -> list[str]:
    if not isinstance(required_ids, list):
        return ["boundary_required_reference_ids_invalid"]
    failures: list[str] = []
    for evidence_id in required_ids:
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or re.search(
                rf"(?m)^- {re.escape(evidence_id)}:", references
            )
            is None
        ):
            failures.append(f"boundary_reference_label_missing:{evidence_id}")
    return failures


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


def evaluate_run(
    authority: dict[str, Any],
    root: Path,
    historical_identity: dict[str, Any] | None = None,
    allowed_stale_collector_sha256: str | None = None,
) -> dict[str, Any]:
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
    result["failures"].extend(
        _validate_harness_identity(
            run, historical_identity, allowed_stale_collector_sha256
        )
    )
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
    ordinal = run.get("plan_ordinal")
    retry_plan_run = (
        ordinal == 24 and run.get("case_kind") == "thorough"
    ) or ordinal == 25
    if retry_plan_run:
        try:
            _validate_retry_run_metadata(run, int(ordinal))
        except BakeoffError:
            failures.append("retry_logical_or_cli_budget_metadata_invalid")
    else:
        if run.get("retry_count") != 0:
            failures.append("retry_observed")
        if run.get("max_ai_credits") != 30:
            failures.append("per_session_soft_cap_mismatch")
        if run.get("cli_max_ai_credits") not in (None, 30):
            failures.append("cli_credit_guard_mismatch")
        if run.get("logical_max_ai_credits") not in (None, 30):
            failures.append("logical_credit_budget_mismatch")
    if _interaction_observed(events):
        failures.append("permission_or_user_input_event_observed")

    tool = _event_tool_evidence(
        events,
        omitted_relevance_terms=tuple(
            str(item)
            for item in case.get("omitted_evidence_relevance_terms") or []
        ),
        narrow_follow_up_query_terms=tuple(
            str(item)
            for item in case.get("narrow_follow_up_query_terms") or []
        ),
    )
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
    if case_kind == "thorough":
        expected_routing = case.get("expected_routing_search_calls")
        minimum_selected = case.get("minimum_selected_database_search_calls")
        maximum_total = case.get("maximum_total_tool_calls")
        expected_database = case.get("expected_database")
        if (
            not isinstance(expected_routing, int)
            or tool["routing_search_calls"] != expected_routing
        ):
            failures.append("routing_search_call_count_mismatch")
        if (
            not isinstance(minimum_selected, int)
            or tool["selected_database_search_calls"] < minimum_selected
        ):
            failures.append("selected_database_search_call_count_below_minimum")
        if (
            not isinstance(expected_database, str)
            or not expected_database
            or set(tool["selected_databases"]) != {expected_database}
        ):
            failures.append("selected_database_mismatch")
        if (
            case.get("forbid_duplicate_selected_database_queries") is not True
            or tool["duplicate_selected_database_queries"]
        ):
            failures.append("duplicate_or_missing_selected_database_query")
        if (
            not isinstance(maximum_total, int)
            or tool["total_tool_calls"] > maximum_total
        ):
            failures.append("total_tool_call_cap_exceeded")
        if case.get("require_omitted_inspectable_evidence_follow_up") is True:
            for call_id, satisfied in tool[
                "omitted_inspectable_follow_up"
            ].items():
                if not satisfied:
                    failures.append(
                        f"omitted_inspectable_evidence_follow_up_missing:{call_id}"
                    )

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
        failures.extend(_thorough_answer_failures(response))
    elif case_kind == "boundary" and response:
        marker = str(case.get("required_response_fragment", ""))
        normalized = response.replace("\r\n", "\n").replace("\r", "\n").strip()
        header = re.search(r"(?m)^## References[ \t]*$", normalized)
        if header is None or not _boundary_primary_matches_marker(
            normalized[: header.start()], marker
        ):
            failures.append("boundary_primary_or_references_contract_invalid")
        if header is not None:
            failures.extend(
                _boundary_reference_label_failures(
                    normalized[header.end() :], case.get("required_reference_ids")
                )
            )
        qualifying = [content for content in tool["contents"] if len(content.encode("utf-8")) >= int(case.get("minimum_tool_result_bytes", 0))]
        if not qualifying:
            failures.append("boundary_over_32k_result_missing")
        else:
            failures.extend(_boundary_fixture_failures(qualifying, case))
            if not any(marker.encode("utf-8") in content.encode("utf-8")[-int(case.get("tool_result_tail_window_bytes", 256)) :] for content in qualifying):
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
        logical_max = run.get("logical_max_ai_credits", run.get("max_ai_credits"))
        if (
            isinstance(logical_max, bool)
            or not isinstance(logical_max, int)
            or logical_max <= 0
        ):
            failures.append("logical_session_credit_cap_invalid")
        elif nano_aiu > logical_max * NANO_AIU_PER_CREDIT:
            failures.append("logical_session_credit_cap_exceeded")

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
            "total_tool_calls": tool["total_tool_calls"],
            "routing_search_calls": tool["routing_search_calls"],
            "selected_database_search_calls": tool[
                "selected_database_search_calls"
            ],
            "selected_databases": sorted(set(tool["selected_databases"])),
            "duplicate_selected_database_queries": tool[
                "duplicate_selected_database_queries"
            ],
            "omitted_inspectable_ids_by_call": tool[
                "omitted_inspectable_ids_by_call"
            ],
            "omitted_inspectable_follow_up": tool[
                "omitted_inspectable_follow_up"
            ],
            "search_evidence_urls": sorted(tool["search_evidence_urls"]),
            "response_urls": sorted(response_urls),
            "markdown_urls": sorted(markdown_urls),
            "maximum_tool_result_bytes": max(tool["result_bytes"], default=0),
            "assistant_response_sha256": _sha256_text(response) if response else None,
        }
    )
    if "logical_max_ai_credits" in run:
        result["logical_max_ai_credits"] = run.get("logical_max_ai_credits")
    if "cli_max_ai_credits" in run:
        result["cli_max_ai_credits"] = run.get("cli_max_ai_credits")
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


def _discover_formal_run_roots(
    authority: dict[str, Any], runs_root: Path, *, retry_plan: bool = False
) -> list[Path]:
    if not runs_root.is_dir() or _is_link_or_reparse(runs_root):
        raise BakeoffError(f"runs directory is invalid: {runs_root}")
    children = sorted(runs_root.iterdir(), key=lambda path: path.name.casefold())
    maximum_runs = 25 if retry_plan else 24
    if len(children) > maximum_runs:
        raise BakeoffError("formal run directory count exceeds the canonical plan")
    roots_by_ordinal: dict[int, Path] = {}
    run_ids: set[str] = set()
    for path in children:
        if not path.is_dir() or _is_link_or_reparse(path):
            raise BakeoffError(f"unknown non-canonical runs entry: {path.name}")
        run_path = path / "run.json"
        if not run_path.is_file() or _is_link_or_reparse(run_path):
            raise BakeoffError(f"formal run directory lacks run.json: {path.name}")
        raw_run = _load_json(run_path)
        ordinal = raw_run.get("plan_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise BakeoffError("formal run ordinal type is invalid")
        contract = _canonical_run_contract(authority, ordinal, retry_plan=retry_plan)
        run_id = raw_run.get("run_id")
        if (
            path.name != contract["leaf"]
            or not _run_matches_canonical_contract(raw_run, contract, ordinal)
        ):
            raise BakeoffError(f"formal run does not match canonical plan: {path.name}")
        if retry_plan and ordinal in (24, 25):
            _validate_retry_run_metadata(raw_run, ordinal)
        if ordinal in roots_by_ordinal or not isinstance(run_id, str) or run_id in run_ids:
            raise BakeoffError("formal run ordinal/run ID is duplicated")
        roots_by_ordinal[ordinal] = path
        run_ids.add(run_id)
    expected_ordinals = list(range(1, len(children) + 1))
    if sorted(roots_by_ordinal) != expected_ordinals:
        raise BakeoffError("formal run ordinals are not an exact canonical prefix")
    return [roots_by_ordinal[ordinal] for ordinal in expected_ordinals]


def _validate_imported_evaluations(
    recovery: dict[str, Any], evaluated_runs: list[dict[str, Any]]
) -> None:
    completed = recovery["completed_ordinal"]
    source_runs = recovery["source_report"].get("runs")
    imported = evaluated_runs[:completed]
    if not isinstance(source_runs, list) or imported != source_runs:
        raise BakeoffError("imported evaluated runs differ from source report")


def collect(
    authority_path: Path,
    raw_root: Path,
    expected_recovery_import_sha256: str | None = None,
    *,
    boundary_wrapper_rescore: bool = False,
) -> dict[str, Any]:
    authority = load_authority(authority_path)
    recovery = _load_recovery_import(
        raw_root,
        authority,
        expected_recovery_import_sha256,
        boundary_wrapper_rescore=boundary_wrapper_rescore,
    )
    retry_plan_name = recovery.get("retry_plan") if recovery is not None else None
    retry_plan = retry_plan_name in (
        "retry_failed_thorough_once_v1",
        "retry_failed_thorough_once_after_preinference_cli_floor_v2",
    )
    runs_root = raw_root / "runs"
    run_roots = _discover_formal_run_roots(
        authority, runs_root, retry_plan=retry_plan
    )
    historical_identities = (
        recovery.get("historical_harness_identities", [])
        if recovery is not None
        else []
    )
    runs = []
    for root in run_roots:
        raw_run = _load_json(root / "run.json")
        if (
            boundary_wrapper_rescore
            and recovery is not None
            and isinstance(raw_run.get("plan_ordinal"), int)
            and raw_run["plan_ordinal"] <= recovery["completed_ordinal"]
        ):
            # Exact r8 recovery validation already anchors every imported raw
            # file and the source report.  Preserve those historical collector
            # decisions byte-for-byte; only r8's two newly executed runs are
            # eligible for the audited scoring corrections.
            runs.append(
                json.loads(
                    json.dumps(
                        recovery["source_report"]["runs"][
                            raw_run["plan_ordinal"] - 1
                        ]
                    )
                )
            )
            continue
        historical_identity = next(
            (
                identity
                for identity in historical_identities
                if _identity_matches_run(raw_run, identity)
            ),
            None,
        )
        runs.append(
            evaluate_run(
                authority,
                root,
                historical_identity,
                (
                    R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_COLLECTOR_SHA256
                    if boundary_wrapper_rescore
                    else None
                ),
            )
        )
    if recovery is not None:
        _validate_imported_evaluations(recovery, runs)
    ordinals = [run.get("plan_ordinal") for run in runs]
    failures: list[str] = []
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ordinals) or ordinals != sorted(set(ordinals)):
        failures.append("plan_ordinals_invalid")
    failures.extend(_global_run_invariants(authority, runs))
    credit_unknown = any("credit_unknown" in run.get("failures", []) for run in runs)
    aggregate_nano = sum(int(run.get("total_nano_aiu", 0)) for run in runs if isinstance(run.get("total_nano_aiu"), int))
    aggregate_credits = aggregate_nano / NANO_AIU_PER_CREDIT
    recovery_nano = (
        int(recovery["recovery_total_nano_aiu"])
        if recovery is not None
        else 0
    )
    true_total_nano = aggregate_nano + recovery_nano
    true_total_credits = true_total_nano / NANO_AIU_PER_CREDIT
    cap_nano_decimal = (
        Decimal(str(authority["aggregate_ai_credit_cap"]))
        * NANO_AIU_PER_CREDIT
    )
    if cap_nano_decimal != cap_nano_decimal.to_integral_value():
        raise BakeoffError("aggregate Credit cap is not an integer nano-AIU value")
    cap_nano = int(cap_nano_decimal)
    if true_total_nano > cap_nano:
        failures.append("aggregate_credit_cap_exceeded")
    if credit_unknown:
        failures.append("aggregate_credit_unknown")
    summaries, ranking, winner = _candidate_summaries(authority, runs)
    savings_complete = all(len([run for run in runs if run.get("case_kind") == "savings" and run.get("candidate_model") == model]) == 3 for model in authority["candidate_models"])
    all_unavailable = savings_complete and all(summary["unavailable_or_skipped_runs"] == 3 for summary in summaries)
    auxiliary_attempt_status = {
        kind: [run.get("status") for run in runs if run.get("case_kind") == kind]
        for kind in ("standard", "thorough", "boundary")
    }
    superseded_auxiliary_attempts: list[dict[str, Any]] = []
    if retry_plan:
        auxiliary_status = {
            "standard": [
                run.get("status") for run in runs if run.get("plan_ordinal") == 22
            ],
            "thorough": [
                run.get("status") for run in runs if run.get("plan_ordinal") == 24
            ],
            "boundary": [
                run.get("status") for run in runs if run.get("plan_ordinal") == 25
            ],
        }
        superseded_auxiliary_attempts = [
            {
                "plan_ordinal": run.get("plan_ordinal"),
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "failures": run.get("failures"),
                "total_nano_aiu": run.get("total_nano_aiu"),
                "ai_credits": run.get("ai_credits"),
                "superseded_only_if_ordinal_24_passes": True,
            }
            for run in runs
            if run.get("plan_ordinal") == 23
        ]
    else:
        auxiliary_status = auxiliary_attempt_status
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
        "formal_aggregate_total_nano_aiu": aggregate_nano,
        "formal_aggregate_ai_credits": aggregate_credits,
        "recovery_total_nano_aiu": recovery_nano,
        "recovery_ai_credits": recovery_nano / NANO_AIU_PER_CREDIT,
        "true_total_nano_aiu": true_total_nano,
        "true_total_ai_credits": true_total_credits,
        "recovery_import": (
            {
                "path": recovery["path"],
                "sha256": recovery["sha256"],
                "source_report_path": recovery["source_report_path"],
                "source_report_sha256": recovery["source_report_sha256"],
                "completed_ordinal": recovery["completed_ordinal"],
                "aborted_attempt": recovery["aborted_attempt"],
                **(
                    {"preinference_attempt": recovery["preinference_attempt"]}
                    if "preinference_attempt" in recovery
                    else {}
                ),
            }
            if recovery is not None
            else None
        ),
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
        "retry_plan": retry_plan_name if retry_plan else None,
        "auxiliary_status": auxiliary_status,
        "auxiliary_attempt_status": auxiliary_attempt_status,
        "superseded_auxiliary_attempts": superseded_auxiliary_attempts,
        "runs": runs,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _rescore_r8_audited_corrections(
    authority_path: Path,
    raw_root: Path,
    expected_recovery_import_sha256: str | None,
) -> tuple[dict[str, Any], Path]:
    source_report_path = raw_root / "reports" / "report-25.json"
    source_report, source_report_sha = _load_anchored_json(
        source_report_path,
        R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_REPORT_SHA256,
    )
    if source_report_sha != R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_REPORT_SHA256:
        raise BakeoffError("r8 source report anchor mismatch")
    exact_run_files = {
        24: (
            "24-LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2",
            R8_AUDITED_RESCORE_RUN24_FILES,
        ),
        25: (
            "25-LRR-AGENT003-CLI-MODEL-BOUNDARY-AUTO",
            R8_BOUNDARY_WRAPPER_REAGGREGATION_RUN25_FILES,
        ),
    }
    for ordinal, (leaf, expected_files) in exact_run_files.items():
        actual_files = {
            relative: digest
            for relative, (digest, _size) in _directory_file_map(
                raw_root / "runs" / leaf
            ).items()
        }
        if actual_files != expected_files:
            raise BakeoffError(f"r8 run {ordinal} raw evidence file authority mismatch")
    rescored = collect(
        authority_path,
        raw_root,
        expected_recovery_import_sha256,
        boundary_wrapper_rescore=True,
    )
    expected = json.loads(json.dumps(source_report))
    if (
        expected.get("authority_sha256")
        != R3_HISTORICAL_HARNESS["authority"][1]
        or expected.get("overall_status") != "FAIL"
        or expected.get("stop_required") is not True
        or not isinstance(expected.get("runs"), list)
        or len(expected["runs"]) != 25
        or expected.get("auxiliary_status")
        != {"standard": ["PASS"], "thorough": ["PASS"], "boundary": ["FAIL"]}
        or expected.get("auxiliary_attempt_status")
        != {
            "standard": ["PASS"],
            "thorough": ["FAIL", "PASS"],
            "boundary": ["FAIL"],
        }
        or expected["runs"][23].get("status") != "PASS"
        or expected["runs"][23].get("failures") != []
        or expected["runs"][24].get("failures")
        != ["boundary_primary_or_references_contract_invalid"]
    ):
        raise BakeoffError("r8 source report is not the exact audited rescore source")
    expected["auxiliary_status"]["thorough"] = ["FAIL"]
    expected["auxiliary_status"]["boundary"] = ["PASS"]
    expected["auxiliary_attempt_status"]["thorough"] = ["FAIL", "FAIL"]
    expected["auxiliary_attempt_status"]["boundary"] = ["PASS"]
    expected["runs"][23]["status"] = "FAIL"
    expected["runs"][23]["failures"] = [
        "thorough_confirmed_7_percent_missing"
    ]
    expected["runs"][24]["status"] = "PASS"
    expected["runs"][24]["failures"] = []
    if rescored != expected:
        raise BakeoffError("r8 rescore changed fields beyond the two audited fixes")
    return rescored, source_report_path


def self_test(authority_path: Path) -> int:
    def expect_rejection(label: str, action: Any) -> None:
        try:
            action()
        except BakeoffError:
            return
        raise BakeoffError(f"tamper-negative self-test was not rejected: {label}")

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
    encoded_url_only = (
        "確定済み増額率は未確認です。"
        "[元資料](https://example.invalid/path/%E7%84%A1%E6%96%99.md)"
    )
    encoded_url_prose = _answer_prose_without_link_destinations(encoded_url_only)
    if (
        re.search(r"(?<!\d)7\s*(?:%|％)", encoded_url_prose) is not None
        or encoded_url_prose != "確定済み増額率は未確認です。元資料"
    ):
        raise BakeoffError(
            "percent-encoded Markdown URL satisfied a visible-prose fact check"
        )
    visible_link_fact = (
        "[確定済みは7%](https://example.invalid/path/%E7%84%A1.md)"
    )
    visible_link_prose = _answer_prose_without_link_destinations(visible_link_fact)
    if (
        visible_link_prose != "確定済みは7%"
        or re.search(r"(?<!\d)7\s*(?:%|％)", visible_link_prose) is None
    ):
        raise BakeoffError("visible Markdown link text was excluded from fact scoring")
    for label, code_only in (
        ("inline code", "`7%`"),
        ("fenced code", "```text\n7%\n```"),
        ("standalone percent encoding", "%E7%84%A1"),
    ):
        visible_code_prose = _answer_prose_without_link_destinations(code_only)
        if re.search(r"(?<!\d)7\s*(?:%|％)", visible_code_prose) is not None:
            raise BakeoffError(f"{label} satisfied a visible-prose fact check")

    valid_thorough = (
        "## 確定事項\n"
        "- 執行確定の増額率は7%。集落は衛星バズ。\n"
        "- 直接接触、技術供与、資源採掘は原則禁止。\n"
        "## 提案段階\n"
        "- 要求された12%増額案は討議中で、Issueはopen。\n"
        "## 未確認\n"
        "- 拠点の正確な座標は未確認で判断情報が不足。\n"
        "資料間の直接の関係は示されていません。\n"
        "要求12%と確定7%には食い違いがあります。"
    )
    thorough_failures = _thorough_answer_failures(valid_thorough)
    if thorough_failures:
        raise BakeoffError(
            f"valid thorough classification fixture failed: {thorough_failures}"
        )
    reversed_thorough = valid_thorough.replace(
        "執行確定の増額率は7%", "要求された増額率は12%"
    ).replace(
        "要求された12%増額案は討議中", "執行確定の増額率は7%"
    )
    reversed_failures = _thorough_answer_failures(reversed_thorough)
    if not {
        "thorough_confirmed_7_percent_missing_or_misclassified",
        "thorough_requested_12_percent_missing_or_misclassified",
    }.issubset(reversed_failures):
        raise BakeoffError(
            "reversed 12/7 classification was not rejected: "
            + repr(reversed_failures)
        )
    handling_failures = _thorough_answer_failures(
        valid_thorough.replace("は原則禁止", "を資料で確認")
    )
    if not {
        "thorough_technology_provision_handling_missing",
        "thorough_mining_handling_missing",
    }.issubset(handling_failures):
        raise BakeoffError("missing prohibition handling was not rejected")
    negated_handling_failures = _thorough_answer_failures(
        valid_thorough.replace(
            "直接接触、技術供与、資源採掘は原則禁止。",
            "直接接触は禁止ではない。技術供与は禁止ではない。資源採掘も禁止ではない。",
        )
    )
    if not {
        "thorough_no_direct_contact_missing_or_misclassified",
        "thorough_technology_provision_handling_missing",
        "thorough_mining_handling_missing",
    }.issubset(negated_handling_failures):
        raise BakeoffError("negated prohibition handling was not rejected")
    negated_core_failures = _thorough_answer_failures(
        valid_thorough.replace("集落は衛星バズ", "集落は衛星バズにはない")
        .replace("Issueはopen", "Issueはopenではなく解決済み")
    )
    if not {
        "thorough_issue_open_missing_or_misclassified",
        "thorough_satellite_buzz_missing_or_misclassified",
    }.issubset(negated_core_failures):
        raise BakeoffError("negated location/state was not rejected")
    wrong_relation_failures = _thorough_answer_failures(
        valid_thorough.replace(
            "資料間の直接の関係は示されていません。",
            "資料間の直接の関係があります。",
        ).replace(
            "要求12%と確定7%には食い違いがあります。",
            "要求12%と確定7%に食い違いはありません。",
        )
    )
    if not {
        "thorough_direct_relationship_finding_missing",
        "thorough_conflict_finding_missing",
    }.issubset(wrong_relation_failures):
        raise BakeoffError("relationship/conflict polarity was not rejected")

    def synthetic_tool_start(
        call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": call_id,
                "toolName": tool_name,
                "arguments": arguments,
            },
        }

    def synthetic_tool_complete(
        call_id: str, structured_content: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": call_id,
                "success": True,
                "result": {
                    "content": json.dumps(
                        structured_content, ensure_ascii=False
                    ),
                    "structuredContent": structured_content,
                },
            },
        }

    route_packet = {
        "status": "database_required",
        "inspectable_evidence_ids": [],
        "evidence": [],
    }
    omitted_packet = {
        "status": "ok",
        "next_action": "answer_now",
        "inspectable_evidence_ids": ["E1"],
        "evidence": [
            {"id": "E1", "text": "要求12%。確定済み増額率は…"}
        ],
    }
    complete_packet = {
        "status": "ok",
        "next_action": "answer_now",
        "inspectable_evidence_ids": ["E1"],
        # This normal packet notice must not be mistaken for excerpt omission.
        "notices": ["locator_hints_omitted"],
        "evidence": [
            {"id": "E1", "text": "要求12%、執行確定値7%。"}
        ],
    }
    review_events = [
        synthetic_tool_start("route", prod.SEARCH_TOOL, {"question": "原質問"}),
        synthetic_tool_complete("route", route_packet),
        synthetic_tool_start(
            "search-original",
            prod.SEARCH_TOOL,
            {"question": "原質問", "database": "fizzbuzz-planet-rag"},
        ),
        synthetic_tool_complete("search-original", omitted_packet),
        synthetic_tool_start(
            "search-budget",
            prod.SEARCH_TOOL,
            {
                "question": "保護区予算の確定済み増額率だけを確認する",
                "database": "fizzbuzz-planet-rag",
            },
        ),
        synthetic_tool_complete("search-budget", complete_packet),
        synthetic_tool_start(
            "search-relationship",
            prod.SEARCH_TOOL,
            {
                "question": "予算資料と集落資料の直接関係だけを確認する",
                "database": "fizzbuzz-planet-rag",
            },
        ),
        synthetic_tool_complete("search-relationship", complete_packet),
    ]
    review_terms = ("予算", "増額", "確定")
    narrow_terms = ("確定", "執行", "7%")
    review_tool = _event_tool_evidence(
        review_events,
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if (
        review_tool["failures"]
        or review_tool["routing_search_calls"] != 1
        or review_tool["selected_database_search_calls"] != 3
        or review_tool["total_tool_calls"] != 4
        or review_tool["selected_databases"]
        != ["fizzbuzz-planet-rag"] * 3
        or review_tool["duplicate_selected_database_queries"]
        or review_tool["omitted_inspectable_follow_up"]
        != {"search-original": True}
    ):
        raise BakeoffError(
            "routing, selected-database, or narrow-search review contract failed"
        )
    missing_follow_up = _event_tool_evidence(
        review_events[:4],
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if missing_follow_up["omitted_inspectable_follow_up"] != {
        "search-original": False
    }:
        raise BakeoffError("omitted inspectable evidence was accepted without follow-up")
    evidence_follow_up_events = [
        *review_events[:4],
        synthetic_tool_start(
            "inspect-e1",
            prod.EVIDENCE_TOOL,
            {"result_token": "opaque", "evidence_ids": ["E1"]},
        ),
        synthetic_tool_complete("inspect-e1", complete_packet),
    ]
    evidence_follow_up = _event_tool_evidence(
        evidence_follow_up_events,
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if evidence_follow_up["omitted_inspectable_follow_up"] != {
        "search-original": True
    }:
        raise BakeoffError("Evidence-detail review did not satisfy omission follow-up")
    duplicate_review = _event_tool_evidence(
        [
            *review_events,
            synthetic_tool_start(
                "search-duplicate",
                prod.SEARCH_TOOL,
                {"question": " 原質問 ", "database": "fizzbuzz-planet-rag"},
            ),
            synthetic_tool_complete("search-duplicate", complete_packet),
        ],
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if duplicate_review["duplicate_selected_database_queries"] != ["原質問"]:
        raise BakeoffError("duplicate selected-database query was not detected")
    unrelated_omission_packet = {
        "status": "ok",
        "next_action": "answer_now",
        "inspectable_evidence_ids": ["E9"],
        "evidence": [{"id": "E9", "text": "集落の補足史料は…"}],
    }
    unrelated_omission_events = [
        synthetic_tool_start(
            "unrelated",
            prod.SEARCH_TOOL,
            {"question": "集落史料", "database": "fizzbuzz-planet-rag"},
        ),
        synthetic_tool_complete("unrelated", unrelated_omission_packet),
    ]
    unrelated_omission = _event_tool_evidence(
        unrelated_omission_events,
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if unrelated_omission["omitted_inspectable_follow_up"]:
        raise BakeoffError("irrelevant omitted Evidence triggered a required follow-up")
    unrelated_follow_up_events = [
        *review_events[:4],
        synthetic_tool_start(
            "search-unrelated",
            prod.SEARCH_TOOL,
            {"question": "集落の歴史だけ", "database": "fizzbuzz-planet-rag"},
        ),
        synthetic_tool_complete("search-unrelated", complete_packet),
    ]
    unrelated_follow_up = _event_tool_evidence(
        unrelated_follow_up_events,
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if unrelated_follow_up["omitted_inspectable_follow_up"] != {
        "search-original": False
    }:
        raise BakeoffError("unrelated narrow search satisfied omission follow-up")
    budget_relationship_follow_up = _event_tool_evidence(
        [
            *review_events[:4],
            synthetic_tool_start(
                "search-budget-relationship",
                prod.SEARCH_TOOL,
                {
                    "question": "予算資料との直接関係",
                    "database": "fizzbuzz-planet-rag",
                },
            ),
            synthetic_tool_complete("search-budget-relationship", complete_packet),
        ],
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if budget_relationship_follow_up["omitted_inspectable_follow_up"] != {
        "search-original": False
    }:
        raise BakeoffError("budget relationship search satisfied 7% omission follow-up")
    location_confirmation_follow_up = _event_tool_evidence(
        [
            *review_events[:4],
            synthetic_tool_start(
                "search-confirmed-location",
                prod.SEARCH_TOOL,
                {
                    "question": "ダム族集落の確定位置",
                    "database": "fizzbuzz-planet-rag",
                },
            ),
            synthetic_tool_complete("search-confirmed-location", complete_packet),
        ],
        omitted_relevance_terms=review_terms,
        narrow_follow_up_query_terms=narrow_terms,
    )
    if location_confirmation_follow_up["omitted_inspectable_follow_up"] != {
        "search-original": False
    }:
        raise BakeoffError("confirmed-location search satisfied 7% omission follow-up")
    boundary_marker = str(authority["boundary_case"]["required_response_fragment"])
    for primary in (
        boundary_marker,
        f"**{boundary_marker}**",
        f"_{boundary_marker}_",
        f"`{boundary_marker}`",
    ):
        if not _boundary_primary_matches_marker(primary, boundary_marker):
            raise BakeoffError(
                f"valid boundary primary Markdown wrapper was rejected: {primary!r}"
            )
    for primary in (
        f"**{boundary_marker}*",
        f"****{boundary_marker}****",
        f"**{boundary_marker}** extra",
        f"**{boundary_marker}X**",
        f"```{boundary_marker}```",
    ):
        if _boundary_primary_matches_marker(primary, boundary_marker):
            raise BakeoffError(
                f"boundary primary wrapper tamper was not rejected: {primary!r}"
            )
    boundary_case = authority["boundary_case"]
    valid_boundary_fixture = json.dumps(
        {
            "schema_version": boundary_case["fixture_schema"],
            "evidence": [{"id": "E1"}],
        }
    )
    if _boundary_fixture_failures([valid_boundary_fixture], boundary_case):
        raise BakeoffError("valid boundary fixture schema/evidence IDs were rejected")
    wrong_schema = json.dumps(
        {"schema_version": "tampered", "evidence": [{"id": "E1"}]}
    )
    if "boundary_fixture_schema_missing" not in _boundary_fixture_failures(
        [wrong_schema], boundary_case
    ):
        raise BakeoffError("boundary fixture schema tamper was not rejected")
    wrong_evidence_id = json.dumps(
        {
            "schema_version": boundary_case["fixture_schema"],
            "evidence": [{"id": "E10"}],
        }
    )
    if "boundary_required_evidence_ids_missing" not in _boundary_fixture_failures(
        [wrong_evidence_id], boundary_case
    ):
        raise BakeoffError("boundary Evidence ID tamper was not rejected")
    if _boundary_reference_label_failures("\n- E1: fixture", ["E1"]):
        raise BakeoffError("valid boundary References list label was rejected")
    if "boundary_reference_label_missing:E1" not in _boundary_reference_label_failures(
        "\n- E10: tampered", ["E1"]
    ):
        raise BakeoffError("boundary References list-label tamper was not rejected")
    if not _matches_boundary_wrapper_reaggregation_collector(
        Path(__file__),
        R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_COLLECTOR_SHA256,
        R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_COLLECTOR_SHA256,
    ):
        raise BakeoffError("exact r8 stale collector rescore identity was rejected")
    if _matches_boundary_wrapper_reaggregation_collector(
        Path(__file__), "0" * 64, R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_COLLECTOR_SHA256
    ):
        raise BakeoffError("stale collector identity tamper was not rejected")
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
    first_contract = _canonical_run_contract(authority, 1)
    sixth_contract = _canonical_run_contract(authority, 6)
    standard_contract = _canonical_run_contract(authority, 22)
    thorough_contract = _canonical_run_contract(authority, 23)
    boundary_contract = _canonical_run_contract(authority, 24)
    retry_contract = _canonical_run_contract(authority, 24, retry_plan=True)
    retry_boundary_contract = _canonical_run_contract(
        authority, 25, retry_plan=True
    )
    if (
        first_contract["run_id"] != "LRR-AGENT003-CLI-MODEL-SAVINGS-C01-R1"
        or first_contract["candidate_model"] != EXPECTED_CANDIDATES[0]
        or sixth_contract["run_id"] != "LRR-AGENT003-CLI-MODEL-SAVINGS-C02-R3"
        or sixth_contract["candidate_model"] != EXPECTED_CANDIDATES[1]
        or standard_contract
        != {
            "run_id": "LRR-AGENT003-CLI-MODEL-STANDARD-AUTO",
            "case_kind": "standard",
            "candidate_model": "",
            "requested_model": "auto",
            "tier": "standard",
            "attempt": 1,
            "leaf": "22-LRR-AGENT003-CLI-MODEL-STANDARD-AUTO",
        }
        or thorough_contract["run_id"]
        != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO"
        or thorough_contract["case_kind"] != "thorough"
        or thorough_contract["candidate_model"] != ""
        or thorough_contract["tier"] != "thorough"
        or boundary_contract["run_id"]
        != "LRR-AGENT003-CLI-MODEL-BOUNDARY-AUTO"
        or boundary_contract["case_kind"] != "boundary"
        or boundary_contract["candidate_model"] != ""
        or boundary_contract["tier"] != "standard"
        or retry_contract["run_id"]
        != "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2"
        or retry_contract["case_kind"] != "thorough"
        or retry_contract["attempt"] != 2
        or retry_contract["leaf"]
        != "24-LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2"
        or retry_boundary_contract["run_id"]
        != "LRR-AGENT003-CLI-MODEL-BOUNDARY-AUTO"
        or retry_boundary_contract["leaf"]
        != "25-LRR-AGENT003-CLI-MODEL-BOUNDARY-AUTO"
    ):
        raise BakeoffError("canonical resume-plan mapping self-test failed")
    expect_rejection(
        "unknown formal ordinal",
        lambda: _canonical_run_contract(authority, 25),
    )
    valid_retry_metadata = {
        "retry_count": 1,
        "retry_of_ordinal": 23,
        "retry_of_run_id": "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO",
        "logical_max_ai_credits": 12,
        "cli_max_ai_credits": 30,
        "max_ai_credits": 30,
    }
    _validate_retry_run_metadata(valid_retry_metadata, 24)
    tampered_retry_metadata = dict(valid_retry_metadata)
    tampered_retry_metadata["retry_of_ordinal"] = 22
    expect_rejection(
        "retry predecessor tamper",
        lambda: _validate_retry_run_metadata(tampered_retry_metadata, 24),
    )
    valid_retry_boundary_metadata = {
        "retry_count": 0,
        "retry_of_ordinal": None,
        "retry_of_run_id": None,
        "logical_max_ai_credits": 8,
        "cli_max_ai_credits": 30,
        "max_ai_credits": 30,
    }
    _validate_retry_run_metadata(valid_retry_boundary_metadata, 25)
    tampered_boundary_metadata = dict(valid_retry_boundary_metadata)
    tampered_boundary_metadata["logical_max_ai_credits"] = 30
    expect_rejection(
        "retry boundary Credit cap tamper",
        lambda: _validate_retry_run_metadata(tampered_boundary_metadata, 25),
    )
    tampered_cli_floor_metadata = dict(valid_retry_metadata)
    tampered_cli_floor_metadata["cli_max_ai_credits"] = 12
    expect_rejection(
        "retry CLI minimum tamper",
        lambda: _validate_retry_run_metadata(tampered_cli_floor_metadata, 24),
    )
    preinference_raw = {
        "schema_version": RUN_SCHEMA,
        "plan_ordinal": 24,
        "run_id": "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2",
        "case_kind": "thorough",
        "candidate_model": "",
        "requested_model": "auto",
        "attempt": 2,
        "execution_state": "executed",
        "help_listed": True,
        "prompt_sha256": R6_THOROUGH_PROMPT_SHA256,
        "fresh_session": True,
        "retry_count": 1,
        "retry_of_ordinal": 23,
        "retry_of_run_id": "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO",
        "max_ai_credits": 12,
        "runner_sha256": R7_RUNNER_SHA256,
        "collector_sha256": R7_COLLECTOR_SHA256,
        "authority_sha256": R3_HISTORICAL_HARNESS["authority"][1],
        "exit_code": 1,
        "timed_out": False,
        "process_tree_terminated": False,
        "stdout_bytes": 0,
        "stderr_bytes": 139,
    }
    preinference_evaluated = {
        "plan_ordinal": 24,
        "run_id": "LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2",
        "case_kind": "thorough",
        "candidate_model": "",
        "attempt": 2,
        "execution_state": "executed",
        "prompt_sha256": R6_THOROUGH_PROMPT_SHA256,
        "status": "FAIL",
        "failures": list(R7_PREINFERENCE_REPORT_FAILURES),
        "ai_credits": 0.0,
        "total_nano_aiu": 0,
        "usage_row_delta": 0,
        "usage_nano_aiu_delta": 0,
        "assistant_message_count": 0,
        "session_id": None,
        "requested_model": "auto",
        "resolved_models": [],
        "otel_response_models": [],
        "search_calls": 0,
        "evidence_calls": 0,
        "maximum_tool_result_bytes": 0,
        "assistant_response_sha256": None,
    }
    preinference_before = {
        "schema_version": SNAPSHOT_SCHEMA,
        "copilot_home": "X",
        "session_store_exists": False,
        "row_count": 0,
        "total_nano_aiu": 0,
        "maximum_usage_event_id": None,
    }
    preinference_after = dict(preinference_before)
    preinference_mutation = {
        "schema_version": "lrr-agent003-cli-model-mutation-audit-v1",
        "tier": "thorough",
        "requested_model": "auto",
        "production_artifacts_modified": False,
    }
    _validate_r7_preinference_semantics(
        preinference_raw,
        preinference_evaluated,
        preinference_before,
        preinference_after,
        preinference_mutation,
        R7_PREINFERENCE_STDERR,
        b"",
        b"",
    )
    tampered_preinference_prompt = dict(preinference_raw)
    tampered_preinference_prompt["prompt_sha256"] = "0" * 64
    expect_rejection(
        "r7 pre-inference prompt tamper",
        lambda: _validate_r7_preinference_semantics(
            tampered_preinference_prompt,
            preinference_evaluated,
            preinference_before,
            preinference_after,
            preinference_mutation,
            R7_PREINFERENCE_STDERR,
            b"",
            b"",
        ),
    )
    tampered_preinference_model = dict(preinference_raw)
    tampered_preinference_model["requested_model"] = "tampered-non-auto-model"
    expect_rejection(
        "r7 pre-inference model tamper",
        lambda: _validate_r7_preinference_semantics(
            tampered_preinference_model,
            preinference_evaluated,
            preinference_before,
            preinference_after,
            preinference_mutation,
            R7_PREINFERENCE_STDERR,
            b"",
            b"",
        ),
    )
    tampered_preinference_credit = dict(preinference_evaluated)
    tampered_preinference_credit["total_nano_aiu"] = 1
    expect_rejection(
        "r7 pre-inference Credit tamper",
        lambda: _validate_r7_preinference_semantics(
            preinference_raw,
            tampered_preinference_credit,
            preinference_before,
            preinference_after,
            preinference_mutation,
            R7_PREINFERENCE_STDERR,
            b"",
            b"",
        ),
    )
    standard_run_fixture = {
        "schema_version": RUN_SCHEMA,
        "plan_ordinal": 22,
        "run_id": standard_contract["run_id"],
        "case_kind": "standard",
        "candidate_model": "",
        "requested_model": "auto",
        "attempt": 1,
    }
    if not _run_matches_canonical_contract(
        standard_run_fixture, standard_contract, 22
    ):
        raise BakeoffError("canonical auxiliary empty-string fixture was rejected")
    invalid_auxiliary = dict(standard_run_fixture)
    invalid_auxiliary["candidate_model"] = None
    if _run_matches_canonical_contract(invalid_auxiliary, standard_contract, 22):
        raise BakeoffError("non-canonical auxiliary null candidate was accepted")
    valid_generations = [
        {"name": "r3", "first_ordinal": 1, "last_ordinal": 5},
        {"name": "r5-prefix", "first_ordinal": 6, "last_ordinal": 22},
    ]
    _require_v2_generation_ranges(valid_generations)
    valid_v3_generations = [
        *valid_generations,
        {
            "name": "r6-failed-thorough",
            "first_ordinal": 23,
            "last_ordinal": 23,
        },
    ]
    _require_v3_generation_ranges(valid_v3_generations)
    overlapping_generations = [dict(item) for item in valid_generations]
    overlapping_generations[1]["first_ordinal"] = 5
    expect_rejection(
        "v2 historical generation overlap",
        lambda: _require_v2_generation_ranges(overlapping_generations),
    )
    bad_v3_generations = [dict(item) for item in valid_v3_generations]
    bad_v3_generations[2]["first_ordinal"] = 22
    expect_rejection(
        "v3 historical generation overlap",
        lambda: _require_v3_generation_ranges(bad_v3_generations),
    )
    with tempfile.TemporaryDirectory(prefix="lrr-agent003-bakeoff-selftest-") as temp:
        temp_root = Path(temp)
        source = temp_root / "source"
        preserved = temp_root / "preserved"
        source.mkdir()
        preserved.mkdir()
        (source / "one.txt").write_bytes(b"one\n")
        (preserved / "one.txt").write_bytes(b"one\n")
        digest = hashlib.sha256(b"one\n").hexdigest()
        authority_entries = [
            {"relative_path": "one.txt", "sha256": digest, "bytes": 4}
        ]
        _validate_exact_file_authority(
            source, preserved, authority_entries, context="self-test"
        )
        (preserved / "extra.txt").write_bytes(b"extra\n")
        expect_rejection(
            "unlisted preserved file",
            lambda: _validate_exact_file_authority(
                source, preserved, authority_entries, context="self-test"
            ),
        )
        (preserved / "extra.txt").unlink()
        duplicate_case_entries = authority_entries + [
            {"relative_path": "ONE.TXT", "sha256": digest, "bytes": 4}
        ]
        expect_rejection(
            "case-folded duplicate file authority",
            lambda: _validate_exact_file_authority(
                source, preserved, duplicate_case_entries, context="self-test"
            ),
        )

        archive_root = authority_path.resolve().parent / "r3-historical-harness"
        historical_source = temp_root / "historical-source"
        historical_preserved = temp_root / "historical-preserved"
        historical_source.mkdir()
        historical_preserved.mkdir()
        historical_entries: list[dict[str, Any]] = []
        for relative, expected_hash in sorted(
            R3_HISTORICAL_HARNESS.values(), key=lambda item: item[0].casefold()
        ):
            payload = (archive_root / relative).read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected_hash:
                raise BakeoffError(f"historical harness archive self-test failed: {relative}")
            (historical_source / relative).write_bytes(payload)
            (historical_preserved / relative).write_bytes(payload)
            historical_entries.append(
                {
                    "relative_path": relative,
                    "sha256": expected_hash,
                    "bytes": len(payload),
                }
            )
        _validate_exact_file_authority(
            historical_source,
            historical_preserved,
            historical_entries,
            context="historical self-test",
        )
        historical_runner = historical_preserved / R3_HISTORICAL_HARNESS["runner"][0]
        original_runner = historical_runner.read_bytes()
        historical_runner.write_bytes(original_runner[:-1])
        expect_rejection(
            "historical runner byte tamper",
            lambda: _validate_exact_file_authority(
                historical_source,
                historical_preserved,
                historical_entries,
                context="historical self-test",
            ),
        )
        historical_runner.write_bytes(original_runner)
        expect_rejection(
            "historical artifact role/file omission",
            lambda: _validate_exact_file_authority(
                historical_source,
                historical_preserved,
                historical_entries[:-1],
                context="historical self-test",
            ),
        )
        r5_archive = authority_path.resolve().parent / "r5-prefix-historical-harness"
        r5_identity = {
            f"{kind}_{field}": (
                str((temp_root / f"historical-{kind}").resolve())
                if field == "path"
                else R5_PREFIX_HISTORICAL_HARNESS[kind][1]
            )
            for kind in ("runner", "collector", "authority")
            for field in ("path", "sha256")
        }
        _validate_versioned_harness_archive(
            r5_identity,
            r5_archive,
            R5_PREFIX_HISTORICAL_HARNESS,
            context="r5-prefix historical self-test",
        )
        tampered_r5_identity = dict(r5_identity)
        tampered_r5_identity["collector_sha256"] = "0" * 64
        expect_rejection(
            "r5 historical generation identity tamper",
            lambda: _validate_versioned_harness_archive(
                tampered_r5_identity,
                r5_archive,
                R5_PREFIX_HISTORICAL_HARNESS,
                context="r5-prefix historical self-test",
            ),
        )

        manifest_path = temp_root / "recovery-import.json"
        manifest_path.write_bytes(
            (json.dumps({"schema_version": RECOVERY_IMPORT_SCHEMA}) + "\n").encode("utf-8")
        )
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        manifest_value, _ = _load_anchored_json(manifest_path, manifest_sha)
        _require_supported_recovery_schema(manifest_value)
        v2_snapshot_manifest = temp_root / "recovery-import-v2.json"
        v2_snapshot_manifest.write_bytes(
            (json.dumps({"schema_version": RECOVERY_IMPORT_SCHEMA_V2}) + "\n").encode(
                "utf-8"
            )
        )
        v2_snapshot_sha = hashlib.sha256(v2_snapshot_manifest.read_bytes()).hexdigest()
        v2_snapshot_value, _ = _load_anchored_json(
            v2_snapshot_manifest, v2_snapshot_sha
        )
        _require_supported_recovery_schema(v2_snapshot_value)
        v3_snapshot_manifest = temp_root / "recovery-import-v3.json"
        v3_snapshot_manifest.write_bytes(
            (json.dumps({"schema_version": RECOVERY_IMPORT_SCHEMA_V3}) + "\n").encode(
                "utf-8"
            )
        )
        v3_snapshot_sha = hashlib.sha256(v3_snapshot_manifest.read_bytes()).hexdigest()
        v3_snapshot_value, _ = _load_anchored_json(
            v3_snapshot_manifest, v3_snapshot_sha
        )
        _require_supported_recovery_schema(v3_snapshot_value)
        v4_snapshot_manifest = temp_root / "recovery-import-v4.json"
        v4_snapshot_manifest.write_bytes(
            (json.dumps({"schema_version": RECOVERY_IMPORT_SCHEMA_V4}) + "\n").encode(
                "utf-8"
            )
        )
        v4_snapshot_sha = hashlib.sha256(v4_snapshot_manifest.read_bytes()).hexdigest()
        v4_snapshot_value, _ = _load_anchored_json(
            v4_snapshot_manifest, v4_snapshot_sha
        )
        _require_supported_recovery_schema(v4_snapshot_value)
        expect_rejection(
            "snapshot recovery schema tamper",
            lambda: _require_supported_recovery_schema(
                {"schema_version": "unsupported"}
            ),
        )
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        expect_rejection(
            "recovery manifest anchor mutation",
            lambda: _load_anchored_json(manifest_path, manifest_sha),
        )

        source_snapshots = temp_root / "source-snapshots"
        preserved_snapshots = temp_root / "preserved-snapshots"
        source_snapshots.mkdir()
        preserved_snapshots.mkdir()
        home = "<redacted-r3-copilot-home>"
        before_value = {
            "schema_version": SNAPSHOT_SCHEMA,
            "captured_at": "2026-08-24T08:15:11.168240+00:00",
            "copilot_home": home,
            "session_store_path": home + r"\session-store.db",
            "session_store_exists": True,
            "row_count": 18,
            "total_nano_aiu": 6_033_745_000,
            "maximum_usage_event_id": 18,
        }
        after_value = {
            "schema_version": SNAPSHOT_SCHEMA,
            "captured_at": "2026-08-24T08:18:43.950152+00:00",
            "copilot_home": home,
            "session_store_path": home + r"\session-store.db",
            "session_store_exists": True,
            "row_count": 21,
            "total_nano_aiu": 6_316_900_000,
            "maximum_usage_event_id": 21,
        }
        before_bytes = (
            json.dumps(before_value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        after_bytes = (
            json.dumps(after_value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        if (
            hashlib.sha256(before_bytes).hexdigest() != R3_RECOVERY_BEFORE_SHA256
            or hashlib.sha256(after_bytes).hexdigest() != R3_RECOVERY_AFTER_SHA256
        ):
            raise BakeoffError("r3 recovery snapshot fixture hash self-test failed")
        for root in (source_snapshots, preserved_snapshots):
            (root / "usage-before.json").write_bytes(before_bytes)
            (root / "usage-after-recovered.json").write_bytes(after_bytes)
        aborted = {
            "files": [
                {
                    "relative_path": "usage-after-recovered.json",
                    "sha256": R3_RECOVERY_AFTER_SHA256,
                    "bytes": len(after_bytes),
                },
                {
                    "relative_path": "usage-before.json",
                    "sha256": R3_RECOVERY_BEFORE_SHA256,
                    "bytes": len(before_bytes),
                },
            ],
            "usage_before_sha256": R3_RECOVERY_BEFORE_SHA256,
            "usage_after_recovered_sha256": R3_RECOVERY_AFTER_SHA256,
            "usage_row_delta": 3,
            "recovery_total_nano_aiu": 283_155_000,
            "recovery_ai_credits": 0.283155,
        }
        source_report = {
            "aggregate_total_nano_aiu": 6_033_745_000,
            "runs": [
                {"usage_row_delta": row, "total_nano_aiu": nano}
                for row, nano in zip(
                    (4, 4, 4, 3, 3),
                    (1_000_000_000, 1_000_000_000, 1_000_000_000, 1_000_000_000, 2_033_745_000),
                )
            ],
        }
        if _validate_recovery_snapshots(
            source_report, source_snapshots, preserved_snapshots, aborted
        ) != (3, 283_155_000):
            raise BakeoffError("valid r3 recovery snapshot fixture failed")
        (preserved_snapshots / "usage-after-recovered.json").write_bytes(
            after_bytes + b" "
        )
        expect_rejection(
            "preserved recovery snapshot byte tamper",
            lambda: _validate_recovery_snapshots(
                source_report, source_snapshots, preserved_snapshots, aborted
            ),
        )
        (preserved_snapshots / "usage-after-recovered.json").write_bytes(after_bytes)
        bad_credit = dict(aborted)
        bad_credit["recovery_ai_credits"] = 0.283156
        expect_rejection(
            "recovery Credit conversion tamper",
            lambda: _validate_recovery_snapshots(
                source_report, source_snapshots, preserved_snapshots, bad_credit
            ),
        )

        stable_runs = [{"run_id": f"run-{index}"} for index in range(1, 6)]
        synthetic_recovery = {
            "completed_ordinal": 5,
            "source_report": {"runs": stable_runs},
        }
        _validate_imported_evaluations(synthetic_recovery, list(stable_runs))
        altered_runs = [dict(item) for item in stable_runs]
        altered_runs[-1]["run_id"] = "tampered"
        expect_rejection(
            "imported evaluated run mismatch",
            lambda: _validate_imported_evaluations(
                synthetic_recovery, altered_runs
            ),
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
    parser.add_argument("--expected-recovery-import-sha256")
    parser.add_argument(
        "--rescore-r8-audited-corrections-only", action="store_true"
    )
    parser.add_argument(
        "--rescore-r8-boundary-wrapper-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--rescore-manifest-output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.snapshot_copilot_home is not None or args.snapshot_output is not None:
            if args.snapshot_copilot_home is None or args.snapshot_output is None:
                raise BakeoffError("snapshot mode requires both snapshot arguments")
            if args.expected_recovery_import_sha256 is not None:
                if args.raw_root is None:
                    raise BakeoffError("anchored snapshot mode requires --raw-root")
                manifest_value, _ = _load_anchored_json(
                    args.raw_root / "recovery-import.json",
                    args.expected_recovery_import_sha256,
                )
                _require_supported_recovery_schema(manifest_value)
            _write_json(args.snapshot_output, snapshot_session_store(args.snapshot_copilot_home))
            return 0
        if args.authority is None:
            raise BakeoffError("--authority is required")
        if args.self_test:
            return self_test(args.authority)
        if args.raw_root is None or args.output is None:
            raise BakeoffError("collection requires --raw-root and --output")
        audited_rescore = (
            args.rescore_r8_audited_corrections_only
            or args.rescore_r8_boundary_wrapper_only
        )
        if (
            args.rescore_r8_audited_corrections_only
            and args.rescore_r8_boundary_wrapper_only
        ):
            raise BakeoffError("select exactly one r8 audited-rescore flag")
        if audited_rescore:
            if args.rescore_manifest_output is None:
                raise BakeoffError("r8 audited rescore requires --rescore-manifest-output")
            source_report_path = args.raw_root / "reports" / "report-25.json"
            if (
                args.output.resolve() == source_report_path.resolve()
                or args.rescore_manifest_output.resolve() == source_report_path.resolve()
                or args.output.resolve() == args.rescore_manifest_output.resolve()
                or args.output.exists()
                or args.rescore_manifest_output.exists()
            ):
                raise BakeoffError("r8 audited rescore outputs must be new and distinct")
            report, anchored_source_report = _rescore_r8_audited_corrections(
                args.authority,
                args.raw_root,
                args.expected_recovery_import_sha256,
            )
            _write_json(args.output, report)
            report_sha = prod._sha256_file(args.output)
            _write_json(
                args.rescore_manifest_output,
                {
                    "schema_version": "lrr-agent003-cli-audited-rescore-v2",
                    "source_evidence_root": str(args.raw_root.resolve()),
                    "source_report_path": str(anchored_source_report.resolve()),
                    "source_report_sha256": R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_REPORT_SHA256,
                    "source_recovery_manifest_sha256": R8_BOUNDARY_WRAPPER_REAGGREGATION_MANIFEST_SHA256,
                    "source_collector_sha256": R8_BOUNDARY_WRAPPER_REAGGREGATION_SOURCE_COLLECTOR_SHA256,
                    "rescore_collector_path": str(Path(__file__).resolve()),
                    "rescore_collector_sha256": prod._sha256_file(Path(__file__)),
                    "output_report_path": str(args.output.resolve()),
                    "output_report_sha256": report_sha,
                    "source_raw_run_anchors": {
                        "24-LRR-AGENT003-CLI-MODEL-THOROUGH-AUTO-R2": R8_AUDITED_RESCORE_RUN24_FILES,
                        "25-LRR-AGENT003-CLI-MODEL-BOUNDARY-AUTO": R8_BOUNDARY_WRAPPER_REAGGREGATION_RUN25_FILES,
                    },
                    "reason": "exclude Markdown link destinations and URLs from visible-prose fact checks, while accepting one balanced inline Markdown wrapper around the exact boundary marker",
                    "source_evidence_modified": False,
                    "copilot_prompt_sent": False,
                    "metered_execution_performed": False,
                    "accepted_delta": {
                        "overall_status": ["FAIL", "FAIL"],
                        "thorough_status": ["PASS", "FAIL"],
                        "thorough_failures": [
                            [],
                            ["thorough_confirmed_7_percent_missing"],
                        ],
                        "boundary_status": ["FAIL", "PASS"],
                        "boundary_failures": [
                            ["boundary_primary_or_references_contract_invalid"],
                            [],
                        ],
                    },
                },
            )
            return 0
        report = collect(
            args.authority,
            args.raw_root,
            args.expected_recovery_import_sha256,
        )
        _write_json(args.output, report)
        return 0 if report["overall_status"] not in ("STOP_CREDIT_OR_EVIDENCE", "FAIL") else 2
    except (BakeoffError, prod.EvidenceError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
