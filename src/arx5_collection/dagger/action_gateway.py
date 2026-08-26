from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol

from .models import InferenceTicket
from arx5_collection.gripper import GripperCalibration
from .policy_client import RtcPolicyContext


@dataclass(frozen=True, slots=True)
class DualArmJointState:
    left: tuple[float, ...]
    right: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.left) != 6 or len(self.right) != 6:
            raise ValueError("dual-arm state must contain six joints per arm")
        if not all(math.isfinite(value) for value in (*self.left, *self.right)):
            raise ValueError("dual-arm state must be finite")


@dataclass(frozen=True, slots=True)
class DualArmJointCommand:
    left: tuple[float, ...]
    right: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.left) != 7 or len(self.right) != 7:
            raise ValueError("dual-arm command must contain seven values per arm")
        if not all(math.isfinite(value) for value in (*self.left, *self.right)):
            raise ValueError("dual-arm command must be finite")


@dataclass(frozen=True, slots=True)
class JointActionSafety:
    max_joint_step_rad: float
    max_joint_departure_rad: float
    min_normalized_gripper: float
    max_normalized_gripper: float

    def __post_init__(self) -> None:
        values = (
            self.max_joint_step_rad,
            self.max_joint_departure_rad,
            self.min_normalized_gripper,
            self.max_normalized_gripper,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("action safety limits must be finite")
        if self.max_joint_step_rad <= 0 or self.max_joint_departure_rad <= 0:
            raise ValueError("joint safety limits must be positive")
        if self.min_normalized_gripper >= self.max_normalized_gripper:
            raise ValueError("normalized gripper limits are invalid")


class JointStateSource(Protocol):
    def read(self) -> DualArmJointState: ...


class AsyncPolicy(Protocol):
    def begin_epoch(self, control_epoch: int) -> None: ...

    def submit(
        self,
        episode_id: str,
        control_epoch: int,
        inference_id: str | None = None,
        rtc: RtcPolicyContext | None = None,
    ) -> Future[InferenceTicket]: ...


class PolicyModeController(Protocol):
    def enable_policy_control(self) -> None: ...


class NoopPolicyModeController:
    def enable_policy_control(self) -> None:
        return None


class DualArmCommandSink(Protocol):
    def publish(self, command: DualArmJointCommand) -> None: ...


class ExecutorStep(str, Enum):
    IDLE = "idle"
    PUBLISHED = "published"
    POLICY_WAIT = "policy_wait"
    POLICY_READY = "policy_ready"
    FAULT = "fault"


class Pi05JointActionContract:
    """Validate robot-space absolute actions without modifying them."""

    def __init__(
        self,
        checkpoint_sha256: str,
        grippers: GripperCalibration,
        safety: JointActionSafety,
    ) -> None:
        self.checkpoint_sha256 = checkpoint_sha256.lower()
        if len(self.checkpoint_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in self.checkpoint_sha256
        ):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        self.grippers = grippers
        self.safety = safety

    def validate(
        self,
        ticket: InferenceTicket,
        state: DualArmJointState,
        control_epoch: int,
    ) -> tuple[DualArmJointCommand, ...]:
        self.validate_ticket_identity(ticket, control_epoch)

        return self.validate_actions(ticket.execution_chunk, state)

    def validate_ticket_identity(
        self,
        ticket: InferenceTicket,
        control_epoch: int,
    ) -> None:
        if ticket.control_epoch != control_epoch:
            raise RuntimeError("action ticket belongs to a stale control epoch")
        if ticket.checkpoint_sha256 != self.checkpoint_sha256:
            raise RuntimeError("action ticket checkpoint does not match the Session")

    def validate_actions(
        self,
        actions: tuple[tuple[float, ...], ...],
        state: DualArmJointState,
    ) -> tuple[DualArmJointCommand, ...]:
        commands: list[DualArmJointCommand] = []
        previous_left = state.left
        previous_right = state.right
        for index, action in enumerate(actions):
            if len(action) != 14 or not all(math.isfinite(value) for value in action):
                raise RuntimeError(f"action[{index}] is not a finite robot-space 14D action")
            left = tuple(action[:6])
            left_gripper = action[6]
            right = tuple(action[7:13])
            right_gripper = action[13]
            self._validate_gripper(left_gripper, "left", index)
            self._validate_gripper(right_gripper, "right", index)
            self._validate_arm(left, previous_left, state.left, "left", index)
            self._validate_arm(right, previous_right, state.right, "right", index)
            commands.append(
                DualArmJointCommand(
                    left=(*left, self._denormalize_left(left_gripper)),
                    right=(*right, self._denormalize_right(right_gripper)),
                )
            )
            previous_left = left
            previous_right = right
        return tuple(commands)

    def _validate_arm(
        self,
        target: tuple[float, ...],
        previous: tuple[float, ...],
        initial: tuple[float, ...],
        side: str,
        index: int,
    ) -> None:
        step = max(
            abs(target_value - previous_value)
            for target_value, previous_value in zip(target, previous)
        )
        departure = max(
            abs(target_value - initial_value)
            for target_value, initial_value in zip(target, initial)
        )
        if step > self.safety.max_joint_step_rad:
            raise RuntimeError(
                f"{side} action[{index}] joint step {step:.6f} rad exceeds "
                f"{self.safety.max_joint_step_rad:.6f} rad"
            )
        if departure > self.safety.max_joint_departure_rad:
            raise RuntimeError(
                f"{side} action[{index}] joint departure {departure:.6f} rad exceeds "
                f"{self.safety.max_joint_departure_rad:.6f} rad"
            )

    def _validate_gripper(self, value: float, side: str, index: int) -> None:
        if not (
            self.safety.min_normalized_gripper
            <= value
            <= self.safety.max_normalized_gripper
        ):
            raise RuntimeError(
                f"{side} action[{index}] normalized gripper {value:.6f} is outside "
                f"[{self.safety.min_normalized_gripper:.6f}, "
                f"{self.safety.max_normalized_gripper:.6f}]"
            )

    def _denormalize_left(self, value: float) -> float:
        return self.grippers.denormalize(value)

    def _denormalize_right(self, value: float) -> float:
        return self.grippers.denormalize(value)


class PolicyActionGateway:
    """Own one epoch-scoped policy request and its validated command queue."""

    action_output_enabled = True

    def __init__(
        self,
        policy: AsyncPolicy,
        state_source: JointStateSource,
        contract: Pi05JointActionContract,
        policy_mode: PolicyModeController,
    ) -> None:
        self.policy = policy
        self.state_source = state_source
        self.contract = contract
        self.policy_mode = policy_mode
        self._control_epoch = 0
        self._gate_open = False
        self._pending: Future[InferenceTicket] | None = None
        self._queue: deque[DualArmJointCommand] = deque()
        self._episode_id: str | None = None
        self._fault: BaseException | None = None
        self._lock = Lock()

    @property
    def gate_open(self) -> bool:
        with self._lock:
            return self._gate_open

    @property
    def pending_command_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def has_pending_policy(self) -> bool:
        with self._lock:
            return self._pending is not None

    @property
    def control_epoch(self) -> int:
        with self._lock:
            return self._control_epoch

    @property
    def episode_id(self) -> str | None:
        with self._lock:
            return self._episode_id

    def close_gate(self, control_epoch: int) -> None:
        with self._lock:
            if control_epoch != self._control_epoch:
                raise RuntimeError("cannot close a stale command lease")
            self._gate_open = False

    def clear_pending(self, control_epoch: int) -> None:
        with self._lock:
            if control_epoch < self._control_epoch:
                raise RuntimeError("control epoch must not move backwards")
            self._control_epoch = control_epoch
            self._pending = None
            self._queue.clear()
            self._gate_open = False
        self.policy.begin_epoch(control_epoch)

    def prepare_policy(self, episode_id: str, control_epoch: int) -> None:
        with self._lock:
            if control_epoch != self._control_epoch:
                raise RuntimeError("cannot prepare policy for a stale control epoch")
            if self._pending is not None or self._queue:
                raise RuntimeError("policy preparation is already active")
            self._episode_id = episode_id
            self._pending = self.policy.submit(episode_id, control_epoch)

    def policy_ready(self, episode_id: str, control_epoch: int) -> bool:
        with self._lock:
            if control_epoch != self._control_epoch:
                raise RuntimeError("cannot poll policy for a stale control epoch")
            if episode_id != self._episode_id:
                raise RuntimeError("cannot poll policy for a different Episode")
            if self._pending is None:
                raise RuntimeError("policy preparation has not started")
            if not self._pending.done():
                return False
            pending = self._pending
            self._pending = None
            needs_policy_enable = not self._gate_open
        ticket = pending.result()
        commands = self.contract.validate(
            ticket,
            self.state_source.read(),
            control_epoch,
        )
        with self._lock:
            if (
                control_epoch != self._control_epoch
                or episode_id != self._episode_id
            ):
                raise RuntimeError("policy became stale before lease activation")
            # Keep the authority lease locked across the physical mode switch.
            # A concurrent Take-over can therefore only close the gate after
            # policy mode has been enabled and this lease is visible.
            if needs_policy_enable:
                self.policy_mode.enable_policy_control()
            self._queue.extend(commands)
            self._gate_open = True
        return True

    def pop_command(self, control_epoch: int) -> DualArmJointCommand:
        with self._lock:
            if control_epoch != self._control_epoch or not self._gate_open:
                raise RuntimeError("command lease is closed or stale")
            if not self._queue:
                raise RuntimeError("command queue is empty")
            return self._queue.popleft()

    def publish_next(
        self,
        control_epoch: int,
        sink: DualArmCommandSink,
    ) -> None:
        """Keep the command lease locked until both ROS publishes return."""
        with self._lock:
            if control_epoch != self._control_epoch or not self._gate_open:
                raise RuntimeError("command lease is closed or stale")
            if not self._queue:
                raise RuntimeError("command queue is empty")
            command = self._queue.popleft()
            sink.publish(command)

    def latch_fault(self, error: BaseException) -> None:
        with self._lock:
            self._gate_open = False
            self._queue.clear()
            if self._fault is None:
                self._fault = error

    def take_fault(self) -> BaseException | None:
        with self._lock:
            error, self._fault = self._fault, None
            return error


class FixedRateCommandExecutor:
    """Execute validated chunks sequentially and fail closed on timing faults."""

    def __init__(
        self,
        gateway: PolicyActionGateway,
        sink: DualArmCommandSink,
        control_rate_hz: float,
        policy_wait_timeout_s: float,
        command_watchdog_s: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if min(control_rate_hz, policy_wait_timeout_s, command_watchdog_s) <= 0:
            raise ValueError("executor rate and timeouts must be positive")
        self.gateway = gateway
        self.sink = sink
        self.period_s = 1.0 / control_rate_hz
        self.policy_wait_timeout_s = policy_wait_timeout_s
        self.command_watchdog_s = command_watchdog_s
        self.clock = clock
        self._active_epoch: int | None = None
        self._next_command_s: float | None = None
        self._policy_wait_started_s: float | None = None
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("command executor is already started")
        self._stop.clear()
        self._thread = Thread(
            target=self._run,
            name="dagger-command-executor",
            daemon=False,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("command executor did not stop")
        self._thread = None

    def step(self) -> ExecutorStep:
        now = self.clock()
        if not self.gateway.gate_open:
            self._reset_schedule()
            return ExecutorStep.IDLE

        epoch = self.gateway.control_epoch
        episode_id = self.gateway.episode_id
        if not episode_id:
            return self._fail(
                RuntimeError("open command lease has no Episode"),
                epoch,
            )
        if self._active_epoch != epoch:
            self._active_epoch = epoch
            self._next_command_s = now
            self._policy_wait_started_s = None

        if self.gateway.has_pending_policy:
            if self._policy_wait_started_s is None:
                return self._fail(
                    RuntimeError("policy wait has no active deadline"),
                    epoch,
                )
            if now - self._policy_wait_started_s > self.policy_wait_timeout_s:
                return self._fail(RuntimeError("policy wait timeout"), epoch)
            try:
                if not self.gateway.policy_ready(episode_id, epoch):
                    return ExecutorStep.POLICY_WAIT
            except BaseException as error:
                return self._fail(error, epoch)
            self._policy_wait_started_s = None
            self._next_command_s = now
            return ExecutorStep.POLICY_READY

        if self.gateway.pending_command_count == 0:
            if self._next_command_s is not None and now < self._next_command_s:
                return ExecutorStep.IDLE
            try:
                self.gateway.prepare_policy(episode_id, epoch)
            except BaseException as error:
                return self._fail(error, epoch)
            self._policy_wait_started_s = now
            return ExecutorStep.POLICY_WAIT

        assert self._next_command_s is not None
        if now < self._next_command_s:
            return ExecutorStep.IDLE
        if now - self._next_command_s > self.command_watchdog_s:
            return self._fail(
                RuntimeError("command publish watchdog expired"),
                epoch,
            )
        try:
            self.gateway.publish_next(epoch, self.sink)
        except BaseException as error:
            return self._fail(error, epoch)
        self._next_command_s += self.period_s
        return ExecutorStep.PUBLISHED

    def _run(self) -> None:
        poll_s = min(self.period_s / 4.0, 0.01)
        while not self._stop.wait(poll_s):
            self.step()

    def _fail(
        self,
        error: BaseException,
        expected_epoch: int,
    ) -> ExecutorStep:
        if (
            self.gateway.control_epoch != expected_epoch
            or not self.gateway.gate_open
        ):
            self._reset_schedule()
            return ExecutorStep.IDLE
        try:
            self.gateway.close_gate(expected_epoch)
        except BaseException as close_error:
            error = RuntimeError(f"{error}; gate close failed: {close_error}")
        self.gateway.latch_fault(error)
        self._reset_schedule()
        return ExecutorStep.FAULT

    def _reset_schedule(self) -> None:
        self._active_epoch = None
        self._next_command_s = None
        self._policy_wait_started_s = None
