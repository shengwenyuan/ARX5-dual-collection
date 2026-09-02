from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from arx5_collection.collection.metadata import (
    ControlOwner,
    ControlSegment,
    DaggerMetadata,
    MetadataContext,
    ShadowMetadata,
    ShadowQuality,
)
from arx5_collection.collection.episode.metadata import (
    build_metadata,
    load_station,
    write_metadata,
)
from arx5_collection.collection.episode.models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeResult,
    StreamMetrics,
    StreamSpec,
)


ROOT = Path(__file__).parents[3]
STATION_PATH = ROOT / "config" / "environment" / "station.example.json"
SCHEMA_PATH = ROOT / "config/specs/schemas/episode-metadata-v1.json"


def request() -> EpisodeRequest:
    return EpisodeRequest(
        task_id="pick",
        task_description="Pick the object",
        output_root=Path("episodes"),
        station_config=STATION_PATH,
        streams=(
            StreamSpec("left_arm", "/embodiments/left_arm/state", True, 60.0),
            StreamSpec("camera_left", "/sensors/camera_left/color", True, 30.0),
        ),
    )


def result() -> EpisodeResult:
    utc_plus_eight = timezone(timedelta(hours=8))
    return EpisodeResult(
        episode_id="episode-001",
        outcome=EpisodeOutcome.SUCCESS,
        started_at=datetime(2026, 8, 16, 9, 0, tzinfo=utc_plus_eight),
        ended_at=datetime(2026, 8, 16, 9, 1, 30, tzinfo=utc_plus_eight),
        duration_s=90.25,
        committed=False,
        mcap_path=Path("episode.mcap"),
        metadata_path=Path("metadata.json"),
        stream_metrics=(
            StreamMetrics("left_arm", 5_400, 90.0, 60.0, 18.0),
            StreamMetrics("camera_left", 2_700, 90.0, 30.0, 35.0, ("late",)),
        ),
    )


class MetadataWriterTest(unittest.TestCase):
    def test_load_station_maps_current_devices(self) -> None:
        station = load_station(STATION_PATH)
        self.assertEqual(station["id"], "station-example")
        self.assertEqual(station["config_schema_version"], 3)
        self.assertEqual(station["ros_domain_id"], 31)
        self.assertEqual(len(station["devices"]), 7)
        self.assertEqual(
            station["devices"][0]["configuration"],
            {"can_interface": "can1", "sdk_type": 2},
        )
        self.assertEqual(station["devices"][2]["serial_number"], "camera-left-serial")

    def test_build_metadata_maps_time_and_streams(self) -> None:
        metadata = build_metadata(
            request(), result(), load_station(STATION_PATH), "0.1.0"
        )
        self.assertEqual(metadata["timing"]["started_at"], "2026-08-16T01:00:00Z")
        self.assertEqual(metadata["timing"]["duration_s"], 90.25)
        self.assertEqual(metadata["task"]["description"], "Pick the object")
        self.assertEqual(metadata["streams"][1]["message_count"], 2_700)
        self.assertEqual(metadata["streams"][1]["warnings"], ["late"])
        self.assertNotIn("duration_s", metadata["streams"][1])
        self.assertEqual(
            metadata["calibration"], {"intrinsics": None, "extrinsics": None}
        )
        self.assertEqual(metadata["collection_type"], "demonstration")
        self.assertNotIn("dagger", metadata)

    def test_builds_dagger_discriminator_and_summary_only(self) -> None:
        context = MetadataContext.for_dagger(
            DaggerMetadata(
                checkpoint_sha256="A" * 64,
                intervention_count=1,
                control_segments=(
                    ControlSegment(ControlOwner.MODEL, 0.0, 2.0),
                    ControlSegment(ControlOwner.HUMAN, 2.1, 4.0, intervention_id=1),
                    ControlSegment(ControlOwner.MODEL, 4.2, 8.0),
                ),
                shadow=ShadowMetadata(
                    ShadowQuality.DEGRADED,
                    inference_attempt_count=3,
                    inference_success_count=2,
                    inference_failure_count=1,
                    recovery_count=1,
                ),
            )
        )
        metadata = build_metadata(
            request(),
            result(),
            load_station(STATION_PATH),
            "0.1.0",
            metadata_context=context,
        )
        self.assertEqual(metadata["collection_type"], "dagger")
        self.assertEqual(metadata["dagger"]["checkpoint_sha256"], "a" * 64)
        self.assertEqual(metadata["dagger"]["intervention_count"], 1)
        self.assertEqual(len(metadata["dagger"]["control_segments"]), 3)
        self.assertEqual(metadata["dagger"]["shadow"]["quality"], "degraded")
        self.assertNotIn("container_versions", metadata["dagger"])
        self.assertNotIn("protocol_version", metadata["dagger"])

        schema = json.loads(SCHEMA_PATH.read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(metadata)

    def test_metadata_matches_schema(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text())
        metadata = build_metadata(
            request(), result(), load_station(STATION_PATH), "0.1.0"
        )
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(metadata)

    def test_stream_metrics_must_match_specs(self) -> None:
        incomplete = result()
        incomplete = EpisodeResult(
            episode_id=incomplete.episode_id,
            outcome=incomplete.outcome,
            started_at=incomplete.started_at,
            ended_at=incomplete.ended_at,
            duration_s=incomplete.duration_s,
            committed=False,
            mcap_path=incomplete.mcap_path,
            metadata_path=incomplete.metadata_path,
            stream_metrics=incomplete.stream_metrics[:1],
        )
        with self.assertRaises(ValueError):
            build_metadata(request(), incomplete, load_station(STATION_PATH), "0.1.0")

    def test_naive_timestamps_are_rejected(self) -> None:
        episode_result = result()
        naive = EpisodeResult(
            episode_id=episode_result.episode_id,
            outcome=episode_result.outcome,
            started_at=episode_result.started_at.replace(tzinfo=None),
            ended_at=episode_result.ended_at,
            duration_s=episode_result.duration_s,
            committed=False,
            mcap_path=episode_result.mcap_path,
            metadata_path=episode_result.metadata_path,
            stream_metrics=episode_result.stream_metrics,
        )
        with self.assertRaises(ValueError):
            build_metadata(request(), naive, load_station(STATION_PATH), "0.1.0")

    def test_write_metadata_uses_utf8_json(self) -> None:
        metadata = build_metadata(
            request(), result(), load_station(STATION_PATH), "0.1.0"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            write_metadata(path, metadata)
            self.assertEqual(json.loads(path.read_text()), metadata)
            self.assertTrue(path.read_text().endswith("\n"))


if __name__ == "__main__":
    unittest.main()
