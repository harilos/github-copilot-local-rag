from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = QUERY_ROOT.parents[2]
INSTALL_SH = REPOSITORY_ROOT / "install.sh"
INSTALL_PS1 = REPOSITORY_ROOT / "install.ps1"


class InstallerExclusionContractTests(unittest.TestCase):
    def test_posix_and_windows_installers_express_same_exclusions(self) -> None:
        shell = INSTALL_SH.read_text(encoding="utf-8")
        powershell = INSTALL_PS1.read_text(encoding="utf-8")

        for fragment in (
            "./rag/config/network.json",
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
        self.assertIn("try {", powershell)
        self.assertIn("} catch {", powershell)
        self.assertEqual(
            2,
            powershell.count(
                "Existing RAG runtime needs setup verification before lookup."
            ),
        )

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
            target_state = target_query / "run" / "ragd.json"
            target_python = target_query / ".venv" / "bin" / "python"
            target_marker = (
                target_query / ".venv" / ".rag-deps-installed"
            )
            target_network.write_text(
                '{"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_state.write_text(
                '{"pid":1234,"target":"preserve"}\n',
                encoding="utf-8",
            )
            target_python.write_text(
                "#!/bin/sh\nexit 0\n",
                encoding="utf-8",
            )
            target_python.chmod(0o755)
            target_marker.write_text(
                "target-marker-sentinel\n",
                encoding="utf-8",
            )
            before = {
                path: path.read_bytes()
                for path in (
                    target_network,
                    target_state,
                    target_python,
                    target_marker,
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
                "print('installed source')\n",
                (target_query / "search.py").read_text(encoding="utf-8"),
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


if __name__ == "__main__":
    unittest.main()
