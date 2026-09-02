from __future__ import annotations

import unittest

from arx5_collection.collection.metadata import ControlOwner
from arx5_collection.collection.dagger.models import (
    DaggerTriggerEvent,
    DaggerTriggerSignal,
)
from arx5_collection.collection.dagger.takeover import (
    AuthorityEventType,
    AuthorityTimeline,
    NoActionGateway,
    TakeoverController,
    TakeoverRecordTrigger,
    TakeoverState,
)
from arx5_collection.collection.episode.models import (
    EpisodeOutcome,
    RecordingStarted,
    RecordingStopping,
)
from arx5_collection.collection.episode.ports import TriggerEvent


class Clock:
    def __init__(self) -> None:
        self.value = 1_000_000_000

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += int(seconds * 1e9)


class Gateway(NoActionGateway):
    def __init__(self, fail_at: str | None = None) -> None:
        self.calls = []
        self.fail_at = fail_at

    def close_gate(self, control_epoch: int) -> None:
        self._call("close", control_epoch)

    def clear_pending(self, control_epoch: int) -> None:
        self._call("clear", control_epoch)

    def prepare_policy(self, episode_id: str, control_epoch: int) -> None:
        self._call("prepare", episode_id, control_epoch)

    def _call(self, name: str, *values) -> None:
        self.calls.append((name, *values))
        if self.fail_at == name:
            raise RuntimeError(f"{name} failed")


class PendingGateway(Gateway):
    action_output_enabled = True

    def __init__(self) -> None:
        super().__init__()
        self.ready = False

    def policy_ready(self, episode_id: str, control_epoch: int) -> bool:
        self._call("poll", episode_id, control_epoch)
        return self.ready


class FaultGateway(PendingGateway):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def take_fault(self) -> BaseException | None:
        error, self.error = self.error, None
        return error


