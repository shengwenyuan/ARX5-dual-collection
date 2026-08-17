from __future__ import annotations

import unittest

from arx5_collection.cleaning.models import MessageRef
from arx5_collection.cleaning.timeline import audit_timeline


def ref(sequence: int, stamp: int) -> MessageRef:
    return MessageRef("/topic", sequence, stamp, stamp)


class TimelineTest(unittest.TestCase):
    def test_reports_duplicate_non_monotonic_and_max_gap(self) -> None:
        stats = audit_timeline((ref(0, 10), ref(1, 10), ref(2, 8), ref(3, 20)))

        self.assertEqual(stats.count, 4)
        self.assertEqual(stats.duplicate_count, 1)
        self.assertEqual(stats.non_monotonic_count, 1)
        self.assertEqual(stats.max_positive_gap_ns, 12)

if __name__ == "__main__":
    unittest.main()
