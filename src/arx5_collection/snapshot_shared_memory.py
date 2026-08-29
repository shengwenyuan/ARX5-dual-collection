from __future__ import annotations

import mmap
import os
from dataclasses import dataclass
from pathlib import Path
from struct import Struct


MAGIC = b"ARX5RGB1"
CHANNELS = 3
SLOT_COUNT = 2
HEADER = Struct("<8sIIII40x")
SLOT_HEADER = Struct("<Q6q14d24x")


class SnapshotArenaUnavailableError(RuntimeError):
    pass


def snapshot_arena_path(ros_domain_id: int) -> Path:
    if ros_domain_id < 0:
        raise ValueError("ROS Domain ID must not be negative")
    return Path(f"/dev/shm/arx5-vla-snapshot-{ros_domain_id}")


def snapshot_socket_path(ros_domain_id: int) -> Path:
    if ros_domain_id < 0:
        raise ValueError("ROS Domain ID must not be negative")
    return Path(f"/tmp/arx5-vla-snapshot-{ros_domain_id}.sock")


@dataclass(frozen=True, slots=True)
class SnapshotArenaSample:
    cutoff_ns: int
    camera_stamps_ns: tuple[int, int, int]
    arm_stamps_ns: tuple[int, int]
    left_arm: tuple[float, ...]
    right_arm: tuple[float, ...]
    frames: tuple[bytes, bytes, bytes]


class SnapshotSharedMemoryReader:
    """Read one atomically committed RGB triplet from the C++ Source arena."""

    def __init__(self, path: Path, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("snapshot dimensions must be positive")
        self.path = path
        self.width = width
        self.height = height
        self.frame_bytes = width * height * CHANNELS
        self.slot_stride = SLOT_HEADER.size + self.frame_bytes * 3
        self.arena_bytes = HEADER.size + SLOT_COUNT * self.slot_stride
        self._file_descriptor: int | None = None
        self._mapping: mmap.mmap | None = None

    def __enter__(self) -> SnapshotSharedMemoryReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def read(self, slot: int, generation: int) -> SnapshotArenaSample:
        if slot < 0 or slot >= SLOT_COUNT or generation <= 0 or generation % 2:
            raise SnapshotArenaUnavailableError("snapshot descriptor is invalid")
        mapping = self._open()
        slot_offset = HEADER.size + slot * self.slot_stride
        metadata = SLOT_HEADER.unpack_from(mapping, slot_offset)
        before = metadata[0]
        if before != generation:
            raise SnapshotArenaUnavailableError("snapshot slot was replaced before read")
        payload_offset = slot_offset + SLOT_HEADER.size
        payload = bytes(mapping[payload_offset : payload_offset + self.frame_bytes * 3])
        after = SLOT_HEADER.unpack_from(mapping, slot_offset)[0]
        if after != generation:
            raise SnapshotArenaUnavailableError("snapshot slot changed during read")
        return SnapshotArenaSample(
            cutoff_ns=metadata[1],
            camera_stamps_ns=(metadata[2], metadata[3], metadata[4]),
            arm_stamps_ns=(metadata[5], metadata[6]),
            left_arm=tuple(metadata[7:14]),
            right_arm=tuple(metadata[14:21]),
            frames=(
                payload[: self.frame_bytes],
                payload[self.frame_bytes : self.frame_bytes * 2],
                payload[self.frame_bytes * 2 :],
            ),
        )

    def close(self) -> None:
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        if self._file_descriptor is not None:
            os.close(self._file_descriptor)
            self._file_descriptor = None

    def _open(self) -> mmap.mmap:
        if self._mapping is not None:
            return self._mapping
        try:
            descriptor = os.open(self.path, os.O_RDONLY)
            if os.fstat(descriptor).st_size != self.arena_bytes:
                raise SnapshotArenaUnavailableError("snapshot arena size is invalid")
            mapping = mmap.mmap(descriptor, self.arena_bytes, access=mmap.ACCESS_READ)
        except BaseException:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        magic, width, height, channels, slots = HEADER.unpack_from(mapping)
        if (magic, width, height, channels, slots) != (
            MAGIC,
            self.width,
            self.height,
            CHANNELS,
            SLOT_COUNT,
        ):
            mapping.close()
            os.close(descriptor)
            raise SnapshotArenaUnavailableError("snapshot arena header is invalid")
        self._file_descriptor = descriptor
        self._mapping = mapping
        return mapping
