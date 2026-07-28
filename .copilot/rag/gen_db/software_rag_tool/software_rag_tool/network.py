from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


NETWORK_CONFIG_VERSION = 1
NETWORK_MODES = {"off", "auto", "required"}
LOCAL_NO_PROXY = ("localhost", "127.0.0.1", "::1")
PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "PIP_PROXY",
    "pip_proxy",
)
CA_ENV_KEYS = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "PIP_CERT")
NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")
ROUTE_MARKER = "RAG_NETWORK_ROUTE_RESOLVED"
ROUTE_METADATA = "RAG_NETWORK_ROUTE_METADATA"
ROUTE_TOKEN = "RAG_NETWORK_ROUTE_TOKEN"
_CREDENTIAL_URL_RE = re.compile(
    r"(?P<scheme>https?://)(?P<userinfo>[^/\s@]+)@",
    re.IGNORECASE,
)
_SECRET_PARAMETER_RE = re.compile(
    r"(?P<name>password|passwd|token|access_token|api_key|apikey|secret)"
    r"(?P<separator>\s*[=:]\s*)"
    r"(?P<value>[^&\s,;]+)",
    re.IGNORECASE,
)


class NetworkConfigError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(redact_text(message))


@dataclass(repr=False)
class NetworkResolution:
    environment: dict[str, str]
    details: dict[str, Any]
    warnings: list[str]
    _proxy_url: str | None = None
    _ca_bundle: str | None = None

    def __repr__(self) -> str:
        return (
            "NetworkResolution("
            f"details={self.details!r}, warnings={self.warnings!r})"
        )

    @property
    def selected_route(self) -> str:
        return str(self.details.get("selected_route") or "direct")

    @property
    def ca_bundle_applied(self) -> bool:
        return bool(self.details.get("ca_bundle_applied"))

    def build_url_opener(self) -> urllib.request.OpenerDirector:
        proxy_handler = urllib.request.ProxyHandler(
            {
                "http": self._proxy_url,
                "https": self._proxy_url,
            }
            if self.selected_route == "proxy" and self._proxy_url
            else {}
        )
        handlers: list[Any] = [proxy_handler]
        if self.ca_bundle_applied and self._ca_bundle:
            context = ssl.create_default_context(cafile=self._ca_bundle)
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)


def add_network_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proxy", help="HTTP or HTTPS proxy URL for this operation")
    parser.add_argument("--ca-bundle", help="Readable company CA certificate bundle")
    parser.add_argument(
        "--no-proxy",
        help="Comma-separated hosts to merge into the effective no-proxy list",
    )
    parser.add_argument(
        "--network-config",
        help="Alternative persistent network configuration file",
    )
    parser.add_argument(
        "--ignore-network-config",
        action="store_true",
        help="Ignore the tool-local persistent network configuration",
    )


def default_network_config_path() -> Path:
    value = os.getenv("RAG_NETWORK_CONFIG")
    if value:
        return Path(value).expanduser()
    return Path(__file__).resolve().parents[3] / "config" / "network.json"


