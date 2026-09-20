from __future__ import annotations

import math
from collections.abc import Callable
from time import monotonic_ns, sleep

from arx5_collection.collection_metadata import CollectionType, MetadataContext
from arx5_collection.dagger.takeover import CommandGateway, HumanModeController
from arx5_collection.episode.models import EpisodeOutcome, RecordingStarted, RecordingStopping
from arx5_collection.episode.ports import RecordTrigger, TriggerEvent, TriggerSignal

from .recording import RecordedControl


class InferController:
    """Episode hooks for a fixed RTC policy, with no human command ownership."""

    def __init__(
        self,
        gateway: CommandGateway,
        human_mode: HumanModeController,
        commands: RecordedControl,
        configuration: dict[str, object],
        post_stop_recording_s: float = 0.1,
        status_sink: Callable[[str], None] = lambda message: None,
        clock: Callable[[], int] = monotonic_ns,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if not math.isfinite(post_stop_recording_s) or post_stop_recording_s <= 0:
            raise ValueError("post-stop recording time must be finite and positive")
        self.gateway = gateway
        self.human_mode = human_mode
        self.commands = commands
        self.configuration = configuration
        self.post_stop_recording_s = post_stop_recording_s
        self.status_sink = status_sink
        self.clock = clock
        self.sleeper = sleeper
        self.control_epoch = 0
        self.active = False
        self.ready = False
        self.episode_id = ""
        self.facts: dict[str, object] = {}
        self._stop_error: BaseException | None = None

    def start_episode(self, started: RecordingStarted) -> None:
        if self.active:
            raise RuntimeError("infer episode is already active")
        self.episode_id = started.episode_id
        self.facts = {
            "schema_version": 1,
            "started_monotonic_ns": started.monotonic_time_ns,
            "label_monotonic_ns": None,
            "label_source": None,
            "termination_reason": None,
            "configuration": self.configuration,
            "post_stop_recording_s": self.post_stop_recording_s,
        }
        self._stop_error = None
        self.active, self.ready = True, False
        try:
            self.gateway.clear_pending(self.control_epoch)
            self.commands.start(started.episode_id)
            self.gateway.prepare_policy(self.episode_id, self.control_epoch)
        except BaseException:
            self.close()
            raise

    def poll(self) -> bool:
        fault = self.gateway.take_fault() or self.commands.failure
        if fault is not None:
            raise RuntimeError(f"infer policy fault: {fault}") from fault
        if not self.ready:
            self.ready = self.gateway.policy_ready(self.episode_id, self.control_epoch)
        return self.ready

    def label(self, signal: TriggerSignal) -> TriggerSignal:
        self.facts.update(
            termination_reason=(
                "label_conflict" if signal.event is TriggerEvent.CONFLICT else "human_label"
            ),
            label_source={
                TriggerEvent.ACTIVATE: "right_pedal",
                TriggerEvent.ABORT: "left_pedal",
                TriggerEvent.CONFLICT: "both_pedals",
            }[signal.event],
            label_monotonic_ns=signal.monotonic_time_ns,
        )
        event = {
            TriggerEvent.ACTIVATE: TriggerEvent.ACTIVATE,
            TriggerEvent.ABORT: TriggerEvent.TASK_FAIL,
            TriggerEvent.CONFLICT: TriggerEvent.ABORT,
        }[signal.event]
        # Close the gate before returning the result to EpisodeRuntime.
        self.close()
        fault = self.gateway.take_fault() or self.commands.failure
        if fault is not None:
            raise RuntimeError(f"infer policy fault at label: {fault}") from fault
        return TriggerSignal(event, signal.monotonic_time_ns)

    def close(self) -> None:
        if not self.active:
            return
        # Attempt gravity compensation even if invalidating pending work fails.
        try:
            try:
                self.gateway.close_gate(self.control_epoch)
            finally:
                try:
                    self.control_epoch += 1
                    self.gateway.clear_pending(self.control_epoch)
                finally:
                    self.human_mode.enable_gravity_compensation()
        except BaseException as error:
            self._stop_error = error
            raise
        finally:
            self.commands.finish()
            self.active, self.ready = False, False
            self.facts["actions_stopped_monotonic_ns"] = self.clock()

    def stop_episode(self, stopping: RecordingStopping) -> None:
        self.close()
        if self._stop_error is not None:
            raise self._stop_error
        if self.facts["termination_reason"] is None:
            self.facts["termination_reason"] = (
                "operator_abort" if stopping.outcome is EpisodeOutcome.ABORTED else "runtime_fault"
            )
        self.facts["ended_monotonic_ns"] = stopping.monotonic_time_ns
        self.facts["command_count"] = self.commands.command_count
        self.facts["last_command_monotonic_ns"] = self.commands.last_command_monotonic_ns
        # Preserve a post-action sensor opportunity. Exporter checks actual coverage.
        self.sleeper(self.post_stop_recording_s)
        self.facts["recording_stop_requested_monotonic_ns"] = self.clock()

    def metadata_context(self) -> MetadataContext:
        return MetadataContext(CollectionType.INFER, infer=dict(self.facts))


class InferRecordTrigger:
    """Map the existing right/left pedal roles to start/success and task failure."""

    def __init__(self, trigger: RecordTrigger, controller: InferController) -> None:
        self.trigger = trigger
        self.controller = controller
        self._running_armed = False

    def arm(self) -> None:
        self._running_armed = False
        self.trigger.arm()

    def disarm(self) -> None:
        self.trigger.disarm()

    def wait(self, timeout_s: float) -> TriggerSignal | None:
        if not self.controller.active:
            signal = self.trigger.wait(timeout_s)
            # Idle left/conflict cannot start HOME or an episode.
            return signal if signal and signal.event is TriggerEvent.ACTIVATE else None
        if not self.controller.poll():
            self.trigger.wait(timeout_s)  # Discard inputs during RTC bootstrap.
            return None
        if not self._running_armed:
            self.trigger.arm()  # Discard buffered HOME/bootstrap inputs once more.
            self._running_armed = True
            self.controller.status_sink("INFER RUNNING: right=success, left=fail; Ctrl+C=abort/exit")
        signal = self.trigger.wait(timeout_s)
        # Prefer a concurrent policy fault over a human success/fail label.
        self.controller.poll()
        return self.controller.label(signal) if signal is not None else None
