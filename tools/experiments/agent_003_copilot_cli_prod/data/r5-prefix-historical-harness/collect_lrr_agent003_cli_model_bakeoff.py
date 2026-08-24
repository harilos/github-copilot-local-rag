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


def _validate_harness_identity(
    run: dict[str, Any], historical_identity: dict[str, Any] | None = None
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
        if not live_match:
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
    authority: dict[str, Any], ordinal: int
) -> dict[str, Any]:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= 24:
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
            "attempt": attempt,
            "leaf": f"{ordinal:02d}-{run_id}",
        }
    key = {22: "standard_case", 23: "thorough_case", 24: "boundary_case"}[ordinal]
    case = authority[key]
    return {
        "run_id": case["id"],
        "case_kind": key.removesuffix("_case"),
        "candidate_model": None,
        "requested_model": "auto",
        "attempt": 1,
        "leaf": f"{ordinal:02d}-{case['id']}",
    }


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
    load_authority(preserved_authority)
    if source_report.get("authority_sha256") != historical["authority_sha256"]:
        raise BakeoffError("source report historical authority identity mismatch")
    historical_for_evaluation = dict(historical)
    historical_for_evaluation["_validated_artifact_paths"] = validated_artifact_paths
    resume_identity = value.get("resume_harness_identity")
    if not isinstance(resume_identity, dict):
        raise BakeoffError("resume harness identity is missing")
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
            raise BakeoffError("resume harness live identity mismatch")
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
        "recovery_total_nano_aiu": derived_nano_delta,
        "recovery_ai_credits": derived_nano_delta / NANO_AIU_PER_CREDIT,
        "recovery_usage_row_delta": derived_row_delta,
        "aborted_attempt": aborted,
    }


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


def evaluate_run(
    authority: dict[str, Any],
    root: Path,
    historical_identity: dict[str, Any] | None = None,
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
        _validate_harness_identity(run, historical_identity)
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


def _discover_formal_run_roots(
    authority: dict[str, Any], runs_root: Path
) -> list[Path]:
    if not runs_root.is_dir() or _is_link_or_reparse(runs_root):
        raise BakeoffError(f"runs directory is invalid: {runs_root}")
    children = sorted(runs_root.iterdir(), key=lambda path: path.name.casefold())
    if len(children) > 24:
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
        contract = _canonical_run_contract(authority, ordinal)
        run_id = raw_run.get("run_id")
        if (
            path.name != contract["leaf"]
            or raw_run.get("schema_version") != RUN_SCHEMA
            or run_id != contract["run_id"]
            or raw_run.get("case_kind") != contract["case_kind"]
            or raw_run.get("candidate_model") != contract["candidate_model"]
            or raw_run.get("requested_model") != contract["requested_model"]
            or raw_run.get("attempt") != contract["attempt"]
        ):
            raise BakeoffError(f"formal run does not match canonical plan: {path.name}")
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
) -> dict[str, Any]:
    authority = load_authority(authority_path)
    recovery = _load_recovery_import(
        raw_root, authority, expected_recovery_import_sha256
    )
    runs_root = raw_root / "runs"
    run_roots = _discover_formal_run_roots(authority, runs_root)
    historical_run_ids = (
        recovery["imported_run_ids"] if recovery is not None else set()
    )
    historical_identity = (
        recovery["historical_harness_identity"]
        if recovery is not None
        else None
    )
    runs = []
    for root in run_roots:
        raw_run = _load_json(root / "run.json")
        run_id = raw_run.get("run_id")
        runs.append(
            evaluate_run(
                authority,
                root,
                historical_identity if run_id in historical_run_ids else None,
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
        "auxiliary_status": auxiliary_status,
        "runs": runs,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


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
    if (
        first_contract["run_id"] != "LRR-AGENT003-CLI-MODEL-SAVINGS-C01-R1"
        or first_contract["candidate_model"] != EXPECTED_CANDIDATES[0]
        or sixth_contract["run_id"] != "LRR-AGENT003-CLI-MODEL-SAVINGS-C02-R3"
        or sixth_contract["candidate_model"] != EXPECTED_CANDIDATES[1]
    ):
        raise BakeoffError("canonical resume-plan mapping self-test failed")
    expect_rejection(
        "unknown formal ordinal",
        lambda: _canonical_run_contract(authority, 25),
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

        manifest_path = temp_root / "recovery-import.json"
        manifest_path.write_bytes(
            (json.dumps({"schema_version": RECOVERY_IMPORT_SCHEMA}) + "\n").encode("utf-8")
        )
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        _load_anchored_json(manifest_path, manifest_sha)
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
                if manifest_value.get("schema_version") != RECOVERY_IMPORT_SCHEMA:
                    raise BakeoffError("recovery import schema is invalid")
            _write_json(args.snapshot_output, snapshot_session_store(args.snapshot_copilot_home))
            return 0
        if args.authority is None:
            raise BakeoffError("--authority is required")
        if args.self_test:
            return self_test(args.authority)
        if args.raw_root is None or args.output is None:
            raise BakeoffError("collection requires --raw-root and --output")
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
