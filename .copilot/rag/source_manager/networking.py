from __future__ import annotations

import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .errors import SourceManagerError
from .subprocess_stream import ProgressCallback, run_streaming_process


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn redirects into ordinary HTTP responses without a second request."""

    handler_order = urllib.request.HTTPRedirectHandler.handler_order - 1

    def http_error_302(
        self,
        request: urllib.request.Request,
        response: BinaryIO,
        code: int,
        message: str,
        headers: Any,
    ) -> Any:
        raise urllib.error.HTTPError(
            request.full_url,
            int(code),
            message,
            headers,
            response,
        )

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def reject_http_redirects(
    opener: urllib.request.OpenerDirector,
) -> urllib.request.OpenerDirector:
    """Install a fail-closed redirect handler on an existing configured opener."""

    marker = "_local_rag_redirects_rejected"
    if not bool(getattr(opener, marker, False)):
        opener.add_handler(_RejectRedirectHandler())
        setattr(opener, marker, True)
    return opener


def is_gitlab_token_request(headers: Mapping[str, Any]) -> bool:
    """Return whether an HTTP request carries GitLab's private token header."""

    return any(
        str(name).casefold() == "private-token"
        for name in headers
    )


@dataclass(frozen=True)
class SourceNetworkRoute:
    environment: dict[str, str]
    command_runner: Any
    http_get: Any


def resolve_source_network_route(
    rag_root: Path,
    *,
    environment: Mapping[str, str] | None,
    progress_callback: ProgressCallback | None = None,
) -> SourceNetworkRoute:
    """Resolve the canonical route exactly once for one Source operation."""
    network = _network_module(Path(rag_root))
    try:
        resolution = network.resolve_network_configuration(
            environ=environment,
            external_operation=True,
        )
    except Exception as exc:
        raise SourceManagerError(
            "Source network configuration could not be resolved"
        ) from exc
    effective_environment = dict(resolution.environment)
    opener = resolution.build_url_opener()
    gitlab_opener: urllib.request.OpenerDirector | None = None

    def command_runner(
        arguments: list[str],
        *,
        stdout_sink: BinaryIO | None = None,
    ):
        return run_streaming_process(
            arguments,
            timeout=300,
            env=effective_environment,
            progress_callback=progress_callback,
            stdout_sink=stdout_sink,
        )

    command_runner.supports_stdout_sink = True

    def http_get(
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ):
        nonlocal gitlab_opener
        request_opener = opener
        if is_gitlab_token_request(headers):
            if gitlab_opener is None:
                isolated_opener = resolution.build_url_opener()
                if isolated_opener is opener:
                    raise SourceManagerError(
                        "GitLab HTTP request requires an isolated network opener"
                    )
                gitlab_opener = reject_http_redirects(isolated_opener)
            request_opener = gitlab_opener
        request = urllib.request.Request(
            url,
            headers=dict(headers),
            method="GET",
        )
        try:
            with request_opener.open(request, timeout=timeout) as response:
                return (
                    int(response.status),
                    response.read(),
                    dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read(), dict(exc.headers or {})

    return SourceNetworkRoute(
        environment=effective_environment,
        command_runner=command_runner,
        http_get=http_get,
    )


def _network_module(rag_root: Path):
    tool_root = (
        Path(rag_root)
        / "gen_db"
        / "software_rag_tool"
    )
    if not tool_root.is_dir():
        raise SourceManagerError("network runtime is unavailable")
    value = str(tool_root)
    if value not in sys.path:
        sys.path.insert(0, value)
    try:
        from software_rag_tool import network
    except Exception as exc:
        raise SourceManagerError("network runtime is unavailable") from exc
    return network
