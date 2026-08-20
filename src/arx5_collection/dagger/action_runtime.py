from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from .action_gateway import (
    AsyncPolicy,
    DualArmCommandSink,
    FixedRateCommandExecutor,
    JointStateSource,
    Pi05JointActionContract,
    PolicyActionGateway,
    PolicyModeController,
)
from .config import DaggerCollectorSettings
from .rtc_scheduler import JsonlRtcLog, RtcActionScheduler
from .takeover import CommandGateway


class ActionExecutor(Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...


class TakeoverControlPort(
    JointStateSource,
    DualArmCommandSink,
    PolicyModeController,
    Protocol,
):
    pass


@dataclass(frozen=True, slots=True)
class TakeoverActionRuntime:
    gateway: CommandGateway
    executor: ActionExecutor
    description: str


@contextmanager
def open_takeover_action_runtime(
    settings: DaggerCollectorSettings,
    policy: AsyncPolicy,
    control: TakeoverControlPort,
    log_dir: Path,
) -> Iterator[TakeoverActionRuntime]:
    if not (
        hasattr(control, "read")
        and hasattr(control, "publish")
        and hasattr(control, "enable_policy_control")
    ):
        raise TypeError("Take-over control port does not implement the required boundary")
    contract = Pi05JointActionContract(
        settings.checkpoint_sha256,
        settings.grippers,
        settings.control.safety,
    )
    if settings.checkpoint_profile.policy_type == "training_time_rtc":
        if settings.rtc_rollout is None:
            raise RuntimeError("RTC checkpoint has no rollout profile")
        with JsonlRtcLog(log_dir / "dagger-rtc.jsonl") as rtc_log:
            scheduler = RtcActionScheduler(
                policy,
                control,
                control,
                contract,
                control,
                settings.checkpoint_profile,
                settings.rtc_rollout,
                settings.control.policy_wait_timeout_s,
                settings.control.command_watchdog_s,
                diagnostic_sink=rtc_log,
            )
            yield TakeoverActionRuntime(
                scheduler,
                scheduler,
                "training_time_rtc",
            )
        return

    gateway = PolicyActionGateway(policy, control, contract, control)
    executor = FixedRateCommandExecutor(
        gateway,
        control,
        settings.execution.control_rate_hz,
        settings.control.policy_wait_timeout_s,
        settings.control.command_watchdog_s,
    )
    yield TakeoverActionRuntime(gateway, executor, "sequential")
