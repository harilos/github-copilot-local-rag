from __future__ import annotations

import atexit
import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(RAG_ROOT))
sys.path.insert(0, str(TOOL_ROOT))

from help_links import MANAGER_HELP_EPILOG
from software_rag_tool.network import (
    CA_ENV_KEYS,
    PROXY_ENV_KEYS,
    NetworkConfigError,
    NetworkResolution,
    ROUTE_TOKEN,
    add_network_arguments,
    redact_text,
    resolve_network_configuration,
)
from setup_contract import (
    completion_marker_for,
    completion_contract_payload,
    completion_contract_valid,
    requirements_fingerprint,
)
from portable_runtime import (
    PortableRuntimeError,
    load_and_verify_runtime,
    manifest_path_for,
)


TEMPORARY_REPAIR_LABEL_JA = "検索利用判定を修復する（一時的）"
TEMPORARY_REPAIR_ACTION = "repair_completion_marker_temporarily"
TEMPORARY_REPAIR_ARGUMENTS = (
    "--repair-completion-marker",
    "--format",
    "json",
)
def _configure_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")




def main() -> int:
    _configure_utf8_streams()
    parser = argparse.ArgumentParser(
        epilog=MANAGER_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help=(
            "Verify the installed runtime, databases, and lookup completion "
            "marker without modifying them"
        ),
    )
    parser.add_argument(
        "--migrate-legacy-marker",
        action="store_true",
        help=(
            "Verify an existing legacy completion marker offline and replace "
            "it with the machine-verifiable contract"
        ),
    )
    parser.add_argument(
        "--refresh-completion-marker",
        action="store_true",
        help=(
            "Verify the existing runtime offline and atomically refresh its "
            "machine-verifiable completion marker"
        ),
    )
    parser.add_argument(
        "--repair-completion-marker",
        action="store_true",
        help=(
            "Temporarily repair only the lookup completion marker after an "
            "offline runtime verification. The previous marker is kept unless "
            "the replacement is fully verified."
        ),
    )
    parser.add_argument(
        "--configure-vscode-auto-approve",
        action="store_true",
        help=(
            "Explicitly opt in to narrowly scoped VS Code terminal "
            "auto-approval rules after packaged runtime verification"
        ),
    )
    parser.add_argument(
        "--prepare-model",
        action="store_true",
        help="Deprecated: model preparation is now the default",
    )
    parser.add_argument(
        "--no-prepare-model",
        action="store_true",
        help="Do not download or recreate the model; verification still applies",
    )
    parser.add_argument(
        "--force-model",
        action="store_true",
        help="Recreate the ONNX INT8 model even if it exists",
    )
    add_network_arguments(parser)
    args = parser.parse_args()
    if args.prepare_model and args.no_prepare_model:
        parser.error("--prepare-model and --no-prepare-model cannot be used together")
    if args.verify_only and (args.force_model or args.prepare_model):
        parser.error("--verify-only cannot prepare or force a model")
    offline_modes = sum(
        bool(value)
        for value in (
            args.verify_only,
            args.migrate_legacy_marker,
            args.refresh_completion_marker,
            args.repair_completion_marker,
        )
    )
    if offline_modes > 1:
        parser.error(
            "--verify-only, --migrate-legacy-marker, "
            "--refresh-completion-marker, and --repair-completion-marker "
            "are mutually exclusive"
        )
    if args.configure_vscode_auto_approve and offline_modes:
        parser.error(
            "--configure-vscode-auto-approve requires a normal setup run"
        )
    if (
        args.migrate_legacy_marker
        or args.refresh_completion_marker
        or args.repair_completion_marker
    ) and (
        args.force_model or args.prepare_model or args.no_prepare_model
    ):
        parser.error(
            "completion-marker maintenance cannot install or prepare components"
        )

    here, venv, python, marker = _setup_paths()
    packaged_runtime = None
    packaged_manifest = manifest_path_for(here)
    if packaged_manifest.is_file():
        try:
            packaged_runtime = load_and_verify_runtime(packaged_manifest)
        except PortableRuntimeError as exc:
            _emit(
                _error_payload(
                    failed_check="packaged_runtime",
                    error_kind="packaged_runtime_invalid",
                    message=str(exc),
                ),
                args.format,
            )
            return 1
        if not args.verify_only:
            try:
                _acquire_setup_lock(here)
            except SetupLockError as exc:
                _emit(
                    _error_payload(
                        failed_check="setup_lock",
                        error_kind="setup_already_running",
                        message=str(exc),
                    ),
                    args.format,
                )
                return 1

        if any(
            (
                args.proxy,
                args.ca_bundle,
                args.no_proxy,
                args.network_config,
                args.force_model,
                args.prepare_model,
                args.no_prepare_model,
            )
        ):
            parser.error("packaged runtime setup is offline and immutable")
    try:
        network = (_packaged_network() if packaged_runtime is not None else resolve_network_configuration(
            cli_proxy=args.proxy,
            cli_ca_bundle=args.ca_bundle,
            cli_no_proxy=args.no_proxy,
            network_config=args.network_config,
            ignore_network_config=args.ignore_network_config,
            external_operation=not (
                args.verify_only
                or args.migrate_legacy_marker
                or args.refresh_completion_marker
                or args.repair_completion_marker
            ),
        ))
    except NetworkConfigError as exc:
        payload = _error_payload(
            failed_check="network_configuration",
            error_kind=exc.kind,
            message=str(exc),
        )
        _emit(payload, args.format)
        return 2

    if args.migrate_legacy_marker and not _is_legacy_completion_marker(marker):
        payload = _error_payload(
            failed_check="completion_marker",
            error_kind="legacy_completion_marker_not_found",
            message=(
                "The completion marker is not the legacy marker expected "
                "by this migration."
            ),
            network=network,
        )
        _emit(payload, args.format)
        return 1
    if args.refresh_completion_marker:
        try:
            _invalidate_completion_marker(marker)
        except SetupStepError as exc:
            payload = _error_payload(
                failed_check=exc.phase,
                error_kind=exc.error_kind,
                message=str(exc),
                network=network,
            )
            _emit(payload, args.format)
            return 1

    network_child_environment = dict(network.environment)
    route_token = secrets.token_urlsafe(24)
    network_child_environment[ROUTE_TOKEN] = route_token
    network_child_environment["PIP_CONFIG_FILE"] = os.devnull
    network_child_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    if packaged_runtime is None and not (
        args.verify_only
        or args.migrate_legacy_marker
        or args.refresh_completion_marker
        or args.repair_completion_marker
    ):
        try:
            _invalidate_completion_marker(marker)
            if not python.exists():
                _run_child(
                    [sys.executable, "-m", "venv", str(venv)],
                    env=network_child_environment,
                    phase="venv",
                )
            _run_child(
                [str(python), "-m", "pip", "install", "--upgrade", "pip"],
                env=network_child_environment,
                phase="pip_upgrade",
            )
            _run_child(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(here / "requirements.txt"),
                ],
                env=network_child_environment,
                phase="dependencies",
            )
            if not args.no_prepare_model:
                command = [
                    str(python),
                    str(here / "prepare_onnx_model.py"),
                    "--resolved-network-token",
                    route_token,
                ]
                if args.force_model:
                    command.append("--force")
                _run_child(
                    command,
                    env=network_child_environment,
                    phase="model_prepare",
                )
        except SetupStepError as exc:
            payload = _error_payload(
                failed_check=exc.phase,
                error_kind=exc.error_kind,
                message=str(exc),
                network=network,
            )
            _emit(payload, args.format)
            return 1

    marker_maintenance = bool(
        args.refresh_completion_marker or args.repair_completion_marker
    )
    repair_previous_marker: bytes | None = None
    repair_marker_previously_existed = False
    if args.repair_completion_marker:
        try:
            repair_previous_marker = _read_optional_bytes(marker)
            repair_marker_previously_existed = marker.is_file()
        except SetupStepError as exc:
            payload = _error_payload(
                failed_check=exc.phase,
                error_kind=exc.error_kind,
                message=str(exc),
                network=network,
            )
            _emit(payload, args.format)
            return 1
    requirements_before: str | None = None
    if marker_maintenance:
        try:
            requirements_before = requirements_fingerprint(RAG_ROOT)
        except OSError as exc:
            payload = _error_payload(
                failed_check="completion_marker",
                error_kind="requirements_fingerprint_failed",
                message=str(exc),
                network=network,
            )
            _emit(payload, args.format)
            return 1

    verification = _run_verification(python)
    verification["network"] = network.details
    verification["warnings"] = [
        *network.warnings,
        *(verification.get("warnings") or []),
    ]
    if (
        packaged_runtime is not None
        and verification.get("setup_complete")
        and not args.verify_only
    ):
        if not args.configure_vscode_auto_approve:
            vscode = {
                "status": "skipped_default_off",
                "targets_checked": 0,
                "targets_changed": 0,
                "policy_effectiveness": "not_requested",
            }
        else:
            appdata = os.environ.get("APPDATA")
            if appdata:
                try:
                    from vscode_settings import configure_vscode

                    vscode = configure_vscode(
                        RAG_ROOT.parent, Path(appdata).expanduser()
                    )
                except Exception as exc:
                    vscode = {
                        "status": "error",
                        "targets_checked": 0,
                        "targets_changed": 0,
                        "policy_effectiveness": "unknown",
                        "error_kinds": [type(exc).__name__],
                    }
            else:
                vscode = {
                    "status": "manual_action_required",
                    "targets_checked": 0,
                    "targets_changed": 0,
                    "policy_effectiveness": "unknown",
                }
        verification["integrations"] = {"vscode": vscode}

    requirements_after: str | None = None
    if marker_maintenance:
        try:
            requirements_after = requirements_fingerprint(RAG_ROOT)
        except OSError as exc:
            verification = _error_payload(
                failed_check="completion_marker",
                error_kind="requirements_fingerprint_failed",
                message=str(exc),
                network=network,
            )
        if (
            verification.get("setup_complete")
            and requirements_after != requirements_before
        ):
            verification = _error_payload(
                failed_check="completion_marker",
                error_kind="requirements_changed_during_verification",
                message=(
                    "The requirements fingerprint changed while runtime "
                    "verification was running."
                ),
                network=network,
            )

    normal_previous_marker: bytes | None = None
    normal_marker_previously_valid = False
    if (
        verification.get("setup_complete")
        and not args.verify_only
        and not marker_maintenance
        and marker.is_file()
    ):
        previous_valid, _previous_reason = completion_contract_valid(
            marker, RAG_ROOT
        )
        if previous_valid:
            normal_previous_marker = _read_optional_bytes(marker)
            normal_marker_previously_valid = normal_previous_marker is not None

    if verification.get("setup_complete") and not args.verify_only:
        try:
            _write_completion_marker(
                marker,
                verification,
                requirements_sha256=(
                    requirements_before if marker_maintenance else None
                ),
            )
        except OSError as exc:
            verification = _error_payload(
                failed_check="completion_marker",
                error_kind="completion_marker_write_failed",
                message=str(exc),
                network=network,
            )
        else:
            if marker_maintenance:
                verification = _postvalidate_marker_maintenance(
                    verification,
                    marker=marker,
                    requirements_before=requirements_before,
                    requirements_after=requirements_after,
                    repair_temporarily=bool(args.repair_completion_marker),
                    previous_marker=repair_previous_marker,
                    marker_previously_existed=repair_marker_previously_existed,
                    network=network,
                )
            else:
                valid, reason = completion_contract_valid(marker, RAG_ROOT)
                if not valid:
                    discard_error = _restore_previous_marker(
                        marker,
                        previous_marker=normal_previous_marker,
                        marker_previously_existed=normal_marker_previously_valid,
                        repair_temporarily=True,
                    )
                    message = (
                        "The completion marker failed post-write validation: "
                        f"{reason}"
                    )
                    if discard_error:
                        message += (
                            "; the invalid marker could not be removed: "
                            f"{discard_error}"
                        )
                    verification = _error_payload(
                        failed_check="completion_marker",
                        error_kind="completion_marker_postvalidation_failed",
                        message=message,
                        network=network,
                    )
    elif args.verify_only:
        _attach_completion_marker_diagnostics(
            verification,
            marker=marker,
            python=python,
        )

    _emit(verification, args.format)
    return 0 if verification.get("setup_complete") else 1


