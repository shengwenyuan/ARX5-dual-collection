from __future__ import annotations

import unittest

from arx5_collection.collection_metadata import (
    CollectionType,
    ControlOwner,
    ControlSegment,
    DaggerMetadata,
    MetadataContext,
    ShadowMetadata,
    ShadowQuality,
)


class CollectionMetadataTest(unittest.TestCase):
    def test_rejects_overlapping_control_segments(self) -> None:
        with self.assertRaisesRegex(ValueError, "ordered and non-overlapping"):
            DaggerMetadata(
                "a" * 64,
                1,
                (
                    ControlSegment(ControlOwner.MODEL, 0.0, 2.0),
                    ControlSegment(ControlOwner.HUMAN, 1.9, 3.0, 1),
                ),
            )

    def test_collection_type_and_dagger_summary_must_agree(self) -> None:
        summary = DaggerMetadata("a" * 64, 0, ())
        with self.assertRaisesRegex(ValueError, "only valid"):
            MetadataContext(CollectionType.DEMONSTRATION, summary)
        with self.assertRaisesRegex(ValueError, "requires"):
            MetadataContext(CollectionType.DAGGER)

    def test_human_segment_requires_intervention_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires intervention_id"):
            ControlSegment(ControlOwner.HUMAN, 0.0, 1.0)

    def test_shadow_summary_requires_consistent_attempt_counts(self) -> None:
        summary = ShadowMetadata(ShadowQuality.DEGRADED, 3, 2, 1, 1)
        self.assertEqual(summary.to_dict()["quality"], "degraded")
        with self.assertRaisesRegex(ValueError, "attempts"):
            ShadowMetadata(ShadowQuality.DEGRADED, 4, 2, 1, 1)


if __name__ == "__main__":
    unittest.main()
