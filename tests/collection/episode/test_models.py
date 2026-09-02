from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

from arx5_collection.collection.episode.models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeResult,
    EpisodeState,
    StreamMetrics,
    StreamSpec,
)


LEFT_ARM = StreamSpec(
    id="arm_left_joint_state",
    topic="/embodiments/arm_left/joint_state",
    required=True,
    expected_hz=60.0,
)


class EpisodeModelsTest(unittest.TestCase):
    def test_enums_encode_as_json_strings(self) -> None:
        self.assertEqual(json.dumps(EpisodeState.RECORDING), '"recording"')
        self.assertEqual(json.dumps(EpisodeOutcome.ABORTED), '"aborted"')

    def test_stream_spec_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            StreamSpec("", "/topic", True, 30.0)
        with self.assertRaises(ValueError):
            StreamSpec("camera", "", True, 30.0)
        with self.assertRaises(ValueError):
            StreamSpec("camera", "/topic", True, 0.0)
        with self.assertRaises(ValueError):
            StreamMetrics("", 0, 0.0, 0.0, 0.0)

    def test_request_rejects_duplicate_stream_ids(self) -> None:
        duplicate = StreamSpec(
            id=LEFT_ARM.id,
            topic="/embodiments/arm_right/joint_state",
            required=True,
            expected_hz=60.0,
        )
        with self.assertRaises(ValueError):
            self.make_request((LEFT_ARM, duplicate))

    def test_request_rejects_duplicate_topics(self) -> None:
        duplicate = StreamSpec(
            id="arm_right_joint_state",
            topic=LEFT_ARM.topic,
            required=True,
            expected_hz=60.0,
        )
        with self.assertRaises(ValueError):
            self.make_request((LEFT_ARM, duplicate))

    def test_models_are_immutable(self) -> None:
        request = self.make_request((LEFT_ARM,))
        with self.assertRaises(FrozenInstanceError):
            request.task_id = "new-task"  # type: ignore[misc]

        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        result = EpisodeResult(
            episode_id="episode-001",
            outcome=EpisodeOutcome.SUCCESS,
            started_at=now,
            ended_at=now,
            duration_s=1.0,
            committed=True,
            mcap_path=Path("episode.mcap"),
            metadata_path=Path("metadata.json"),
        )
        with self.assertRaises(FrozenInstanceError):
            result.committed = False  # type: ignore[misc]

    def test_all_episode_outcomes_are_representable(self) -> None:
        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        metrics = StreamMetrics(
            id=LEFT_ARM.id,
            count=5_400,
            duration_s=90.0,
            observed_hz=60.0,
            max_gap_ms=18.0,
        )

        for outcome in EpisodeOutcome:
            with self.subTest(outcome=outcome):
                result = EpisodeResult(
                    episode_id="episode-001",
                    outcome=outcome,
                    started_at=now,
                    ended_at=now,
                    duration_s=90.0,
                    committed=outcome is EpisodeOutcome.SUCCESS,
                    mcap_path=Path("episode-001/episode.mcap"),
                    metadata_path=Path("episode-001/metadata.json"),
                    stream_metrics=(metrics,),
                )
                self.assertEqual(result.outcome, outcome)

    @staticmethod
    def make_request(streams: tuple[StreamSpec, ...]) -> EpisodeRequest:
        return EpisodeRequest(
            task_id="pick-and-place",
            task_description="Move the object into the tray",
            output_root=Path("episodes"),
            station_config=Path("config/station.yaml"),
            streams=streams,
        )


if __name__ == "__main__":
    unittest.main()
