from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from source_manager import build_fetch_plan, execute_fetch_plan


def _svn_command(name: str) -> str:
    configured = os.environ.get("LOCAL_RAG_SVN_BIN")
    if configured:
        candidate = Path(configured) / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    discovered = shutil.which(name)
    if discovered:
        return discovered
    candidates = (
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Subversion"
        / "bin"
        / f"{name}.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "TortoiseSVN"
        / "bin"
        / f"{name}.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise AssertionError(f"required Subversion command is unavailable: {name}")


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("temporary svnserve did not start")


class SvnProtocolWindowsIntegrationTests(unittest.TestCase):
    def test_svn_transport_checkout_update_with_space_path(self) -> None:
        svn = _svn_command("svn")
        svnadmin = _svn_command("svnadmin")
        svnserve = _svn_command("svnserve")
        with tempfile.TemporaryDirectory(prefix="local-rag-svn-") as value:
            root = Path(value)
            repositories = root / "repositories"
            repository = repositories / "fixture"
            repositories.mkdir()
            subprocess.run([svnadmin, "create", str(repository)], check=True)
            (repository / "conf" / "svnserve.conf").write_text(
                "[general]\n"
                "anon-access = write\n"
                "auth-access = write\n",
                encoding="utf-8",
            )
            port = _unused_loopback_port()
            server = subprocess.Popen(
                [
                    svnserve,
                    "--daemon",
                    "--foreground",
                    "--listen-host",
                    "127.0.0.1",
                    "--listen-port",
                    str(port),
                    "--root",
                    str(repositories),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                _wait_for_port(port)
                incoming = root / "incoming"
                (incoming / "docs").mkdir(parents=True)
                (incoming / "README.md").write_text(
                    "temporary SVN integration fixture\n",
                    encoding="utf-8",
                )
                (incoming / "docs" / "design.md").write_text(
                    "temporary nested fixture\n",
                    encoding="utf-8",
                )
                repository_url = f"svn://127.0.0.1:{port}/fixture"
                subprocess.run(
                    [svn, "import", str(incoming), repository_url, "-m", "init"],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

                source_key = "src_svn-integration-0123456789ab"
                work_path = (
                    f"sources/{source_key}/work/ingest/{source_key}"
                )
                work = root / "Temporary DB" / work_path
                work.mkdir(parents=True)
                plan = build_fetch_plan(
                    source_key=source_key,
                    provider="svn",
                    settings={
                        "repository_url": repository_url,
                        "recursive": True,
                        "updated_within_days": None,
                    },
                    logical_root=work_path,
                    work_path=work_path,
                ).to_dict()

                def command_runner(arguments):
                    executable = (
                        svn if str(arguments[0]) == "svn" else arguments[0]
                    )
                    return subprocess.run(
                        [executable, *arguments[1:]],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )

                first = execute_fetch_plan(
                    plan,
                    work,
                    {"started_at": "2026-08-01T00:00:00Z"},
                    command_runner=command_runner,
                )
                self.assertEqual(2, first["documents"])
                self.assertTrue((work / "README.md").is_file())
                self.assertTrue((work / "docs" / "design.md").is_file())

                second = execute_fetch_plan(
                    plan,
                    work,
                    {"started_at": "2026-08-01T00:00:00Z"},
                    command_runner=command_runner,
                )
                self.assertEqual(2, second["documents"])
                self.assertEqual(first["revision"], second["revision"])
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
                if server.stderr is not None:
                    server.stderr.close()


if __name__ == "__main__":
    unittest.main()
