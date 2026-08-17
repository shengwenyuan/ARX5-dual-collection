from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from arx5_collection.episode.ports import StreamMonitor
from arx5_collection.reset.ports import DualArmResetController


@runtime_checkable
class SessionStreamMonitor(StreamMonitor, Protocol):
    def open(self) -> None: ...

    def wait_until_ready(
        self,
        stream_ids: tuple[str, ...],
        timeout_s: float,
        process_check: Callable[[], None],
    ) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class SessionArmController(DualArmResetController, Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...
