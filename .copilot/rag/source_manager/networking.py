from __future__ import annotations

import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError


@dataclass(frozen=True)
class SourceNetworkRoute:
    environment: dict[str, str]
    command_runner: Any
    http_get: Any


def resolve_source_network_route(
    rag_root: Path,
    *,
    environment: Mapping[str, str] | None,
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

    def command_runner(arguments: list[str]):
        return subprocess.run(
            arguments,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
            env=effective_environment,
        )

    def http_get(
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ):
        request = urllib.request.Request(
            url,
            headers=dict(headers),
            method="GET",
        )
        try:
            with opener.open(request, timeout=timeout) as response:
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
