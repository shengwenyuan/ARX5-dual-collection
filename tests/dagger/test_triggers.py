from __future__ import annotations

import os
import unittest

from arx5_collection.dagger.models import DaggerTriggerEvent
from arx5_collection.dagger.triggers import (
    DaggerAutoTriggerFactory,
    DaggerKeyboardTrigger,
    DaggerPedalTriggerAdapter,
)
from arx5_collection.episode.adapters.pedal import HidrawPedal, PedalUnavailable
from arx5_collection.episode.ports import TriggerEvent, TriggerSignal
from arx5_collection.production.config import (
    ArmConfig,
    CameraConfig,
    PedalConfig,
    StationConfig,
    TriggerConfig,
)


class FakeRecordTrigger:
    def __init__(self, events):
        self.events = iter(events)

    def wait(self, timeout_s: float):
        event = next(self.events, None)
        if event is None:
            return None
        return TriggerSignal(event, 123)


class FakeResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def resolve(self, activate, abort):
        if self.error is not None:
            raise self.error
        return {
            TriggerEvent.ACTIVATE: HidrawPedal(os.devnull, 101),
            TriggerEvent.ABORT: HidrawPedal(os.devnull, 102),
        }


def station() -> StationConfig:
    return StationConfig(
        schema_version=2,
        station_id="test",
        sdk_type=2,
        arms=(ArmConfig("left", "left", "can1"), ArmConfig("right", "right", "can3")),
        cameras=(
            CameraConfig("left", "camera-left"),
            CameraConfig("right", "camera-right"),
            CameraConfig("overview", "camera-overview"),
        ),
        triggers=TriggerConfig(
            PedalConfig("activate", "8088", "0015", "one"),
            PedalConfig("abort", "8088", "0015", "two"),
        ),
    )


class DaggerPedalTriggerAdapterTest(unittest.TestCase):
    def test_maps_bound_pedal_roles_without_rebinding_station(self) -> None:
        trigger = DaggerPedalTriggerAdapter(
            FakeRecordTrigger([TriggerEvent.ACTIVATE, TriggerEvent.ABORT, None])
        )
        first = trigger.wait(0.1)
        second = trigger.wait(0.1)
        self.assertIs(first.event, DaggerTriggerEvent.RECORD_TOGGLE)
        self.assertIs(second.event, DaggerTriggerEvent.OWNERSHIP_TOGGLE)
        self.assertEqual(first.monotonic_time_ns, 123)
        self.assertIsNone(trigger.wait(0.1))


class DaggerKeyboardTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.master_fd, slave_fd = os.openpty()
        self.stream = os.fdopen(slave_fd, "r")

    def tearDown(self) -> None:
        self.stream.close()
        os.close(self.master_fd)

    def test_space_t_and_a_are_distinct(self) -> None:
        with DaggerKeyboardTrigger(self.stream) as trigger:
            os.write(self.master_fd, b" ")
            self.assertIs(
                trigger.wait(0.1).event,
                DaggerTriggerEvent.RECORD_TOGGLE,
            )
            os.write(self.master_fd, b"t")
            self.assertIs(
                trigger.wait(0.1).event,
                DaggerTriggerEvent.OWNERSHIP_TOGGLE,
            )
            os.write(self.master_fd, b"a")
            self.assertIs(trigger.wait(0.1).event, DaggerTriggerEvent.ABORT)

    def test_auto_factory_falls_back_to_three_key_keyboard_profile(self) -> None:
        messages: list[str] = []
        factory = DaggerAutoTriggerFactory(
            resolver=FakeResolver(PedalUnavailable("pedals missing")),  # type: ignore[arg-type]
            keyboard_stream=self.stream,
            status_sink=messages.append,
        )
        with factory.open(station()) as trigger:
            os.write(self.master_fd, b"t")
            self.assertIs(
                trigger.wait(0.1).event,
                DaggerTriggerEvent.OWNERSHIP_TOGGLE,
            )
            os.write(self.master_fd, b"a")
            self.assertIs(trigger.wait(0.1).event, DaggerTriggerEvent.ABORT)
        self.assertEqual(
            messages,
            ["DAGGER_TRIGGER_MODE=keyboard-fallback reason=pedals missing"],
        )


if __name__ == "__main__":
    unittest.main()
