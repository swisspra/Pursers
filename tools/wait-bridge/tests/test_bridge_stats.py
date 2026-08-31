from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import pursers_wait_server as wait_server  # noqa: E402


class BridgeStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.path = Path(self.temporary.name) / "stats.json"
        self.now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        self.stats = wait_server.BridgeStats(self.path, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record(
        self,
        tool: str,
        request_bytes: int,
        response_bytes: int,
        *,
        board_id: str = "board-one",
        agent_name: str = "worker-one",
    ) -> None:
        asyncio.run(
            self.stats.record(
                board_id,
                agent_name,
                tool,
                request_bytes,
                response_bytes,
            )
        )

    def test_accumulates_size_and_call_counts_without_payloads(self) -> None:
        self.record("board_catchup", 100, 400)
        self.record("board_catchup", 60, 140)
        self.record("ticket_list", 50, 90)

        document = json.loads(self.path.read_text(encoding="utf-8"))
        day = document["days"]["2030-01-01"]
        seat = next(iter(day["seats"].values()))
        self.assertEqual(seat["request_bytes"], 210)
        self.assertEqual(seat["response_bytes"], 630)
        self.assertEqual(
            seat["calls"]["board_catchup"],
            {"count": 2, "request_bytes": 160, "response_bytes": 540},
        )
        serialized = self.path.read_text(encoding="utf-8")
        self.assertNotIn("arguments", serialized)
        self.assertNotIn("credentials", serialized)

    def test_day_rollover_and_retention_keep_latest_seven_days(self) -> None:
        for offset in range(9):
            self.now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc) + timedelta(
                days=offset
            )
            self.record("board_catchup", 10 + offset, 20 + offset)

        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            list(document["days"]),
            [
                "2030-01-03",
                "2030-01-04",
                "2030-01-05",
                "2030-01-06",
                "2030-01-07",
                "2030-01-08",
                "2030-01-09",
            ],
        )

    def test_retention_is_an_inclusive_utc_calendar_window(self) -> None:
        self.record("board_catchup", 40, 60)
        self.now = datetime(2030, 1, 10, 12, tzinfo=timezone.utc)
        self.record("board_catchup", 100, 300)

        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(document["days"]), ["2030-01-10"])


if __name__ == "__main__":
    unittest.main()
