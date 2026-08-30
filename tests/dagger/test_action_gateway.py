from __future__ import annotations

import unittest
from concurrent.futures import Future
from threading import Event, Thread
from time import sleep

from arx5_collection.dagger.action_gateway import (
    DualArmJointState,
    ExecutorStep,
    FixedRateCommandExecutor,
    JointActionSafety,
    NoopPolicyModeController,
    Pi05JointActionContract,
    PolicyActionGateway,
)
from arx5_collection.dagger.models import InferenceTicket, PolicyExecutionProfile
from arx5_collection.gripper import ARX5_GRIPPER_CALIBRATION


SHA = "a" * 64
EXECUTION = PolicyExecutionProfile(4, 14, 3, 25.0)


def action(value: float = 0.0, left_gripper: float = 0.0, right_gripper: float = 1.0):
    return (
        value,
        value,
        value,
        value,
        value,
        value,
        left_gripper,
        value,
        value,
        value,
        value,
        value,
        value,
        right_gripper,
    )


def ticket(epoch: int = 0, actions=None) -> InferenceTicket:
    return InferenceTicket(
        inference_id="inference-1",
        control_epoch=epoch,
        checkpoint_sha256=SHA,
        action_chunk=tuple(actions or [action()] * 4),
        execution=EXECUTION,
    )


class StateSource:
    def read(self) -> DualArmJointState:
        return DualArmJointState((0.0,) * 6, (0.0,) * 6)


class Policy:
    def __init__(self) -> None:
        self.epoch = 0
        self.future: Future[InferenceTicket] = Future()
        self.calls = []

    def begin_epoch(self, control_epoch: int) -> None:
        self.epoch = control_epoch

    def submit(self, episode_id: str, control_epoch: int, inference_id=None):
        self.calls.append((episode_id, control_epoch, inference_id))
        return self.future

    def reset_future(self) -> Future[InferenceTicket]:
        self.future = Future()
        return self.future


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class CommandSink:
    def __init__(self, fail: bool = False) -> None:
        self.commands = []
        self.fail = fail

    def publish(self, command) -> None:
        if self.fail:
            raise RuntimeError("publisher failed")
        self.commands.append(command)


class PolicyMode:
    def __init__(self) -> None:
        self.calls = 0

    def enable_policy_control(self) -> None:
        self.calls += 1


class BlockingPolicyMode(PolicyMode):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def enable_policy_control(self) -> None:
        super().enable_policy_control()
        self.entered.set()
        if not self.release.wait(1.0):
            raise RuntimeError("test policy mode was not released")


class BlockingCommandSink(CommandSink):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def publish(self, command) -> None:
        self.entered.set()
        if not self.release.wait(1.0):
            raise RuntimeError("test publish was not released")
        super().publish(command)


