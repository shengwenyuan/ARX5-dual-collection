from __future__ import annotations

import select
import sys
import termios
import tty
from types import TracebackType
from typing import TextIO


class KeyboardTrigger:
    def __init__(self, stream: TextIO | None = None, key: str = " ") -> None:
        if len(key) != 1:
            raise ValueError("trigger key must be one character")
        self.stream = stream or sys.stdin
        self.key = key
        self._original_settings: list[object] | None = None

    def __enter__(self) -> KeyboardTrigger:
        if not self.stream.isatty():
            raise RuntimeError("keyboard trigger requires a TTY")
        file_descriptor = self.stream.fileno()
        self._original_settings = termios.tcgetattr(file_descriptor)
        tty.setcbreak(file_descriptor)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._original_settings is not None:
            termios.tcsetattr(
                self.stream.fileno(),
                termios.TCSADRAIN,
                self._original_settings,
            )
            self._original_settings = None

    def wait(self, timeout_s: float) -> bool:
        if self._original_settings is None:
            raise RuntimeError("keyboard trigger must be used as a context manager")
        readable, _, _ = select.select([self.stream], [], [], timeout_s)
        return bool(readable) and self.stream.read(1) == self.key
