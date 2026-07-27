from __future__ import annotations

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
sys.path.insert(0, str(TOOL_ROOT))

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
from setup_contract import completion_contract_payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify the installed runtime and databases without modifying them",
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
    if args.verify_only and args.migrate_legacy_marker:
        parser.error(
            "--verify-only and --migrate-legacy-marker cannot be combined"
        )
    if args.migrate_legacy_marker and (
        args.force_model or args.prepare_model or args.no_prepare_model
    ):
        parser.error(
            "--migrate-legacy-marker cannot install or prepare components"
        )

    try:
        network = resolve_network_configuration(
            cli_proxy=args.proxy,
            cli_ca_bundle=args.ca_bundle,
            cli_no_proxy=args.no_proxy,
            network_config=args.network_config,
            ignore_network_config=args.ignore_network_config,
            external_operation=not (
                args.verify_only or args.migrate_legacy_marker
            ),
        )
    except NetworkConfigError as exc:
        payload = _error_payload(
            failed_check="network_configuration",
            error_kind=exc.kind,
            message=str(exc),
        )
        _emit(payload, args.format)
        return 2

    here = Path(__file__).resolve().parent
    venv = here / ".venv"
    python = venv / (
        "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
    )
    marker = venv / ".rag-deps-installed"
    if args.migrate_legacy_marker and not _is_legacy_completion_marker(
        marker
    ):
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
    network_child_environment = dict(network.environment)
    route_token = secrets.token_urlsafe(24)
    network_child_environment[ROUTE_TOKEN] = route_token
    network_child_environment["PIP_CONFIG_FILE"] = os.devnull
    network_child_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    if not (args.verify_only or args.migrate_legacy_marker):
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

    verification = _run_verification(python)
    verification["network"] = network.details
    verification["warnings"] = [
        *network.warnings,
        *(verification.get("warnings") or []),
    ]
    if verification.get("setup_complete") and not args.verify_only:
        try:
            _write_completion_marker(marker, verification)
        except OSError as exc:
            verification = _error_payload(
                failed_check="completion_marker",
                error_kind="completion_marker_write_failed",
                message=str(exc),
                network=network,
            )
    _emit(verification, args.format)
    return 0 if verification.get("setup_complete") else 1


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
        print(redact_text(output), file=sys.stderr, end="" if output.endswith("\n") else "\n")
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
) -> None:
    payload = completion_contract_payload(
        runtime=verification.get("runtime") or {},
        rag_root=RAG_ROOT,
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
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
        print(
            "Ready: runtime verification passed"
            if payload.get("lookup_ready")
            else "Runtime ready; no healthy database is currently available"
        )
        return
    print(
        f"Setup verification failed: {payload.get('failed_check')} "
        f"({payload.get('error_kind')})"
    )
    if payload.get("next_action"):
        print(str(payload["next_action"]))


if __name__ == "__main__":
    raise SystemExit(main())
