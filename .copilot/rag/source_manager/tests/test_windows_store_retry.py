from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from source_manager.errors import SourceManagerError
from source_manager.runner import (
    register_source,
    update_source,
    update_source_configuration,
)
from source_manager.store import SourceStore
from source_manager import store as store_module


def _windows_access_denied() -> PermissionError:
    error = PermissionError(13, "Access is denied")
    error.winerror = 5
    return error


class WindowsSourceStoreRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-windows-store-retry-"
        )
        self.db_root = Path(self.temporary.name) / "fixture-rag"
        self.db_root.mkdir()
        self.registered = register_source(
            self.db_root,
            source_type="redmine",
            display_name="Issue tracker",
            fetch={
                "project_url": (
                    "https://issues.example.invalid/projects/fixture"
                ),
                "updated_within_days": 30,
                "api_key_env": "LOCAL_RAG_REDMINE_API_KEY",
            },
        )
        update_source(
            self.db_root,
            self.registered["local_source_key"],
        )
        self.store = SourceStore(self.db_root)
        self.state_path = self.store.read_state(
            self.registered["local_source_key"]
        ).path

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_redmine_configuration_retries_transient_state_read_denial(
        self,
    ) -> None:
        original_read_bytes = Path.read_bytes
        denied = False

        def flaky_read(path: Path) -> bytes:
            nonlocal denied
            if Path(path) == self.state_path and not denied:
                denied = True
                raise _windows_access_denied()
            return original_read_bytes(path)

        current = self.store.read_source(
            self.registered["local_source_key"]
        ).payload["fetch"]
        with (
            mock.patch.object(store_module, "_is_windows", return_value=True),
            mock.patch.object(store_module.time, "sleep", return_value=None),
            mock.patch.object(Path, "read_bytes", new=flaky_read),
        ):
            update_source_configuration(
                self.db_root,
                self.registered["local_source_key"],
                fetch={
                    **current,
                    "updated_within_days": 90,
                },
            )

        saved = self.store.read_source(
            self.registered["local_source_key"]
        )
        self.assertTrue(denied)
        self.assertEqual(90, saved.payload["fetch"]["updated_within_days"])

    def test_state_replace_retries_transient_access_denial(self) -> None:
        state = self.store.read_state(
            self.registered["local_source_key"]
        )
        original_replace = store_module.os.replace
        denied = False

        def flaky_replace(source: Path, target: Path) -> None:
            nonlocal denied
            if Path(target) == self.state_path and not denied:
                denied = True
                raise _windows_access_denied()
            original_replace(source, target)

        with (
            mock.patch.object(store_module, "_is_windows", return_value=True),
            mock.patch.object(store_module.time, "sleep", return_value=None),
            mock.patch.object(
                store_module.os,
                "replace",
                side_effect=flaky_replace,
            ),
        ):
            saved = self.store.save_state(
                self.registered["local_source_key"],
                dict(state.payload),
                expected_revision=state.revision,
                expected_etag=state.etag,
            )

        self.assertTrue(denied)
        self.assertEqual(state.revision + 1, saved.revision)

    def test_replace_retry_rechecks_revision_before_overwrite(self) -> None:
        state = self.store.read_state(
            self.registered["local_source_key"]
        )
        external = dict(state.payload)
        external["revision"] = state.revision + 1
        external["updated_at"] = "2026-07-30T00:00:00+00:00"
        original_replace = store_module.os.replace
        denied = False
        changed = False

        def flaky_replace(source: Path, target: Path) -> None:
            nonlocal denied
            if Path(target) == self.state_path and not denied:
                denied = True
                raise _windows_access_denied()
            original_replace(source, target)

        def competing_write(_delay: float) -> None:
            nonlocal changed
            if changed:
                return
            changed = True
            self.state_path.write_text(
                json.dumps(
                    external,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

        with (
            mock.patch.object(store_module, "_is_windows", return_value=True),
            mock.patch.object(
                store_module.time,
                "sleep",
                side_effect=competing_write,
            ),
            mock.patch.object(
                store_module.os,
                "replace",
                side_effect=flaky_replace,
            ),
            self.assertRaisesRegex(
                SourceManagerError,
                "source_configuration_changed",
            ),
        ):
            self.store.save_state(
                self.registered["local_source_key"],
                dict(state.payload),
                expected_revision=state.revision,
                expected_etag=state.etag,
            )

        retained = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(denied)
        self.assertTrue(changed)
        self.assertEqual(state.revision + 1, retained["revision"])
        self.assertFalse(
            any(self.state_path.parent.glob(f".{self.state_path.name}.*.tmp"))
        )

    def test_persistent_denial_preserves_original_state(self) -> None:
        state = self.store.read_state(
            self.registered["local_source_key"]
        )
        original = self.state_path.read_bytes()
        with (
            mock.patch.object(store_module, "_is_windows", return_value=True),
            mock.patch.object(
                store_module,
                "WINDOWS_FILE_RETRY_SECONDS",
                0.0,
            ),
            mock.patch.object(
                store_module.os,
                "replace",
                side_effect=_windows_access_denied(),
            ),
            self.assertRaises(PermissionError),
        ):
            self.store.save_state(
                self.registered["local_source_key"],
                dict(state.payload),
                expected_revision=state.revision,
                expected_etag=state.etag,
            )

        self.assertEqual(original, self.state_path.read_bytes())
        self.assertFalse(
            any(self.state_path.parent.glob(f".{self.state_path.name}.*.tmp"))
        )


if __name__ == "__main__":
    unittest.main()
