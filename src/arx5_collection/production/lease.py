"""A host-shared advisory lock held for the entire CAN/camera session."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class HardwareLease:
    def __init__(self, path=Path("/var/lib/arx5-collection/hardware.lock")):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        if self.stream is not None:
            raise RuntimeError("hardware lease already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise RuntimeError(
                "hardware is in use by collect, DAgger, station setup or calibration"
            ) from error
        self.stream = stream
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(f"pid={os.getpid()}\n")
            stream.flush()
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.stream is not None:
            fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None
        # Keep the inode: unlinking a flock file can create two independent owners.