def _postvalidate_marker_maintenance(
    verification: dict[str, Any],
    *,
    marker: Path,
    requirements_before: str | None,
    requirements_after: str | None,
    repair_temporarily: bool,
    previous_marker: bytes | None,
    marker_previously_existed: bool,
    network: NetworkResolution,
) -> dict[str, Any]:
    try:
        requirements_final = requirements_fingerprint(RAG_ROOT)
    except OSError as exc:
        discard_error = _restore_previous_marker(
            marker,
            previous_marker=previous_marker,
            marker_previously_existed=marker_previously_existed,
            repair_temporarily=repair_temporarily,
        )
        message = (
            "The requirements fingerprint could not be read after the "
            f"completion marker was refreshed: {exc}"
        )
        if discard_error:
            message += (
                "; the invalid marker could not be removed: "
                f"{discard_error}"
            )
        return _error_payload(
            failed_check="completion_marker",
            error_kind="requirements_fingerprint_failed",
            message=message,
            network=network,
        )

    if not (
        requirements_before == requirements_after == requirements_final
    ):
        discard_error = _restore_previous_marker(
            marker,
            previous_marker=previous_marker,
            marker_previously_existed=marker_previously_existed,
            repair_temporarily=repair_temporarily,
        )
        message = (
            "The requirements fingerprint changed while the completion "
            "marker was being refreshed."
        )
        if discard_error:
            message += (
                "; the invalid marker could not be removed: "
                f"{discard_error}"
            )
        return _error_payload(
            failed_check="completion_marker",
            error_kind="requirements_changed_during_verification",
            message=message,
            network=network,
        )

    valid, reason = completion_contract_valid(marker, RAG_ROOT)
    if not valid:
        discard_error = _restore_previous_marker(
            marker,
            previous_marker=previous_marker,
            marker_previously_existed=marker_previously_existed,
            repair_temporarily=repair_temporarily,
        )
        message = (
            "The refreshed completion marker failed post-write validation: "
            f"{reason}"
        )
        if discard_error:
            message += (
                "; the invalid marker could not be removed: "
                f"{discard_error}"
            )
        return _error_payload(
            failed_check="completion_marker",
            error_kind="completion_marker_postvalidation_failed",
            message=message,
            network=network,
        )

    action = "repaired_temporarily" if repair_temporarily else "refreshed"
    verification["completion_marker"] = {
        "action": action,
        "refreshed": True,
        "valid": True,
        "reason": None,
        "temporary": repair_temporarily,
        "label_ja": (
            TEMPORARY_REPAIR_LABEL_JA
            if repair_temporarily
            else "検索利用判定を更新"
        ),
        "requirements_sha256": requirements_final,
    }
    if repair_temporarily:
        verification["next_action"] = (
            "検索利用判定の完了マーカーだけを一時的に修復しました。"
            "Python環境、モデル、DB、検索索引は再構築していません。"
        )
    return verification


