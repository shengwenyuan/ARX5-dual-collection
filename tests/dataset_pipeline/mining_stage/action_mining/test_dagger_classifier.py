from __future__ import annotations

import unittest
from pathlib import Path
import json

from jsonschema import Draft202012Validator

from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority.artifacts import (
    classification_quality,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority.artifacts import (
    segment_rows,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority.classifier import (
    AuthorityAlignmentPolicy,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority.classifier import (
    classify_authority,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    AuthorityClass,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    AuthorityEventRecord,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    AuthorityEventType,
)


MONO_ANCHOR = 100_000_000_000
BAG_ANCHOR = 1_000_000_000_000
POLICY = AuthorityAlignmentPolicy(1_000, 2_000_000)


def metadata(
    segments: list[dict[str, object]], interventions: int = 1
) -> dict[str, object]:
    return {
        "collection_type": "dagger",
        "episode_id": "episode-a",
        "timing": {"duration_s": 10.0},
        "dagger": {
            "checkpoint_sha256": "a" * 64,
            "intervention_count": interventions,
            "control_segments": segments,
        },
    }


def event(
    sequence: int,
    offset_s: float,
    event_type: AuthorityEventType,
    intervention_id: int,
    epoch: int,
) -> AuthorityEventRecord:
    offset_ns = round(offset_s * 1e9)
    publish_delay_ns = sequence * 20_000
    return AuthorityEventRecord(
        sequence=sequence,
        monotonic_time_ns=MONO_ANCHOR + offset_ns,
        intervention_id=intervention_id,
        control_epoch=epoch,
        event_type=event_type,
        reason="test",
        bag_timestamp_ns=BAG_ANCHOR + offset_ns + publish_delay_ns,
        header_stamp_ns=BAG_ANCHOR + offset_ns + publish_delay_ns,
    )


def normal_metadata() -> dict[str, object]:
    return metadata(
        [
            {"owner": "model", "started_offset_s": 1.0, "ended_offset_s": 2.0},
            {
                "owner": "human",
                "started_offset_s": 2.1,
                "ended_offset_s": 5.0,
                "intervention_id": 1,
            },
            {"owner": "model", "started_offset_s": 5.2, "ended_offset_s": 10.0},
        ]
    )


def normal_events() -> tuple[AuthorityEventRecord, ...]:
    return (
        event(11, 1.0, AuthorityEventType.POLICY_ACTIVE, 0, 0),
        event(12, 2.0, AuthorityEventType.TAKEOVER_REQUESTED, 1, 0),
        event(13, 2.1, AuthorityEventType.HUMAN_ACTIVE, 1, 1),
        event(14, 5.0, AuthorityEventType.RESUME_REQUESTED, 1, 1),
        event(15, 5.2, AuthorityEventType.POLICY_ACTIVE, 1, 1),
    )


class AuthorityClassifierTest(unittest.TestCase):
    def test_classifies_one_complete_correction_with_semantic_boundaries(self) -> None:
        result = classify_authority(normal_metadata(), normal_events(), POLICY)

        self.assertTrue(result.valid, result.issues)
        self.assertEqual(result.episode_monotonic_anchor_ns, MONO_ANCHOR)
        self.assertEqual(result.episode_bag_anchor_ns, BAG_ANCHOR + 220_000)
        self.assertEqual(
            [segment.authority_class for segment in result.segments],
            [
                AuthorityClass.RESUME,
                AuthorityClass.POLICY,
                AuthorityClass.HANDOVER,
                AuthorityClass.EXPERT_CORRECTION,
                AuthorityClass.RESUME,
                AuthorityClass.POLICY,
            ],
        )
        correction = result.expert_segments[0]
        self.assertEqual(correction.intervention_id, 1)
        self.assertEqual(correction.started_offset_ns, 2_100_000_000)
        self.assertEqual(correction.ended_offset_ns, 5_000_000_000)
        schema_root = Path(__file__).parents[4] / "config/specs/schemas"
        Draft202012Validator(
            json.loads((schema_root / "dagger-authority-quality-v1.json").read_text())
        ).validate(classification_quality(result))
        segment_schema = json.loads(
            (schema_root / "dagger-authority-segment-v1.json").read_text()
        )
        for row in segment_rows(result):
            Draft202012Validator(segment_schema).validate(row)

    def test_incomplete_human_tail_is_audited_but_not_trainable(self) -> None:
        value = metadata(
            [
                {"owner": "model", "started_offset_s": 1.0, "ended_offset_s": 2.0},
                {
                    "owner": "human",
                    "started_offset_s": 2.1,
                    "ended_offset_s": 10.0,
                    "intervention_id": 1,
                },
            ]
        )
        events = normal_events()[:3]

        result = classify_authority(value, events, POLICY)

        self.assertTrue(result.valid, result.issues)
        corrections = [
            segment
            for segment in result.segments
            if segment.authority_class is AuthorityClass.EXPERT_CORRECTION
        ]
        self.assertEqual(len(corrections), 1)
        self.assertFalse(corrections[0].complete)
        self.assertFalse(corrections[0].training_eligible)
        self.assertEqual(corrections[0].exclusion_reason, "incomplete_correction")

    def test_fault_does_not_invalidate_an_earlier_complete_correction(self) -> None:
        value = metadata(
            [
                {"owner": "model", "started_offset_s": 1.0, "ended_offset_s": 2.0},
                {
                    "owner": "human",
                    "started_offset_s": 2.1,
                    "ended_offset_s": 5.0,
                    "intervention_id": 1,
                },
                {"owner": "model", "started_offset_s": 5.2, "ended_offset_s": 7.0},
            ]
        )
        events = normal_events() + (
            event(16, 7.0, AuthorityEventType.FAULT_HOLD, 1, 2),
        )

        result = classify_authority(value, events, POLICY)

        self.assertTrue(result.valid, result.issues)
        self.assertEqual(len(result.expert_segments), 1)
        self.assertIs(result.segments[-1].authority_class, AuthorityClass.FAULT)

    def test_rejects_event_and_metadata_disagreement(self) -> None:
        value = normal_metadata()
        value["dagger"]["control_segments"][1]["ended_offset_s"] = 5.1  # type: ignore[index]

        result = classify_authority(value, normal_events(), POLICY)

        self.assertFalse(result.valid)
        self.assertIn("anchor spread", result.issues[0])

    def test_rejects_active_metadata_tail_before_episode_stop(self) -> None:
        value = normal_metadata()
        value["dagger"]["control_segments"][-1]["ended_offset_s"] = 9.9  # type: ignore[index]

        result = classify_authority(value, normal_events(), POLICY)

        self.assertFalse(result.valid)
        self.assertIn("Episode boundary", result.issues[0])

    def test_shadow_is_valid_but_has_no_authority_training_data(self) -> None:
        value = metadata([], interventions=0)
        value["dagger"]["shadow"] = {"quality": "healthy"}  # type: ignore[index]

        result = classify_authority(value, (), POLICY)

        self.assertTrue(result.valid)
        self.assertEqual(result.issues, ("shadow_episode",))
        self.assertEqual(result.segments, ())


if __name__ == "__main__":
    unittest.main()
