from __future__ import annotations

import unittest

from source_manager.progress import ProgressRenderer


class RedmineHttpNoiseTests(unittest.TestCase):
    def test_success_is_quiet_but_retry_remains_visible(self) -> None:
        output: list[str] = []
        renderer = ProgressRenderer(
            output.append,
            operation="Source追加",
            provider="Redmine",
            is_tty=False,
            clock=lambda: 10.0,
        )
        base = {
            "event": "redmine.http_attempt",
            "provider": "redmine",
            "phase": "redmine.http",
            "attempt": 1,
            "max_attempts": 3,
            "url": "https://example.invalid/issues/10.json",
        }
        renderer({**base, "status": 200, "retry": False})
        self.assertEqual([], output)

        renderer(
            {
                **base,
                "status": 429,
                "retry": True,
                "retry_after": "2",
            }
        )
        self.assertEqual(1, len(output))
        self.assertIn("Retry-After=2", output[0])


if __name__ == "__main__":
    unittest.main()