class HumanMode:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def enable_gravity_compensation(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("gravity failed")


class Trigger:
    def __init__(self, *events: DaggerTriggerEvent) -> None:
        self.events = list(events)

    def arm(self) -> None:
        pass

    def disarm(self) -> None:
        pass

    def wait(self, timeout_s: float):
        del timeout_s
        if not self.events:
            return None
        return DaggerTriggerSignal(self.events.pop(0), 123)


class TakeoverControllerTest(unittest.TestCase):
    def make_controller(self, gateway=None, human=None):
        clock = Clock()
        events = []
        timeline = AuthorityTimeline("a" * 64, events.append, clock)
        controller = TakeoverController(
            gateway or Gateway(),
            human or HumanMode(),
            timeline,
        )
        return controller, clock, events

    @staticmethod
    def start(controller, clock, episode_id="episode-1") -> None:
        controller.start_episode(RecordingStarted(episode_id, clock.value))

    @staticmethod
    def stop(controller, clock) -> None:
        controller.stop_episode(RecordingStopping(EpisodeOutcome.SUCCESS, clock.value))

    def test_single_intervention_orders_side_effects_events_and_segments(self) -> None:
        gateway = Gateway()
        human = HumanMode()
        controller, clock, events = self.make_controller(gateway, human)
        self.start(controller, clock)

        clock.advance(0.1)
        self.assertIs(
            controller.toggle_ownership(clock.value),
            TakeoverState.HUMAN_ACTIVE,
        )
        clock.advance(0.2)
        self.assertIs(
            controller.toggle_ownership(clock.value),
            TakeoverState.MODEL_CONTROL,
        )
        clock.advance(0.3)
        stopping_time_ns = clock.value
        clock.advance(0.5)  # Control cleanup starts after the pedal boundary.
        controller.stop_episode(
            RecordingStopping(EpisodeOutcome.SUCCESS, stopping_time_ns)
        )

        self.assertEqual(
            [event.event_type for event in events],
            [
                AuthorityEventType.POLICY_ACTIVE,
                AuthorityEventType.TAKEOVER_REQUESTED,
                AuthorityEventType.HUMAN_ACTIVE,
                AuthorityEventType.RESUME_REQUESTED,
                AuthorityEventType.POLICY_ACTIVE,
            ],
        )
        self.assertEqual([event.sequence for event in events], [1, 2, 3, 4, 5])
        self.assertEqual(
            [event.monotonic_time_ns for event in events],
            [
                1_000_000_000,
                1_100_000_000,
                1_100_000_000,
                1_300_000_000,
                1_300_000_000,
            ],
        )
        self.assertEqual([event.control_epoch for event in events], [0, 0, 1, 1, 1])
        self.assertEqual(
            gateway.calls,
            [
                ("prepare", "episode-1", 0),
                ("close", 0),
                ("clear", 1),
                ("prepare", "episode-1", 1),
                ("close", 1),
                ("clear", 2),
            ],
        )
        self.assertEqual(human.calls, 2)
        context = controller.metadata_context()
        assert context.dagger is not None
        self.assertEqual(context.dagger.intervention_count, 1)
        self.assertEqual(
            [segment.owner for segment in context.dagger.control_segments],
            [ControlOwner.MODEL, ControlOwner.HUMAN, ControlOwner.MODEL],
        )
        self.assertEqual(
            context.dagger.control_segments[1].intervention_id,
            1,
        )
        self.assertEqual(
            [
                (segment.started_offset_s, segment.ended_offset_s)
                for segment in context.dagger.control_segments
            ],
            [(0.0, 0.1), (0.1, 0.3), (0.3, 0.6)],
        )

    def test_policy_authority_waits_for_asynchronous_gateway_readiness(self) -> None:
        gateway = PendingGateway()
        controller, clock, events = self.make_controller(gateway)

        self.start(controller, clock)

        self.assertIs(controller.state, TakeoverState.RESUME_PENDING)
        self.assertEqual(events, [])
        gateway.ready = True
        clock.advance(0.1)
        self.assertIs(controller.poll_policy(), TakeoverState.MODEL_CONTROL)
        self.assertEqual(
            [event.event_type for event in events],
            [AuthorityEventType.POLICY_ACTIVE],
        )
        self.assertEqual(events[0].reason, "action_gateway_ready")

    def test_multiple_interventions_keep_epoch_and_sequence_monotonic(self) -> None:
        controller, clock, events = self.make_controller()
        self.start(controller, clock)
        for _ in range(2):
            clock.advance(0.1)
            controller.toggle_ownership(clock.value)
            clock.advance(0.1)
            controller.toggle_ownership(clock.value)
        self.stop(controller, clock)

        self.assertEqual(controller.control_epoch, 3)
        self.assertEqual([event.sequence for event in events], list(range(1, 10)))
        context = controller.metadata_context()
        assert context.dagger is not None
        self.assertEqual(context.dagger.intervention_count, 2)

    def test_new_episode_resets_interventions_but_not_epoch_or_sequence(self) -> None:
        controller, clock, events = self.make_controller()
        self.start(controller, clock)
        clock.advance(0.1)
        controller.toggle_ownership(clock.value)
        self.stop(controller, clock)

        self.start(controller, clock, "episode-2")
        clock.advance(0.1)
        self.stop(controller, clock)

        self.assertEqual(controller.control_epoch, 3)
        self.assertEqual([event.sequence for event in events], [1, 2, 3, 4])
        context = controller.metadata_context()
        assert context.dagger is not None
        self.assertEqual(context.dagger.intervention_count, 0)
        self.assertEqual(
            [segment.owner for segment in context.dagger.control_segments],
            [ControlOwner.MODEL],
        )

    def test_failure_enters_latched_fault_hold_without_raising(self) -> None:
        controller, clock, events = self.make_controller(Gateway("clear"))
        self.start(controller, clock)
        clock.advance(0.1)

        self.assertIs(
            controller.toggle_ownership(clock.value),
            TakeoverState.FAULT_HOLD,
        )
        self.assertIs(
            controller.toggle_ownership(clock.value),
            TakeoverState.FAULT_HOLD,
        )
        self.stop(controller, clock)

        self.assertIs(events[-1].event_type, AuthorityEventType.FAULT_HOLD)
        self.assertIn("clear failed", events[-1].reason)

    def test_runtime_fault_fails_closed_and_enters_gravity_compensation(self) -> None:
        gateway = FaultGateway(RuntimeError("watchdog expired"))
        human = HumanMode()
        controller, clock, events = self.make_controller(gateway, human)
        self.start(controller, clock)

        self.assertIs(controller.poll_runtime(), TakeoverState.FAULT_HOLD)

        self.assertEqual(human.calls, 1)
        self.assertEqual(controller.control_epoch, 1)
        self.assertIs(events[-1].event_type, AuthorityEventType.FAULT_HOLD)
        self.assertIn("watchdog expired", events[-1].reason)

    def test_runtime_fault_requests_immediate_episode_failure(self) -> None:
        controller, clock, _ = self.make_controller(
            FaultGateway(RuntimeError("policy rejected action"))
        )
        self.start(controller, clock)
        trigger = TakeoverRecordTrigger(Trigger(), controller)

        signal = trigger.wait(0)

        assert signal is not None
        self.assertIs(signal.event, TriggerEvent.FAIL)
        self.assertEqual(signal.monotonic_time_ns, clock.value)
        self.assertIn("policy rejected action", signal.detail or "")

    def test_gravity_compensation_failure_is_safety_critical(self) -> None:
        human = HumanMode(fail=True)
        controller, clock, events = self.make_controller(
            FaultGateway(RuntimeError("watchdog expired")),
            human,
        )
        self.start(controller, clock)

        self.assertIs(controller.poll_runtime(), TakeoverState.FAULT_HOLD)
        with self.assertRaisesRegex(
            RuntimeError,
            "G_COMPENSATION recovery failed",
        ):
            controller.poll_runtime()
        with self.assertRaisesRegex(
            RuntimeError,
            "G_COMPENSATION recovery failed",
        ):
            self.stop(controller, clock)

        self.assertIs(events[-1].event_type, AuthorityEventType.FAULT_HOLD)
        self.assertIn("gravity_compensation=gravity failed", events[-1].reason)

    def test_no_action_gateway_explicitly_disables_output(self) -> None:
        self.assertFalse(NoActionGateway.action_output_enabled)

    def test_trigger_keeps_record_and_ownership_domains_separate(self) -> None:
        controller, _, _ = self.make_controller()
        trigger = TakeoverRecordTrigger(
            Trigger(
                DaggerTriggerEvent.OWNERSHIP_TOGGLE,
                DaggerTriggerEvent.RECORD_TOGGLE,
                DaggerTriggerEvent.ABORT,
            ),
            controller,
        )

        self.assertIsNone(trigger.wait(0))
        record = trigger.wait(0)
        abort = trigger.wait(0)
        self.assertIs(record.event, TriggerEvent.ACTIVATE)
        self.assertIs(abort.event, TriggerEvent.ABORT)
        self.assertEqual(record.monotonic_time_ns, 123)


if __name__ == "__main__":
    unittest.main()
