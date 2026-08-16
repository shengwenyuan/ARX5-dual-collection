from __future__ import annotations

import unittest

from arx5_collection.production.config import EXPECTED_STREAMS
from arx5_collection.production.readiness import (
    EXPECTED_TOPIC_TYPES,
    ReadinessLedger,
)


class ReadinessLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ReadinessLedger()
        self.stream_id = "camera_left_color"
        self.topic = EXPECTED_STREAMS[self.stream_id]
        self.types = {self.topic: (EXPECTED_TOPIC_TYPES[self.stream_id],)}

    def test_requires_real_count_fresh_heartbeat_topic_and_type(self) -> None:
        self.ledger.observe(self.stream_id, self.topic, 10, 100.0)
        result = self.ledger.check(self.stream_id, self.types, 101.0, 2.5)
        self.assertTrue(result.passed)

    def test_publisher_without_telemetry_is_not_ready(self) -> None:
        self.ledger.observe(self.stream_id, self.topic, 0, 100.0)
        result = self.ledger.check(self.stream_id, self.types, 101.0, 2.5)
        self.assertFalse(result.passed)

    def test_stale_or_wrong_type_is_not_ready(self) -> None:
        self.ledger.observe(self.stream_id, self.topic, 10, 100.0)
        stale = self.ledger.check(self.stream_id, self.types, 103.0, 2.5)
        wrong_type = self.ledger.check(
            self.stream_id,
            {self.topic: ("sensor_msgs/msg/CompressedImage",)},
            101.0,
            2.5,
        )
        self.assertFalse(stale.passed)
        self.assertFalse(wrong_type.passed)

    def test_status_topic_must_match_fixed_contract(self) -> None:
        self.ledger.observe(self.stream_id, "/wrong", 10, 100.0)
        result = self.ledger.check(self.stream_id, self.types, 101.0, 2.5)
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