def resolve_network_configuration(
    *,
    cli_proxy: str | None = None,
    cli_ca_bundle: str | None = None,
    cli_no_proxy: str | None = None,
    network_config: str | Path | None = None,
    ignore_network_config: bool = False,
    environ: Mapping[str, str] | None = None,
    external_operation: bool = True,
    probe: Callable[[str, int, float], bool] | None = None,
    default_config_path: str | Path | None = None,
    inherited_route_token: str | None = None,
) -> NetworkResolution:
    base_env = dict(os.environ if environ is None else environ)
    explicit_cli_configuration = any(
        (
            cli_proxy,
            cli_ca_bundle,
            cli_no_proxy,
            network_config,
            ignore_network_config,
        )
    )
    if inherited_route_token and not explicit_cli_configuration:
        inherited = _resolution_from_marker(
            base_env,
            inherited_route_token,
        )
        if inherited is not None:
            return inherited

    if ignore_network_config and network_config:
        raise NetworkConfigError(
            "invalid_network_config",
            "--network-config and --ignore-network-config cannot be used together",
        )

    warnings: list[str] = []
    config, config_path, config_found, configured_mode = _load_optional_config(
        network_config=network_config,
        ignore=ignore_network_config,
        environ=base_env,
        warnings=warnings,
        default_path=default_config_path,
    )

    env_proxy = _first_nonempty(base_env, PROXY_ENV_KEYS)
    env_ca = _first_nonempty(base_env, CA_ENV_KEYS)
    config_proxy = (
        _optional_string(config.get("proxy_url"))
        if config and configured_mode != "off"
        else None
    )
    config_ca = (
        _optional_string(config.get("ca_bundle"))
        if config and configured_mode != "off"
        else None
    )
    config_no_proxy = (
        _config_no_proxy(config)
        if config and configured_mode != "off"
        else []
    )

    proxy_source = "none"
    proxy_url: str | None = None
    if cli_proxy:
        proxy_source = "cli"
        proxy_url = cli_proxy.strip()
    elif env_proxy:
        proxy_source = "environment"
        proxy_url = env_proxy
    elif config and configured_mode != "off" and config_proxy:
        proxy_source = "tool_config"
        proxy_url = config_proxy

    if proxy_url:
        _validate_proxy_url(proxy_url)

    selected_route = "direct"
    proxy_reachable: bool | None = None
    if not external_operation and proxy_source == "tool_config":
        selected_route = "not_required"
    elif proxy_source in {"cli", "environment"}:
        selected_route = "proxy"
    elif proxy_source == "tool_config":
        if configured_mode == "required":
            proxy_reachable = _probe_proxy(
                proxy_url,
                config,
                probe=probe,
            )
            if not proxy_reachable:
                raise NetworkConfigError(
                    "proxy_unavailable",
                    "The required configured proxy is unavailable.",
                )
            selected_route = "proxy"
        elif configured_mode == "auto":
            proxy_reachable = _probe_proxy(
                proxy_url,
                config,
                probe=probe,
            )
            if proxy_reachable:
                selected_route = "proxy"
            else:
                selected_route = "direct"
                warnings.append("proxy_config_unavailable_using_direct")
    elif config and configured_mode == "required" and external_operation:
        raise NetworkConfigError(
            "invalid_network_config",
            "mode=required needs a non-empty proxy_url",
        )

    ca_source = "none"
    ca_bundle: str | None = None
    if cli_ca_bundle:
        ca_source = "cli"
        ca_bundle = cli_ca_bundle.strip()
    elif env_ca:
        ca_source = "environment"
        ca_bundle = env_ca
    elif (
        config
        and configured_mode != "off"
        and proxy_source == "tool_config"
        and selected_route == "proxy"
        and config_ca
    ):
        ca_source = "tool_config"
        ca_bundle = config_ca

    ca_applied = bool(ca_bundle and external_operation)
    if ca_applied:
        _validate_ca_bundle(ca_bundle)

    no_proxy_values = _merge_no_proxy(
        *[base_env.get(key, "") for key in NO_PROXY_ENV_KEYS],
        cli_no_proxy or "",
        ",".join(config_no_proxy)
        if config and configured_mode != "off"
        else "",
        ",".join(LOCAL_NO_PROXY),
    )
    child_env = dict(base_env)
    if proxy_source == "tool_config" and selected_route != "proxy":
        _remove_keys(child_env, PROXY_ENV_KEYS)
    if selected_route == "proxy" and proxy_url:
        for key in PROXY_ENV_KEYS:
            child_env[key] = proxy_url
    if ca_bundle and ca_applied:
        for key in CA_ENV_KEYS:
            child_env[key] = ca_bundle
    elif ca_source == "tool_config":
        _remove_keys(child_env, CA_ENV_KEYS)
    child_env["NO_PROXY"] = ",".join(no_proxy_values)
    child_env["no_proxy"] = ",".join(no_proxy_values)

    proxy_display = redact_proxy_url(proxy_url) if proxy_url else None
    details: dict[str, Any] = {
        "config_file_found": config_found,
        "configured_mode": configured_mode,
        "selected_route": selected_route,
        "proxy_configured": bool(proxy_url),
        "proxy_reachable": proxy_reachable,
        "proxy_source": proxy_source,
        "proxy_display": proxy_display,
        "ca_bundle_configured": bool(cli_ca_bundle or env_ca or config_ca),
        "ca_bundle_applied": ca_applied,
        "ca_source": ca_source,
        "no_proxy_includes_localhost": all(
            value in no_proxy_values for value in LOCAL_NO_PROXY
        ),
    }
    safe_marker = json.dumps(details, ensure_ascii=True, sort_keys=True)
    child_env[ROUTE_MARKER] = "1"
    child_env[ROUTE_METADATA] = safe_marker
    return NetworkResolution(
        environment=child_env,
        details=details,
        warnings=warnings,
        _proxy_url=proxy_url if selected_route == "proxy" else None,
        _ca_bundle=ca_bundle if ca_applied else None,
    )