def _attach_completion_marker_diagnostics(
    verification: dict[str, Any],
    *,
    marker: Path,
    python: Path,
) -> None:
    valid, reason = completion_contract_valid(marker, RAG_ROOT)
    repair_available = bool(
        python.is_file() and verification.get("setup_complete")
    )
    verification["completion_marker"] = {
        "action": "verified",
        "refreshed": False,
        "valid": valid,
        "reason": reason,
        "temporary": False,
        "repair_available": repair_available,
        "repair_action": (
            TEMPORARY_REPAIR_ACTION if repair_available and not valid else None
        ),
        "repair_label_ja": (
            TEMPORARY_REPAIR_LABEL_JA
            if repair_available and not valid
            else None
        ),
        "repair_command": (
            {
                "script": "query/setup.py",
                "arguments": list(TEMPORARY_REPAIR_ARGUMENTS),
            }
            if repair_available and not valid
            else None
        ),
    }
    if not valid and verification.get("setup_complete"):
        had_healthy_database = bool(verification.get("lookup_ready"))
        verification["runtime_lookup_ready"] = had_healthy_database
        verification["lookup_ready"] = False
        verification["status"] = (
            "runtime_ready_completion_marker_repair_required"
        )
        verification["next_action"] = (
            f"{TEMPORARY_REPAIR_LABEL_JA}を実行してください。"
            "これは完了マーカーだけを再検証して置き換え、"
            "モデルやDBを再構築しません。"
        )


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SetupStepError(
            "completion_marker",
            -1,
            str(exc),
        ) from None


