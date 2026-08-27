#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence, TextIO


MCAP_NAME = "episode.mcap"
METADATA_NAME = "metadata.json"
ABORT_DIRECTORY = "abort"
STREAM_IDS = {
    "left_arm_state",
    "right_arm_state",
    "camera_left_color",
    "camera_left_aligned_depth",
    "camera_right_color",
    "camera_right_aligned_depth",
    "camera_overview_color",
    "camera_overview_aligned_depth",
}
VALUE_OPTIONS = {"--concurrency", "--sync-type", "--traffic-limit"}
RETRY_EXIT_CODE = 75


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    min_mcap_bytes: int
    sample_fraction: float
    max_samples: int
    conversion_profile: Path
    rgb_encoding: str
    depth_encoding: str
    width: int
    height: int
    stall_seconds: int
    retry_delays_seconds: tuple[int, ...]

    @classmethod
    def load(cls, path: Path) -> UploadPolicy:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        return cls(
            min_mcap_bytes=int(value["coarse"]["min_mcap_bytes"]),
            sample_fraction=float(value["sample"]["fraction"]),
            max_samples=int(value["sample"]["max_count"]),
            conversion_profile=(path.parent / value["deep"]["conversion_profile"]).resolve(),
            rgb_encoding=str(value["deep"]["rgb_encoding"]),
            depth_encoding=str(value["deep"]["depth_encoding"]),
            width=int(value["deep"]["width"]),
            height=int(value["deep"]["height"]),
            stall_seconds=int(value["upload"]["stall_seconds"]),
            retry_delays_seconds=tuple(
                int(item) for item in value["upload"]["retry_delays_seconds"]
            ),
        )


@dataclass(frozen=True, slots=True)
class SyncCommand:
    source: Path
    destination: str
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Episode:
    source_root: Path
    directory: Path
    relative_dir: Path
    mcap_size: int | None
    metadata_size: int | None
    metadata: dict[str, object] | None

    @property
    def episode_id(self) -> str:
        return self.directory.name


@dataclass(frozen=True, slots=True)
class GateIssue:
    episode: Episode
    reason: str


def parse_commands(lines: Iterable[str]) -> tuple[SyncCommand, ...]:
    commands = []
    for line in lines:
        if not line.strip():
            continue
        words = shlex.split(line)
        if words[:3] != ["bcecmd", "bos", "sync"] or len(words) < 5:
            raise ValueError(f"不是标准 bcecmd bos sync 语句：{line}")
        source_text, destination = words[3:5]
        source = Path(source_text).resolve()
        if not source.is_absolute() or not source.is_dir():
            raise ValueError(f"本地来源目录不存在：{source_text}")
        if source.name == ABORT_DIRECTORY:
            raise ValueError("不能上传 abort 目录")
        if not destination.startswith("bos:/") or not destination.endswith("/"):
            raise ValueError(f"BOS 目标必须使用 bos:/.../：{destination}")
        _validate_options(words[5:])
        commands.append(SyncCommand(source, destination, tuple(words[5:])))
    if not commands:
        raise ValueError("没有 bcecmd 命令")
    for index, left in enumerate(commands):
        for right in commands[index + 1 :]:
            if (
                left.source == right.source
                or left.source in right.source.parents
                or right.source in left.source.parents
                or left.destination.startswith(right.destination)
                or right.destination.startswith(left.destination)
            ):
                raise ValueError("多条 sync 的本地来源或 BOS 目标不能重叠")
    return tuple(commands)


def _validate_options(options: Sequence[str]) -> None:
    index = 0
    while index < len(options):
        option = options[index]
        if option not in VALUE_OPTIONS or index + 1 == len(options):
            raise ValueError(f"不支持的 bcecmd 参数：{option}")
        if option == "--sync-type" and options[index + 1] == "force-overwrite":
            raise ValueError("禁止 force-overwrite")
        index += 2


def discover_episodes(source_root: Path) -> tuple[Episode, ...]:
    episodes = []
    for directory, child_names, file_names in os.walk(source_root, topdown=True):
        child_names[:] = sorted(name for name in child_names if name != ABORT_DIRECTORY)
        if MCAP_NAME not in file_names and METADATA_NAME not in file_names:
            continue
        path = Path(directory)
        child_names.clear()
        metadata_path = path / METADATA_NAME
        try:
            value = json.loads(metadata_path.read_text()) if metadata_path.is_file() else None
        except json.JSONDecodeError:
            value = None
        episodes.append(
            Episode(
                source_root=source_root,
                directory=path,
                relative_dir=path.relative_to(source_root),
                mcap_size=(path / MCAP_NAME).stat().st_size
                if (path / MCAP_NAME).is_file()
                else None,
                metadata_size=metadata_path.stat().st_size if metadata_path.is_file() else None,
                metadata=value if isinstance(value, dict) else None,
            )
        )
    return tuple(sorted(episodes, key=lambda item: item.relative_dir.as_posix()))


