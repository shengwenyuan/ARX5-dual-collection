from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import ModuleType
from types import SimpleNamespace
import unittest


BASELINE_COMMIT = "42e61c6"
EPISODE_ENV = "ARX5_REAL_EXPORT_EPISODE"
WORK_DIR_ENV = "ARX5_REAL_EXPORT_WORK_DIR"
EXPORT_FRAME_COUNT = 64


class RealExportRegressionTest(unittest.TestCase):
    def test_real_episode_matches_pre_migration_commit(self) -> None:
        episode_value = os.environ.get(EPISODE_ENV)
        work_value = os.environ.get(WORK_DIR_ENV)
        if episode_value is None or work_value is None:
            self.skipTest(f"set {EPISODE_ENV} and {WORK_DIR_ENV} to run real export")
        episode = Path(episode_value).resolve()
        work_dir = Path(work_value).resolve()
        if not (episode / "episode.mcap").is_file():
            raise FileNotFoundError(episode / "episode.mcap")
        if not (episode / "metadata.json").is_file():
            raise FileNotFoundError(episode / "metadata.json")
        work_dir.mkdir(parents=True, exist_ok=False)
        baseline_root = work_dir / "baseline-source"
        archive = work_dir / "baseline.tar"
        repository_root = Path(__file__).resolve().parents[2]
        subprocess.run(
            ["git", "archive", "--format=tar", "-o", str(archive), BASELINE_COMMIT],
            cwd=repository_root,
            check=True,
        )
        baseline_root.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(baseline_root, filter="data")
        archive.unlink()
        baseline_output = work_dir / "baseline-output"
        current_output = work_dir / "current-output"
        _run_revision(baseline_root, episode, baseline_output)
        _run_revision(repository_root, episode, current_output)
        summary = _compare_trees(baseline_output, current_output)
        (work_dir / "comparison.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )


def _run_revision(revision: Path, episode: Path, output: Path) -> None:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "worker",
            str(revision),
            str(episode),
            str(output),
        ],
        check=True,
        env=environment,
    )


class _DecodedPayload(bytes):
    def __new__(cls, value: bytes, decoded: object) -> _DecodedPayload:
        result = super().__new__(cls, value)
        result.decoded = decoded
        return result


class _StorageOptions:
    def __init__(self, *, uri: str, storage_id: str) -> None:
        self.uri = uri
        self.storage_id = storage_id


class _ConverterOptions:
    def __init__(self, input_serialization: str, output_serialization: str) -> None:
        self.input_serialization = input_serialization
        self.output_serialization = output_serialization


class _SequentialReader:
    def __init__(self) -> None:
        self._stream = None
        self._messages = None
        self._next = None
        self._topic_types = ()

    def open(self, storage: _StorageOptions, converter: _ConverterOptions) -> None:
        from mcap.reader import make_reader
        from mcap_ros2.decoder import DecoderFactory

        if storage.storage_id != "mcap":
            raise ValueError(storage.storage_id)
        if converter.input_serialization or converter.output_serialization:
            raise ValueError("serialization conversion is unsupported")
        self._stream = Path(storage.uri).open("rb")
        reader = make_reader(self._stream, decoder_factories=[DecoderFactory()])
        summary = reader.get_summary()
        self._topic_types = tuple(
            SimpleNamespace(
                name=channel.topic,
                type=summary.schemas[channel.schema_id].name,
            )
            for channel in summary.channels.values()
        )
        self._messages = reader.iter_decoded_messages(log_time_order=False)
        self._advance()

    def get_all_topics_and_types(self) -> tuple[object, ...]:
        return self._topic_types

    def has_next(self) -> bool:
        return self._next is not None

    def read_next(self) -> tuple[str, bytes, int]:
        if self._next is None:
            raise RuntimeError("MCAP reader is exhausted")
        _, channel, message, decoded = self._next
        result = (
            channel.topic,
            _DecodedPayload(message.data, decoded),
            message.log_time,
        )
        self._advance()
        return result

    def _advance(self) -> None:
        try:
            self._next = next(self._messages)
        except StopIteration:
            self._next = None