def _restore_previous_marker(
    marker: Path,
    *,
    previous_marker: bytes | None,
    marker_previously_existed: bool,
    repair_temporarily: bool,
) -> str | None:
    if not repair_temporarily:
        return _discard_completion_marker(marker)
    try:
        if marker_previously_existed and previous_marker is not None:
            _atomic_write_bytes(marker, previous_marker)
        else:
            marker.unlink(missing_ok=True)
    except OSError as exc:
        return redact_text(str(exc))
    return None


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.restore.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _packaged_network() -> NetworkResolution:
    return NetworkResolution(
        environment=_offline_child_environment(os.environ),
        details={
            "mode": "off",
            "selected_route": "offline",
            "external_operation": False,
            "packaged_runtime": True,
        },
        warnings=[],
    )


def _setup_paths() -> tuple[Path, Path, Path, Path]:
    here = Path(__file__).resolve().parent
    venv = here / ".venv"
    python = venv / (
        "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
    )
    return here, venv, python, completion_marker_for(here)


_SETUP_LOCK_HANDLE = None


class SetupLockError(RuntimeError):
    pass


def _acquire_setup_lock(query_root: Path) -> None:
    global _SETUP_LOCK_HANDLE
    if _SETUP_LOCK_HANDLE is not None:
        return
    lock_path = query_root / ".setup.lock"
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise SetupLockError("another Local RAG setup is already running") from exc
    _SETUP_LOCK_HANDLE = handle
    atexit.register(_release_setup_lock)


