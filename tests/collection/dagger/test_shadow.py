from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from threading import Condition
from time import monotonic

from arx5_collection.collection.metadata import ShadowQuality
from arx5_collection.collection.dagger.models import (
    DaggerTriggerEvent,
    DaggerTriggerSignal,
    InferenceTicket,
)
from arx5_collection.collection.dagger.observation import (
    ObservationFailureCode,
    ObservationUnavailableError,
)
from arx5_collection.collection.dagger.shadow import (
    JsonlShadowLog,
    ShadowEpisodeHooks,
    ShadowInferenceLoop,
    ShadowRecordTrigger,
)
from arx5_collection.collection.episode.models import (
    EpisodeOutcome,
    RecordingStarted,
    RecordingStopping,
)
from arx5_collection.collection.episode.ports import TriggerEvent


ACTION_CHUNK = ((0.0,) * 14,) * 50


class AsyncPolicy:
    def __init__(self, outcomes: list[BaseException | None] | None = None) -> None:
        self._condition = Condition()
        self.calls: list[tuple[str, int, str | None]] = []
        self.outcomes = list(outcomes or [])
        self.epochs: list[int] = []

    def begin_epoch(self, control_epoch: int) -> None:
        self.epochs.append(control_epoch)

    def submit(self, episode_id: str, control_epoch: int, inference_id=None):
        future = Future()
        with self._condition:
            self.calls.append((episode_id, control_epoch, inference_id))
            outcome = self.outcomes.pop(0) if self.outcomes else None
            self._condition.notify_all()
        if outcome is None:
            future.set_result(
                InferenceTicket(inference_id, control_epoch, "a" * 64, ACTION_CHUNK)
            )
        else:
            future.set_exception(outcome)
        return future

    def wait_calls(self, count: int, timeout_s: float = 1.0) -> bool:
        deadline = monotonic() + timeout_s
        with self._condition:
            while len(self.calls) < count:
                remaining = deadline - monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    return False
        return True


class Trigger:
    def __init__(self, event: DaggerTriggerEvent | None = None) -> None:
        self.event = event

    def arm(self) -> None:
        pass

    def disarm(self) -> None:
        pass

    def wait(self, timeout_s: float):
        event, self.event = self.event, None
        return None if event is None else DaggerTriggerSignal(event, 123)


class ShadowInferenceLoopTest(unittest.TestCase):
    def test_uses_async_client_without_gateway(self) -> None:
        policy = AsyncPolicy()
        attempts = []
        shadow = ShadowInferenceLoop(
            policy,
            period_s=10.0,
            inference_id_factory=lambda: "request-1",
            attempt_sink=attempts.append,
        )
        shadow.start("episode-1")
        self.assertTrue(policy.wait_calls(1))
        shadow.stop(1.0)

        self.assertEqual(policy.epochs, [0])
        self.assertEqual(attempts[0].status, "success")
        self.assertEqual(shadow.summary.quality, ShadowQuality.HEALTHY)

    def test_failure_is_logged_retried_and_recovered_without_aborting(self) -> None:
        policy = AsyncPolicy(
            [
                ObservationUnavailableError(
                    ObservationFailureCode.CAMERA_SPAN_EXCEEDED,
                    observed_ns=45_000_000,
                    limit_ns=40_000_000,
                ),
                None,
            ]
        )
        attempts = []
        statuses = []
        ids = iter(("failed", "recovered", "next"))
        shadow = ShadowInferenceLoop(
            policy,
            period_s=0.01,
            inference_id_factory=lambda: next(ids),
            attempt_sink=attempts.append,
            status_sink=statuses.append,
        )
        shadow.start("episode-1")
        self.assertTrue(policy.wait_calls(2))
        shadow.stop(1.0)

        self.assertEqual(attempts[0].status, "camera_span_exceeded")
        self.assertEqual(attempts[0].observed_ns, 45_000_000)
        self.assertEqual(attempts[0].limit_ns, 40_000_000)
        self.assertEqual(attempts[1].status, "recovered")
        self.assertEqual(shadow.summary.inference_failure_count, 1)
        self.assertEqual(shadow.summary.recovery_count, 1)

    def test_jsonl_is_session_diagnostic_not_mcap_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.jsonl"
            policy = AsyncPolicy()
            with JsonlShadowLog(path) as log:
                shadow = ShadowInferenceLoop(policy, period_s=10.0, attempt_sink=log)
                shadow.start("episode-1")
                self.assertTrue(policy.wait_calls(1))
                shadow.stop(1.0)
            self.assertIn('"episode_id":"episode-1"', path.read_text())

    def test_episode_hook_exposes_summary_only(self) -> None:
        policy = AsyncPolicy([RuntimeError("transient"), None])
        shadow = ShadowInferenceLoop(policy, period_s=0.01)
        hooks = ShadowEpisodeHooks(shadow, "a" * 64)
        hooks.recording_started(RecordingStarted("episode-1", 100))
        self.assertTrue(policy.wait_calls(2))
        hooks.recording_stopping(RecordingStopping(EpisodeOutcome.SUCCESS, 200))

        context = hooks.metadata_context()
        assert context.dagger is not None and context.dagger.shadow is not None
        self.assertEqual(context.dagger.intervention_count, 0)
        self.assertEqual(context.dagger.shadow.inference_failure_count, 1)

    def test_maps_record_and_abort_but_ignores_ownership(self) -> None:
        messages = []
        record = ShadowRecordTrigger(Trigger(DaggerTriggerEvent.RECORD_TOGGLE)).wait(0)
        abort = ShadowRecordTrigger(Trigger(DaggerTriggerEvent.ABORT)).wait(0)
        self.assertIs(record.event, TriggerEvent.ACTIVATE)
        self.assertIs(abort.event, TriggerEvent.ABORT)
        self.assertEqual(record.monotonic_time_ns, 123)
        self.assertIsNone(
            ShadowRecordTrigger(
                Trigger(DaggerTriggerEvent.OWNERSHIP_TOGGLE), messages.append
            ).wait(0)
        )
        self.assertEqual(len(messages), 1)


if __name__ == "__main__":
    unittest.main()
