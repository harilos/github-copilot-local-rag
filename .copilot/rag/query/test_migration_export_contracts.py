from __future__ import annotations

import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "export_migration.sh"
HELPER = Path(__file__).resolve().parents[1] / "migration_archive.py"
POSIX_SHELL_AVAILABLE = os.name != "nt" and Path("/bin/sh").exists()


@unittest.skipUnless(POSIX_SHELL_AVAILABLE, "POSIX migration shell test")
class MigrationExportContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.copilot = self.root / ".copilot"
        self.rag = self.copilot / "rag"

        required = {
            "instructions/rag.instructions.md": "routing\n",
            "skills/local-rag/SKILL.md": "lookup\n",
            "skills/local-rag-admin/SKILL.md": "admin\n",
        }
        for relative, content in required.items():
            path = self.copilot / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        files = {
            "VERSION": "1.0.1\n",
            "future/new-feature.dat": "future compatible\n",
            "dbs/example-rag/db.json": "{}\n",
            "dbs/example-rag/catalog.sqlite": "catalog\n",
            "dbs/example-rag/source-links.json": (
                '{"schema_version":"rag-source-links-v1",'
                '"database":"example-rag","revision":1,"sources":[]}\n'
            ),
            "dbs/example-rag/source-links.json.bak": (
                '{"schema_version":"rag-source-links-v1",'
                '"database":"example-rag","revision":1,"sources":[]}\n'
            ),
            "dbs/example-rag/catalog.sqlite-wal": "",
            "dbs/example-rag/catalog.sqlite-shm": "transient\n",
            "dbs/example-rag/logs/progress.json": '{"status":"completed"}\n',
            "dbs/example-rag/logs/index_state.json": "{}\n",
            "models/example/model.onnx": "model\n",
            "query/search.py": "print('search')\n",
            "query/.venv/lib/huge.bin": "venv\n",
            "query/run/ragd.log": "token=private\n",
            "query/__pycache__/search.pyc": "cache\n",
            "config/network.example.json": "{}\n",
            "config/network.json": (
                '{"proxy_url":"http://proxy.example:8080"}\n'
            ),
            "config/sensitive-terms.local": "private local term\n",
            "gen_db/software_rag_tool/.env.example": "SAFE=example\n",
            "gen_db/software_rag_tool/.env": "SECRET=value\n",
            "gen_db/software_rag_tool/.env.production": "SECRET=prod\n",
            "config/credentials.json": '{"token":"private"}\n',
            "config/private.pem": (
                "-----BEGIN PRIVATE KEY-----\nprivate\n"
                "-----END PRIVATE KEY-----\n"
            ),
            "docs/将来 仕様.md": "future specification\n",
        }
        for relative, content in files.items():
            path = self.rag / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        unrelated = {
            "instructions/unrelated.instructions.md": "unrelated\n",
            "skills/unrelated/SKILL.md": "unrelated\n",
            "copilot-instructions.md": "user owned\n",
            "config.json": '{"secret":"outside"}\n',
            "logs/session.log": "private\n",
        }
        for relative, content in unrelated.items():
            path = self.copilot / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_export(
        self,
        name: str = "migration.tar.gz",
        *extra: str,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        archive = self.root / name
        completed = subprocess.run(
            [
                "/bin/sh",
                str(SCRIPT),
                "--copilot-home",
                str(self.copilot),
                "--output",
                str(archive),
                *extra,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return completed, archive

    @staticmethod
    def archive_names(archive: Path) -> set[str]:
        with tarfile.open(archive, "r:gz") as package:
            return set(package.getnames())

    def test_blacklist_inside_rag_and_whitelist_outside_rag(self) -> None:
        completed, archive = self.run_export()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        names = self.archive_names(archive)
        prefix = "local-rag-migration-v1/.copilot/"

        self.assertIn(prefix + "rag/future/new-feature.dat", names)
        self.assertNotIn(
            prefix + "rag/config/sensitive-terms.local",
            names,
        )
        self.assertIn(prefix + "rag/dbs/example-rag/catalog.sqlite", names)
        self.assertIn(
            prefix + "rag/dbs/example-rag/source-links.json",
            names,
        )
        self.assertIn(
            prefix + "rag/dbs/example-rag/source-links.json.bak",
            names,
        )
        self.assertIn(
            prefix + "rag/dbs/example-rag/logs/index_state.json",
            names,
        )
        self.assertIn(prefix + "rag/models/example/model.onnx", names)
        self.assertIn(prefix + "rag/docs/将来 仕様.md", names)
        self.assertNotIn(prefix + "rag/query/.venv/lib/huge.bin", names)
        self.assertNotIn(prefix + "rag/query/run/ragd.log", names)
        self.assertNotIn(prefix + "rag/query/__pycache__/search.pyc", names)
        self.assertNotIn(
            prefix + "rag/dbs/example-rag/catalog.sqlite-shm",
            names,
        )
        self.assertIn(prefix + "rag/config/network.json", names)
        self.assertNotIn(
            prefix + "rag/gen_db/software_rag_tool/.env",
            names,
        )
        self.assertNotIn(
            prefix + "rag/gen_db/software_rag_tool/.env.example",
            names,
        )
        self.assertNotIn(
            prefix + "rag/gen_db/software_rag_tool/.env.production",
            names,
        )
        self.assertNotIn(prefix + "rag/config/credentials.json", names)
        self.assertNotIn(prefix + "rag/config/private.pem", names)
        self.assertFalse(any("/._" in name for name in names))

        self.assertIn(prefix + "instructions/rag.instructions.md", names)
        self.assertIn(prefix + "skills/local-rag/SKILL.md", names)
        self.assertIn(prefix + "skills/local-rag-admin/SKILL.md", names)
        self.assertNotIn(
            prefix + "instructions/unrelated.instructions.md",
            names,
        )
        self.assertNotIn(prefix + "skills/unrelated/SKILL.md", names)
        self.assertNotIn(prefix + "copilot-instructions.md", names)
        self.assertNotIn(prefix + "config.json", names)
        self.assertNotIn(prefix + "logs/session.log", names)

        mode = stat.S_IMODE(archive.stat().st_mode)
        self.assertEqual(mode, 0o600)
        with tarfile.open(archive, "r:gz") as package:
            network_member = package.getmember(
                prefix + "rag/config/network.json"
            )
            self.assertEqual(stat.S_IMODE(network_member.mode), 0o600)
        self.assertNotIn("secret", completed.stdout.casefold())
        self.assertNotIn("secret", completed.stderr.casefold())

    def test_network_config_is_default_and_can_be_excluded(self) -> None:
        completed, archive = self.run_export("with-network.tar.gz")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        names = self.archive_names(archive)
        self.assertIn(
            "local-rag-migration-v1/.copilot/rag/config/network.json",
            names,
        )

        excluded, without_network = self.run_export(
            "without-network.tar.gz",
            "--exclude-network-config",
        )
        self.assertEqual(excluded.returncode, 0, excluded.stderr)
        names = self.archive_names(without_network)
        self.assertNotIn(
            "local-rag-migration-v1/.copilot/rag/config/network.json",
            names,
        )

    def test_proxy_credentials_stop_export_without_disclosure(self) -> None:
        network = self.rag / "config/network.json"
        network.write_text(
            '{"proxy_url":"http://user:very-secret@proxy.example:8080"}\n',
            encoding="utf-8",
        )
        completed, archive = self.run_export("credentialed.tar.gz")
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(archive.exists())
        combined = completed.stdout + completed.stderr
        self.assertNotIn("very-secret", combined)
        self.assertNotIn("user:", combined)

        network.write_text(
            '{"proxy_url":"http://proxy.example:8080",'
            '"auth":{"access_token":"another-secret"}}\n',
            encoding="utf-8",
        )
        field_result, field_archive = self.run_export(
            "credential-field.tar.gz"
        )
        self.assertNotEqual(field_result.returncode, 0)
        self.assertFalse(field_archive.exists())
        combined = field_result.stdout + field_result.stderr
        self.assertNotIn("another-secret", combined)

        network.write_text(
            '{"proxy_url":"http://proxy.example:8080",'
            '"auth":{"password":""}}\n',
            encoding="utf-8",
        )
        empty_field_result, empty_field_archive = self.run_export(
            "empty-credential-field.tar.gz"
        )
        self.assertNotEqual(empty_field_result.returncode, 0)
        self.assertFalse(empty_field_archive.exists())

        network.write_text(
            '{"proxy_url":["http://proxy.example:8080"]}\n',
            encoding="utf-8",
        )
        malformed_result, malformed_archive = self.run_export(
            "malformed-proxy-url.tar.gz"
        )
        self.assertNotEqual(malformed_result.returncode, 0)
        self.assertFalse(malformed_archive.exists())
        self.assertNotIn("Traceback", malformed_result.stderr)
        self.assertNotIn("proxy.example", malformed_result.stderr)

        network.write_text(
            '{"proxy_url":"http://proxy.example:8080'
            '?client_secret=query-secret"}\n',
            encoding="utf-8",
        )
        query_result, query_archive = self.run_export(
            "credential-query.tar.gz"
        )
        self.assertNotEqual(query_result.returncode, 0)
        self.assertFalse(query_archive.exists())
        combined = query_result.stdout + query_result.stderr
        self.assertNotIn("query-secret", combined)

        excluded, excluded_archive = self.run_export(
            "credential-excluded.tar.gz",
            "--exclude-network-config",
        )
        self.assertEqual(excluded.returncode, 0, excluded.stderr)
        names = self.archive_names(excluded_archive)
        self.assertNotIn(
            "local-rag-migration-v1/.copilot/rag/config/network.json",
            names,
        )

    def test_include_network_compatibility_option_keeps_default(self) -> None:
        completed, archive = self.run_export(
            "include-network.tar.gz",
            "--include-network-config",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        names = self.archive_names(archive)
        self.assertIn(
            "local-rag-migration-v1/.copilot/rag/config/network.json",
            names,
        )

    def test_created_archive_verifies_and_cannot_self_include(self) -> None:
        completed, archive = self.run_export()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        verified = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--verify", str(archive)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)

        inside = self.rag / "unsafe.tar.gz"
        rejected = subprocess.run(
            [
                "/bin/sh",
                str(SCRIPT),
                "--copilot-home",
                str(self.copilot),
                "--output",
                str(inside),
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(inside.exists())

    def test_verify_rejects_links_and_incomplete_checksum_coverage(self) -> None:
        malicious = self.root / "malicious.tar.gz"
        with tarfile.open(malicious, "w:gz") as package:
            root = tarfile.TarInfo("local-rag-migration-v1")
            root.type = tarfile.DIRTYPE
            package.addfile(root)
            link = tarfile.TarInfo(
                "local-rag-migration-v1/.copilot/rag/escape"
            )
            link.type = tarfile.SYMTYPE
            link.linkname = "../../../../outside"
            package.addfile(link)
        link_result = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--verify", str(malicious)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(link_result.returncode, 0)

        completed, archive = self.run_export("complete.tar.gz")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        extracted = self.root / "tampered"
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(extracted, filter="data")
        bundle = extracted / "local-rag-migration-v1"
        checksum_file = bundle / "SHA256SUMS"
        lines = checksum_file.read_text(encoding="utf-8").splitlines()
        checksum_file.write_text(
            "\n".join(
                line
                for line in lines
                if "future/new-feature.dat" not in line
            )
            + "\n",
            encoding="utf-8",
        )
        incomplete = self.root / "incomplete.tar.gz"
        with tarfile.open(incomplete, "w:gz") as package:
            package.add(bundle, arcname="local-rag-migration-v1")
        coverage_result = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--verify", str(incomplete)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(coverage_result.returncode, 0)

    def test_verify_enforces_manifest_and_outside_rag_whitelist(self) -> None:
        completed, archive = self.run_export("topology-source.tar.gz")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        extracted = self.root / "topology"
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(extracted, filter="data")
        bundle = extracted / "local-rag-migration-v1"
        manifest = bundle / "MANIFEST.txt"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "schema=local-rag-migration-v1",
                "schema=not-a-migration",
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "write-checksums",
                "--root",
                str(bundle),
                "--output",
                str(bundle / "SHA256SUMS"),
            ],
            check=True,
            timeout=30,
        )
        invalid_manifest = self.root / "invalid-manifest.tar.gz"
        with tarfile.open(invalid_manifest, "w:gz") as package:
            package.add(bundle, arcname="local-rag-migration-v1")
        invalid_result = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--verify", str(invalid_manifest)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(invalid_result.returncode, 0)

        restored_lines: list[str] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line == "schema=not-a-migration":
                line = "schema=local-rag-migration-v1"
            elif line.startswith("file_count="):
                line = f"file_count={int(line.split('=', 1)[1]) + 1}"
            restored_lines.append(line)
        manifest.write_text(
            "\n".join(restored_lines) + "\n",
            encoding="utf-8",
        )
        outside = bundle / ".copilot/config.json"
        outside.write_text('{"unrelated":true}\n', encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "write-checksums",
                "--root",
                str(bundle),
                "--output",
                str(bundle / "SHA256SUMS"),
            ],
            check=True,
            timeout=30,
        )
        unexpected = self.root / "unexpected-outside.tar.gz"
        with tarfile.open(unexpected, "w:gz") as package:
            package.add(bundle, arcname="local-rag-migration-v1")
        outside_result = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--verify", str(unexpected)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(outside_result.returncode, 0)

    def test_active_build_and_nonempty_wal_fail_without_partial_archive(
        self,
    ) -> None:
        progress = self.rag / "dbs/example-rag/logs/progress.json"
        progress.write_text('{"status": "running"}\n', encoding="utf-8")
        running, running_archive = self.run_export("running.tar.gz")
        self.assertNotEqual(running.returncode, 0)
        self.assertFalse(running_archive.exists())

        progress.write_text('{"status": "completed"}\n', encoding="utf-8")
        wal = self.rag / "dbs/example-rag/catalog.sqlite-wal"
        wal.write_text("uncheckpointed", encoding="utf-8")
        busy, busy_archive = self.run_export("busy.tar.gz")
        self.assertNotEqual(busy.returncode, 0)
        self.assertFalse(busy_archive.exists())


if __name__ == "__main__":
    unittest.main()
