from __future__ import annotations

import unittest

from arx5_collection.collection_metadata import ControlOwner
from arx5_collection.dagger.models import DaggerTriggerEvent
from arx5_collection.dagger.takeover import (
    AuthorityEventType,
    AuthorityTimeline,
    NoActionGateway,
    TakeoverController,
    TakeoverRecordTrigger,
    TakeoverState,
)
from arx5_collection.episode.models import EpisodeOutcome
from arx5_collection.episode.ports import TriggerEvent


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

    def wait(self, timeout_s: float):
        del timeout_s
        return self.events.pop(0) if self.events else None


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

    def test_single_intervention_orders_side_effects_events_and_segments(self) -> None:
        gateway = Gateway()
        human = HumanMode()
        controller, clock, events = self.make_controller(gateway, human)
        controller.start_episode("episode-1")

        clock.advance(0.1)
        self.assertIs(controller.toggle_ownership(), TakeoverState.HUMAN_ACTIVE)
        clock.advance(0.2)
        self.assertIs(controller.toggle_ownership(), TakeoverState.MODEL_CONTROL)
        clock.advance(0.3)
        controller.stop_episode(EpisodeOutcome.SUCCESS)

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
            [event.control_epoch for event in events], [0, 0, 1, 1, 1]
        )
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

    def test_policy_authority_waits_for_asynchronous_gateway_readiness(self) -> None:
        gateway = PendingGateway()
        controller, clock, events = self.make_controller(gateway)

        controller.start_episode("episode-1")

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
        controller.start_episode("episode-1")
        for _ in range(2):
            clock.advance(0.1)
            controller.toggle_ownership()
            clock.advance(0.1)
            controller.toggle_ownership()
        controller.stop_episode(EpisodeOutcome.SUCCESS)

        self.assertEqual(controller.control_epoch, 3)
        self.assertEqual([event.sequence for event in events], list(range(1, 10)))
        context = controller.metadata_context()
        assert context.dagger is not None
        self.assertEqual(context.dagger.intervention_count, 2)

    def test_new_episode_resets_interventions_but_not_epoch_or_sequence(self) -> None:
        controller, clock, events = self.make_controller()
        controller.start_episode("episode-1")
        clock.advance(0.1)
        controller.toggle_ownership()
        controller.stop_episode(EpisodeOutcome.SUCCESS)

        controller.start_episode("episode-2")
        clock.advance(0.1)
        controller.stop_episode(EpisodeOutcome.SUCCESS)

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
        controller.start_episode("episode-1")
        clock.advance(0.1)

        self.assertIs(controller.toggle_ownership(), TakeoverState.FAULT_HOLD)
        self.assertIs(controller.toggle_ownership(), TakeoverState.FAULT_HOLD)
        controller.stop_episode(EpisodeOutcome.SUCCESS)

        self.assertIs(events[-1].event_type, AuthorityEventType.FAULT_HOLD)
        self.assertIn("clear failed", events[-1].reason)

    def test_runtime_fault_fails_closed_and_enters_gravity_compensation(self) -> None:
        gateway = FaultGateway(RuntimeError("watchdog expired"))
        human = HumanMode()
        controller, _, events = self.make_controller(gateway, human)
        controller.start_episode("episode-1")

        self.assertIs(controller.poll_runtime(), TakeoverState.FAULT_HOLD)

        self.assertEqual(human.calls, 1)
        self.assertEqual(controller.control_epoch, 1)
        self.assertIs(events[-1].event_type, AuthorityEventType.FAULT_HOLD)
        self.assertIn("watchdog expired", events[-1].reason)

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
        self.assertIs(trigger.wait(0), TriggerEvent.ACTIVATE)
        self.assertIs(trigger.wait(0), TriggerEvent.ABORT)


if __name__ == "__main__":
    unittest.main()
