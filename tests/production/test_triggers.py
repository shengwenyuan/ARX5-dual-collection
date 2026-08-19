from __future__ import annotations

import os
import unittest
from pathlib import Path

from arx5_collection.episode.adapters.pedal import HidrawPedal, PedalUnavailable
from arx5_collection.episode.ports import TriggerEvent
from arx5_collection.production.config import (
    ArmConfig,
    CameraConfig,
    PedalConfig,
    StationConfig,
    TriggerConfig,
)
from arx5_collection.production.triggers import AutoTriggerFactory
from arx5_collection.production.cli import DEFAULT_STATION_CONFIG, build_parser


class FakeResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.activate = HidrawPedal(Path("activate"), 10021)
        self.abort = HidrawPedal(Path("abort"), 10023)

    def resolve(self, activate, abort):
        if self.error is not None:
            raise self.error
        return {
            TriggerEvent.ACTIVATE: self.activate,
            TriggerEvent.ABORT: self.abort,
        }


def station(with_triggers: bool = True) -> StationConfig:
    triggers = None
    if with_triggers:
        triggers = TriggerConfig(
            PedalConfig("activate", "8088", "0015", "one"),
            PedalConfig("abort", "8088", "0015", "two"),
        )
    return StationConfig(
        schema_version=2 if with_triggers else 1,
        station_id="test",
        sdk_type=2,
        arms=(ArmConfig("left", "left", "can1"), ArmConfig("right", "right", "can3")),
        cameras=(
            CameraConfig("left", "camera-left"),
            CameraConfig("right", "camera-right"),
            CameraConfig("overview", "camera-overview"),
        ),
        triggers=triggers,
    )


class AutoTriggerFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.master_fd, slave_fd = os.openpty()
        self.stream = os.fdopen(slave_fd, "r")
        self.messages: list[str] = []

    def tearDown(self) -> None:
        self.stream.close()
        os.close(self.master_fd)

    def test_uses_pedal_when_both_are_available(self) -> None:
        resolver = FakeResolver()
        factory = AutoTriggerFactory(
            resolver=resolver,  # type: ignore[arg-type]
            keyboard_stream=self.stream,
            status_sink=self.messages.append,
        )
        with factory.open(station()) as trigger:
            self.assertIsNotNone(trigger)
        self.assertEqual(self.messages, ["TRIGGER_MODE=pedal"])

    def test_missing_pedal_falls_back_to_whole_keyboard_pair(self) -> None:
        factory = AutoTriggerFactory(
            resolver=FakeResolver(  # type: ignore[arg-type]
                PedalUnavailable("abort pedal missing")
            ),
            keyboard_stream=self.stream,
            status_sink=self.messages.append,
        )
        with factory.open(station()) as trigger:
            os.write(self.master_fd, b" ")
            self.assertIs(trigger.wait(0.1), TriggerEvent.ACTIVATE)
            os.write(self.master_fd, b"a")
            self.assertIs(trigger.wait(0.1), TriggerEvent.ABORT)
        self.assertEqual(
            self.messages,
            ["TRIGGER_MODE=keyboard-fallback reason=abort pedal missing"],
        )

    def test_schema_v1_without_pedals_falls_back(self) -> None:
        factory = AutoTriggerFactory(
            resolver=FakeResolver(),  # type: ignore[arg-type]
            keyboard_stream=self.stream,
            status_sink=self.messages.append,
        )
        with factory.open(station(with_triggers=False)):
            pass
        self.assertIn("no pedal pair", self.messages[0])

    def test_runtime_pedal_error_does_not_hot_switch_to_keyboard(self) -> None:
        resolver = FakeResolver()
        factory = AutoTriggerFactory(
            resolver=resolver,  # type: ignore[arg-type]
            keyboard_stream=self.stream,
            status_sink=self.messages.append,
        )
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            with factory.open(station()):
                raise RuntimeError("disconnected")
        self.assertEqual(self.messages, ["TRIGGER_MODE=pedal"])

    def test_production_cli_has_only_auto_and_defaults_station_path(self) -> None:
        parser = build_parser()
        devices = parser.parse_args(["devices"])
        self.assertEqual(devices.station_config, DEFAULT_STATION_CONFIG)
        run = parser.parse_args(
            [
                "run",
                "--task-config",
                "task.json",
                "--output-root",
                "episodes",
            ]
        )
        self.assertEqual(run.station_config, Path("/var/lib/arx5-collection/station.json"))
        shadow = parser.parse_args(
            [
                "dagger",
                "shadow",
                "--task-config",
                "task.json",
                "--policy-config",
                "policy.toml",
                "--output-root",
                "episodes",
            ]
        )
        self.assertEqual(shadow.dagger_command, "shadow")
        dry_run = parser.parse_args(
            [
                "dagger",
                "takeover-dry-run",
                "--task-config",
                "task.json",
                "--policy-config",
                "policy.toml",
                "--output-root",
                "episodes",
            ]
        )
        self.assertEqual(dry_run.dagger_command, "takeover-dry-run")

        checkpoint_sha = parser.parse_args(
            ["dagger", "checkpoint-sha", "/checkpoints/example/9999"]
        )
        self.assertEqual(checkpoint_sha.command, "dagger")
        self.assertEqual(checkpoint_sha.dagger_command, "checkpoint-sha")
        self.assertEqual(
            checkpoint_sha.checkpoint, Path("/checkpoints/example/9999")
        )
        self.assertEqual(shadow.policy_config, Path("policy.toml"))
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "run",
                    "--task-config",
                    "task.json",
                    "--output-root",
                    "episodes",
                    "--trigger-key",
                    "x",
                ]
            )


if __name__ == "__main__":
    unittest.main()
