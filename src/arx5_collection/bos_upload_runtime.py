from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
from threading import Lock
import time
from typing import Protocol
from typing import TextIO


RETRY_EXIT_CODE = 75


class ProgressProbe(Protocol):
    def sample(self) -> int: ...


class BceProgressProbe:
    """Expose bcecmd multipart completion as a monotonic byte counter."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path.home() / ".go-bcecli"
        if not self.root.is_dir():
            raise FileNotFoundError(self.root)
        self._observed: dict[Path, int] = {}
        self._completed_bytes = 0
        self._lock = Lock()
        self.sample()

    def sample(self) -> int:
        with self._lock:
            for directory, parser in (
                (self.root / "task_progress", self._task_bytes),
                (self.root / "multiupload_infos", self._multipart_bytes),
            ):
                if not directory.is_dir():
                    continue
                for path in directory.rglob("*"):
                    if not path.is_file():
                        continue
                    try:
                        value = parser(json.loads(path.read_text()))
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    previous = self._observed.get(path)
                    if previous is not None and value > previous:
                        self._completed_bytes += value - previous
                    self._observed[path] = max(previous or 0, value)
            return self._completed_bytes

    @staticmethod
    def _task_bytes(value: object) -> int:
        if not isinstance(value, dict):
            raise ValueError("task progress must be an object")
        result = value.get("syncSuccBytes")
        if isinstance(result, bool) or not isinstance(result, int) or result < 0:
            raise ValueError("invalid syncSuccBytes")
        return result

    @staticmethod
    def _multipart_bytes(value: object) -> int:
        if not isinstance(value, dict):
            raise ValueError("multipart progress must be an object")
        file_size = value.get("fileSize")
        part_size = value.get("partSize")
        complete = value.get("completePartList")
        if (
            isinstance(file_size, bool)
            or not isinstance(file_size, int)
            or file_size < 0
            or isinstance(part_size, bool)
            or not isinstance(part_size, int)
            or part_size <= 0
            or not isinstance(complete, list)
        ):
            raise ValueError("invalid multipart progress")
        return min(file_size, len(complete) * part_size)


class UploadProgressWatchdog:
    def __init__(
        self,
        progress: ProgressProbe,
        stall_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if stall_seconds <= 0:
            raise ValueError("progress stall seconds must be positive")
        self.progress = progress
        self.stall_seconds = stall_seconds
        self.clock = clock
        self.last_value = progress.sample()
        self.last_progress_at = clock()

    def stalled(self) -> bool:
        value = self.progress.sample()
        now = self.clock()
        if value > self.last_value:
            self.last_value = value
            self.last_progress_at = now
        return now - self.last_progress_at >= self.stall_seconds


class ProcessRunner:
    """Run bcecmd processes with shared progress and bounded shutdown."""

    def __init__(self) -> None:
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._process_lock = Lock()
        self._output_lock = Lock()

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def terminate_all(self) -> None:
        with self._process_lock:
            processes = tuple(self._processes)
        for process in processes:
            self._terminate(process)

    def stream(
        self,
        argv: Sequence[str],
        stall_seconds: int,
        log: TextIO,
        progress: ProgressProbe,
    ) -> int:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with self._process_lock:
            self._processes.add(process)
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        watchdog = UploadProgressWatchdog(progress, stall_seconds)
        try:
            while process.poll() is None:
                events = selector.select(timeout=1)
                if events:
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if chunk:
                        text = chunk.decode(errors="replace")
                        self._write_output(text)
                        log.write(text)
                        log.flush()
                if watchdog.stalled():
                    self._terminate(process)
                    return RETRY_EXIT_CODE
            remainder = process.stdout.read().decode(errors="replace")
            if remainder:
                self._write_output(remainder)
                log.write(remainder)
            return process.returncode
        finally:
            selector.close()
            with self._process_lock:
                self._processes.discard(process)
            if process.poll() is None:
                self._terminate(process)

    def _write_output(self, value: str) -> None:
        with self._output_lock:
            sys.stdout.write(value)
            sys.stdout.flush()

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
