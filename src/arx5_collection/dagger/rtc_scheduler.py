from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time_ns
from typing import Any, Protocol
from uuid import uuid4

from .action_gateway import (
    AsyncPolicy,
    DualArmCommandSink,
    DualArmJointCommand,
    GripperSaturation,
    JointStateSource,
    Pi05JointActionContract,
    PolicyModeController,
)
from .models import InferenceTicket, Pi05CheckpointProfile, RtcRolloutProfile
from .policy_client import RtcPolicyContext


RtcDiagnosticSink = Callable[[Mapping[str, Any]], None]


class JsonlRtcLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream = None
        self._lock = Lock()

    def __enter__(self) -> JsonlRtcLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8", buffering=1)
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __call__(self, event: Mapping[str, Any]) -> None:
        if self._stream is None:
            raise RuntimeError("RTC JSONL log is not open")
        row = {"logged_at_ns": time_ns(), **event}
        with self._lock:
            self._stream.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            )

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None


@dataclass(frozen=True, slots=True)
class QueuedRtcAction:
    raw: tuple[float, ...]
    command: DualArmJointCommand


@dataclass(frozen=True, slots=True)
class PendingRtcInference:
    inference_id: str
    future: Future[InferenceTicket]
    issued_at_submit: int
    submitted_at_s: float
    estimated_delay_steps: int | None
    action_prefix: tuple[tuple[float, ...], ...]
    bootstrap: bool


class RollingMaxDelayEstimator:
    def __init__(self, initial: int, capacity: int, maximum_exclusive: int) -> None:
        if not 0 <= initial < maximum_exclusive:
            raise ValueError("initial delay is outside the trained range")
        if capacity <= 0:
            raise ValueError("delay history capacity must be positive")
        self._initial = initial
        self._maximum_exclusive = maximum_exclusive
        self._history: deque[int] = deque(maxlen=capacity)

    @property
    def estimate(self) -> int:
        return max(self._history, default=self._initial)

    @property
    def history(self) -> tuple[int, ...]:
        return tuple(self._history)

    def observe(self, delay_steps: int) -> None:
        if not 0 <= delay_steps < self._maximum_exclusive:
            raise ValueError("actual delay is outside the trained range")
        self._history.append(delay_steps)

    def reset(self) -> None:
        self._history.clear()