def apply_network_environment(resolution: NetworkResolution) -> None:
    for key in (*PROXY_ENV_KEYS, *CA_ENV_KEYS, *NO_PROXY_ENV_KEYS):
        os.environ.pop(key, None)
    for key in (
        *PROXY_ENV_KEYS,
        *CA_ENV_KEYS,
        *NO_PROXY_ENV_KEYS,
        ROUTE_MARKER,
        ROUTE_METADATA,
        ROUTE_TOKEN,
    ):
        value = resolution.environment.get(key)
        if value is not None:
            os.environ[key] = value


def redact_proxy_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        auth = "***:***@" if parsed.username is not None else ""
        path = parsed.path or ""
        return f"{parsed.scheme}://{auth}{host}{path}"
    except (TypeError, ValueError):
        return redact_text(str(value))


def redact_text(value: str) -> str:
    redacted = _CREDENTIAL_URL_RE.sub(
        lambda match: f"{match.group('scheme')}***:***@",
        str(value),
    )
    return _SECRET_PARAMETER_RE.sub(
        lambda match: (
            f"{match.group('name')}{match.group('separator')}***"
        ),
        redacted,
    )


def _load_optional_config(
    *,
    network_config: str | Path | None,
    ignore: bool,
    environ: Mapping[str, str],
    warnings: list[str],
    default_path: str | Path | None,
) -> tuple[dict[str, Any] | None, Path | None, bool, str]:
    if ignore:
        return None, None, False, "off"
    env_path = environ.get("RAG_NETWORK_CONFIG", "").strip()
    explicit = network_config is not None or bool(env_path)
    path = (
        Path(network_config).expanduser()
        if network_config is not None
        else Path(env_path).expanduser()
        if env_path
        else Path(default_path).expanduser()
        if default_path is not None
        else Path(__file__).resolve().parents[3] / "config" / "network.json"
    )
    if not path.exists():
        if explicit:
            raise NetworkConfigError(
                "invalid_network_config",
                f"Network configuration file does not exist: {path}",
            )
        return None, path, False, "auto"
    parsed_mode = "auto"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be a JSON object")
        version = payload.get("version")
        if version != NETWORK_CONFIG_VERSION:
            raise ValueError(
                f"unsupported version {version!r}; expected {NETWORK_CONFIG_VERSION}"
            )
        mode = str(payload.get("mode") or "auto").strip().lower()
        parsed_mode = mode
        if mode not in NETWORK_MODES:
            raise ValueError(f"unsupported mode: {mode}")
        if mode != "off":
            for key in ("proxy_url", "ca_bundle"):
                if payload.get(key) is not None and not isinstance(
                    payload.get(key), str
                ):
                    raise ValueError(f"{key} must be a string or null")
            if payload.get("proxy_url"):
                _validate_proxy_url(str(payload["proxy_url"]))
            timeout = payload.get("proxy_probe_timeout_seconds", 1.0)
            if not isinstance(timeout, (int, float)) or not 0.05 <= float(timeout) <= 30:
                raise ValueError("proxy_probe_timeout_seconds must be between 0.05 and 30")
            _config_no_proxy(payload)
        return payload, path, True, mode
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        NetworkConfigError,
    ) as exc:
        if explicit or parsed_mode == "required":
            raise NetworkConfigError(
                "invalid_network_config",
                f"Invalid explicit network configuration: {exc}",
            ) from None
        warnings.append("invalid_optional_network_config")
        return None, path, True, "auto"


