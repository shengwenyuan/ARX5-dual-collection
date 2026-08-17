from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from ..ports import RecordTrigger, TriggerEvent


class CompositeTrigger:
    """Merge already-open trigger sources while preserving abort priority."""

    def __init__(
        self,
        triggers: Sequence[RecordTrigger],
        poll_interval_s: float = 0.01,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not triggers:
            raise ValueError("composite trigger requires at least one source")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.triggers = tuple(triggers)
        self.poll_interval_s = poll_interval_s
        self.monotonic_clock = monotonic_clock
        self.sleep = sleep

    def wait(self, timeout_s: float) -> TriggerEvent | None:
        if timeout_s < 0:
            raise ValueError("timeout_s must not be negative")
        deadline = self.monotonic_clock() + timeout_s
        while True:
            candidates = {
                event
                for trigger in self.triggers
                if (event := trigger.wait(0.0)) is not None
            }
            if TriggerEvent.ABORT in candidates:
                return TriggerEvent.ABORT
            if TriggerEvent.ACTIVATE in candidates:
                return TriggerEvent.ACTIVATE

            remaining = deadline - self.monotonic_clock()
            if remaining <= 0:
                return None
            self.sleep(min(self.poll_interval_s, remaining))
