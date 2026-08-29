from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from time import perf_counter

from arx5_collection.ros2_adapters.mcap_metrics import audit_mcap

from .models import StreamMetrics, StreamSpec


GIB = 1024**3
MCAP_CLI = Path("/usr/local/bin/mcap")
MCAP_CLI_VERSION = "0.3.0"
_CROSS_TOPIC_ORDER_WARNING = " is less than the latest log time "

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
McapAuditor = Callable[
    [Path, tuple[StreamSpec, ...]],
    tuple[StreamMetrics, ...],
]


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(command),
        check=True,
        capture_output=True,
        text=True,
    )


def _audit(path: Path, streams: tuple[StreamSpec, ...]) -> tuple[StreamMetrics, ...]:
    return audit_mcap(path, streams)


def _available_memory_bytes(path: Path = Path("/proc/meminfo")) -> int:
    for line in path.read_text().splitlines():
        name, separator, value = line.partition(":")
        if name == "MemAvailable" and separator:
            fields = value.split()
            if len(fields) == 2 and fields[1] == "kB":
                return int(fields[0]) * 1024
    raise RuntimeError("/proc/meminfo does not contain MemAvailable")


class McapFinalizer:
    """Rewrite one closed MCAP safely before its Episode is committed."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        executable: Path = MCAP_CLI,
        command_runner: CommandRunner = _run_command,
        auditor: McapAuditor = _audit,
        available_memory: Callable[[], int] = _available_memory_bytes,
        disk_free: Callable[[Path], int] | None = None,
        clock: Callable[[], float] = perf_counter,
        warning_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.executable = executable
        self.command_runner = command_runner
        self.auditor = auditor
        self.available_memory = available_memory
        self.disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.clock = clock
        self.warning_sink = warning_sink or (lambda message: None)

    def finalize(
        self,
        mcap_path: Path,
        streams: tuple[StreamSpec, ...],
        expected_metrics: tuple[StreamMetrics, ...],
    ) -> dict[str, object]:
        if not mcap_path.is_file():
            raise FileNotFoundError(mcap_path)
        source_bytes = mcap_path.stat().st_size
        if source_bytes <= 0:
            raise RuntimeError("cannot finalize an empty MCAP")
        if not self.enabled:
            return self._metadata(
                algorithm="none",
                status="skipped",
                source_bytes=source_bytes,
                output_bytes=source_bytes,
                compression_s=0.0,
                doctor_s=0.0,
            )

        self._require_capacity(mcap_path.parent, source_bytes)
        temporary_path = mcap_path.with_name(f".{mcap_path.name}.zstd.tmp")
        if temporary_path.exists():
            raise FileExistsError(temporary_path)

        compression_started = self.clock()
        self.command_runner(
            (
                str(self.executable),
                "compress",
                str(mcap_path),
                "--output",
                str(temporary_path),
                "--compression",
                "zstd",
                "--order",
                "preserve",
            )
        )
        compression_s = self.clock() - compression_started
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise RuntimeError("mcap compress did not produce a non-empty output")

        doctor_started = self.clock()
        diagnosis = self.command_runner(
            (str(self.executable), "doctor", str(temporary_path))
        )
        doctor_s = self.clock() - doctor_started
        for line in diagnosis.stderr.splitlines():
            warning = line.strip()
            if warning and not (
                warning.startswith("Warning: Message.log_time ")
                and _CROSS_TOPIC_ORDER_WARNING in warning
            ):
                self.warning_sink(f"mcap doctor: {warning}")

        actual_metrics = self.auditor(temporary_path, streams)
        self._require_equivalent_metrics(expected_metrics, actual_metrics)
        output_bytes = temporary_path.stat().st_size
        os.replace(temporary_path, mcap_path)
        return self._metadata(
            algorithm="zstd",
            status="compressed",
            source_bytes=source_bytes,
            output_bytes=output_bytes,
            compression_s=compression_s,
            doctor_s=doctor_s,
        )

    def _require_capacity(self, directory: Path, source_bytes: int) -> None:
        required_memory = max(source_bytes, 10 * GIB) + 2 * GIB
        available_memory = self.available_memory()
        if available_memory < required_memory:
            raise OSError(
                "insufficient available memory for MCAP compression: "
                f"{available_memory} < {required_memory} bytes"
            )
        required_disk = source_bytes + GIB
        free_disk = self.disk_free(directory)
        if free_disk < required_disk:
            raise OSError(
                "insufficient disk space for MCAP compression: "
                f"{free_disk} < {required_disk} bytes"
            )

    @staticmethod
    def _require_equivalent_metrics(
        expected: tuple[StreamMetrics, ...],
        actual: tuple[StreamMetrics, ...],
    ) -> None:
        def signature(metrics: StreamMetrics) -> tuple[object, ...]:
            return (
                metrics.id,
                metrics.count,
                metrics.duration_s,
                metrics.observed_hz,
                metrics.max_gap_ms,
            )

        if tuple(map(signature, actual)) != tuple(map(signature, expected)):
            raise RuntimeError("compressed MCAP stream metrics differ from source")

    @staticmethod
    def _metadata(
        *,
        algorithm: str,
        status: str,
        source_bytes: int,
        output_bytes: int,
        compression_s: float,
        doctor_s: float,
    ) -> dict[str, object]:
        return {
            "mcap_compression": {
                "tool": "foxglove-mcap-cli",
                "version": MCAP_CLI_VERSION,
                "algorithm": algorithm,
                "status": status,
                "source_bytes": source_bytes,
                "output_bytes": output_bytes,
                "compression_s": compression_s,
                "doctor_s": doctor_s,
            }
        }
