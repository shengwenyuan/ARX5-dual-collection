from __future__ import annotations

import mmap
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from arx5_collection.dagger.local_snapshot import LocalVlaSnapshotClient, REPLY
from arx5_collection.snapshot_shared_memory import (
    HEADER,
    MAGIC,
    SLOT_HEADER,
    SnapshotSharedMemoryReader,
)


class LocalSnapshotClientTest(unittest.TestCase):
    def test_reads_one_socket_selected_arena_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arena_path = root / "arena"
            socket_path = root / "snapshot.sock"
            reader = SnapshotSharedMemoryReader(arena_path, width=2, height=1)
            arena_path.write_bytes(b"\x00" * reader.arena_bytes)
            with arena_path.open("r+b") as file:
                mapping = mmap.mmap(file.fileno(), reader.arena_bytes)
                HEADER.pack_into(mapping, 0, MAGIC, 2, 1, 3, 2)
                slot_offset = HEADER.size + reader.slot_stride
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

            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)

            def serve() -> None:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(1)
                    connection.sendall(REPLY.pack(0, 1, 2, -1, -1))

            thread = threading.Thread(target=serve)
            thread.start()
            with LocalVlaSnapshotClient(
                socket_path=socket_path,
                arena_path=arena_path,
                width=2,
                height=1,
                timeout_s=0.2,
            ) as client:
                sample = client.capture()
            thread.join()
            listener.close()

            self.assertEqual(sample.cutoff_ns, 100)
            self.assertEqual(sample.camera_left.data, bytes(range(6)))
            self.assertEqual(sample.left_arm.joint_positions, tuple(range(6)))
            self.assertEqual(sample.right_arm.gripper_position, 13)


if __name__ == "__main__":
    unittest.main()
