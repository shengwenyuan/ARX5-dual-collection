from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum
from time import monotonic_ns
from typing import Protocol

from arx5_collection.collection_metadata import (
    ControlOwner,
    ControlSegment,
    DaggerMetadata,
    MetadataContext,
)
from arx5_collection.episode.models import RecordingStarted, RecordingStopping
from arx5_collection.episode.ports import TriggerEvent, TriggerSignal

from .models import DaggerTriggerEvent
from .ports import DaggerTrigger


class TakeoverState(str, Enum):
    IDLE = "idle"
    MODEL_CONTROL = "model_control"
    HANDOVER_PENDING = "handover_pending"
    HUMAN_ACTIVE = "human_active"
    RESUME_PENDING = "resume_pending"
    FAULT_HOLD = "fault_hold"


class AuthorityEventType(IntEnum):
    TAKEOVER_REQUESTED = 1
    HUMAN_ACTIVE = 2
    RESUME_REQUESTED = 3
    POLICY_ACTIVE = 4
    FAULT_HOLD = 5


@dataclass(frozen=True, slots=True)
class AuthorityEvent:
    sequence: int
    monotonic_time_ns: int
    intervention_id: int
    control_epoch: int
    event_type: AuthorityEventType
    reason: str


class AuthorityEventSink(Protocol):
    def __call__(self, event: AuthorityEvent) -> None: ...


class CommandGateway(Protocol):
    action_output_enabled: bool

    def close_gate(self, control_epoch: int) -> None: ...
    def clear_pending(self, control_epoch: int) -> None: ...
    def prepare_policy(self, episode_id: str, control_epoch: int) -> None: ...
    def policy_ready(self, episode_id: str, control_epoch: int) -> bool: ...
    def take_fault(self) -> BaseException | None: ...


class HumanModeController(Protocol):
    def enable_gravity_compensation(self) -> None: ...


class NoActionGateway:
    """Exercise command-authority ordering without creating an action output."""

    action_output_enabled = False

    def close_gate(self, control_epoch: int) -> None:
        del control_epoch

    def clear_pending(self, control_epoch: int) -> None:
        del control_epoch

    def prepare_policy(self, episode_id: str, control_epoch: int) -> None:
        del episode_id, control_epoch

    def policy_ready(self, episode_id: str, control_epoch: int) -> bool:
        del episode_id, control_epoch
        return True

    def take_fault(self) -> BaseException | None:
        return None


