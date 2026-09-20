from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.capture import CaptureProfile, RGB_ONLY_STREAMS
from arx5_collection.dagger.application import (
    DaggerApplicationBuilder,
    DaggerRunSpec,
    DaggerSessionBuilder,
)
from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.episode.cli import load_request


ROOT = Path(__file__).parents[2]


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
            ROOT / "config" / "dagger.policy.example.toml"
        )
        spec = DaggerRunSpec(
            station_config=ROOT / "config" / "station.example.json",
            task_config=ROOT / "config" / "task.eight-stream.json",
            task_description="folding the cloth",
            policy_config=ROOT / "config" / "dagger.policy.example.toml",
            output_root=Path("episodes"),
            episodes=1,
            min_free_gib=1,
            readiness_timeout_s=30.0,
            software_version="test",
            session_id="session-1",
        )

        request = load_request(
            spec.task_config,
            spec.output_root,
            spec.station_config,
            task_description=spec.task_description,
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
            station_config=ROOT / "config" / "station.example.json",
            task_config=ROOT / "config" / "task.rgb-only.json",
            task_description="folding the cloth",
            policy_config=ROOT / "config" / "dagger.policy.example.toml",
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
                (ROOT / "config" / "dagger.policy.example.toml").read_text()
            )
            spec = DaggerRunSpec(
                station_config=ROOT / "config" / "station.example.json",
                task_config=ROOT / "config" / "task.eight-stream.json",
                task_description="folding the cloth",
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
        self.assertEqual(application.request.task_id, "eight-stream-collection")
        self.assertEqual(application.request.task_description, "folding the cloth")

    def test_takeover_dry_run_adds_only_sparse_authority_topic(self) -> None:
        fake_session_builder = FakeSessionBuilder()
        with tempfile.TemporaryDirectory() as directory:
            policy_config = Path(directory) / "policy.toml"
            policy_config.write_text(
                (ROOT / "config" / "dagger.policy.example.toml").read_text()
            )
            spec = DaggerRunSpec(
                station_config=ROOT / "config" / "station.example.json",
                task_config=ROOT / "config" / "task.eight-stream.json",
                task_description="folding the cloth",
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
                (ROOT / "config" / "dagger.pi05-stacking-v2.toml").read_text()
            )
            spec = DaggerRunSpec(
                station_config=ROOT / "config" / "station.example.json",
                task_config=ROOT / "config" / "task.eight-stream.json",
                task_description="folding the cloth",
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
            "6855485b55e04707d9c0aa96ad4ca1c8374afac5919d9f4777b71023ea7021a0",
        )
        self.assertEqual(
            fake_session_builder.additional_recording_topics,
            ("/dagger/authority",),
        )


if __name__ == "__main__":
    unittest.main()


def test_takeover_and_dry_run_attach_authority_publisher_to_recording_backend(tmp_path):
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import MagicMock, Mock, patch
    from arx5_collection.dagger.takeover import NoActionGateway
    from arx5_collection.episode.models import RecordingStarted, RecordingStopping, EpisodeOutcome
    from tests.infer.test_collection import spec

    for dry_run in (True, False):
        builder = DaggerApplicationBuilder()
        app = builder.build_takeover_dry_run(spec(tmp_path)) if dry_run else builder.build_takeover(spec(tmp_path))
        original = app.session
        app.session = MagicMock(station=original.station, camera_snapshot=original.camera_snapshot)
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch(name)) for name in (
                'arx5_collection.dagger.authority_ros.RosAuthorityEventPublisher',
                'arx5_collection.dagger.authority_ros.require_no_action_publishers',
                'arx5_collection.dagger.application.DaggerAutoTriggerFactory',
                'arx5_collection.dagger.application.OpenPiDaggerTransport',
                'arx5_collection.dagger.application.LocalVlaSnapshotClient',
                'arx5_collection.dagger.application.AsyncPi05PolicyClient',
                'arx5_collection.dagger.application.run_episode_loop',
                'arx5_collection.dagger.command_ros.RosDualArmControlPort',
                'arx5_collection.dagger.action_runtime.open_takeover_action_runtime',
            )}
            publisher = mocks['arx5_collection.dagger.authority_ros.RosAuthorityEventPublisher'].return_value.__enter__.return_value
            actions = SimpleNamespace(gateway=NoActionGateway(), executor=Mock())
            mocks['arx5_collection.dagger.action_runtime.open_takeover_action_runtime'].return_value.__enter__.return_value = actions

            def loop(*args, **kwargs):
                assert app.session.backend.recording_publisher is publisher
                hooks = app.session.create_runtime.call_args.kwargs
                hooks['recording_started_hook'](RecordingStarted('ep', 0))
                publisher.assert_called_once()  # Actual controller's POLICY_ACTIVE.
                hooks['recording_stopping_hook'](RecordingStopping(EpisodeOutcome.SUCCESS, 10**18))
                return 0

            mocks['arx5_collection.dagger.application.run_episode_loop'].side_effect = loop
            assert app.run() == 0
            if not dry_run:
                actions.executor.start.assert_called_once()
                actions.executor.close.assert_called_once()
                mocks['arx5_collection.dagger.application.AsyncPi05PolicyClient'].return_value.close.assert_called_once()
