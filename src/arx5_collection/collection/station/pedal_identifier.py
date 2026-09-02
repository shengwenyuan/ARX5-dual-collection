from __future__ import annotations

import os
import select
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType

from arx5_collection.collection.episode.adapters.pedal import (
    PRESS_REPORT,
    HidrawPedalIdentity,
    PedalUnavailable,
    discover_hidraw_pedals,
)
from arx5_collection.collection.runtime.config import PedalConfig, TriggerConfig
from arx5_collection.collection.environment import ENVIRONMENT


class PedalIdentificationError(RuntimeError):
    pass


SelectFunction = Callable[
    [Sequence[int], Sequence[int], Sequence[int], float],
    tuple[Sequence[int], Sequence[int], Sequence[int]],
]


@dataclass(slots=True)
class _OpenedPedal:
    identity: HidrawPedalIdentity
    file_descriptor: int


class PedalIdentifier:
    """Bind two physical pedals by the order of their validated raw reports."""

    def __init__(
        self,
        candidates: Sequence[HidrawPedalIdentity] | None = None,
        open_function: Callable[[str | os.PathLike[str], int], int] = os.open,
        close_function: Callable[[int], None] = os.close,
        read_function: Callable[[int, int], bytes] = os.read,
        select_function: SelectFunction = select.select,
    ) -> None:
        self.candidates = tuple(candidates) if candidates is not None else None
        self.open_function = open_function
        self.close_function = close_function
        self.read_function = read_function
        self.select_function = select_function
        self._opened: list[_OpenedPedal] = []

    def __enter__(self) -> PedalIdentifier:
        candidates = (
            self.candidates if self.candidates is not None else discover_hidraw_pedals()
        )
        unique = {
            (candidate.vendor_id, candidate.product_id, candidate.serial_number)
            for candidate in candidates
        }
        if len(unique) < 2:
            raise PedalUnavailable("station configure requires two distinct pedals")
        try:
            for candidate in candidates:
                self._opened.append(
                    _OpenedPedal(
                        candidate,
                        self.open_function(candidate.path, os.O_RDONLY | os.O_NONBLOCK),
                    )
                )
        except OSError as error:
            self.close()
            raise PedalUnavailable(f"cannot open pedal candidates: {error}") from error
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        for pedal in self._opened:
            try:
                self.close_function(pedal.file_descriptor)
            except OSError:
                pass
        self._opened.clear()

    def wait_for_press(
        self,
        timeout_s: float,
        excluded_serials: frozenset[str] = frozenset(),
    ) -> HidrawPedalIdentity:
        if not self._opened:
            raise RuntimeError("pedal identifier must be used as a context manager")
        readable, _, _ = self.select_function(
            tuple(pedal.file_descriptor for pedal in self._opened), (), (), timeout_s
        )
        if not readable:
            raise PedalIdentificationError("timed out waiting for pedal press")
        matches = []
        by_fd = {pedal.file_descriptor: pedal.identity for pedal in self._opened}
        try:
            for file_descriptor in readable:
                report = self.read_function(file_descriptor, len(PRESS_REPORT))
                identity = by_fd[file_descriptor]
                if (
                    report == PRESS_REPORT
                    and identity.serial_number not in excluded_serials
                ):
                    matches.append(identity)
        except OSError as error:
            raise PedalIdentificationError(
                f"cannot read pedal report: {error}"
            ) from error
        identities = {
            (match.vendor_id, match.product_id, match.serial_number): match
            for match in matches
        }
        if len(identities) != 1:
            raise PedalIdentificationError(
                "expected exactly one distinct validated pedal press"
            )
        return next(iter(identities.values()))

    def identify(
        self,
        timeout_s: float = ENVIRONMENT.station.pedal_timeout_s,
        prompt: Callable[[str], None] | None = None,
    ) -> TriggerConfig:
        prompt = prompt or (lambda role: None)
        prompt("activate")
        activate = self.wait_for_press(timeout_s)
        prompt("abort")
        abort = self.wait_for_press(timeout_s, frozenset({activate.serial_number}))
        return TriggerConfig(
            activate=_config("activate", activate),
            abort=_config("abort", abort),
        )


def _config(role: str, identity: HidrawPedalIdentity) -> PedalConfig:
    return PedalConfig(
        role=role,
        vendor_id=identity.vendor_id,
        product_id=identity.product_id,
        serial_number=identity.serial_number,
    )
