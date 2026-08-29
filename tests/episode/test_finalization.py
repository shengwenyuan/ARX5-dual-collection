from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from arx5_collection.episode.finalization import GIB, McapFinalizer
from arx5_collection.episode.models import StreamMetrics, StreamSpec


STREAMS = (
    StreamSpec("left_arm", "/embodiments/left_arm/state", True, 1000.0),
)
METRICS = (StreamMetrics("left_arm", 1000, 1.0, 1000.0, 1.1),)


class FakeMcapCli:
    def __init__(self, doctor_error: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.doctor_error = doctor_error

    def __call__(self, command):
        argv = tuple(command)
        self.calls.append(argv)
        if argv[1] == "compress":
            source = Path(argv[2])
            output = Path(argv[argv.index("--output") + 1])
            shutil.copyfile(source, output)
            output.write_bytes(b"compressed")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if self.doctor_error:
            raise subprocess.CalledProcessError(1, argv, stderr="Error: corrupt")
        return subprocess.CompletedProcess(
            argv,
            0,
            "",
            "Warning: Message.log_time 10 on \"/left\" is less than the latest "
            "log time 11\nWarning: unexpected doctor warning\n",
        )


def finalizer(cli: FakeMcapCli, **overrides) -> McapFinalizer:
    values = {
        "command_runner": cli,
        "auditor": lambda path, streams: METRICS,
        "available_memory": lambda: 64 * GIB,
        "disk_free": lambda path: 64 * GIB,
        "clock": iter((0.0, 1.5, 2.0, 2.75)).__next__,
    }
    values.update(overrides)
    return McapFinalizer(**values)


def test_compresses_validates_and_atomically_replaces(tmp_path: Path) -> None:
    source = tmp_path / "episode.mcap"
    source.write_bytes(b"raw")
    cli = FakeMcapCli()
    warnings: list[str] = []

    extension = finalizer(cli, warning_sink=warnings.append).finalize(
        source,
        STREAMS,
        METRICS,
    )

    assert source.read_bytes() == b"compressed"
    assert not (tmp_path / ".episode.mcap.zstd.tmp").exists()
    assert [call[1] for call in cli.calls] == ["compress", "doctor"]
    assert cli.calls[0][-4:] == ("--compression", "zstd", "--order", "preserve")
    assert warnings == ["mcap doctor: Warning: unexpected doctor warning"]
    metadata = extension["mcap_compression"]
    assert metadata["algorithm"] == "zstd"
    assert metadata["status"] == "compressed"
    assert metadata["source_bytes"] == 3
    assert metadata["output_bytes"] == 10
    assert metadata["compression_s"] == 1.5
    assert metadata["doctor_s"] == 0.75


def test_no_compress_skips_tool_and_capacity_checks(tmp_path: Path) -> None:
    source = tmp_path / "episode.mcap"
    source.write_bytes(b"raw")
    cli = FakeMcapCli()
    result = McapFinalizer(
        enabled=False,
        command_runner=cli,
        available_memory=lambda: (_ for _ in ()).throw(AssertionError()),
    ).finalize(source, STREAMS, METRICS)

    assert cli.calls == []
    assert source.read_bytes() == b"raw"
    assert result["mcap_compression"]["algorithm"] == "none"


def test_low_memory_fails_before_starting_tool(tmp_path: Path) -> None:
    source = tmp_path / "episode.mcap"
    source.write_bytes(b"raw")
    cli = FakeMcapCli()

    with pytest.raises(OSError, match="available memory"):
        finalizer(cli, available_memory=lambda: 11 * GIB).finalize(
            source,
            STREAMS,
            METRICS,
        )

    assert cli.calls == []
    assert source.read_bytes() == b"raw"


def test_doctor_failure_preserves_raw_and_temporary_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "episode.mcap"
    source.write_bytes(b"raw")
    cli = FakeMcapCli(doctor_error=True)

    with pytest.raises(subprocess.CalledProcessError):
        finalizer(cli).finalize(source, STREAMS, METRICS)

    assert source.read_bytes() == b"raw"
    assert (tmp_path / ".episode.mcap.zstd.tmp").read_bytes() == b"compressed"


def test_metric_mismatch_preserves_raw_and_temporary_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "episode.mcap"
    source.write_bytes(b"raw")
    cli = FakeMcapCli()
    different = (StreamMetrics("left_arm", 999, 1.0, 999.0, 1.1),)

    with pytest.raises(RuntimeError, match="metrics differ"):
        finalizer(cli, auditor=lambda path, streams: different).finalize(
            source,
            STREAMS,
            METRICS,
        )

    assert source.read_bytes() == b"raw"
    assert (tmp_path / ".episode.mcap.zstd.tmp").is_file()
