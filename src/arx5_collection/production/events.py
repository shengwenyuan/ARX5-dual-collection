from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


EVENT_SCHEMA_VERSION = 1
EventWarningSink = Callable[[str], None]


class EventEmitter(Protocol):
    def emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        ...


class NullEventEmitter:
    def emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        return None


class UnixDatagramEventEmitter:
    """Best-effort structured events; control-plane loss must not stop collection."""

    def __init__(
        self,
        path: Path,
        warning_sink: EventWarningSink | None = None,
    ) -> None:
        if not path.is_absolute():
            raise ValueError("event socket path must be absolute")
        self.path = path
        self.warning_sink = warning_sink or (lambda message: None)

    def emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        if not event_type or any(character.isspace() for character in event_type):
            raise ValueError("event type must be a non-empty token")
        envelope = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": dict(payload or {}),
        }
        encoded = json.dumps(envelope, separators=(",", ":")).encode()
        if len(encoded) > 60_000:
            raise ValueError("event payload exceeds Unix datagram limit")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as event_socket:
                event_socket.sendto(encoded, str(self.path))
        except OSError as error:
            self.warning_sink(f"collector event delivery failed: {error}")