def _install_ros_shim() -> None:
    rosbag2_py = ModuleType("rosbag2_py")
    rosbag2_py.SequentialReader = _SequentialReader
    rosbag2_py.StorageOptions = _StorageOptions
    rosbag2_py.ConverterOptions = _ConverterOptions
    rclpy = ModuleType("rclpy")
    serialization = ModuleType("rclpy.serialization")
    serialization.deserialize_message = lambda payload, message_type: payload.decoded
    rclpy.serialization = serialization
    rosidl_runtime_py = ModuleType("rosidl_runtime_py")
    utilities = ModuleType("rosidl_runtime_py.utilities")
    utilities.get_message = lambda name: object
    rosidl_runtime_py.utilities = utilities
    sensor_msgs = ModuleType("sensor_msgs")
    sensor_msgs_msg = ModuleType("sensor_msgs.msg")
    sensor_msgs_msg.Image = object
    sensor_msgs.msg = sensor_msgs_msg
    sys.modules.update(
        {
            "rosbag2_py": rosbag2_py,
            "rclpy": rclpy,
            "rclpy.serialization": serialization,
            "rosidl_runtime_py": rosidl_runtime_py,
            "rosidl_runtime_py.utilities": utilities,
            "sensor_msgs": sensor_msgs,
            "sensor_msgs.msg": sensor_msgs_msg,
        }
    )


def _worker(revision: Path, episode: Path, output: Path) -> None:
    os.chdir(revision)
    os.environ["ARX5_CONFIG_ROOT"] = str(revision / "config")
    sys.path.insert(0, str(revision / "src"))
    _install_ros_shim()
    if (revision / "src/arx5_collection/dataset_pipeline").is_dir():
        selection_dir = _run_current(revision, episode, output)
        from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator import (
            export_lerobot,
        )
    else:
        selection_dir = _run_baseline(revision, episode, output)
        from arx5_collection.pi05_dataset.exporter import export_lerobot

    export_selection = _bounded_selection(selection_dir, output)
    export_root = output / "export"
    export_lerobot(
        episode,
        export_selection,
        export_root,
        "local/real-export-regression",
        mode="image",
        dataset_root=export_root / "lerobot",
    )


def _run_current(revision: Path, episode: Path, output: Path) -> Path:
    from arx5_collection.dataset_pipeline.configuration.recipe import (
        DatasetPipelineRecipe,
    )
    from arx5_collection.dataset_pipeline.execution.episode_pipeline import (
        EPISODE_UNIT_RUNNERS,
    )
    from arx5_collection.dataset_pipeline.execution.models import FileIdentity
    from arx5_collection.dataset_pipeline.execution.models import StageReceipt
    from arx5_collection.dataset_pipeline.execution.unit_runtime import (
        EpisodePipelineContext,
    )

    metadata = json.loads((episode / "metadata.json").read_text())
    recipe = DatasetPipelineRecipe.load(
        revision / "config/specs/recipes/pi05-equal-eef-v3.toml"
    )
    mcap_stat = (episode / "episode.mcap").stat()
    metadata_stat = (episode / "metadata.json").stat()
    receipt = StageReceipt(
        episode.name,
        "real-export-regression/session",
        episode,
        episode,
        FileIdentity(mcap_stat.st_size, mcap_stat.st_mtime_ns),
        FileIdentity(metadata_stat.st_size, metadata_stat.st_mtime_ns),
    )
    context = EpisodePipelineContext(
        receipt,
        output,
        metadata["task"]["description"],
        "local/real-export-regression",
        recipe,
    )
    timed = lambda name, operation: operation()
    for stage in recipe.pipeline.stages:
        if stage.name == "dataset_generator":
            break
        for unit in stage.units:
            EPISODE_UNIT_RUNNERS[unit.type](context, unit, timed)
            if context.exclusion_reason is not None:
                raise RuntimeError(context.exclusion_reason)
    if context.selection is None or context.selection.output_dir is None:
        raise RuntimeError("current pipeline produced no selection")
    return context.selection.output_dir


def _run_baseline(revision: Path, episode: Path, output: Path) -> Path:
    from arx5_collection.cleaning.pipeline import clean_episode
    from arx5_collection.pi05_dataset.selection_pipeline import (
        select_equal_eef_dataset,
    )
    from arx5_collection.streaming_conversion.recipe import Pi05ConversionRecipe

    metadata = json.loads((episode / "metadata.json").read_text())
    recipe = Pi05ConversionRecipe.load(
        revision / "config/conversion.pi05-equal-eef-v3.toml"
    )
    cleaning = clean_episode(episode, output / "audit", recipe.cleaning)
    if cleaning.quality["grade"] == "C":
        raise RuntimeError("baseline cleaning rejected the real Episode")
    selection = select_equal_eef_dataset(
        [episode],
        output / "audit",
        output,
        metadata["task"]["description"],
        recipe.gripper,
        recipe.gripper,
        recipe.selection,
        source_session_ids={episode.name: "real-export-regression/session"},
    )
    if not selection.episodes or selection.output_dir is None:
        raise RuntimeError("baseline pipeline produced no selection")
    return selection.output_dir