class Pi05JointActionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = Pi05JointActionContract(
            SHA,
            ARX5_GRIPPER_CALIBRATION,
            JointActionSafety(0.25, 1.5, 0.0, 1.0),
        )
        self.state = DualArmJointState((0.0,) * 6, (0.0,) * 6)

    def test_maps_only_execution_horizon_and_denormalizes_grippers(self) -> None:
        commands = self.contract.validate(ticket(), self.state, 0)

        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0].left, (0.0,) * 6 + (-3.4,))
        self.assertEqual(commands[0].right, (0.0,) * 6 + (0.0,))

    def test_saturates_accepted_policy_gripper_range(self) -> None:
        saturations = []
        commands = self.contract.validate_actions(
            (
                action(left_gripper=-1.0, right_gripper=2.0),
                action(left_gripper=-0.002147, right_gripper=1.1),
            ),
            self.state,
            saturation_sink=saturations.append,
        )

        self.assertEqual(commands[0].left[-1], -3.4)
        self.assertEqual(commands[0].right[-1], 0.0)
        self.assertEqual(commands[1].left[-1], -3.4)
        self.assertEqual(commands[1].right[-1], 0.0)
        self.assertEqual(len(saturations), 4)
        self.assertEqual(saturations[2].input_value, -0.002147)
        self.assertEqual(saturations[2].output_value, 0.0)

    def test_applies_configured_gripper_offset_to_command(self) -> None:
        contract = Pi05JointActionContract(
            SHA,
            ARX5_GRIPPER_CALIBRATION,
            JointActionSafety(0.25, 1.5, 0.0, 1.0),
            gripper_action_offset=0.1,
        )
        commands = contract.validate_actions(
            (action(left_gripper=0.0, right_gripper=0.5),),
            self.state,
        )

        self.assertAlmostEqual(commands[0].left[-1], -3.06)
        self.assertAlmostEqual(commands[0].right[-1], -1.36)

    def test_allows_configured_gripper_preload_above_one(self) -> None:
        contract = Pi05JointActionContract(
            SHA,
            ARX5_GRIPPER_CALIBRATION,
            JointActionSafety(0.25, 1.5, 0.0, 1.11),
            gripper_action_offset=0.1,
        )
        commands = contract.validate_actions(
            (action(left_gripper=1.0, right_gripper=1.02),),
            self.state,
        )

        self.assertAlmostEqual(commands[0].left[-1], 0.34)
        self.assertAlmostEqual(commands[0].right[-1], 0.374)

    def test_rejects_step_gripper_epoch_and_checkpoint(self) -> None:
        invalid = (
            ticket(actions=[action(), action(0.3), action(), action()]),
            ticket(actions=[action(left_gripper=2.000001)] * 4),
            ticket(actions=[action(left_gripper=-1.000001)] * 4),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(RuntimeError):
                    self.contract.validate(candidate, self.state, 0)
        with self.assertRaises(RuntimeError):
            self.contract.validate(ticket(epoch=1), self.state, 0)
        mismatched = InferenceTicket(
            "inference-2", 0, "b" * 64, tuple([action()] * 4), EXECUTION
        )
        with self.assertRaises(RuntimeError):
            self.contract.validate(mismatched, self.state, 0)

    def test_rejects_accumulated_departure(self) -> None:
        execution = PolicyExecutionProfile(10, 14, 10, 25.0)
        departure_ticket = InferenceTicket(
            "inference-departure",
            0,
            SHA,
            tuple(action(index * 0.2) for index in range(10)),
            execution,
        )

        with self.assertRaisesRegex(RuntimeError, "departure"):
            self.contract.validate(departure_ticket, self.state, 0)


class PolicyActionGatewayTest(unittest.TestCase):
    def make_gateway(self, mode=None):
        policy = Policy()
        contract = Pi05JointActionContract(
            SHA,
            ARX5_GRIPPER_CALIBRATION,
            JointActionSafety(0.25, 1.5, 0.0, 1.0),
        )
        mode = mode or NoopPolicyModeController()
        return PolicyActionGateway(
            policy,
            StateSource(),
            contract,
            mode,
        ), policy

    def test_opens_lease_only_after_future_and_contract_are_ready(self) -> None:
        gateway, policy = self.make_gateway()
        gateway.prepare_policy("episode-1", 0)

        self.assertFalse(gateway.policy_ready("episode-1", 0))
        self.assertFalse(gateway.gate_open)
        policy.future.set_result(ticket())
        self.assertTrue(gateway.policy_ready("episode-1", 0))
        self.assertTrue(gateway.gate_open)
        self.assertEqual(gateway.pending_command_count, 3)

    def test_epoch_change_discards_future_and_queue(self) -> None:
        gateway, policy = self.make_gateway()
        gateway.prepare_policy("episode-1", 0)
        policy.future.set_result(ticket())
        gateway.policy_ready("episode-1", 0)

        gateway.close_gate(0)
        gateway.clear_pending(1)

        self.assertFalse(gateway.gate_open)
        self.assertEqual(gateway.pending_command_count, 0)
        self.assertEqual(policy.epoch, 1)
        with self.assertRaises(RuntimeError):
            gateway.pop_command(0)

    def test_invalid_ticket_never_opens_lease(self) -> None:
        mode = PolicyMode()
        gateway, policy = self.make_gateway(mode)
        gateway.prepare_policy("episode-1", 0)
        policy.future.set_result(
            ticket(actions=[action(left_gripper=2.000001)] * 4)
        )

        with self.assertRaises(RuntimeError):
            gateway.policy_ready("episode-1", 0)
        self.assertFalse(gateway.gate_open)
        self.assertEqual(gateway.pending_command_count, 0)
        self.assertEqual(mode.calls, 0)

    def test_policy_mode_is_enabled_once_per_new_lease(self) -> None:
        mode = PolicyMode()
        gateway, policy = self.make_gateway(mode)
        gateway.prepare_policy("episode-1", 0)
        policy.future.set_result(ticket())
        gateway.policy_ready("episode-1", 0)
        self.assertEqual(mode.calls, 1)

        for _ in range(3):
            gateway.pop_command(0)
        policy.reset_future().set_result(ticket())
        gateway.prepare_policy("episode-1", 0)
        gateway.policy_ready("episode-1", 0)
        self.assertEqual(mode.calls, 1)

    def test_gate_close_waits_for_physical_policy_enable(self) -> None:
        mode = BlockingPolicyMode()
        gateway, policy = self.make_gateway(mode)
        gateway.prepare_policy("episode-1", 0)
        policy.future.set_result(ticket())
        ready_thread = Thread(
            target=lambda: gateway.policy_ready("episode-1", 0)
        )
        ready_thread.start()
        self.assertTrue(mode.entered.wait(1.0))

        closed = Event()

        def close_gate() -> None:
            gateway.close_gate(0)
            closed.set()

        close_thread = Thread(target=close_gate)
        close_thread.start()
        sleep(0.02)
        self.assertFalse(closed.is_set())
        mode.release.set()
        ready_thread.join(1.0)
        close_thread.join(1.0)

        self.assertTrue(closed.is_set())
        self.assertFalse(gateway.gate_open)


class FixedRateCommandExecutorTest(unittest.TestCase):
    def make_runtime(self, sink=None):
        gateway, policy = PolicyActionGatewayTest().make_gateway()
        policy.future.set_result(ticket())
        gateway.prepare_policy("episode-1", 0)
        gateway.policy_ready("episode-1", 0)
        clock = Clock()
        sink = sink or CommandSink()
        executor = FixedRateCommandExecutor(
            gateway,
            sink,
            control_rate_hz=25.0,
            policy_wait_timeout_s=0.5,
            command_watchdog_s=0.12,
            clock=clock,
        )
        return executor, gateway, policy, sink, clock

    def test_executes_at_fixed_rate_then_waits_for_fresh_policy(self) -> None:
        executor, gateway, policy, sink, clock = self.make_runtime()

        self.assertIs(executor.step(), ExecutorStep.PUBLISHED)
        clock.advance(0.02)
        self.assertIs(executor.step(), ExecutorStep.IDLE)
        clock.advance(0.02)
        self.assertIs(executor.step(), ExecutorStep.PUBLISHED)
        clock.advance(0.04)
        self.assertIs(executor.step(), ExecutorStep.PUBLISHED)
        self.assertEqual(len(sink.commands), 3)

        policy.reset_future()
        clock.advance(0.02)
        self.assertIs(executor.step(), ExecutorStep.IDLE)
        clock.advance(0.02)
        self.assertIs(executor.step(), ExecutorStep.POLICY_WAIT)
        self.assertEqual(len(sink.commands), 3)
        policy.future.set_result(ticket())
        self.assertIs(executor.step(), ExecutorStep.POLICY_READY)
        self.assertEqual(len(sink.commands), 3)
        self.assertTrue(gateway.gate_open)

    def test_policy_timeout_fails_closed_without_republishing(self) -> None:
        executor, gateway, policy, sink, clock = self.make_runtime()
        for _ in range(3):
            self.assertIs(executor.step(), ExecutorStep.PUBLISHED)
            clock.advance(0.04)
        policy.reset_future()
        self.assertIs(executor.step(), ExecutorStep.POLICY_WAIT)
        clock.advance(0.51)

        self.assertIs(executor.step(), ExecutorStep.FAULT)
        self.assertFalse(gateway.gate_open)
        self.assertEqual(len(sink.commands), 3)
        self.assertIn("policy wait timeout", str(gateway.take_fault()))

    def test_watchdog_and_publisher_errors_fail_closed(self) -> None:
        executor, gateway, _, _, clock = self.make_runtime()
        self.assertIs(executor.step(), ExecutorStep.PUBLISHED)
        clock.advance(0.2)
        self.assertIs(executor.step(), ExecutorStep.FAULT)
        self.assertFalse(gateway.gate_open)
        self.assertIn("watchdog", str(gateway.take_fault()))

        failing = CommandSink(fail=True)
        executor, gateway, _, _, _ = self.make_runtime(failing)
        self.assertIs(executor.step(), ExecutorStep.FAULT)
        self.assertFalse(gateway.gate_open)
        self.assertIn("publisher failed", str(gateway.take_fault()))

    def test_gate_close_waits_for_in_progress_paired_publish(self) -> None:
        sink = BlockingCommandSink()
        executor, gateway, _, _, _ = self.make_runtime(sink)
        publish_thread = Thread(target=executor.step)
        publish_thread.start()
        self.assertTrue(sink.entered.wait(1.0))

        closed = Event()

        def close_gate() -> None:
            gateway.close_gate(0)
            closed.set()

        close_thread = Thread(target=close_gate)
        close_thread.start()
        sleep(0.02)
        self.assertFalse(closed.is_set())
        sink.release.set()
        publish_thread.join(1.0)
        close_thread.join(1.0)

        self.assertTrue(closed.is_set())
        self.assertFalse(gateway.gate_open)
        self.assertEqual(len(sink.commands), 1)


if __name__ == "__main__":
    unittest.main()