def _config_no_proxy(config: Mapping[str, Any] | None) -> list[str]:
    if not config:
        return []
    value = config.get("no_proxy", [])
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise ValueError("no_proxy must be a string, a list of strings, or null")


def _validate_proxy_url(value: str) -> None:
    if any(char in value for char in "\r\n"):
        raise NetworkConfigError("invalid_proxy_config", "Proxy URL contains control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise NetworkConfigError("invalid_proxy_config", f"Invalid proxy URL: {exc}") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise NetworkConfigError(
            "invalid_proxy_config",
            "Proxy URL must be an http:// or https:// host URL without "
            "credentials, query, or fragment",
        )
    if port is not None and not 1 <= port <= 65535:
        raise NetworkConfigError("invalid_proxy_config", "Proxy port is out of range")


def _validate_ca_bundle(value: str) -> None:
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise NetworkConfigError(
            "invalid_ca_bundle",
            f"CA bundle is not a readable regular file: {path}",
        )


def _probe_proxy(
    proxy_url: str | None,
    config: Mapping[str, Any] | None,
    *,
    probe: Callable[[str, int, float], bool] | None,
) -> bool:
    if not proxy_url:
        return False
    parsed = urlsplit(proxy_url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    timeout = float((config or {}).get("proxy_probe_timeout_seconds", 1.0))
    checker = probe or _tcp_probe
    try:
        return bool(checker(str(parsed.hostname), port, timeout))
    except (OSError, TimeoutError):
        return False


def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    with socket.create_connection((host, port), timeout=timeout):
        return True


def _resolution_from_marker(
    environ: Mapping[str, str],
    inherited_route_token: str,
) -> NetworkResolution | None:
    if environ.get(ROUTE_MARKER) != "1":
        return None
    environment_token = environ.get(ROUTE_TOKEN, "")
    if not environment_token or not secrets.compare_digest(
        environment_token,
        inherited_route_token,
    ):
        raise NetworkConfigError(
            "invalid_resolved_network_marker",
            "The inherited resolved network marker token does not match.",
        )
    try:
        details = json.loads(environ.get(ROUTE_METADATA, "{}"))
        if not isinstance(details, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        raise NetworkConfigError(
            "invalid_resolved_network_marker",
            "The inherited resolved network marker is invalid.",
        ) from None
    proxy = _first_nonempty(environ, PROXY_ENV_KEYS)
    ca = _first_nonempty(environ, CA_ENV_KEYS)
    return NetworkResolution(
        environment=dict(environ),
        details=details,
        warnings=[],
        _proxy_url=proxy if details.get("selected_route") == "proxy" else None,
        _ca_bundle=ca if details.get("ca_bundle_applied") else None,
    )


def _first_nonempty(
    environ: Mapping[str, str],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NetworkConfigError(
            "invalid_network_config",
            "proxy_url and ca_bundle must be strings or null",
        )
    return value.strip() or None


def _merge_no_proxy(*values: str) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in str(value or "").split(","):
            item = item.strip()
            key = item.casefold()
            if not item or key in seen:
                continue
            seen.add(key)
            output.append(item)
    return output


def _remove_keys(environment: dict[str, str], keys: tuple[str, ...]) -> None:
    for key in keys:
        environment.pop(key, None)