def coarse_issues(episode: Episode, policy: UploadPolicy) -> tuple[GateIssue, ...]:
    reasons = []
    metadata = episode.metadata
    if episode.mcap_size is None:
        reasons.append("缺少 episode.mcap")
    elif episode.mcap_size < policy.min_mcap_bytes:
        reasons.append(f"MCAP 小于 {policy.min_mcap_bytes} bytes")
    if metadata is None:
        reasons.append("metadata.json 缺失或不是有效 JSON object")
        return tuple(GateIssue(episode, reason) for reason in reasons)
    if metadata.get("episode_id") != episode.episode_id:
        reasons.append("metadata episode_id 与目录名不一致")
    if metadata.get("schema_version") != 1:
        reasons.append("metadata schema_version 不是 1")
    for key in ("collection_type", "outcome"):
        if metadata.get(key) in (None, ""):
            reasons.append(f"metadata 缺少 {key}")
    task = metadata.get("task")
    if not isinstance(task, dict) or not task.get("id") or not task.get("description"):
        reasons.append("metadata task 不完整")
    station = metadata.get("station")
    if not isinstance(station, dict) or not station.get("id"):
        reasons.append("metadata station.id 缺失")
    timing = metadata.get("timing")
    if (
        not isinstance(timing, dict)
        or not timing.get("started_at")
        or not timing.get("ended_at")
        or str(timing.get("ended_at")) <= str(timing.get("started_at"))
        or not isinstance(timing.get("duration_s"), (int, float))
        or isinstance(timing.get("duration_s"), bool)
        or not math.isfinite(float(timing.get("duration_s", 0)))
        or float(timing.get("duration_s", 0)) <= 0
    ):
        reasons.append("metadata timing 不完整或 duration_s 非正数")
    streams = metadata.get("streams")
    by_id = {
        item.get("id"): item
        for item in streams
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(streams, list) else {}
    if not isinstance(streams, list) or len(by_id) != len(streams):
        reasons.append("metadata streams 缺失或存在重复 id")
    for stream_id in sorted(STREAM_IDS):
        stream = by_id.get(stream_id)
        if (
            stream is None
            or stream.get("required") is not True
            or not isinstance(stream.get("message_count"), int)
            or int(stream["message_count"]) <= 0
        ):
            reasons.append(f"八路 metadata 不合格：{stream_id}")
    collection_type = metadata.get("collection_type")
    outcome = metadata.get("outcome")
    dagger_fail = (
        collection_type == "dagger"
        and outcome == "fail"
        and "dagger_fail" in episode.directory.parts
    )
    if not (
        (collection_type == "demonstration" and outcome == "success")
        or (collection_type == "dagger" and outcome == "success")
        or dagger_fail
    ):
        reasons.append("仅支持普通 success、DAgger success 或 dagger_fail")
    errors = metadata.get("errors")
    if not dagger_fail and errors != []:
        reasons.append("metadata errors 必须为空列表")
    return tuple(GateIssue(episode, reason) for reason in reasons)


def choose_samples(episodes: Sequence[Episode], policy: UploadPolicy) -> tuple[Episode, ...]:
    count = min(policy.max_samples, max(1, math.ceil(len(episodes) * policy.sample_fraction)))
    ordered = sorted(episodes, key=lambda item: (item.mcap_size or 0, item.relative_dir.as_posix()))
    selected = []
    for item in (ordered[0], ordered[-1]):
        if item not in selected and len(selected) < count:
            selected.append(item)
    for item in sorted(episodes, key=lambda value: value.relative_dir.as_posix()):
        if item not in selected and len(selected) < count:
            selected.append(item)
    return tuple(selected)


class DeepGate:
    def __init__(self, policy: UploadPolicy) -> None:
        self.policy = policy

    def validate(self, episode: Episode) -> None:
        from arx5_collection.cleaning.pipeline import clean_episode
        from arx5_collection.cleaning.pipeline import inspect_episode
        from arx5_collection.dagger_dataset.pipeline import classify_dagger_episode
        from arx5_collection.dagger_dataset.selection import select_equal_eef_dagger_dataset
        from arx5_collection.pi05_dataset.exporter import export_lerobot
        from arx5_collection.pi05_dataset.selection_pipeline import select_equal_eef_dataset
        from arx5_collection.pi05_dataset.validate import validate_lerobot
        from arx5_collection.streaming_conversion.recipe import Pi05ConversionRecipe

        recipe = Pi05ConversionRecipe.load(self.policy.conversion_profile)
        quality = inspect_episode(episode.directory, recipe.cleaning).quality
        if quality["grade"] not in {"A", "B"}:
            raise ValueError(f"cleaning grade={quality['grade']}")
        for topic, timeline in quality["timeline"].items():
            if timeline["duplicate_count"] or timeline["non_monotonic_count"]:
                raise ValueError(f"时间戳重复或逆序：{topic}")
            limit = (
                recipe.cleaning.arm_gap_warning_ns
                if "arm/state" in topic
                else recipe.cleaning.camera_gap_warning_ns
            )
            if timeline["max_positive_gap_ns"] > limit:
                raise ValueError(f"时间间隔超限：{topic}")
        self._validate_images(episode.directory)
        metadata = episode.metadata or {}
        task = str(metadata["task"]["description"])
        calibration = recipe.calibration_for(str(metadata["station"]["id"]))
        with tempfile.TemporaryDirectory(prefix="arx5-bos-deep-") as temporary_text:
            temporary = Path(temporary_text)
            audit = temporary / "audit"
            clean_episode(episode.directory, audit, recipe.cleaning)
            if metadata["collection_type"] == "dagger":
                classify_dagger_episode(episode.directory, audit)
                selection = select_equal_eef_dagger_dataset(
                    [episode.directory], audit, temporary, task,
                    calibration.left, calibration.right, recipe.selection,
                )
            else:
                selection = select_equal_eef_dataset(
                    [episode.directory], audit, temporary, task,
                    calibration.left, calibration.right, recipe.selection,
                )
            if not selection.episodes:
                raise ValueError("无法生成有效训练 Segment")
            dataset = export_lerobot(
                episode.directory,
                selection.output_dir,
                temporary,
                "local/bos_upload_validation",
                dataset_root=temporary / "lerobot",
            )
            validate_lerobot(
                dataset,
                "local/bos_upload_validation",
                action_horizon=recipe.selection.action_horizon,
                expected_task=task,
            )

    def _validate_images(self, episode_dir: Path) -> None:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import Image

        expected = {
            f"/sensors/camera_{role}/{leaf}/image_raw": (
                self.policy.rgb_encoding if leaf == "color" else self.policy.depth_encoding
            )
            for role in ("left", "right", "overview")
            for leaf in ("color", "aligned_depth")
        }
        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(uri=str(episode_dir / MCAP_NAME), storage_id="mcap"),
            rosbag2_py.ConverterOptions("", ""),
        )
        observed = set()
        while reader.has_next() and observed != set(expected):
            topic, payload, _ = reader.read_next()
            if topic not in expected or topic in observed:
                continue
            message = deserialize_message(payload, Image)
            if (
                message.encoding.lower() != expected[topic].lower()
                or message.width != self.policy.width
                or message.height != self.policy.height
            ):
                raise ValueError(f"图像规格不匹配：{topic}")
            observed.add(topic)
        if observed != set(expected):
            raise ValueError(f"图像 Topic 缺失：{sorted(set(expected) - observed)}")


