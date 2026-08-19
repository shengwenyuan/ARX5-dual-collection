from __future__ import annotations

from concurrent.futures import Future
from typing import Protocol

from .models import DaggerTriggerEvent, InferenceTicket


class AsyncPolicyClient(Protocol):
    def begin_epoch(self, control_epoch: int) -> None: ...

    def submit(
        self,
        episode_id: str,
        control_epoch: int,
        inference_id: str | None = None,
    ) -> Future[InferenceTicket]: ...


class DaggerTrigger(Protocol):
    def wait(self, timeout_s: float) -> DaggerTriggerEvent | None: ...
