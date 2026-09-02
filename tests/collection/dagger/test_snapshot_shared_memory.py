from __future__ import annotations

import mmap
import tempfile
import unittest
from pathlib import Path

from arx5_collection.collection.snapshot_shared_memory import (
    HEADER,
    MAGIC,
    SLOT_HEADER,
    SnapshotArenaUnavailableError,
    SnapshotSharedMemoryReader,
)


class SnapshotSharedMemoryReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "snapshot"
        self.reader = SnapshotSharedMemoryReader(self.path, width=2, height=1)
        self.path.write_bytes(b"\x00" * self.reader.arena_bytes)
        with self.path.open("r+b") as file:
            mapping = mmap.mmap(file.fileno(), self.reader.arena_bytes)
            HEADER.pack_into(mapping, 0, MAGIC, 2, 1, 3, 2)
            slot_offset = HEADER.size + self.reader.slot_stride
            SLOT_HEADER.pack_into(
                mapping,
                slot_offset,
                2,
                100,
                90,
                91,
                92,
                93,
                94,
                *range(14),
            )
            payload_offset = slot_offset + SLOT_HEADER.size
            mapping[payload_offset : payload_offset + 18] = bytes(range(18))
            mapping.close()

    def tearDown(self) -> None:
        self.reader.close()
        self.directory.cleanup()

    def test_reads_one_committed_triplet(self) -> None:
        sample = self.reader.read(slot=1, generation=2)

        self.assertEqual(
            sample.frames,
            (bytes(range(6)), bytes(range(6, 12)), bytes(range(12, 18))),
        )
        self.assertEqual(sample.cutoff_ns, 100)
        self.assertEqual(sample.camera_stamps_ns, (90, 91, 92))
        self.assertEqual(sample.arm_stamps_ns, (93, 94))
        self.assertEqual(sample.left_arm, tuple(float(value) for value in range(7)))
        self.assertEqual(
            sample.right_arm, tuple(float(value) for value in range(7, 14))
        )

    def test_rejects_replaced_or_in_progress_slot(self) -> None:
        with self.assertRaises(SnapshotArenaUnavailableError):
            self.reader.read(slot=1, generation=4)
        with self.assertRaises(SnapshotArenaUnavailableError):
            self.reader.read(slot=1, generation=3)


if __name__ == "__main__":
    unittest.main()