class ProcessRunner:
    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stream(self, argv: Sequence[str], stall_seconds: int, log: TextIO) -> int:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        last_output = time.monotonic()
        while process.poll() is None:
            events = selector.select(timeout=1)
            if events:
                chunk = os.read(process.stdout.fileno(), 8192)
                if chunk:
                    text = chunk.decode(errors="replace")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    log.write(text)
                    log.flush()
                    last_output = time.monotonic()
            elif time.monotonic() - last_output >= stall_seconds:
                process.terminate()
                process.wait()
                return RETRY_EXIT_CODE
        remainder = process.stdout.read().decode(errors="replace")
        if remainder:
            sys.stdout.write(remainder)
            log.write(remainder)
        return process.returncode


def parse_bos_listing(output: str, prefix: str) -> dict[str, int]:
    value = json.loads(output)
    bucket = prefix.removeprefix("bos:/").split("/", 1)[0]
    return {
        f"bos:/{bucket}/{item['key']}": int(item["size"])
        for item in value.get("objects") or []
    }


class BosClient:
    def __init__(self, runner: ProcessRunner) -> None:
        self.runner = runner

    def list(self, prefix: str) -> dict[str, int]:
        result = self.runner.run(
            ["bcecmd", "bos", "ls", prefix, "--recursive", "--all", "--output", "json"]
        )
        if result.returncode:
            raise RuntimeError(result.stdout.strip())
        return parse_bos_listing(result.stdout, prefix)

    def metadata(self, path: str, target: Path) -> dict[str, object]:
        result = self.runner.run(
            ["bcecmd", "bos", "cp", path, str(target), "--yes", "--disable-bar"]
        )
        if result.returncode:
            raise RuntimeError(result.stdout.strip())
        value = json.loads(target.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"远端 metadata 不是 object：{path}")
        return value


