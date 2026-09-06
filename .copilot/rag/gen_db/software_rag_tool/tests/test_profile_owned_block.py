from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import profile


class ProfileOwnedBlockTests(unittest.TestCase):
    def test_refresh_preserves_bom_crlf_and_all_user_owned_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-owned-") as temporary:
            root = Path(temporary)
            path = root / "DB_PROFILE.md"
            prefix = (
                b"\xef\xbb\xbf# User title\r\n\r\n"
                b"## Query Hint\r\n\r\nKeep exact user hint.\r\n\r\n"
                b"## User Notes\r\n\r\nKeep exact notes.\r\n\r\n"
            )
            suffix = b"## Custom Tail\r\n\r\nKeep exact tail.\r\n"
            path.write_bytes(
                prefix
                + b"## Auto Profile\r\n\r\nold generated value\r\n\r\n"
                + suffix
            )
            records = [
                {
                    "text": "Alpha evidence",
                    "metadata": {"source_id": "s1", "title": "doc.md"},
                }
            ]
            with (
                mock.patch.object(profile, "output_root", return_value=root),
                mock.patch.object(profile, "_load_sample_records", return_value=records),
                mock.patch.object(profile, "_db_title", return_value="ignored"),
            ):
                self.assertTrue(profile.update_profile_from_clean())
                first = path.read_bytes()
                self.assertTrue(profile.update_profile_from_clean())
                second = path.read_bytes()
            self.assertEqual(first, second)
            self.assertTrue(first.startswith(prefix + profile.AUTO_PROFILE_START.encode()))
            self.assertTrue(first.endswith(suffix))
            self.assertEqual(1, first.count(profile.AUTO_PROFILE_START.encode()))
            self.assertEqual(1, first.count(profile.AUTO_PROFILE_END.encode()))
            self.assertNotIn(b"\n", first.replace(b"\r\n", b""))

    def test_ambiguous_owned_sections_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            profile._replace_owned_auto_profile(
                "## Auto Profile\nold\n## Auto Profile\nsecond\n",
                "replacement\n",
                "\n",
            )


if __name__ == "__main__":
    unittest.main()