class AuthorityTimeline:
    """Build sparse authority events and non-overlapping active-owner segments."""

    def __init__(
        self,
        checkpoint_sha256: str,
        event_sink: AuthorityEventSink,
        clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        self.checkpoint_sha256 = checkpoint_sha256
        self.event_sink = event_sink
        self.clock_ns = clock_ns
        self._sequence = 0
        self._episode_started_ns: int | None = None
        self._intervention_count = 0
        self._segments: list[ControlSegment] = []
        self._active: tuple[ControlOwner, float, int | None] | None = None

    def start_episode(self, control_epoch: int, monotonic_time_ns: int) -> None:
        if self._episode_started_ns is not None:
            raise RuntimeError("authority timeline is already active")
        self._episode_started_ns = monotonic_time_ns
        self._intervention_count = 0
        self._segments = []
        self._active = None

    def takeover_requested(
        self,
        control_epoch: int,
        reason: str,
        monotonic_time_ns: int,
    ) -> int:
        self._require_active()
        at_s = self._offset_s(monotonic_time_ns)
        self._close_active(at_s)
        self._intervention_count += 1
        self._emit(
            AuthorityEventType.TAKEOVER_REQUESTED,
            intervention_id=self._intervention_count,
            control_epoch=control_epoch,
            reason=reason,
            monotonic_time_ns=monotonic_time_ns,
        )
        return self._intervention_count

    def human_active(self, intervention_id: int, control_epoch: int) -> None:
        monotonic_time_ns = self.clock_ns()
        at_s = self._offset_s(monotonic_time_ns)
        self._active = (ControlOwner.HUMAN, at_s, intervention_id)
        self._emit(
            AuthorityEventType.HUMAN_ACTIVE,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason="gravity_compensation_confirmed",
            monotonic_time_ns=monotonic_time_ns,
        )

    def resume_requested(
        self,
        intervention_id: int,
        control_epoch: int,
        monotonic_time_ns: int,
    ) -> None:
        at_s = self._offset_s(monotonic_time_ns)
        self._close_active(at_s)
        self._emit(
            AuthorityEventType.RESUME_REQUESTED,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason="operator_requested_policy_resume",
            monotonic_time_ns=monotonic_time_ns,
        )

    def policy_active(
        self,
        intervention_id: int,
        control_epoch: int,
        reason: str,
    ) -> None:
        monotonic_time_ns = self.clock_ns()
        at_s = self._offset_s(monotonic_time_ns)
        self._active = (ControlOwner.MODEL, at_s, None)
        self._emit(
            AuthorityEventType.POLICY_ACTIVE,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason=reason,
            monotonic_time_ns=monotonic_time_ns,
        )

    def fault_hold(
        self,
        intervention_id: int,
        control_epoch: int,
        reason: str,
    ) -> int:
        monotonic_time_ns = self.clock_ns()
        self._close_active(self._offset_s(monotonic_time_ns))
        self._emit(
            AuthorityEventType.FAULT_HOLD,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason=reason,
            monotonic_time_ns=monotonic_time_ns,
        )
        return monotonic_time_ns

    def finish_episode(self, monotonic_time_ns: int) -> None:
        self._require_active()
        self._close_active(self._offset_s(monotonic_time_ns))
        self._episode_started_ns = None

    def metadata(self) -> MetadataContext:
        if self._episode_started_ns is not None:
            raise RuntimeError("authority metadata is not final")
        return MetadataContext.for_dagger(
            DaggerMetadata(
                checkpoint_sha256=self.checkpoint_sha256,
                intervention_count=self._intervention_count,
                control_segments=tuple(self._segments),
            )
        )

    def _emit(
        self,
        event_type: AuthorityEventType,
        intervention_id: int,
        control_epoch: int,
        reason: str,
        monotonic_time_ns: int,
    ) -> None:
        self._sequence += 1
        self.event_sink(
            AuthorityEvent(
                sequence=self._sequence,
                monotonic_time_ns=monotonic_time_ns,
                intervention_id=intervention_id,
                control_epoch=control_epoch,
                event_type=event_type,
                reason=reason,
            )
        )

    def _offset_s(self, monotonic_time_ns: int) -> float:
        self._require_active()
        assert self._episode_started_ns is not None
        if monotonic_time_ns < self._episode_started_ns:
            raise RuntimeError("authority event precedes Episode start")
        return (monotonic_time_ns - self._episode_started_ns) / 1e9

    def _close_active(self, ended_offset_s: float) -> None:
        if self._active is None:
            return
        owner, started_offset_s, intervention_id = self._active
        self._segments.append(
            ControlSegment(
                owner=owner,
                started_offset_s=started_offset_s,
                ended_offset_s=max(started_offset_s, ended_offset_s),
                intervention_id=intervention_id,
            )
        )
        self._active = None

    def _require_active(self) -> None:
        if self._episode_started_ns is None:
            raise RuntimeError("authority timeline is not active")


class TakeoverController:
    """Sequence authority transfer side effects independently of Episode control."""

    def __init__(
        self,
        gateway: CommandGateway,
        human_mode: HumanModeController,
        timeline: AuthorityTimeline,
        status_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.human_mode = human_mode
        self.timeline = timeline
        self.status_sink = status_sink or (lambda message: None)
        self.state = TakeoverState.IDLE
        self.control_epoch = 0
        self.episode_id: str | None = None
        self.intervention_id = 0
        self._safety_failure: RuntimeError | None = None
        self._fault_detail: str | None = None
        self._fault_time_ns: int | None = None

    def start_episode(self, started: RecordingStarted) -> None:
        if self.state is not TakeoverState.IDLE:
            raise RuntimeError("Take-over episode cannot start")
        self.episode_id = started.episode_id
        self.intervention_id = 0
        self._safety_failure = None
        self._fault_detail = None
        self._fault_time_ns = None
        self.timeline.start_episode(self.control_epoch, started.monotonic_time_ns)
        self.state = TakeoverState.RESUME_PENDING
        try:
            self.gateway.prepare_policy(started.episode_id, self.control_epoch)
            self.poll_policy()
        except Exception as error:
            self._fault(error)

    def poll_policy(self) -> TakeoverState:
        if self.state is not TakeoverState.RESUME_PENDING:
            return self.state
        assert self.episode_id is not None
        try:
            if not self.gateway.policy_ready(self.episode_id, self.control_epoch):
                return self.state
            reason = (
                "action_gateway_ready"
                if self.gateway.action_output_enabled
                else "no_action_gateway_ready"
            )
            self.timeline.policy_active(
                self.intervention_id,
                self.control_epoch,
                reason,
            )
            self.state = TakeoverState.MODEL_CONTROL
            mode = (
                "action lease ready"
                if self.gateway.action_output_enabled
                else "no action output"
            )
            self.status_sink(
                f"DAGGER POLICY_ACTIVE intervention={self.intervention_id} "
                f"epoch={self.control_epoch}; {mode}"
            )
        except Exception as error:
            self._fault(error)
        return self.state

    def poll_runtime(self) -> TakeoverState:
        if self._safety_failure is not None:
            raise self._safety_failure
        if self.state in (TakeoverState.IDLE, TakeoverState.FAULT_HOLD):
            return self.state
        error = self.gateway.take_fault()
        if error is not None:
            self._fault(error)
            return self.state
        return self.poll_policy()

    def toggle_ownership(self, monotonic_time_ns: int) -> TakeoverState:
        if self.state is TakeoverState.MODEL_CONTROL:
            self._takeover(monotonic_time_ns)
        elif self.state is TakeoverState.HUMAN_ACTIVE:
            self._resume(monotonic_time_ns)
        else:
            self.status_sink(
                f"DAgger ownership toggle ignored in state={self.state.value}"
            )
        return self.state

    def episode_fault_signal(self) -> TriggerSignal | None:
        if self.state is not TakeoverState.FAULT_HOLD:
            return None
        if self._fault_detail is None or self._fault_time_ns is None:
            raise RuntimeError("FAULT_HOLD is missing its failure record")
        return TriggerSignal(
            TriggerEvent.FAIL,
            self._fault_time_ns,
            self._fault_detail,
        )

    def stop_episode(self, stopping: RecordingStopping) -> None:
        if self.state is TakeoverState.IDLE:
            return
        if self.state is not TakeoverState.FAULT_HOLD:
            try:
                self.gateway.close_gate(self.control_epoch)
                self.control_epoch += 1
                self.gateway.clear_pending(self.control_epoch)
                self.human_mode.enable_gravity_compensation()
            except Exception as error:
                self._fault(error)
        self.timeline.finish_episode(stopping.monotonic_time_ns)
        self.state = TakeoverState.IDLE
        self.episode_id = None
        self.intervention_id = 0
        self._fault_detail = None
        self._fault_time_ns = None
        if self._safety_failure is not None:
            failure, self._safety_failure = self._safety_failure, None
            raise failure

    def metadata_context(self) -> MetadataContext:
        return self.timeline.metadata()

    def _takeover(self, monotonic_time_ns: int) -> None:
        self.state = TakeoverState.HANDOVER_PENDING
        self.intervention_id = self.timeline.takeover_requested(
            self.control_epoch,
            "operator_ownership_toggle",
            monotonic_time_ns,
        )
        try:
            self.gateway.close_gate(self.control_epoch)
            self.control_epoch += 1
            self.gateway.clear_pending(self.control_epoch)
            self.human_mode.enable_gravity_compensation()
            self.timeline.human_active(
                self.intervention_id,
                self.control_epoch,
            )
            self.state = TakeoverState.HUMAN_ACTIVE
            self.status_sink(
                f"DAGGER HUMAN_ACTIVE intervention={self.intervention_id} "
                f"epoch={self.control_epoch}"
            )
        except Exception as error:
            self._fault(error)

    def _resume(self, monotonic_time_ns: int) -> None:
        assert self.episode_id is not None
        self.state = TakeoverState.RESUME_PENDING
        self.timeline.resume_requested(
            self.intervention_id,
            self.control_epoch,
            monotonic_time_ns,
        )
        try:
            self.gateway.prepare_policy(self.episode_id, self.control_epoch)
            self.poll_policy()
        except Exception as error:
            self._fault(error)

    def _fault(self, error: BaseException) -> None:
        if self.state is TakeoverState.FAULT_HOLD:
            return
        failures = [f"{type(error).__name__}: {error}"]
        try:
            self.gateway.close_gate(self.control_epoch)
        except BaseException as cleanup_error:
            failures.append(f"close_gate={cleanup_error}")
        self.control_epoch += 1
        try:
            self.gateway.clear_pending(self.control_epoch)
        except BaseException as cleanup_error:
            failures.append(f"clear_pending={cleanup_error}")
        try:
            self.human_mode.enable_gravity_compensation()
        except BaseException as cleanup_error:
            failures.append(f"gravity_compensation={cleanup_error}")
            self._safety_failure = RuntimeError(
                "dual-arm G_COMPENSATION recovery failed: "
                f"{cleanup_error}"
            )
        reason = "; ".join(failures)
        fault_time_ns = self.timeline.fault_hold(
            self.intervention_id,
            self.control_epoch,
            reason,
        )
        self._fault_detail = reason
        self._fault_time_ns = fault_time_ns
        self.state = TakeoverState.FAULT_HOLD
        self.status_sink(f"DAGGER FAULT_HOLD: {reason}")


class TakeoverRecordTrigger:
    def __init__(
        self,
        trigger: DaggerTrigger,
        controller: TakeoverController,
        status_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.trigger = trigger
        self.controller = controller
        self.status_sink = status_sink or (lambda message: None)

    def arm(self) -> None:
        self.trigger.arm()

    def disarm(self) -> None:
        self.trigger.disarm()

    def wait(self, timeout_s: float) -> TriggerSignal | None:
        signal = self.trigger.wait(timeout_s)
        self.controller.poll_runtime()
        fault = self.controller.episode_fault_signal()
        if fault is not None:
            return fault
        if signal is None:
            return None
        if signal.event is DaggerTriggerEvent.RECORD_TOGGLE:
            return TriggerSignal(TriggerEvent.ACTIVATE, signal.monotonic_time_ns)
        if signal.event is DaggerTriggerEvent.ABORT:
            return TriggerSignal(TriggerEvent.ABORT, signal.monotonic_time_ns)
        if signal.event is DaggerTriggerEvent.OWNERSHIP_TOGGLE:
            if self.controller.state is TakeoverState.IDLE:
                self.status_sink("DAgger ownership toggle ignored before Episode start")
            else:
                self.controller.toggle_ownership(signal.monotonic_time_ns)
                fault = self.controller.episode_fault_signal()
                if fault is not None:
                    return fault
        return None
