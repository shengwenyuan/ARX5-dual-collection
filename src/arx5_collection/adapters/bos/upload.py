from __future__ import annotations

import argparse
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence, TextIO

from arx5_collection.adapters.bos.runtime import BceProgressProbe
from arx5_collection.adapters.bos.runtime import ProcessRunner
from arx5_collection.adapters.bos.runtime import ProgressProbe
from arx5_collection.adapters.bos.runtime import RETRY_EXIT_CODE
from arx5_collection.adapters.bos.runtime import UploadProgressWatchdog
from arx5_collection.collection.capture import CaptureProfile
from arx5_collection.collection.capture import CAPTURE_PROFILES
from arx5_collection.collection.capture import profile_from_metadata
from arx5_collection.collection.capture import stream_contract
from arx5_collection.collection.configuration import CollectionConfig
from arx5_collection.collection.episode.duration import format_duration
from arx5_collection.collection.episode.duration import summarize
from arx5_collection.config import config_path


MCAP_NAME = "episode.mcap"
METADATA_NAME = "metadata.json"
ABORT_DIRECTORY = "abort"
VALUE_OPTIONS = {"--concurrency", "--sync-type", "--traffic-limit"}
FLAG_OPTIONS = {"--follow-symlink"}
BUILTIN_POLICY_PREFIX = "builtin:"
DEFAULT_UPLOAD_POLICY = config_path("runner/bos-upload-validation-v1.toml")


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    min_mcap_bytes: int
    sample_fraction: float
    max_samples: int
    pipeline_profile: str | Path
    rgb_encoding: str
    depth_encoding: str
    width: int
    height: int
    processes: int
    concurrency_per_process: int
    progress_stall_seconds: int
    retry_delays_seconds: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            min(
                self.min_mcap_bytes,
                self.processes,
                self.concurrency_per_process,
                self.progress_stall_seconds,
            )
            <= 0
        ):
            raise ValueError("upload limits must be positive")

    @classmethod
    def load(cls, reference: str | Path) -> UploadPolicy:
        policy_path: Path | None
        if isinstance(reference, str) and reference.startswith(BUILTIN_POLICY_PREFIX):
            name = reference.removeprefix(BUILTIN_POLICY_PREFIX)
            if not name or Path(name).name != name:
                raise ValueError(f"invalid upload policy name: {name}")
            policy_path = config_path(f"runner/{name}.toml")
            stream_context = policy_path.open("rb")
        else:
            policy_path = Path(reference)
            stream_context = policy_path.open("rb")
        with stream_context as stream:
            value = tomllib.load(stream)
        pipeline_profile_value = str(value["deep"]["pipeline_profile"])
        if pipeline_profile_value.startswith("builtin:"):
            pipeline_profile: str | Path = pipeline_profile_value
        else:
            pipeline_profile = (policy_path.parent / pipeline_profile_value).resolve()
        return cls(
            min_mcap_bytes=int(value["coarse"]["min_mcap_bytes"]),
            sample_fraction=float(value["sample"]["fraction"]),
            max_samples=int(value["sample"]["max_count"]),
            pipeline_profile=pipeline_profile,
            rgb_encoding=str(value["deep"]["rgb_encoding"]),
            depth_encoding=str(value["deep"]["depth_encoding"]),
            width=int(value["deep"]["width"]),
            height=int(value["deep"]["height"]),
            processes=int(value["upload"]["processes"]),
            concurrency_per_process=int(value["upload"]["concurrency_per_process"]),
            progress_stall_seconds=int(value["upload"]["progress_stall_seconds"]),
            retry_delays_seconds=tuple(
                int(item) for item in value["upload"]["retry_delays_seconds"]
            ),
        )


@dataclass(frozen=True, slots=True)
class UploadRoute:
    root: str
    concurrency: int
    sync_type: str

    @classmethod
    def load(cls, path: str | Path = DEFAULT_UPLOAD_POLICY) -> UploadRoute:
        with Path(path).open("rb") as stream:
            value = tomllib.load(stream)
        destination = value.get("destination")
        sync = value.get("sync")
        if not isinstance(destination, dict) or set(destination) != {"root"}:
            raise ValueError("upload runner destination keys are invalid")
        if not isinstance(sync, dict) or set(sync) != {"concurrency", "sync_type"}:
            raise ValueError("upload runner sync keys are invalid")
        route = cls(
            root=str(destination["root"]).rstrip("/"),
            concurrency=int(sync["concurrency"]),
            sync_type=str(sync["sync_type"]),
        )
        if not route.root.startswith("bos:/"):
            raise ValueError("upload destination root must use bos:/")
        if route.concurrency <= 0 or route.sync_type != "dest-not-exist":
            raise ValueError("upload sync settings are invalid")
        return route


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