def _release_setup_lock() -> None:
    global _SETUP_LOCK_HANDLE
    handle = _SETUP_LOCK_HANDLE
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        _SETUP_LOCK_HANDLE = None



class SetupStepError(RuntimeError):

    def __init__(self, phase: str, returncode: int, output: str) -> None:
        self.phase = phase
        safe = redact_text(output).strip()
        normalized = safe.casefold()
        if (
            "certificate verify failed" in normalized
            or "sslcertverificationerror" in normalized
            or "certificate verification" in normalized
        ):
            self.error_kind = "ca_verification_failed"
        elif "407" in normalized and "proxy" in normalized:
            self.error_kind = "proxy_authentication_failed"
        else:
            self.error_kind = "child_process_failed"
        message = f"{phase} failed with exit code {returncode}"
        if safe:
            message += f": {safe[-1200:]}"
        super().__init__(message)


def _run_child(
    command: list[str],
    *,
    env: dict[str, str],
    phase: str,
) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        raise SetupStepError(phase, -1, str(exc)) from None
    output = "\n".join(
        part
        for part in (completed.stdout, completed.stderr)
        if part
    )
    if output:
        print(
            redact_text(output),
            file=sys.stderr,
            end="" if output.endswith("\n") else "\n",
        )
    if completed.returncode != 0:
        raise SetupStepError(phase, completed.returncode, output)