class RtcActionScheduler:
    """Own the epoch-scoped RTC queue, inference overlap and atomic splice."""

    action_output_enabled = True

    def __init__(
        self,
        policy: AsyncPolicy,
        state_source: JointStateSource,
        sink: DualArmCommandSink,
        contract: Pi05JointActionContract,
        policy_mode: PolicyModeController,
        checkpoint: Pi05CheckpointProfile,
        rollout: RtcRolloutProfile,
        policy_wait_timeout_s: float,
        command_watchdog_s: float,
        diagnostic_sink: RtcDiagnosticSink | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        rollout.validate_for(checkpoint)
        if min(policy_wait_timeout_s, command_watchdog_s) <= 0:
            raise ValueError("RTC scheduler timeouts must be positive")
        self.policy = policy
        self.state_source = state_source
        self.sink = sink
        self.contract = contract
        self.policy_mode = policy_mode
        self.checkpoint = checkpoint
        self.rollout = rollout
        self.policy_wait_timeout_s = policy_wait_timeout_s
        self.command_watchdog_s = command_watchdog_s
        self.diagnostic_sink = diagnostic_sink or (lambda event: None)
        self.clock = clock
        self.period_s = 1.0 / checkpoint.execution.control_rate_hz
        self.safe_window_steps = rollout.safe_window_steps(checkpoint)
        self.delay = RollingMaxDelayEstimator(
            rollout.initial_delay_steps,
            rollout.delay_history_size,
            checkpoint.max_delay_steps,
        )
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._control_epoch = 0
        self._episode_id: str | None = None
        self._gate_open = False
        self._pending: PendingRtcInference | None = None
        self._queue: deque[QueuedRtcAction] = deque()
        self._issued_total = 0
        self._issued_since_splice = 0
        self._next_command_s: float | None = None
        self._fault: BaseException | None = None

    @property
    def gate_open(self) -> bool:
        with self._lock:
            return self._gate_open

    @property
    def control_epoch(self) -> int:
        with self._lock:
            return self._control_epoch

    @property
    def pending_command_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def has_pending_policy(self) -> bool:
        with self._lock:
            return self._pending is not None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("RTC scheduler is already started")
        self._stop.clear()
        self._thread = Thread(target=self._run, name="dagger-rtc-scheduler", daemon=False)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.policy_wait_timeout_s + self.period_s)
            if self._thread.is_alive():
                raise RuntimeError("RTC scheduler did not stop")
        self._thread = None

    def close_gate(self, control_epoch: int) -> None:
        with self._lock:
            if control_epoch != self._control_epoch:
                raise RuntimeError("cannot close a stale RTC command lease")
            self._gate_open = False

    def clear_pending(self, control_epoch: int) -> None:
        with self._lock:
            if control_epoch < self._control_epoch:
                raise RuntimeError("control epoch must not move backwards")
            self._control_epoch = control_epoch
            self._gate_open = False
            self._pending = None
            self._queue.clear()
            self._episode_id = None
            self._issued_total = 0
            self._issued_since_splice = 0
            self._next_command_s = None
            self.delay.reset()
        self.policy.begin_epoch(control_epoch)
        self._emit("epoch_reset", control_epoch=control_epoch)

    def prepare_policy(self, episode_id: str, control_epoch: int) -> None:
        with self._lock:
            if control_epoch != self._control_epoch:
                raise RuntimeError("cannot bootstrap a stale RTC epoch")
            if self._pending is not None or self._queue or self._gate_open:
                raise RuntimeError("RTC policy preparation is already active")
            self._episode_id = episode_id
            self._submit_locked(bootstrap=True, context=None)

    def policy_ready(self, episode_id: str, control_epoch: int) -> bool:
        with self._lock:
            self._require_identity_locked(episode_id, control_epoch)
            pending = self._pending
            if pending is None:
                raise RuntimeError("RTC bootstrap has not started")
            if not pending.future.done():
                if self.clock() - pending.submitted_at_s > self.policy_wait_timeout_s:
                    raise RuntimeError("RTC bootstrap inference timeout")
                return False
        self._accept_pending(bootstrap_required=True)
        return True

    def take_fault(self) -> BaseException | None:
        with self._lock:
            error, self._fault = self._fault, None
            return error

    def step(self) -> None:
        try:
            self._step()
        except BaseException as error:
            self._fail(error)

    def _step(self) -> None:
        now = self.clock()
        with self._lock:
            if not self._gate_open:
                self._next_command_s = None
                return
            pending_done = self._pending is not None and self._pending.future.done()
        if pending_done:
            self._accept_pending(bootstrap_required=False)

        with self._lock:
            if not self._gate_open:
                return
            if self._next_command_s is None:
                self._next_command_s = now
            if now < self._next_command_s:
                return
            if now - self._next_command_s > self.command_watchdog_s:
                raise RuntimeError("RTC command publish watchdog expired")
            if not self._queue:
                raise RuntimeError("RTC validated action queue underrun")
            queued = self._queue.popleft()
            self.sink.publish(queued.command)
            self._issued_total += 1
            self._issued_since_splice += 1
            self._next_command_s += self.period_s
            self._emit_locked(
                "command_issued",
                issued_total=self._issued_total,
                queue_remaining=len(self._queue),
            )
            if (
                self._issued_since_splice == self.rollout.prefetch_after_steps
                and self._pending is None
            ):
                estimate = self.delay.estimate
                if len(self._queue) < estimate:
                    raise RuntimeError("RTC queue cannot supply the configured action prefix")
                prefix = tuple(item.raw for item in tuple(self._queue)[:estimate])
                self._submit_locked(
                    bootstrap=False,
                    context=RtcPolicyContext(estimate, prefix),
                )

    def _submit_locked(
        self,
        bootstrap: bool,
        context: RtcPolicyContext | None,
    ) -> None:
        assert self._episode_id is not None
        inference_id = uuid4().hex
        future = self.policy.submit(
            self._episode_id,
            self._control_epoch,
            inference_id,
            rtc=context,
        )
        self._pending = PendingRtcInference(
            inference_id=inference_id,
            future=future,
            issued_at_submit=self._issued_total,
            submitted_at_s=self.clock(),
            estimated_delay_steps=(
                None if context is None else context.estimated_delay_steps
            ),
            action_prefix=() if context is None else context.action_prefix,
            bootstrap=bootstrap,
        )
        self._emit_locked(
            "inference_submitted",
            inference_id=inference_id,
            bootstrap=bootstrap,
            issued_at_submit=self._issued_total,
            queue_remaining=len(self._queue),
            estimated_delay_steps=(
                None if context is None else context.estimated_delay_steps
            ),
        )

    def _accept_pending(self, bootstrap_required: bool) -> None:
        with self._lock:
            pending = self._pending
            if pending is None or not pending.future.done():
                raise RuntimeError("RTC response is not ready")
            if pending.bootstrap != bootstrap_required:
                raise RuntimeError("RTC response phase mismatch")
            epoch = self._control_epoch
            episode_id = self._episode_id
            issued_total = self._issued_total
        ticket = pending.future.result()
        self.contract.validate_ticket_identity(ticket, epoch)
        if pending.action_prefix:
            returned_prefix = ticket.action_chunk[: len(pending.action_prefix)]
            prefix_error = max(
                abs(actual - expected)
                for actual_row, expected_row in zip(
                    returned_prefix, pending.action_prefix
                )
                for actual, expected in zip(actual_row, expected_row)
            )
            if prefix_error > self.checkpoint.hard_prefix_tolerance:
                raise RuntimeError(
                    f"RTC hard prefix changed by {prefix_error:.8f}; limit="
                    f"{self.checkpoint.hard_prefix_tolerance:.8f}"
                )
        actual_delay = issued_total - pending.issued_at_submit
        start = 0 if pending.bootstrap else actual_delay
        if pending.bootstrap:
            if actual_delay != 0:
                raise RuntimeError("RTC bootstrap issued actions before policy readiness")
        elif not 0 <= actual_delay < self.checkpoint.max_delay_steps:
            raise RuntimeError(
                f"RTC actual delay {actual_delay} is outside trained range "
                f"[0, {self.checkpoint.max_delay_steps})"
            )
        stop = start + self.safe_window_steps
        actions = ticket.action_chunk[start:stop]
        if len(actions) != self.safe_window_steps:
            raise RuntimeError("RTC response cannot provide the required safe window")
        saturations: list[GripperSaturation] = []
        executed_actions: list[tuple[float, ...]] = []
        commands = self.contract.validate_actions(
            actions,
            self.state_source.read(),
            saturation_sink=saturations.append,
            executed_action_sink=executed_actions.append,
        )
        replacement = deque(
            QueuedRtcAction(raw, command)
            for raw, command in zip(executed_actions, commands)
        )
        with self._lock:
            if (
                epoch != self._control_epoch
                or episode_id != self._episode_id
                or pending is not self._pending
            ):
                raise RuntimeError("RTC response became stale before splice")
            if pending.bootstrap:
                self.policy_mode.enable_policy_control()
                self._gate_open = True
                self._next_command_s = self.clock()
            else:
                self.delay.observe(actual_delay)
            self._queue = replacement
            self._issued_since_splice = 0
            self._pending = None
            self._emit_locked(
                "inference_accepted",
                inference_id=pending.inference_id,
                bootstrap=pending.bootstrap,
                estimated_delay_steps=pending.estimated_delay_steps,
                actual_delay_steps=actual_delay,
                splice_start=start,
                queue_size=len(self._queue),
                round_trip_ms=(self.clock() - pending.submitted_at_s) * 1000.0,
            )
            if saturations:
                self._emit_locked(
                    "gripper_saturated",
                    count=len(saturations),
                    sides=sorted({item.side for item in saturations}),
                    min_input_value=min(item.input_value for item in saturations),
                    max_input_value=max(item.input_value for item in saturations),
                )

    def _fail(self, error: BaseException) -> None:
        with self._lock:
            if not self._gate_open and self._pending is None:
                return
            self._gate_open = False
            self._queue.clear()
            self._pending = None
            if self._fault is None:
                self._fault = error
            self._emit_locked("fault", reason=str(error))

    def _require_identity_locked(self, episode_id: str, control_epoch: int) -> None:
        if control_epoch != self._control_epoch or episode_id != self._episode_id:
            raise RuntimeError("RTC policy identity is stale")

    def _run(self) -> None:
        poll_s = min(self.period_s / 4.0, self.command_watchdog_s / 4.0)
        while not self._stop.wait(poll_s):
            self.step()

    def _emit(self, event: str, **values: Any) -> None:
        self.diagnostic_sink({"event": event, **values})

    def _emit_locked(self, event: str, **values: Any) -> None:
        self.diagnostic_sink(
            {
                "event": event,
                "control_epoch": self._control_epoch,
                "episode_id": self._episode_id,
                **values,
            }
        )
