from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from setup_contract import (
    completion_contract_payload,
    completion_contract_valid,
)


QUERY_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = QUERY_ROOT.parents[2]
INSTALL_SH = REPOSITORY_ROOT / "install.sh"
INSTALL_PS1 = REPOSITORY_ROOT / "install.ps1"


class InstallerExclusionContractTests(unittest.TestCase):
    @staticmethod
    def _write_current_valid_marker(
        rag_root: Path,
        marker: Path,
    ) -> None:
        query_requirements = rag_root / "query" / "requirements.txt"
        tool_requirements = (
            rag_root
            / "gen_db"
            / "software_rag_tool"
            / "requirements.txt"
        )
        query_requirements.parent.mkdir(parents=True, exist_ok=True)
        tool_requirements.parent.mkdir(parents=True, exist_ok=True)
        query_requirements.write_text(
            "-r ../gen_db/software_rag_tool/requirements.txt\n",
            encoding="utf-8",
        )
        tool_requirements.write_text("demo>=1\n", encoding="utf-8")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                completion_contract_payload(
                    runtime={
                        "venv": "pass",
                        "dependencies": "pass",
                        "requirements": "pass",
                        "pip_check": "pass",
                        "model_files": "pass",
                        "model_manifest": "pass",
                        "model_load": "pass",
                        "embedding_dimension": 256,
                        "list_dbs": "pass",
                    },
                    rag_root=rag_root,
                    verified_at="2026-07-27T00:00:00+00:00",
                )
            ),
            encoding="utf-8",
        )
        assert completion_contract_valid(marker, rag_root)[0]

    def test_posix_and_windows_installers_express_same_exclusions(self) -> None:
        shell = INSTALL_SH.read_text(encoding="utf-8")
        powershell = INSTALL_PS1.read_text(encoding="utf-8")

        for fragment in (
            "./rag/config/network.json",
            "./rag/config/manage-custom.json",
            "./rag/config/windows-test-connection.local.json",
            "./rag/query/run",
            "*/.venv",
            "*/__pycache__",
            "*.pyc",
            "*.pyo",
            "*/.DS_Store",
        ):
            self.assertIn(fragment, shell)
        for fragment in (
            "Test-InstallPayloadExcluded",
            r"rag\config\network.json",
            r"rag\config\manage-custom.json",
            r"rag\config\windows-test-connection.local.json",
            r"rag\query\run",
            '".venv"',
            '"__pycache__"',
            '".pyc"',
            '".pyo"',
            '".DS_Store"',
        ):
            self.assertIn(fragment, powershell)
        self.assertNotIn("Remove-Item", powershell)
        self.assertNotIn("rm -rf", shell)
        self.assertIn("--refresh-completion-marker", shell)
        self.assertIn("--refresh-completion-marker", powershell)
        self.assertNotIn("--migrate-legacy-marker", shell)
        self.assertNotIn("--migrate-legacy-marker", powershell)
        self.assertIn("exit 1", shell)
        self.assertIn("throw (", powershell)
        self.assertIn("setup_required:", shell)
        self.assertIn("setup_required:", powershell)
        self.assertIn('if [ -x "$RUNTIME_PYTHON" ]; then', shell)
        self.assertIn(
            "if (Test-Path -LiteralPath $RuntimePython -PathType Leaf)",
            powershell,
        )
        self.assertIn(".pre-update.", shell)
        self.assertIn(".pre-update.", powershell)
        self.assertIn('mv "$COMPLETION_MARKER"', shell)
        self.assertIn(
            "[System.IO.File]::Move($CompletionMarker, $PreUpdateMarker)",
            powershell,
        )
        for retired in (
            "rag/export_migration.sh",
            "rag/migration_archive.py",
            "rag/gen_db/migrate_source_metadata.py",
            (
                "rag/gen_db/software_rag_tool/software_rag_tool/"
                "source_metadata_migration.py"
            ),
            "skills/local-rag-admin/SKILL.md",
        ):
            self.assertIn(retired, shell)
            self.assertIn(retired.replace("/", "\\"), powershell)

    @unittest.skipIf(
        os.name == "nt",
        "POSIX installer execution is not applicable on Windows",
    )
    def test_posix_install_preserves_runtime_and_skips_transients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            payload = source / ".copilot" / "rag"
            query = payload / "query"
            target = root / "target"
            target_query = target / "rag" / "query"

            source.mkdir()
            shutil.copy2(INSTALL_SH, source / "install.sh")
            (query / "run").mkdir(parents=True)
            (query / ".venv" / "bin").mkdir(parents=True)
            (query / "__pycache__").mkdir(parents=True)
            (payload / "config").mkdir(parents=True)
            (query / "search.py").write_text(
                "print('installed source')\n",
                encoding="utf-8",
            )
            (payload / "config" / "network.json").write_text(
                '{"source":"must-not-copy"}\n',
                encoding="utf-8",
            )
            (payload / "config" / "manage-custom.json").write_text(
                '{"source":"must-not-copy"}\n',
                encoding="utf-8",
            )
            (
                payload
                / "config"
                / "windows-test-connection.local.json"
            ).write_text(
                '{"source":"must-not-copy"}\n',
                encoding="utf-8",
            )
            (query / "run" / "ragd.json").write_text(
                '{"pid":82059,"source":"stale"}\n',
                encoding="utf-8",
            )
            (query / "run" / "source-only.tmp").write_text(
                "transient\n",
                encoding="utf-8",
            )
            (query / ".venv" / "bin" / "python").write_text(
                "source venv\n",
                encoding="utf-8",
            )
            (query / ".venv" / ".rag-deps-installed").write_text(
                "source marker\n",
                encoding="utf-8",
            )
            (query / "__pycache__" / "module.pyc").write_bytes(b"pyc")
            (query / "module.pyc").write_bytes(b"pyc")
            (query / "module.pyo").write_bytes(b"pyo")
            (query / ".DS_Store").write_bytes(b"finder")

            (target / "rag" / "config").mkdir(parents=True)
            (target_query / "run").mkdir(parents=True)
            (target_query / ".venv" / "bin").mkdir(parents=True)
            target_network = target / "rag" / "config" / "network.json"
            target_manage_custom = (
                target / "rag" / "config" / "manage-custom.json"
            )
            target_windows_test = (
                target
                / "rag"
                / "config"
                / "windows-test-connection.local.json"
            )
            target_state = target_query / "run" / "ragd.json"
            target_python = target_query / ".venv" / "bin" / "python"
            target_marker = (
                target_query / ".venv" / ".rag-deps-installed"
            )
            retired_files = (
                target / "rag" / "export_migration.sh",
                target / "rag" / "migration_archive.py",
                target / "rag" / "gen_db" / "migrate_source_metadata.py",
                (
                    target
                    / "rag"
                    / "gen_db"
                    / "software_rag_tool"
                    / "software_rag_tool"
                    / "source_metadata_migration.py"
                ),
                target / "skills" / "local-rag-admin" / "SKILL.md",
            )
            for retired in retired_files:
                retired.parent.mkdir(parents=True, exist_ok=True)
                retired.write_text("retired\n", encoding="utf-8")
            refresh_log = root / "refresh-arguments.txt"
            target_network.write_text(
                '{"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_manage_custom.write_text(
                '{"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_windows_test.write_text(
                '{"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_state.write_text(
                '{"pid":1234,"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_python.write_text(
                (
                    "#!/bin/sh\n"
                    f"printf '%s\\n' \"$@\" > '{refresh_log}'\n"
                    "printf '%s\\n' '{\"refreshed\":true}' > "
                    "\"$(dirname \"$0\")/../.rag-deps-installed\"\n"
                    "exit 0\n"
                ),
                encoding="utf-8",
            )
            target_python.chmod(0o755)
            self._write_current_valid_marker(
                target / "rag",
                target_marker,
            )
            before = {
                path: path.read_bytes()
                for path in (
                    target_network,
                    target_manage_custom,
                    target_windows_test,
                    target_state,
                    target_python,
                )
            }

            environment = os.environ.copy()
            environment["COPILOT_HOME"] = str(target)
            environment["HOME"] = str(root / "home")
            completed = subprocess.run(
                ["sh", str(source / "install.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=completed.stderr,
            )
            for path, contents in before.items():
                self.assertEqual(contents, path.read_bytes(), msg=str(path))
            self.assertEqual(
                '{"refreshed":true}\n',
                target_marker.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                [],
                list(target_marker.parent.glob(
                    ".rag-deps-installed.pre-update.*"
                )),
            )
            self.assertEqual(
                "print('installed source')\n",
                (target_query / "search.py").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "--refresh-completion-marker",
                refresh_log.read_text(encoding="utf-8"),
            )
            for relative in (
                "run/source-only.tmp",
                "__pycache__/module.pyc",
                "module.pyc",
                "module.pyo",
                ".DS_Store",
            ):
                self.assertFalse(
                    (target_query / relative).exists(),
                    msg=relative,
                )
            for retired in retired_files:
                self.assertFalse(retired.exists(), msg=str(retired))
            self.assertFalse(
                (target / "skills" / "local-rag-admin").exists()
            )

    @unittest.skipIf(
        os.name == "nt",
        "POSIX installer execution is not applicable on Windows",
    )
    def test_posix_install_fails_closed_when_runtime_refresh_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            query = source / ".copilot" / "rag" / "query"
            target = root / "target"
            target_query = target / "rag" / "query"
            query.mkdir(parents=True)
            shutil.copy2(INSTALL_SH, source / "install.sh")
            (query / "setup.py").write_text(
                "raise SystemExit(99)\n",
                encoding="utf-8",
            )
            (target_query / ".venv" / "bin").mkdir(parents=True)
            target_python = target_query / ".venv" / "bin" / "python"
            target_python.write_text(
                "#!/bin/sh\nexit 7\n",
                encoding="utf-8",
            )
            target_python.chmod(0o755)
            marker = target_query / ".venv" / ".rag-deps-installed"
            self._write_current_valid_marker(target / "rag", marker)
            completed = subprocess.run(
                ["sh", str(source / "install.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={
                    **os.environ,
                    "COPILOT_HOME": str(target),
                    "HOME": str(root / "home"),
                },
                timeout=30,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("setup_required:", completed.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(
                1,
                len(list(marker.parent.glob(
                    ".rag-deps-installed.pre-update.*"
                ))),
            )

    @unittest.skipIf(
        os.name == "nt",
        "POSIX installer execution is not applicable on Windows",
    )
    def test_posix_copy_failure_leaves_lookup_gate_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source_query = source / ".copilot" / "rag" / "query"
            target = root / "target"
            target_query = target / "rag" / "query"
            source_query.mkdir(parents=True)
            shutil.copy2(INSTALL_SH, source / "install.sh")
            (source_query / "search.py").write_text(
                "print('source')\n",
                encoding="utf-8",
            )
            (target_query / "search.py").mkdir(parents=True)
            marker = target_query / ".venv" / ".rag-deps-installed"
            self._write_current_valid_marker(target / "rag", marker)
            completed = subprocess.run(
                ["sh", str(source / "install.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={
                    **os.environ,
                    "COPILOT_HOME": str(target),
                    "HOME": str(root / "home"),
                },
                timeout=30,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(marker.exists())
            self.assertEqual(
                1,
                len(list(marker.parent.glob(
                    ".rag-deps-installed.pre-update.*"
                ))),
            )


if __name__ == "__main__":
    unittest.main()