@dataclass(frozen=True, slots=True)
class EpisodeUploadWork:
    parent: SyncCommand
    episode: Episode

    def command(self, concurrency: int) -> SyncCommand:
        options = _replace_option(
            self.parent.options,
            "--concurrency",
            str(concurrency),
        )
        destination = (
            f"{self.parent.destination.rstrip('/')}"
            f"/{self.episode.relative_dir.as_posix()}/"
        )
        return SyncCommand(self.episode.directory, destination, options)


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


def collection_sync_command(
    source: Path,
    collection: CollectionConfig,
    route: UploadRoute | None = None,
) -> SyncCommand:
    source = source.resolve()
    if not source.is_dir():
        raise ValueError(f"本地来源目录不存在：{source}")
    collection_date = source.parent.name
    try:
        date.fromisoformat(collection_date)
    except ValueError as error:
        raise ValueError("ARX5_OUTPUT_ROOT 父目录必须是 YYYY-MM-DD") from error
    settings = route or UploadRoute.load()
    destination = f"{settings.root}/{collection.upload_directory}/{collection_date}/"
    return SyncCommand(
        source,
        destination,
        (
            "--concurrency",
            str(settings.concurrency),
            "--sync-type",
            settings.sync_type,
        ),
    )


def _validate_options(options: Sequence[str]) -> None:
    index = 0
    while index < len(options):
        option = options[index]
        if option in FLAG_OPTIONS:
            index += 1
            continue
        if option not in VALUE_OPTIONS or index + 1 == len(options):
            raise ValueError(f"不支持的 bcecmd 参数：{option}")
        if option == "--sync-type" and options[index + 1] == "force-overwrite":
            raise ValueError("禁止 force-overwrite")
        index += 2


