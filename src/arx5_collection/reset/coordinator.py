from __future__ import annotations

from collections.abc import Callable
from time import sleep

from .models import ResetState
from .ports import DualArmResetController


class ResetCoordinator:
    def __init__(
        self,
        controller: DualArmResetController,
        delay_s: float = 5.0,
        sleep_fn: Callable[[float], None] = sleep,
        state_sink: Callable[[ResetState], None] | None = None,
    ) -> None:
        if delay_s < 0:
            raise ValueError("reset delay must not be negative")
        self.controller = controller
        self.delay_s = delay_s
        self.sleep_fn = sleep_fn
        self.state_sink = state_sink or (lambda state: None)

    def run(self) -> None:
        self.state_sink(ResetState.WAITING)
        self.sleep_fn(self.delay_s)
        self.state_sink(ResetState.RESETTING)
        self.controller.reset_both()
        self.state_sink(ResetState.COMPLETE)
