from __future__ import annotations

import unittest
from concurrent.futures import Future

from arx5_collection.dagger.action_gateway import (
    DualArmJointState,
    JointActionSafety,
    NoopPolicyModeController,
    Pi05JointActionContract,
)
from arx5_collection.dagger.models import (
    InferenceTicket,
    Pi05CheckpointProfile,
    Pi05InputProfile,
    PolicyExecutionProfile,
    RtcRolloutProfile,
)
from arx5_collection.dagger.observation import GripperCalibration
from arx5_collection.dagger.rtc_scheduler import (
    RollingMaxDelayEstimator,
    RtcActionScheduler,
)


SHA = "a" * 64
EXECUTION = PolicyExecutionProfile(8, 14, 2, 25.0)
CHECKPOINT = Pi05CheckpointProfile(
    policy_type="training_time_rtc",
    execution=EXECUTION,
    max_delay_steps=3,
    flow_steps=4,
    action_semantics="absolute_joint",
    prefix_mode="hard_prefix",
    input=Pi05InputProfile(
        640,
        360,
        3,
        "chw",
        "rgb",
        "uint8",
        "inter_area",
        "none",
        "none",
        224,
        224,
        "resize_with_pad",
        "overview",
        "left",
        "right",
    ),
)
ROLLOUT = RtcRolloutProfile(2, 1, 3, "rolling_max")


def action(value: float = 0.0) -> tuple[float, ...]:
    return (value,) * 6 + (0.0,) + (value,) * 6 + (0.0,)


def ticket(epoch: int, value: float = 0.0, fixed_prefix: int = 0) -> InferenceTicket:
    actions = (action(),) * fixed_prefix + (action(value),) * (8 - fixed_prefix)
    return InferenceTicket("request", epoch, SHA, actions, EXECUTION)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self) -> None:
        self.value += 1.0 / EXECUTION.control_rate_hz


class Policy:
    def __init__(self) -> None:
        self.epoch = 0
        self.calls = []
        self.futures: list[Future] = []

    def begin_epoch(self, epoch: int) -> None:
        self.epoch = epoch

    def submit(self, episode_id, epoch, inference_id=None, rtc=None):
        future = Future()
        self.futures.append(future)
        self.calls.append((episode_id, epoch, inference_id, rtc))
        return future


class State:
    def read(self):
        return DualArmJointState((0.0,) * 6, (0.0,) * 6)


class Sink:
    def __init__(self) -> None:
        self.commands = []

    def publish(self, command) -> None:
        self.commands.append(command)


class Mode(NoopPolicyModeController):
    def __init__(self) -> None:
        self.calls = 0

    def enable_policy_control(self) -> None:
        self.calls += 1


class RtcSchedulerTest(unittest.TestCase):
    def make_scheduler(self):
        policy = Policy()
        sink = Sink()
        mode = Mode()
        clock = Clock()
        diagnostics = []
        scheduler = RtcActionScheduler(
            policy,
            State(),
            sink,
            Pi05JointActionContract(
                SHA,
                GripperCalibration(-3.0, 0.0, -3.0, 0.0),
                JointActionSafety(0.25, 1.5, 0.0, 1.0),
            ),
            mode,
            CHECKPOINT,
            ROLLOUT,
            policy_wait_timeout_s=1.0,
            command_watchdog_s=0.1,
            diagnostic_sink=diagnostics.append,
            clock=clock,
        )
        return scheduler, policy, sink, mode, clock, diagnostics

    @staticmethod
    def bootstrap(scheduler, policy):
        scheduler.prepare_policy("episode-1", 0)
        policy.futures[0].set_result(ticket(0))
        assert scheduler.policy_ready("episode-1", 0)

    def issue(self, scheduler, clock, count):
        for _ in range(count):
            scheduler.step()
            clock.advance()

    def test_bootstrap_validates_safe_window_then_prefetches_one_request(self) -> None:
        scheduler, policy, sink, mode, clock, _ = self.make_scheduler()
        self.bootstrap(scheduler, policy)

        self.issue(scheduler, clock, 2)

        self.assertEqual(len(sink.commands), 2)
        self.assertEqual(mode.calls, 1)
        self.assertEqual(len(policy.calls), 2)
        context = policy.calls[1][3]
        self.assertEqual(context.estimated_delay_steps, 1)
        self.assertEqual(context.action_prefix, (action(),))

    def test_accepts_delay_two_and_atomically_replaces_safe_tail(self) -> None:
        scheduler, policy, _, _, clock, diagnostics = self.make_scheduler()
        self.bootstrap(scheduler, policy)
        self.issue(scheduler, clock, 2)
        self.issue(scheduler, clock, 2)
        policy.futures[1].set_result(ticket(0, 0.1, fixed_prefix=1))

        scheduler.step()

        self.assertIsNone(scheduler.take_fault())
        self.assertEqual(scheduler.pending_command_count, 3)
        accepted = [row for row in diagnostics if row["event"] == "inference_accepted"]
        self.assertEqual(accepted[-1]["actual_delay_steps"], 2)
        self.assertEqual(accepted[-1]["splice_start"], 2)

    def test_delay_at_trained_upper_bound_fails_closed(self) -> None:
        scheduler, policy, _, _, clock, _ = self.make_scheduler()
        self.bootstrap(scheduler, policy)
        self.issue(scheduler, clock, 2)
        self.issue(scheduler, clock, 3)

        self.assertIn("queue underrun", str(scheduler.take_fault()))
        self.assertFalse(scheduler.gate_open)

    def test_epoch_change_discards_queue_and_inflight_response(self) -> None:
        scheduler, policy, _, _, clock, _ = self.make_scheduler()
        self.bootstrap(scheduler, policy)
        self.issue(scheduler, clock, 2)

        scheduler.close_gate(0)
        scheduler.clear_pending(1)
        policy.futures[1].set_result(ticket(0))
        scheduler.step()

        self.assertEqual(policy.epoch, 1)
        self.assertEqual(scheduler.pending_command_count, 0)
        self.assertIsNone(scheduler.take_fault())

    def test_changed_hard_prefix_fails_closed_before_splice(self) -> None:
        scheduler, policy, _, _, clock, _ = self.make_scheduler()
        self.bootstrap(scheduler, policy)
        self.issue(scheduler, clock, 2)
        policy.futures[1].set_result(ticket(0, 0.1))

        scheduler.step()

        self.assertIn("hard prefix changed", str(scheduler.take_fault()))
        self.assertFalse(scheduler.gate_open)


class RollingMaxDelayEstimatorTest(unittest.TestCase):
    def test_uses_initial_then_bounded_rolling_maximum(self) -> None:
        estimator = RollingMaxDelayEstimator(1, 2, 4)
        self.assertEqual(estimator.estimate, 1)
        estimator.observe(3)
        estimator.observe(2)
        self.assertEqual(estimator.estimate, 3)
        estimator.observe(1)
        self.assertEqual(estimator.estimate, 2)


if __name__ == "__main__":
    unittest.main()
