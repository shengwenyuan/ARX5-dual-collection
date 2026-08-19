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
from arx5_collection.episode.models import EpisodeOutcome
from arx5_collection.episode.ports import TriggerEvent

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

    def start_episode(self, control_epoch: int) -> None:
        if self._episode_started_ns is not None:
            raise RuntimeError("authority timeline is already active")
        self._episode_started_ns = self.clock_ns()
        self._intervention_count = 0
        self._segments = []
        self._active = None

    def takeover_requested(self, control_epoch: int, reason: str) -> int:
        self._require_active()
        at_s = self._offset_s()
        self._close_active(at_s)
        self._intervention_count += 1
        self._emit(
            AuthorityEventType.TAKEOVER_REQUESTED,
            intervention_id=self._intervention_count,
            control_epoch=control_epoch,
            reason=reason,
        )
        return self._intervention_count

    def human_active(self, intervention_id: int, control_epoch: int) -> None:
        at_s = self._offset_s()
        self._active = (ControlOwner.HUMAN, at_s, intervention_id)
        self._emit(
            AuthorityEventType.HUMAN_ACTIVE,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason="gravity_compensation_confirmed",
        )

    def resume_requested(self, intervention_id: int, control_epoch: int) -> None:
        at_s = self._offset_s()
        self._close_active(at_s)
        self._emit(
            AuthorityEventType.RESUME_REQUESTED,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason="operator_requested_policy_resume",
        )

    def policy_active(
        self,
        intervention_id: int,
        control_epoch: int,
        reason: str,
    ) -> None:
        at_s = self._offset_s()
        self._active = (ControlOwner.MODEL, at_s, None)
        self._emit(
            AuthorityEventType.POLICY_ACTIVE,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason=reason,
        )

    def fault_hold(
        self,
        intervention_id: int,
        control_epoch: int,
        reason: str,
    ) -> None:
        self._close_active(self._offset_s())
        self._emit(
            AuthorityEventType.FAULT_HOLD,
            intervention_id=intervention_id,
            control_epoch=control_epoch,
            reason=reason,
        )

    def finish_episode(self) -> None:
        self._require_active()
        self._close_active(self._offset_s())
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
    ) -> None:
        self._sequence += 1
        self.event_sink(
            AuthorityEvent(
                sequence=self._sequence,
                monotonic_time_ns=self.clock_ns(),
                intervention_id=intervention_id,
                control_epoch=control_epoch,
                event_type=event_type,
                reason=reason,
            )
        )

    def _offset_s(self) -> float:
        self._require_active()
        assert self._episode_started_ns is not None
        return max(0.0, (self.clock_ns() - self._episode_started_ns) / 1e9)

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

    def start_episode(self, episode_id: str) -> None:
        if self.state is not TakeoverState.IDLE or not episode_id:
            raise RuntimeError("Take-over episode cannot start")
        self.episode_id = episode_id
        self.intervention_id = 0
        self.timeline.start_episode(self.control_epoch)
        self.state = TakeoverState.RESUME_PENDING
        try:
            self.gateway.prepare_policy(episode_id, self.control_epoch)
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
        if self.state in (TakeoverState.IDLE, TakeoverState.FAULT_HOLD):
            return self.state
        error = self.gateway.take_fault()
        if error is not None:
            self._fault(error)
            return self.state
        return self.poll_policy()

    def toggle_ownership(self) -> TakeoverState:
        if self.state is TakeoverState.MODEL_CONTROL:
            self._takeover()
        elif self.state is TakeoverState.HUMAN_ACTIVE:
            self._resume()
        else:
            self.status_sink(
                f"DAgger ownership toggle ignored in state={self.state.value}"
            )
        return self.state

    def stop_episode(self, outcome: EpisodeOutcome) -> None:
        del outcome
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
        self.timeline.finish_episode()
        self.state = TakeoverState.IDLE
        self.episode_id = None
        self.intervention_id = 0

    def metadata_context(self) -> MetadataContext:
        return self.timeline.metadata()

    def _takeover(self) -> None:
        self.state = TakeoverState.HANDOVER_PENDING
        self.intervention_id = self.timeline.takeover_requested(
            self.control_epoch,
            "operator_ownership_toggle",
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

    def _resume(self) -> None:
        assert self.episode_id is not None
        self.state = TakeoverState.RESUME_PENDING
        self.timeline.resume_requested(
            self.intervention_id,
            self.control_epoch,
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
        reason = "; ".join(failures)
        self.timeline.fault_hold(
            self.intervention_id,
            self.control_epoch,
            reason,
        )
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

    def wait(self, timeout_s: float) -> TriggerEvent | None:
        event = self.trigger.wait(timeout_s)
        if event is DaggerTriggerEvent.RECORD_TOGGLE:
            return TriggerEvent.ACTIVATE
        if event is DaggerTriggerEvent.ABORT:
            return TriggerEvent.ABORT
        self.controller.poll_runtime()
        if event is DaggerTriggerEvent.OWNERSHIP_TOGGLE:
            if self.controller.state is TakeoverState.IDLE:
                self.status_sink("DAgger ownership toggle ignored before Episode start")
            else:
                self.controller.toggle_ownership()
        return None
