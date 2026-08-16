from __future__ import annotations

import pytest

from arx5_collection.reset import ResetCoordinator, ResetState


class FakeController:
    def __init__(self, events: list[object], error: Exception | None = None) -> None:
        self.events = events
        self.error = error

    def reset_both(self) -> None:
        self.events.append("controller")
        if self.error is not None:
            raise self.error


def test_reset_waits_five_seconds_then_completes() -> None:
    events: list[object] = []
    coordinator = ResetCoordinator(
        FakeController(events),
        sleep_fn=lambda delay: events.append(("sleep", delay)),
        state_sink=events.append,
    )

    coordinator.run()

    assert events == [
        ResetState.WAITING,
        ("sleep", 5.0),
        ResetState.RESETTING,
        "controller",
        ResetState.COMPLETE,
    ]


def test_reset_failure_never_reports_complete() -> None:
    events: list[object] = []
    coordinator = ResetCoordinator(
        FakeController(events, RuntimeError("reset failed")),
        delay_s=0,
        sleep_fn=lambda delay: events.append(("sleep", delay)),
        state_sink=events.append,
    )

    with pytest.raises(RuntimeError, match="reset failed"):
        coordinator.run()

    assert ResetState.COMPLETE not in events


def test_zero_delay_starts_reset_without_waiting() -> None:
    events: list[object] = []
    coordinator = ResetCoordinator(
        FakeController(events),
        delay_s=0,
        sleep_fn=lambda delay: events.append(("sleep", delay)),
        state_sink=events.append,
    )

    coordinator.run()

    assert events == [ResetState.RESETTING, "controller", ResetState.COMPLETE]
