from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.collection.capture import CaptureProfile, RGB_ONLY_STREAMS
from arx5_collection.collection.dagger.application import (
    DaggerApplicationBuilder,
    DaggerRunSpec,
    DaggerSessionBuilder,
)
from arx5_collection.collection.dagger.config import DaggerCollectorSettings
from arx5_collection.collection.configuration import CollectionConfig


ROOT = Path(__file__).parents[3]


class FakeSessionBuilder:
    def __init__(self) -> None:
        self.settings = None
        self.session = object()
        self.additional_recording_topics = ()
        self.request = None

    def build(self, spec, settings, request, additional_recording_topics=()):
        del spec
        self.settings = settings
        self.request = request
        self.additional_recording_topics = additional_recording_topics
        return self.session


class DaggerApplicationBuilderTest(unittest.TestCase):
    def test_dagger_session_routes_failures_to_dagger_fail(self) -> None:
        settings = DaggerCollectorSettings.load(
            ROOT / "config" / "collection" / "dagger.policy.example.toml"
        )
        spec = DaggerRunSpec(
            station_config=ROOT / "config" / "environment" / "station.example.json",
            collection_config=ROOT / "config" / "collection" / "fold-cloth-rgbd.toml",
            policy_config=ROOT / "config" / "collection" / "dagger.policy.example.toml",
            output_root=Path("episodes"),
            episodes=1,
            min_free_gib=1,
            readiness_timeout_s=30.0,
            software_version="test",
            session_id="session-1",
        )

        request = CollectionConfig.load(spec.collection_config).request(
            spec.output_root, spec.station_config
        )
        session = DaggerSessionBuilder().build(spec, settings, request)

        self.assertEqual(session.fail_directory, "dagger_fail")
        assert session.camera_snapshot is not None
        self.assertEqual(session.camera_snapshot.width, 640)
        self.assertEqual(session.camera_snapshot.height, 360)
        self.assertEqual(
            session.camera_snapshot.arena_path,
            Path("/dev/shm/arx5-vla-snapshot-31"),
        )
        self.assertEqual(
            session.camera_snapshot.socket_path,
            Path("/tmp/arx5-vla-snapshot-31.sock"),
        )
        self.assertEqual(session.monitor.display_period_s, 10.0)

    def test_rgb_only_profile_passes_five_stream_request(self) -> None:
        fake_session_builder = FakeSessionBuilder()
        spec = DaggerRunSpec(
            station_config=ROOT / "config" / "environment" / "station.example.json",
            collection_config=ROOT
            / "config"
            / "collection"
            / "fold-cloth-rgb-only.toml",
            policy_config=ROOT / "config" / "collection" / "dagger.policy.example.toml",
            output_root=Path("episodes"),
            episodes=1,
            min_free_gib=1,
            readiness_timeout_s=30.0,
            software_version="test",
            session_id="session-1",
        )

        application = DaggerApplicationBuilder(
            session_builder=fake_session_builder  # type: ignore[arg-type]
        ).build_shadow(spec)

        self.assertIs(application.capture_profile, CaptureProfile.RGB_ONLY)
        assert fake_session_builder.request is not None
        self.assertEqual(
            tuple(stream.id for stream in fake_session_builder.request.streams),
            tuple(RGB_ONLY_STREAMS),
        )
        settings = DaggerCollectorSettings.load(spec.policy_config)
        session = DaggerSessionBuilder().build(
            spec,
            settings,
            fake_session_builder.request,
        )
        self.assertEqual(session.required_stream_ids, tuple(RGB_ONLY_STREAMS))

    def test_builds_shadow_from_profile_without_starting_resources(self) -> None:
        fake_session_builder = FakeSessionBuilder()
        with tempfile.TemporaryDirectory() as directory:
            policy_config = Path(directory) / "policy.toml"
            policy_config.write_text(
                (
                    ROOT / "config" / "collection" / "dagger.policy.example.toml"
                ).read_text()
            )
            spec = DaggerRunSpec(
                station_config=ROOT / "config" / "environment" / "station.example.json",
                collection_config=ROOT
                / "config"
                / "collection"
                / "fold-cloth-rgbd.toml",
                policy_config=policy_config,
                output_root=Path(directory) / "episodes",
                episodes=1,
                min_free_gib=1,
                readiness_timeout_s=30.0,
                software_version="test",
                session_id="session-1",
            )
            self.assertEqual(
                spec.log_dir,
                Path(directory) / "episodes" / "logs" / "session-1",
            )
            application = DaggerApplicationBuilder(
                session_builder=fake_session_builder  # type: ignore[arg-type]
            ).build_shadow(spec)

        self.assertIs(application.session, fake_session_builder.session)
        self.assertEqual(application.settings.execution.control_rate_hz, 25.0)
        self.assertEqual(application.settings.arm_profile.name, "dagger")
        self.assertEqual(application.request.task_id, "fold-cloth-rgbd")
        self.assertEqual(application.request.task_description, "folding the cloth")

    def test_takeover_dry_run_adds_only_sparse_authority_topic(self) -> None:
        fake_session_builder = FakeSessionBuilder()
        with tempfile.TemporaryDirectory() as directory:
            policy_config = Path(directory) / "policy.toml"
            policy_config.write_text(
                (
                    ROOT / "config" / "collection" / "dagger.policy.example.toml"
                ).read_text()
            )
            spec = DaggerRunSpec(
                station_config=ROOT / "config" / "environment" / "station.example.json",
                collection_config=ROOT
                / "config"
                / "collection"
                / "fold-cloth-rgbd.toml",
                policy_config=policy_config,
                output_root=Path(directory) / "episodes",
                episodes=1,
                min_free_gib=1,
                readiness_timeout_s=30.0,
                software_version="test",
                session_id="session-1",
            )
            application = DaggerApplicationBuilder(
                session_builder=fake_session_builder  # type: ignore[arg-type]
            ).build_takeover_dry_run(spec)

        self.assertIs(application.session, fake_session_builder.session)
        self.assertEqual(
            fake_session_builder.additional_recording_topics,
            ("/dagger/authority",),
        )

    def test_takeover_build_is_inert_and_adds_only_authority_topic(self) -> None:
        fake_session_builder = FakeSessionBuilder()
        with tempfile.TemporaryDirectory() as directory:
            policy_config = Path(directory) / "policy.toml"
            policy_config.write_text(
                (
                    ROOT / "config" / "collection" / "dagger.policy.example.toml"
                ).read_text()
            )
            spec = DaggerRunSpec(
                station_config=ROOT / "config" / "environment" / "station.example.json",
                collection_config=ROOT
                / "config"
                / "collection"
                / "fold-cloth-rgbd.toml",
                policy_config=policy_config,
                output_root=Path(directory) / "episodes",
                episodes=1,
                min_free_gib=1,
                readiness_timeout_s=30.0,
                software_version="test",
                session_id="session-1",
            )
            application = DaggerApplicationBuilder(
                session_builder=fake_session_builder  # type: ignore[arg-type]
            ).build_takeover(spec)

        self.assertIs(application.session, fake_session_builder.session)
        self.assertEqual(application.settings.execution.execution_steps, 10)
        self.assertEqual(application.settings.execution.control_rate_hz, 25.0)
        self.assertEqual(
            application.settings.checkpoint_sha256,
            "0" * 64,
        )
        self.assertEqual(
            fake_session_builder.additional_recording_topics,
            ("/dagger/authority",),
        )


if __name__ == "__main__":
    unittest.main()