def remote_path(command: SyncCommand, episode: Episode, name: str) -> str:
    return f"{command.destination.rstrip('/')}/{episode.relative_dir.as_posix()}/{name}"


def verify_bos(
    command: SyncCommand,
    episodes: Sequence[Episode],
    policy: UploadPolicy,
    client: BosClient,
) -> None:
    objects = client.list(command.destination)
    with tempfile.TemporaryDirectory(prefix="arx5-bos-metadata-") as temporary_text:
        temporary = Path(temporary_text)
        for index, episode in enumerate(episodes):
            mcap_path = remote_path(command, episode, MCAP_NAME)
            metadata_path = remote_path(command, episode, METADATA_NAME)
            if objects.get(mcap_path) != episode.mcap_size:
                raise ValueError(f"BOS MCAP 缺失或大小不符：{episode.relative_dir}")
            if objects.get(metadata_path) != episode.metadata_size:
                raise ValueError(f"BOS metadata 缺失或大小不符：{episode.relative_dir}")
            remote = Episode(
                source_root=Path("/bos"),
                directory=episode.directory,
                relative_dir=episode.relative_dir,
                mcap_size=objects[mcap_path],
                metadata_size=objects[metadata_path],
                metadata=client.metadata(metadata_path, temporary / f"{index}.json"),
            )
            issues = coarse_issues(remote, policy)
            if issues:
                raise ValueError(f"BOS 粗筛失败：{episode.relative_dir}：{issues[0].reason}")


def upload_argv(
    command: SyncCommand,
    supports_dest_not_exist: bool,
) -> list[str]:
    options = []
    has_sync_type = False
    index = 0
    while index < len(command.options):
        option, value = command.options[index : index + 2]
        index += 2
        if option == "--sync-type":
            has_sync_type = True
            if value == "dest-not-exist" and not supports_dest_not_exist:
                continue
        options.extend((option, value))
    if not has_sync_type and supports_dest_not_exist:
        options.extend(("--sync-type", "dest-not-exist"))
    options.extend(("--exclude", "abort/*", "--yes"))
    return ["bcecmd", "bos", "sync", f"{command.source}/", command.destination, *options]


