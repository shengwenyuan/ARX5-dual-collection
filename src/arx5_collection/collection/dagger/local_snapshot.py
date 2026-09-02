from __future__ import annotations

import socket
from pathlib import Path
from struct import Struct
from time import monotonic_ns

from arx5_collection.collection.snapshot_shared_memory import (
    SnapshotArenaUnavailableError,
    SnapshotSharedMemoryReader,
)

from .observation import (
    ObservationFailureCode,
    ObservationUnavailableError,
    RawArmSample,
    RgbFrame,
    VlaObservationStep,
)


REPLY = Struct("<IIQqq")
REQUEST = b"\x01"
FAILURE_CODES = {
    1: ObservationFailureCode.BUFFERS_NOT_READY,
    2: ObservationFailureCode.CAMERA_SPAN_EXCEEDED,
    3: ObservationFailureCode.SNAPSHOT_STALE,
    4: ObservationFailureCode.LEFT_ARM_STALE,
    5: ObservationFailureCode.RIGHT_ARM_STALE,
}


class LocalVlaSnapshotClient:
    """Request one causal observation over local socket and shared memory."""

    def __init__(
        self,
        *,
        socket_path: Path,
        arena_path: Path,
        width: int,
        height: int,
        timeout_s: float,
        monotonic_clock_ns=monotonic_ns,
    ) -> None:
        if timeout_s <= 0 or not socket_path.is_absolute():
            raise ValueError("snapshot timeout and socket path are invalid")
        self.socket_path = socket_path
        self.timeout_s = timeout_s
        self._clock_ns = monotonic_clock_ns
        self._arena = SnapshotSharedMemoryReader(arena_path, width, height)
        self._socket: socket.socket | None = None

    def __enter__(self) -> LocalVlaSnapshotClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def capture(self) -> VlaObservationStep:
        started_ns = self._clock_ns()
        deadline_ns = started_ns + int(self.timeout_s * 1_000_000_000)
        try:
            connection = self._connect(deadline_ns)
            self._set_remaining_timeout(connection, deadline_ns)
            connection.sendall(REQUEST)
            reply = self._receive_reply(connection, deadline_ns)
        except (OSError, TimeoutError) as error:
            self._close_socket()
            raise self._unavailable(started_ns, str(error)) from error

        status, slot, generation, observed_ns, limit_ns = REPLY.unpack(reply)
        if status != 0:
            code = FAILURE_CODES.get(status)
            if code is None:
                raise RuntimeError(f"snapshot source returned unknown status {status}")
            raise ObservationUnavailableError(
                code,
                observed_ns=_optional_ns(observed_ns),
                limit_ns=_optional_ns(limit_ns),
            )

        try:
            sample = self._arena.read(slot, generation)
        except SnapshotArenaUnavailableError as error:
            raise ObservationUnavailableError(
                ObservationFailureCode.BUFFERS_NOT_READY,
                detail=str(error),
            ) from error
        left_data, overview_data, right_data = sample.frames
        left_stamp, overview_stamp, right_stamp = sample.camera_stamps_ns
        return VlaObservationStep(
            cutoff_ns=sample.cutoff_ns,
            camera_left=self._frame(left_data, left_stamp),
            camera_overview=self._frame(overview_data, overview_stamp),
            camera_right=self._frame(right_data, right_stamp),
            left_arm=_arm_sample(sample.left_arm, sample.arm_stamps_ns[0]),
            right_arm=_arm_sample(sample.right_arm, sample.arm_stamps_ns[1]),
        )

    def close(self) -> None:
        self._close_socket()
        self._arena.close()

    def _connect(self, deadline_ns: int) -> socket.socket:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._set_remaining_timeout(self._socket, deadline_ns)
            self._socket.connect(str(self.socket_path))
        return self._socket

    def _receive_reply(self, connection: socket.socket, deadline_ns: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < REPLY.size:
            self._set_remaining_timeout(connection, deadline_ns)
            chunk = connection.recv(REPLY.size - len(chunks))
            if not chunk:
                raise ConnectionError("snapshot socket closed before reply")
            chunks.extend(chunk)
        return bytes(chunks)

    def _set_remaining_timeout(
        self, connection: socket.socket, deadline_ns: int
    ) -> None:
        remaining_ns = deadline_ns - self._clock_ns()
        if remaining_ns <= 0:
            raise TimeoutError("snapshot request timed out")
        connection.settimeout(remaining_ns / 1_000_000_000)

    def _frame(self, data: bytes, stamp_ns: int) -> RgbFrame:
        return RgbFrame(
            data=data,
            stamp_ns=stamp_ns,
            width=self._arena.width,
            height=self._arena.height,
        )

    def _close_socket(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _unavailable(self, started_ns: int, detail: str) -> ObservationUnavailableError:
        return ObservationUnavailableError(
            ObservationFailureCode.BUFFERS_NOT_READY,
            observed_ns=self._clock_ns() - started_ns,
            limit_ns=int(self.timeout_s * 1_000_000_000),
            detail=detail,
        )


def _optional_ns(value: int) -> int | None:
    return None if value < 0 else value


def _arm_sample(values: tuple[float, ...], stamp_ns: int) -> RawArmSample:
    return RawArmSample(
        joint_positions=values[:6],
        gripper_position=values[6],
        stamp_ns=stamp_ns,
    )
