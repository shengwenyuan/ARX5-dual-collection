from __future__ import annotations

from collections.abc import Callable

from .models import ResetState
from .ports import DualArmResetController


class ResetCoordinator:
    def __init__(
        self,
        controller: DualArmResetController,
        state_sink: Callable[[ResetState], None] | None = None,
    ) -> None:
        self.controller = controller
        self.state_sink = state_sink or (lambda state: None)

    def run(self) -> None:
        self.state_sink(ResetState.RESETTING)
        self.controller.reset_both()
        self.state_sink(ResetState.COMPLETE)
