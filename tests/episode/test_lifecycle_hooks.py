from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arx5_collection.episode.cli import run_episode_loop
from arx5_collection.episode.models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeResult,
)


class EmptyStore:
    def list_partials(self):
        return ()


class FakeRuntime:
    def __init__(self, result: EpisodeResult | BaseException, events: list[str]) -> None:
        self.store = EmptyStore()
        self.state_sink = None
        self.result = result
        self.events = events

    def run_once(self, request):
        self.events.append("run_once")
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def request() -> EpisodeRequest:
    return EpisodeRequest("task", "task", Path("out"), Path("station"), ())


def result(outcome: EpisodeOutcome, errors: tuple[str, ...] = ()) -> EpisodeResult:
    now = datetime.now(timezone.utc)
    return EpisodeResult(
        "episode",
        outcome,
        now,
        now,
        1.0,
        True,
        Path("episode.mcap"),
        Path("metadata.json"),
        errors=errors,
    )


@pytest.mark.parametrize(
    ("outcome", "errors", "exit_code"),
    [
        (EpisodeOutcome.SUCCESS, (), 0),
        (EpisodeOutcome.ABORTED, ("operator requested abort",), 0),
        (EpisodeOutcome.ABORTED, ("required camera stopped",), 2),
        (EpisodeOutcome.ABORTED, ("recording interrupted",), 2),
    ],
)
def test_committed_episode_runs_reset_once(outcome, errors, exit_code) -> None:
    events: list[str] = []
    runtime = FakeRuntime(result(outcome, errors), events)

    actual = run_episode_loop(
        runtime,  # type: ignore[arg-type]
        request(),
        episodes=1,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        after_episode=lambda: events.append("reset"),
        on_idle_exit=lambda: events.append("idle_reset"),
    )

    assert actual == exit_code
    assert events == ["run_once", "reset"]


def test_idle_interrupt_resets_before_exit() -> None:
    events: list[str] = []
    runtime = FakeRuntime(KeyboardInterrupt(), events)

    actual = run_episode_loop(
        runtime,  # type: ignore[arg-type]
        request(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        after_episode=lambda: events.append("reset"),
        on_idle_exit=lambda: events.append("idle_reset"),
    )

    assert actual == 0
    assert events == ["run_once", "idle_reset"]