def summarize_duration(command: SyncCommand, episode_count: int) -> tuple[float, str]:
    tool = Path(__file__).with_name("summarize_episode_duration.py")
    result = subprocess.run(
        [sys.executable, str(tool), "--directory", str(command.source), "--block", ABORT_DIRECTORY],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise RuntimeError(result.stdout.strip())
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if int(values["EPISODES"]) != episode_count:
        raise ValueError("时长统计 Episode 集合与上传集合不一致")
    return float(values["DURATION_SECONDS"]), values["DURATION"]


def _confirm(prompt: str, input_stream: TextIO, output_stream: TextIO) -> None:
    output_stream.write(prompt)
    output_stream.flush()
    if input_stream.readline() not in {"\n", "\r\n"}:
        raise RuntimeError("用户取消")


def _collect(commands: Sequence[SyncCommand], policy: UploadPolicy) -> dict[SyncCommand, tuple[Episode, ...]]:
    result = {command: discover_episodes(command.source) for command in commands}
    seen = set()
    issues = []
    for episodes in result.values():
        for episode in episodes:
            issues.extend(coarse_issues(episode, policy))
            if episode.episode_id in seen:
                issues.append(GateIssue(episode, "本批 episode_id 重复"))
            seen.add(episode.episode_id)
    if issues:
        for issue in issues:
            print(f"不合格：{issue.episode.directory}：{issue.reason}")
        _confirm("按 ENTER 将以上 Episode 迁移到 abort/；其他输入取消：", sys.stdin, sys.stdout)
        invalid = {issue.episode.directory: issue.episode for issue in issues}
        for episode in invalid.values():
            target = episode.source_root / ABORT_DIRECTORY / episode.episode_id
            target.parent.mkdir(exist_ok=True)
            shutil.move(str(episode.directory), target)
        result = {command: discover_episodes(command.source) for command in commands}
        remaining = [
            issue
            for episodes in result.values()
            for episode in episodes
            for issue in coarse_issues(episode, policy)
        ]
        if remaining:
            raise RuntimeError("迁移后粗筛仍未清零")
    return result


def execute(
    commands: Sequence[SyncCommand],
    policy: UploadPolicy,
    *,
    full_check: bool = True,
    runner: ProcessRunner | None = None,
    deep_validate: Callable[[Episode], None] | None = None,
) -> None:
    runner = runner or ProcessRunner()
    client = BosClient(runner)
    for command in commands:
        existing = client.list(command.destination)
        conflicts = [
            remote_path(command, episode, name)
            for episode in discover_episodes(command.source)
            for name in (MCAP_NAME, METADATA_NAME)
            if remote_path(command, episode, name) in existing
        ]
        if conflicts:
            raise RuntimeError(f"BOS 目标已存在同名对象：{conflicts[0]}")
    grouped = _collect(commands, policy)
    all_episodes = tuple(episode for episodes in grouped.values() for episode in episodes)
    if not all_episodes:
        raise RuntimeError("没有可上传的合格 Episode")
    if full_check:
        validate = deep_validate or DeepGate(policy).validate
        samples = choose_samples(all_episodes, policy)
        for index, episode in enumerate(samples, 1):
            print(f"深检 {index}/{len(samples)}：{episode.directory}")
            validate(episode)
    else:
        print("深检：SKIPPED (--full-check false)")
    total_duration = 0.0
    for command in commands:
        seconds, formatted = summarize_duration(command, len(grouped[command]))
        total_duration += seconds
        print(f"时长：{command.source} episodes={len(grouped[command])} duration={formatted}")
    print(
        f"READY commands={len(commands)} episodes={len(all_episodes)} "
        f"bytes={sum(item.mcap_size or 0 for item in all_episodes)} "
        f"duration_s={total_duration:.3f} full_check={str(full_check).lower()}"
    )
    _confirm("按 ENTER 开始上传；其他输入取消：", sys.stdin, sys.stdout)
    help_result = runner.run(["bcecmd", "bos", "sync", "--help"])
    supports_dest_not_exist = "dest-not-exist" in help_result.stdout
    log_path = Path(tempfile.gettempdir()) / f"arx5-bos-upload-{int(time.time())}.log"
    with log_path.open("w") as log:
        for command in commands:
            argv = upload_argv(command, supports_dest_not_exist)
            started = time.monotonic()
            for attempt in range(len(policy.retry_delays_seconds) + 1):
                code = runner.stream(argv, policy.stall_seconds, log)
                try:
                    if code:
                        raise RuntimeError(f"bcecmd exit={code}")
                    verify_bos(command, grouped[command], policy, client)
                    break
                except Exception as error:
                    if attempt == len(policy.retry_delays_seconds):
                        raise RuntimeError(f"上传停滞：{command.source}：{error}") from error
                    delay = policy.retry_delays_seconds[attempt]
                    print(f"重试 {attempt + 1}/5，{delay}s 后继续：{error}")
                    time.sleep(delay)
            elapsed = time.monotonic() - started
            uploaded = sum(item.mcap_size or 0 for item in grouped[command])
            print(f"PASS {command.source} -> {command.destination} avg_Bps={uploaded / elapsed:.1f}")
    print(f"UPLOAD COMPLETE log={log_path}")


def _command_lines(path: Path | None) -> list[str]:
    if path is not None:
        return path.read_text().splitlines()
    if not sys.stdin.isatty():
        return sys.stdin.read().splitlines()
    print("逐行粘贴 bcecmd 命令，空行结束：")
    lines = []
    while line := input():
        lines.append(line)
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计并上传 ARX5 Episodes 到 BOS")
    parser.add_argument("--commands-file", type=Path)
    parser.add_argument(
        "--full-check",
        choices=("true", "false"),
        default="true",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config/bos-upload-validation.v1.toml",
    )
    args = parser.parse_args(argv)
    try:
        execute(
            parse_commands(_command_lines(args.commands_file)),
            UploadPolicy.load(args.policy),
            full_check=args.full_check == "true",
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
