from __future__ import annotations

import platform
import tempfile
import unittest
from pathlib import Path

from verify_runtime_requirements import _load_lock, verify


def _distribution(root: Path, name: str, version: str) -> None:
    metadata = root / f"{name.replace('-', '_')}-{version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


class RuntimeRequirementGateTests(unittest.TestCase):
    def test_includes_are_exact_and_matching_runtime_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "search.lock").write_text("Alpha_Pkg==1.2.3\n", encoding="utf-8")
            (root / "admin.lock").write_text(
                "-r search.lock\nBeta[extra]==4.5.6\n",
                encoding="utf-8",
            )
            site = root / "site-packages"
            _distribution(site, "Alpha-Pkg", "1.2.3")
            _distribution(site, "Beta", "4.5.6")

            pins = _load_lock(root / "admin.lock")
            result = verify(
                root / "admin.lock",
                python_version=platform.python_version(),
                site_packages=site,
            )

            self.assertEqual(2, len(pins))
            self.assertEqual("pass", result["status"])
            self.assertEqual([], result["mismatches"])

    def test_version_mismatch_and_missing_distribution_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "runtime.lock"
            lock.write_text("Alpha==1.0\nMissing==2.0\n", encoding="utf-8")
            site = root / "site-packages"
            _distribution(site, "Alpha", "9.0")

            result = verify(
                lock,
                python_version=platform.python_version(),
                site_packages=site,
            )

            self.assertEqual("fail", result["status"])
            self.assertEqual(
                [
                    {"name": "Alpha", "expected": "1.0", "actual": "9.0"},
                    {"name": "Missing", "expected": "2.0", "actual": None},
                ],
                result["mismatches"],
            )

    def test_non_exact_requirement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "runtime.lock"
            lock.write_text("Alpha>=1.0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not an exact pin"):
                _load_lock(lock)


if __name__ == "__main__":
    unittest.main()
