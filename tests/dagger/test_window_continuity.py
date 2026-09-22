"""CPU-only regressions: no ROS, network, model inference, or physical commands."""

from dataclasses import replace
import unittest

from arx5_collection.dagger.action_gateway import DualArmJointCommand, DualArmJointState
from arx5_collection.dagger.models import (
    InferenceTicket,
    PolicyExecutionProfile,
    RtcRolloutProfile,
)
from arx5_collection.dagger.expo import ExpoActionScheduler
from concurrent.futures import Future
from tests.dagger.test_rtc_scheduler import CHECKPOINT, SHA, State, Sink, Mode
from arx5_collection.dagger.action_gateway import (
    Pi05JointActionContract,
    JointActionSafety,
)
from arx5_collection.gripper import ARX5_GRIPPER_CALIBRATION


class Policy:
    def __init__(self):
        self.slow = []
        self.fast = []

    def begin_epoch(self, epoch):
        pass

    def submit(self, *args, **kwargs):
        future = Future()
        self.slow.append((future, args, kwargs))
        return future

    def finish(self, *args):
        future = Future()
        self.fast.append((future, args))
        return future


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def action(value, side="right"):

    row = [0.0] * 14
    row[0 if side == "left" else 7] = value
    return tuple(row)


def setup(side="right"):
    p, clock, sink, events = Policy(), Clock(), Sink(), []
    cp = replace(
        CHECKPOINT,
        execution=PolicyExecutionProfile(30, 14, 9, 30.0),
        max_delay_steps=10,
    )
    contract = Pi05JointActionContract(
        SHA, ARX5_GRIPPER_CALIBRATION, JointActionSafety(0.25, 1.5, 0.0, 1.0)
    )
    s = ExpoActionScheduler(
        p,
        State(),
        sink,
        contract,
        Mode(),
        cp,
        RtcRolloutProfile(1, 8, 10, "rolling_max"),
        0.15,
        0.12,
        clock=clock,
        bootstrap_timeout_s=2.0,
        diagnostic_sink=events.append,
    )
    s.clear_pending(1)
    s.prepare_policy("synthetic", 1)
    p.slow[0][0].set_result(
        InferenceTicket("initial", 1, SHA, (action(0.24, side),) * 30, cp.execution)
    )
    assert s.policy_ready("synthetic", 1)
    return s, p, clock, sink, events, cp


def continuation(s, p, clock, cp, value, side="right"):
    s.step()
    p.slow[1][0].set_result({"ticket": "prepared"})
    for i in range(1, 9):
        clock.value = i / 30
        s.step()
    p.fast[0][0].set_result(
        InferenceTicket(
            "next",
            1,
            SHA,
            (action(0.24, side),) * 8 + (action(value, side),) * 22,
            cp.execution,
            candidate_identity=(
                ("bundle_id", "synthetic"),
                ("selected_index", 41),
                ("mode", "q_edit"),
            ),
        )
    )
    clock.value = 8 / 30 + 0.001
    s.step()
    clock.value = 9 / 30
    s.step()


class WindowContinuityTests(unittest.TestCase):
    def test_tracking_lag_cannot_hide_cross_window_jump(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                s, p, clock, sink, events, cp = setup(side)
                continuation(s, p, clock, cp, -0.24, side)
                self.assertFalse(s.gate_open)
                self.assertEqual(len(sink.commands), 9)
                self.assertIn("published_target_step", str(s.take_fault()))
                event = next(e for e in events if e["event"] == "window_rejected")
                self.assertEqual(event["violation"]["side"], side)
                self.assertEqual(event["violation"]["joint_index"], 0)
                self.assertAlmostEqual(event["violation"]["delta_rad"], 0.48)
                self.assertEqual(event["candidate_identity"]["selected_index"], 41)
                self.assertEqual(event["measured_state"][side][0], 0.0)
                self.assertEqual(event["previous_published_target"][side][0], 0.24)
                self.assertEqual(event["first_target"][side][0], -0.24)

    def test_continuous_window_is_accepted(self):
        s, p, clock, sink, events, cp = setup()
        continuation(s, p, clock, cp, 0.2)
        self.assertTrue(s.gate_open)
        self.assertEqual(len(sink.commands), 10)
        self.assertIsNone(s.take_fault())
        self.assertFalse(any(e["event"] == "window_rejected" for e in events))

    def test_measured_state_guard_remains(self):
        s, p, clock, sink, events, cp = setup()
        continuation(s, p, clock, cp, 0.270307)
        self.assertEqual(len(sink.commands), 9)
        self.assertIn("measured_state_step", str(s.take_fault()))

    def test_reset_forgets_old_target_and_allows_new_bootstrap(self):
        s, p, clock, sink, events, cp = setup()
        s.step()
        self.assertIsNotNone(s._last_published_command)
        s.clear_pending(2)
        self.assertIsNone(s._last_published_command)
        s.prepare_policy("new", 2)
        p.slow[-1][0].set_result(
            InferenceTicket("new", 2, SHA, (action(-0.24),) * 30, cp.execution)
        )
        self.assertTrue(s.policy_ready("new", 2))

    def test_failed_publish_does_not_advance_reference(self):
        s, p, clock, sink, events, cp = setup()

        def fail(command):
            raise RuntimeError("sink failed")

        sink.publish = fail
        s.step()
        self.assertIsNone(s._last_published_command)
        self.assertFalse(s.gate_open)

    def test_log_failure_does_not_mask_safety_or_keep_gate_open(self):
        s, p, clock, sink, events, cp = setup()

        def log(event):
            if event["event"] == "window_rejected":
                raise OSError("disk full")

        s.diagnostic_sink = log
        continuation(s, p, clock, cp, -0.24)
        self.assertFalse(s.gate_open)
        self.assertEqual(s.pending_command_count, 0)
        self.assertIn("published_target_step", str(s.take_fault()))

    def test_gripper_not_compared_as_joint(self):
        s, *_ = setup()
        previous = DualArmJointCommand((0.0,) * 6 + (100.0,), (0.0,) * 6 + (-100.0,))
        self.assertEqual(
            len(
                s.contract.validate_actions(
                    (action(0.0),),
                    DualArmJointState((0.0,) * 6, (0.0,) * 6),
                    last_published_command=previous,
                )
            ),
            1,
        )

    def test_within_window_guard_remains(self):
        s, *_ = setup()
        with self.assertRaisesRegex(RuntimeError, "within_window_step"):
            s.contract.validate_actions((action(0.24), action(-0.24)), State().read())


if __name__ == "__main__":
    unittest.main()