def _replace_option(
    options: Sequence[str],
    name: str,
    value: str,
) -> tuple[str, ...]:
    updated = []
    replaced = False
    index = 0
    while index < len(options):
        option = options[index]
        if option in FLAG_OPTIONS:
            updated.append(option)
            index += 1
            continue
        option, current = options[index : index + 2]
        index += 2
        if option == name:
            updated.extend((name, value))
            replaced = True
        else:
            updated.extend((option, current))
    if not replaced:
        updated.extend((name, value))
    return tuple(updated)


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
            value = (
                json.loads(metadata_path.read_text())
                if metadata_path.is_file()
                else None
            )
        except json.JSONDecodeError:
            value = None
        episodes.append(
            Episode(
                source_root=source_root,
                directory=path,
                relative_dir=path.relative_to(source_root),
                mcap_size=(
                    (path / MCAP_NAME).stat().st_size
                    if (path / MCAP_NAME).is_file()
                    else None
                ),
                metadata_size=(
                    metadata_path.stat().st_size if metadata_path.is_file() else None
                ),
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
    by_id = (
        {
            item.get("id"): item
            for item in streams
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if isinstance(streams, list)
        else {}
    )
    if not isinstance(streams, list) or len(by_id) != len(streams):
        reasons.append("metadata streams 缺失或存在重复 id")
    try:
        capture_profile = profile_from_metadata(metadata)
    except ValueError as error:
        reasons.append(str(error))
        capture_profile = CaptureProfile.RGBD
    expected_stream_ids = set(stream_contract(capture_profile))
    unexpected_stream_ids = set(by_id) - expected_stream_ids
    if unexpected_stream_ids:
        reasons.append(
            f"metadata 含 profile 外 stream：{sorted(unexpected_stream_ids)}"
        )
    for stream_id in sorted(expected_stream_ids):
        stream = by_id.get(stream_id)
        if (
            stream is None
            or stream.get("required") is not True
            or not isinstance(stream.get("message_count"), int)
            or int(stream["message_count"]) <= 0
        ):
            reasons.append(f"{capture_profile.value} metadata 不合格：{stream_id}")
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


def choose_samples(
    episodes: Sequence[Episode], policy: UploadPolicy
) -> tuple[Episode, ...]:
    count = min(
        policy.max_samples, max(1, math.ceil(len(episodes) * policy.sample_fraction))
    )
    ordered = sorted(
        episodes, key=lambda item: (item.mcap_size or 0, item.relative_dir.as_posix())
    )
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
        from arx5_collection.dataset_pipeline.mining_stage.action_mining.utils import (
            derive_source_session_id,
        )
        from arx5_collection.dataset_pipeline.execution.models import FileIdentity
        from arx5_collection.dataset_pipeline.execution.models import StageReceipt
        from arx5_collection.dataset_pipeline.execution.episode_pipeline import (
            run_episode_pipeline,
        )
        from arx5_collection.dataset_pipeline.configuration.recipe import (
            DatasetPipelineRecipe,
        )

        recipe = DatasetPipelineRecipe.load(self.policy.pipeline_profile)
        metadata = episode.metadata or {}
        self._validate_images(
            episode.directory,
            profile_from_metadata(metadata),
        )
        task = str(metadata["task"]["description"])
        with tempfile.TemporaryDirectory(prefix="arx5-bos-deep-") as temporary_text:
            temporary = Path(temporary_text)
            mcap = episode.directory / "episode.mcap"
            metadata_path = episode.directory / "metadata.json"
            mcap_stat = mcap.stat()
            metadata_stat = metadata_path.stat()
            receipt = StageReceipt(
                episode_id=str(metadata["episode_id"]),
                source_session_id=derive_source_session_id(episode.directory),
                source_dir=episode.directory,
                stage_dir=episode.directory,
                mcap=FileIdentity(mcap_stat.st_size, mcap_stat.st_mtime_ns),
                metadata=FileIdentity(
                    metadata_stat.st_size,
                    metadata_stat.st_mtime_ns,
                ),
            )
            result = run_episode_pipeline(
                receipt,
                temporary,
                recipe,
                task,
                "local/bos_upload_validation",
            )
            quality = result.cleaning.quality
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
            if result.exclusion_reason is not None:
                raise ValueError("无法生成有效训练 Segment")

    def _validate_images(
        self,
        episode_dir: Path,
        capture_profile: CaptureProfile,
    ) -> None:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import Image

        expected = {
            stream.topic: (
                self.policy.depth_encoding
                if "depth" in stream.id
                else self.policy.rgb_encoding
            )
            for stream in CAPTURE_PROFILES[capture_profile].streams
            if stream.message_type == "sensor_msgs/msg/Image"
        }
        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(
                uri=str(episode_dir / MCAP_NAME), storage_id="mcap"
            ),
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
    failures = bos_verification_failures(command, episodes, policy, client)
    if failures:
        relative_dir, reason = next(iter(failures.items()))
        if reason.startswith("粗筛失败："):
            raise ValueError(
                f"BOS 粗筛失败：{relative_dir}：{reason.removeprefix('粗筛失败：')}"
            )
        raise ValueError(f"BOS 验证失败：{relative_dir}：{reason}")


def bos_verification_failures(
    command: SyncCommand,
    episodes: Sequence[Episode],
    policy: UploadPolicy,
    client: BosClient,
) -> dict[Path, str]:
    objects = client.list(command.destination)
    failures: dict[Path, str] = {}
    with tempfile.TemporaryDirectory(prefix="arx5-bos-metadata-") as temporary_text:
        temporary = Path(temporary_text)
        for index, episode in enumerate(episodes):
            mcap_path = remote_path(command, episode, MCAP_NAME)
            metadata_path = remote_path(command, episode, METADATA_NAME)
            if objects.get(mcap_path) != episode.mcap_size:
                failures[episode.relative_dir] = "MCAP 缺失或大小不符"
                continue
            if objects.get(metadata_path) != episode.metadata_size:
                failures[episode.relative_dir] = "metadata 缺失或大小不符"
                continue
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
                failures[episode.relative_dir] = f"粗筛失败：{issues[0].reason}"
    return failures


def upload_argv(
    command: SyncCommand,
    supports_dest_not_exist: bool,
) -> list[str]:
    options = []
    has_sync_type = False
    index = 0
    while index < len(command.options):
        if command.options[index] in FLAG_OPTIONS:
            options.append(command.options[index])
            index += 1
            continue
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
    return [
        "bcecmd",
        "bos",
        "sync",
        f"{command.source}/",
        command.destination,
        *options,
    ]


def summarize_duration(command: SyncCommand, episode_count: int) -> tuple[float, str]:
    summary = summarize(command.source, (ABORT_DIRECTORY,))
    if summary.episode_count != episode_count:
        raise ValueError("时长统计 Episode 集合与上传集合不一致")
    return summary.duration_s, format_duration(summary.duration_s)


def _confirm(prompt: str, input_stream: TextIO, output_stream: TextIO) -> None:
    output_stream.write(prompt)
    output_stream.flush()
    if input_stream.readline() not in {"\n", "\r\n"}:
        raise RuntimeError("用户取消")


def _collect(
    commands: Sequence[SyncCommand], policy: UploadPolicy
) -> dict[SyncCommand, tuple[Episode, ...]]:
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
        _confirm(
            "按 ENTER 将以上 Episode 迁移到 abort/；其他输入取消：",
            sys.stdin,
            sys.stdout,
        )
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


def validate_episode_tasks(episodes: Sequence[Episode], expected: str) -> None:
    for episode in episodes:
        task = (episode.metadata or {}).get("task")
        actual = task.get("description") if isinstance(task, dict) else None
        if actual != expected:
            raise ValueError(
                f"Episode task 与 collection task_description 不一致：{episode.directory}"
            )


def _upload_one(
    work: EpisodeUploadWork,
    policy: UploadPolicy,
    runner: ProcessRunner,
    supports_dest_not_exist: bool,
    log_dir: Path,
    round_index: int,
    progress: ProgressProbe,
) -> int:
    command = work.command(policy.concurrency_per_process)
    argv = upload_argv(command, supports_dest_not_exist)
    log_path = log_dir / f"{work.episode.episode_id}.attempt-{round_index + 1}.log"
    with log_path.open("w") as log:
        return runner.stream(
            argv,
            policy.progress_stall_seconds,
            log,
            progress,
        )


def _run_upload_round(
    works: Sequence[EpisodeUploadWork],
    policy: UploadPolicy,
    runner: ProcessRunner,
    supports_dest_not_exist: bool,
    log_dir: Path,
    round_index: int,
    progress: ProgressProbe,
) -> dict[tuple[SyncCommand, Path], int]:
    results: dict[tuple[SyncCommand, Path], int] = {}
    executor = ThreadPoolExecutor(max_workers=policy.processes)
    try:
        futures = {
            executor.submit(
                _upload_one,
                work,
                policy,
                runner,
                supports_dest_not_exist,
                log_dir,
                round_index,
                progress,
            ): work
            for work in works
        }
        for future in as_completed(futures):
            work = futures[future]
            key = (work.parent, work.episode.relative_dir)
            try:
                results[key] = future.result()
            except Exception as error:
                print(f"UPLOAD ERROR episode={work.episode.episode_id}: {error}")
                results[key] = RETRY_EXIT_CODE
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        terminate_all = getattr(runner, "terminate_all", None)
        if terminate_all is not None:
            terminate_all()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return results


def _verification_failures(
    grouped: dict[SyncCommand, tuple[Episode, ...]],
    policy: UploadPolicy,
    client: BosClient,
) -> dict[tuple[SyncCommand, Path], str]:
    failures: dict[tuple[SyncCommand, Path], str] = {}
    for command, episodes in grouped.items():
        try:
            current = bos_verification_failures(command, episodes, policy, client)
        except Exception as error:
            current = {episode.relative_dir: str(error) for episode in episodes}
        failures.update(
            ((command, relative_dir), reason)
            for relative_dir, reason in current.items()
        )
    return failures


def _upload_episodes(
    grouped: dict[SyncCommand, tuple[Episode, ...]],
    policy: UploadPolicy,
    runner: ProcessRunner,
    client: BosClient,
    supports_dest_not_exist: bool,
    log_dir: Path,
    progress_factory: Callable[[], ProgressProbe],
) -> None:
    works = tuple(
        EpisodeUploadWork(command, episode)
        for command, episodes in grouped.items()
        for episode in episodes
    )
    pending = works
    for round_index in range(len(policy.retry_delays_seconds) + 1):
        print(
            f"UPLOAD ROUND {round_index + 1} pending={len(pending)} "
            f"processes={policy.processes} "
            f"concurrency_per_process={policy.concurrency_per_process}"
        )
        progress = progress_factory()
        process_results = _run_upload_round(
            pending,
            policy,
            runner,
            supports_dest_not_exist,
            log_dir,
            round_index,
            progress,
        )
        verification = _verification_failures(grouped, policy, client)
        next_pending = []
        reasons = {}
        for work in works:
            key = (work.parent, work.episode.relative_dir)
            process_code = process_results.get(key)
            reason = verification.get(key)
            if reason is None:
                continue
            next_pending.append(work)
            reasons[key] = (
                reason
                if process_code in (None, 0)
                else f"bcecmd exit={process_code}; {reason}"
            )
        if not next_pending:
            return
        if round_index == len(policy.retry_delays_seconds):
            work = next_pending[0]
            key = (work.parent, work.episode.relative_dir)
            raise RuntimeError(f"上传失败：{work.episode.directory}：{reasons[key]}")
        delay = policy.retry_delays_seconds[round_index]
        print(
            f"重试 {round_index + 1}/{len(policy.retry_delays_seconds)}，"
            f"{delay}s 后继续，pending={len(next_pending)}"
        )
        time.sleep(delay)
        pending = tuple(next_pending)


def execute(
    commands: Sequence[SyncCommand],
    policy: UploadPolicy,
    *,
    full_check: bool = True,
    runner: ProcessRunner | None = None,
    deep_validate: Callable[[Episode], None] | None = None,
    task_description: str | None = None,
    progress_factory: Callable[[], ProgressProbe] | None = None,
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
    all_episodes = tuple(
        episode for episodes in grouped.values() for episode in episodes
    )
    if not all_episodes:
        raise RuntimeError("没有可上传的合格 Episode")
    if task_description is not None:
        validate_episode_tasks(all_episodes, task_description)
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
        print(
            f"时长：{command.source} episodes={len(grouped[command])} duration={formatted}"
        )
    print(
        f"READY commands={len(commands)} episodes={len(all_episodes)} "
        f"bytes={sum(item.mcap_size or 0 for item in all_episodes)} "
        f"duration_s={total_duration:.3f} full_check={str(full_check).lower()}"
    )
    _confirm("按 ENTER 开始上传；其他输入取消：", sys.stdin, sys.stdout)
    help_result = runner.run(["bcecmd", "bos", "sync", "--help"])
    supports_dest_not_exist = "dest-not-exist" in help_result.stdout
    log_dir = Path(tempfile.mkdtemp(prefix="arx5-bos-upload-"))
    started = time.monotonic()
    _upload_episodes(
        grouped,
        policy,
        runner,
        client,
        supports_dest_not_exist,
        log_dir,
        progress_factory or BceProgressProbe,
    )
    elapsed = time.monotonic() - started
    uploaded = sum(item.mcap_size or 0 for item in all_episodes)
    print(f"PASS avg_Bps={uploaded / elapsed:.1f}")
    print(f"UPLOAD COMPLETE logs={log_dir}")


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


def main(
    argv: Sequence[str] | None = None,
    *,
    default_policy: str | Path = DEFAULT_UPLOAD_POLICY,
) -> int:
    parser = argparse.ArgumentParser(description="审计并上传 ARX5 Episodes 到 BOS")
    parser.add_argument("--commands-file", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--collection-config", type=Path)
    parser.add_argument(
        "--full-check",
        choices=("true", "false"),
        default="true",
    )
    parser.add_argument(
        "--policy",
        type=str,
        default=default_policy,
    )
    args = parser.parse_args(argv)
    try:
        policy = UploadPolicy.load(args.policy)
        route = UploadRoute.load(args.policy)
        if args.source is not None:
            if args.commands_file is not None or args.collection_config is None:
                raise ValueError("--source 必须与 --collection-config 单独使用")
            collection = CollectionConfig.load(args.collection_config)
            commands = (
                collection_sync_command(
                    args.source,
                    collection,
                    route,
                ),
            )
            print(
                f"ROUTE source={commands[0].source} "
                f"task={collection.task_description!r} "
                f"destination={commands[0].destination}"
            )
            task_description = collection.task_description
        else:
            if args.collection_config is not None:
                raise ValueError("--collection-config 需要 --source")
            commands = parse_commands(_command_lines(args.commands_file))
            task_description = None
        execute(
            commands,
            policy,
            full_check=args.full_check == "true",
            task_description=task_description,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
