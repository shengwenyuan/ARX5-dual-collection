from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.dagger.application import (
    DaggerApplicationBuilder,
    DaggerRunSpec,
)


ROOT = Path(__file__).parents[2]


class FakeSessionBuilder:
    def __init__(self) -> None:
        self.settings = None
        self.session = object()

    def build(self, spec, settings):
        del spec
        self.settings = settings
        return self.session


class DaggerApplicationBuilderTest(unittest.TestCase):
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
                policy_config=policy_config,
                output_root=Path(directory) / "episodes",
                session_log_root=Path(directory) / "logs",
                episodes=1,
                min_free_gib=1,
                readiness_timeout_s=30.0,
                software_version="test",
                session_id="session-1",
            )
            application = DaggerApplicationBuilder(
                session_builder=fake_session_builder  # type: ignore[arg-type]
            ).build_shadow(spec)

        self.assertIs(application.session, fake_session_builder.session)
        self.assertEqual(application.settings.execution.control_rate_hz, 25.0)
        self.assertEqual(application.settings.arm_profile.name, "dagger")
        self.assertEqual(application.request.task_id, "eight-stream-collection")


if __name__ == "__main__":
    unittest.main()
