from __future__ import annotations

import json
import os
import select
import socket
from pathlib import Path
from types import TracebackType

from ..ports import TriggerEvent


CONTROL_SCHEMA_VERSION = 1


class UnixSocketTrigger:
    """Receive structured trigger events over a private Unix datagram socket."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("control socket path must be absolute")
        self.path = path
        self._socket: socket.socket | None = None

    def __enter__(self) -> UnixSocketTrigger:
        if self._socket is not None:
            raise RuntimeError("remote trigger is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            control_socket.bind(str(self.path))
            os.chmod(self.path, 0o660)
            control_socket.setblocking(False)
        except BaseException:
            control_socket.close()
            self.path.unlink(missing_ok=True)
            raise
        self._socket = control_socket
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self.path.unlink(missing_ok=True)

    def wait(self, timeout_s: float) -> TriggerEvent | None:
        if self._socket is None:
            raise RuntimeError("remote trigger must be used as a context manager")
        if timeout_s < 0:
            raise ValueError("timeout_s must not be negative")
        readable, _, _ = select.select((self._socket,), (), (), timeout_s)
        if not readable:
            return None
        try:
            payload = json.loads(self._socket.recv(4096))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError("invalid remote trigger payload") from error
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "type",
            "event",
        }:
            raise RuntimeError("invalid remote trigger envelope")
        if payload["schema_version"] != CONTROL_SCHEMA_VERSION:
            raise RuntimeError("unsupported remote trigger schema")
        if payload["type"] != "trigger":
            raise RuntimeError("unsupported remote control message")
        try:
            return TriggerEvent(payload["event"])
        except (TypeError, ValueError) as error:
            raise RuntimeError("unsupported remote trigger event") from error


def send_trigger(path: Path, event: TriggerEvent) -> None:
    payload = json.dumps(
        {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "type": "trigger",
            "event": event.value,
        },
        separators=(",", ":"),
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control_socket:
        control_socket.sendto(payload, str(path))