def _run_verification(python: Path) -> dict[str, Any]:
    if not python.is_file():
        return _error_payload(
            failed_check="venv",
            error_kind="venv_python_missing",
            message=f"Virtual-environment Python was not found: {python}",
        )
    environment = _offline_child_environment(os.environ)
    try:
        completed = subprocess.run(
            [
                str(python),
                str(Path(__file__).with_name("setup_verification.py")),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return _error_payload(
            failed_check="verification",
            error_kind="verification_timeout",
            message="Post-install verification exceeded 120 seconds.",
        )
    except OSError as exc:
        return _error_payload(
            failed_check="verification",
            error_kind="verification_process_failed",
            message=str(exc),
        )
    if completed.stderr:
        print(redact_text(completed.stderr), file=sys.stderr, end="")
    try:
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        return _error_payload(
            failed_check="verification",
            error_kind="invalid_verification_output",
            message=redact_text(completed.stdout[-1000:]),
        )
    return payload


def _write_completion_marker(
    marker: Path,
    verification: dict[str, Any],
    *,
    requirements_sha256: str | None = None,
) -> None:
    payload = completion_contract_payload(
        runtime=verification.get("runtime") or {},
        rag_root=RAG_ROOT,
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
    if requirements_sha256 is not None:
        payload["requirements_sha256"] = requirements_sha256
    marker.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{marker.name}.",
        dir=str(marker.parent),
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _invalidate_completion_marker(marker: Path) -> None:
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        raise SetupStepError(
            "completion_marker",
            -1,
            str(exc),
        ) from None


def _discard_completion_marker(marker: Path) -> str | None:
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        return redact_text(str(exc))
    return None


def _is_legacy_completion_marker(marker: Path) -> bool:
    try:
        return marker.read_text(encoding="utf-8").strip() == "ok"
    except OSError:
        return False


def _offline_child_environment(
    base: dict[str, str] | os._Environ[str],
) -> dict[str, str]:
    environment = dict(base)
    for key in (*PROXY_ENV_KEYS, *CA_ENV_KEYS):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    environment["ANONYMIZED_TELEMETRY"] = "False"
    return environment


def _error_payload(
    *,
    failed_check: str,
    error_kind: str,
    message: str,
    network: NetworkResolution | None = None,
) -> dict[str, Any]:
    return {
        "status": "error",
        "setup_complete": False,
        "lookup_ready": False,
        "runtime": {},
        "network": network.details if network else {},
        "databases": {"healthy": [], "unhealthy": []},
        "warnings": list(network.warnings if network else []),
        "failed_check": failed_check,
        "error_kind": error_kind,
        "error": redact_text(message),
        "next_action": (
            "Confirm the company CA certificate and proxy configuration."
            if failed_check == "network_configuration"
            or error_kind
            in {"ca_verification_failed", "proxy_authentication_failed"}
            else "Review sanitized stderr diagnostics and run setup again."
        ),
    }


def _emit(payload: dict[str, Any], output_format: str) -> None:
    for warning in payload.get("warnings") or []:
        print(redact_text(str(warning)), file=sys.stderr)
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if payload.get("setup_complete"):
        if payload.get("lookup_ready"):
            print("Ready: runtime verification and lookup gate passed")
        elif (
            payload.get("status")
            == "runtime_ready_completion_marker_repair_required"
        ):
            print(
                "Runtime and database verification passed, but the lookup "
                f"completion marker needs repair ({TEMPORARY_REPAIR_LABEL_JA})."
            )
        else:
            print("Runtime ready; no healthy database is currently available")
        if payload.get("next_action"):
            print(str(payload["next_action"]))
        vscode = (payload.get("integrations") or {}).get("vscode")
        if vscode is not None:
            print(
                "VS Code: use Copilot Chat in Agent mode; in Configure Tools "
                "enable runInTerminal and enable readFile for file delivery."
            )
            print("Global auto-approve, Bypass Approvals, and Autopilot are not required.")

        return
    print(
        f"Setup verification failed: {payload.get('failed_check')} "
        f"({payload.get('error_kind')})"
    )
    if payload.get("next_action"):
        print(str(payload["next_action"]))


if __name__ == "__main__":
    raise SystemExit(main())
