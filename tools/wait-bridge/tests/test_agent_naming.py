from __future__ import annotations

import sys
import unittest
from pathlib import Path


BRIDGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_ROOT))

from agent_naming import resolve_agent_name  # noqa: E402


class AgentNamingTests(unittest.TestCase):
    def test_unset_instance_preserves_legacy_name_exactly(self) -> None:
        self.assertEqual(resolve_agent_name("purser-worker", None), "purser-worker")
        self.assertEqual(resolve_agent_name("purser-worker", ""), "purser-worker")

    def test_instance_is_distinct_and_stable(self) -> None:
        first = resolve_agent_name("purser-worker", "window-a")
        restarted = resolve_agent_name("purser-worker", "window-a")
        other = resolve_agent_name("purser-worker", "window-b")
        self.assertEqual(first, "purser-worker-window-a")
        self.assertEqual(restarted, first)
        self.assertNotEqual(other, first)

    def test_invalid_instance_is_rejected(self) -> None:
        for value in (" ", "window a", "slash/name", "x" * 33):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "ONBOARD_AGENT_INSTANCE"):
                    resolve_agent_name("purser-worker", value)

    def test_resolved_name_must_fit_board_identifier_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not exceed 80"):
            resolve_agent_name("b" * 70, "instance-a")


if __name__ == "__main__":
    unittest.main()