def _bounded_selection(selection_dir: Path, output: Path) -> Path:
    sample_rows = _read_jsonl(selection_dir / "sample_index.jsonl")
    segment_rows = _read_jsonl(selection_dir / "segments.jsonl")
    source_rows = _read_jsonl(selection_dir / "source_manifest.jsonl")
    if not segment_rows:
        raise RuntimeError("selection contains no segment")
    segment = dict(segment_rows[0])
    segment_id = segment["segment_id"]
    selected = sorted(
        (
            row
            for row in sample_rows
            if row["training_eligible"] and row["segment_id"] == segment_id
        ),
        key=lambda row: row["source_sample_index"],
    )[:EXPORT_FRAME_COUNT]
    if len(selected) != EXPORT_FRAME_COUNT:
        raise RuntimeError(f"first segment has only {len(selected)} frames")
    segment["frame_count"] = len(selected)
    segment["source_end_sample_index_exclusive"] = (
        selected[-1]["source_sample_index"] + 1
    )
    segment["end_tick_ns"] = selected[-1]["tick_ns"]
    report = json.loads((selection_dir / "selection.json").read_text())
    report["sample_count"] = len(selected)
    report["eligible_sample_count"] = len(selected)
    report["segment_count"] = 1
    target = output / "export-selection"
    target.mkdir()
    _write_jsonl(target / "sample_index.jsonl", selected)
    _write_jsonl(target / "segments.jsonl", [segment])
    _write_jsonl(
        target / "source_manifest.jsonl",
        [row for row in source_rows if row["segment_id"] == segment_id],
    )
    (target / "selection.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    return target


def _compare_trees(baseline: Path, current: Path) -> dict[str, object]:
    baseline_files = {
        path.relative_to(baseline) for path in baseline.rglob("*") if path.is_file()
    }
    current_files = {
        path.relative_to(current) for path in current.rglob("*") if path.is_file()
    }
    if baseline_files != current_files:
        raise AssertionError(
            f"output file sets differ: baseline_only={sorted(baseline_files - current_files)}, "
            f"current_only={sorted(current_files - baseline_files)}"
        )
    parquet_files = []
    json_files = []
    binary_files = []
    for relative in sorted(baseline_files):
        left = baseline / relative
        right = current / relative
        if relative.suffix == ".parquet":
            _compare_parquet(left, right)
            parquet_files.append(str(relative))
        elif relative.suffix in {".json", ".jsonl"}:
            left_value = _normalized_json_file(left, baseline)
            right_value = _normalized_json_file(right, current)
            if left_value != right_value:
                raise AssertionError(f"JSON output differs: {relative}")
            json_files.append(str(relative))
        else:
            if _sha256(left) != _sha256(right):
                raise AssertionError(f"binary output differs: {relative}")
            binary_files.append(str(relative))
    return {
        "baseline_commit": BASELINE_COMMIT,
        "status": "identical",
        "file_count": len(baseline_files),
        "json_files": json_files,
        "parquet_files": parquet_files,
        "binary_files": binary_files,
    }


def _compare_parquet(left: Path, right: Path) -> None:
    import pyarrow.parquet as pq

    left_table = pq.read_table(left)
    right_table = pq.read_table(right)
    if left_table.schema != right_table.schema:
        raise AssertionError(f"Parquet schema differs: {left.name}")
    if not left_table.equals(right_table):
        raise AssertionError(f"Parquet values differ: {left.name}")


def _normalized_json_file(path: Path, root: Path) -> object:
    if path.suffix == ".jsonl":
        value = _read_jsonl(path)
    else:
        value = json.loads(path.read_text())
    return _normalize_paths(value, str(root.resolve()))


def _normalize_paths(value: object, root: str) -> object:
    if isinstance(value, dict):
        return {key: _normalize_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_paths(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(root, "<output>")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "worker":
    _worker(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
