from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def termination_as_interrupt() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame) -> None:
        del signum, frame
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
