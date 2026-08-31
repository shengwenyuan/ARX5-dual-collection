from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from bos_upload_episodes import ABORT_DIRECTORY  # noqa: E402
from bos_upload_episodes import BceProgressProbe  # noqa: E402
from bos_upload_episodes import BosClient  # noqa: E402
from bos_upload_episodes import Episode  # noqa: E402
from bos_upload_episodes import EpisodeUploadWork  # noqa: E402
from bos_upload_episodes import SyncCommand  # noqa: E402
from bos_upload_episodes import UploadPolicy  # noqa: E402
from bos_upload_episodes import UploadProgressWatchdog  # noqa: E402
from bos_upload_episodes import _collect  # noqa: E402
from bos_upload_episodes import _run_upload_round  # noqa: E402
from bos_upload_episodes import choose_samples  # noqa: E402
from bos_upload_episodes import coarse_issues  # noqa: E402
from bos_upload_episodes import parse_bos_listing  # noqa: E402
from bos_upload_episodes import parse_commands  # noqa: E402
from bos_upload_episodes import execute  # noqa: E402
from bos_upload_episodes import remote_path  # noqa: E402
from bos_upload_episodes import station_sync_command  # noqa: E402
from bos_upload_episodes import upload_argv  # noqa: E402
from bos_upload_episodes import validate_episode_tasks  # noqa: E402
from bos_upload_episodes import verify_bos  # noqa: E402


def policy(min_mcap_bytes: int = 1) -> UploadPolicy:
    return UploadPolicy(
        min_mcap_bytes=min_mcap_bytes,
        sample_fraction=0.1,
        max_samples=15,
        conversion_profile=Path("/config.toml"),
        rgb_encoding="rgb8",
        depth_encoding="16UC1",
        width=848,
        height=480,
        processes=2,
        concurrency_per_process=8,
        progress_stall_seconds=900,
        retry_delays_seconds=(10, 30, 60, 300, 900),
    )


def metadata(
    episode_id: str,
    *,
    outcome: str = "success",
    rgb_only: bool = False,
) -> dict[str, object]:
    stream_ids = (
        "left_arm_state",
        "right_arm_state",
        "camera_left_color",
        "camera_left_aligned_depth",
        "camera_right_color",
        "camera_right_aligned_depth",
        "camera_overview_color",
        "camera_overview_aligned_depth",
    )
    value = {
        "schema_version": 1,
        "collection_type": "demonstration",
        "episode_id": episode_id,
        "outcome": outcome,
        "task": {"id": "task", "description": "folding the cloth"},
        "timing": {"started_at": "2026-08-26T00:00:00Z", "ended_at": "2026-08-26T00:01:00Z", "duration_s": 60.0},
        "station": {"id": "w3"},
        "streams": [
            {"id": stream_id, "required": True, "message_count": 10}
            for stream_id in stream_ids
        ],
        "errors": [],
    }
    if rgb_only:
        value["streams"] = [
            stream
            for stream in value["streams"]
            if "aligned_depth" not in stream["id"]
        ]
        value["extensions"] = {
            "capture": {
                "profile": "rgb_only",
                "omitted_streams": [],
            }
        }
    return value


def write_episode(root: Path, episode_id: str, *, size: int = 8) -> Episode:
    directory = root / episode_id
    directory.mkdir(parents=True)
    (directory / "episode.mcap").write_bytes(b"m" * size)
    (directory / "metadata.json").write_text(json.dumps(metadata(episode_id)))
    return Episode(root, directory, Path(episode_id), size, (directory / "metadata.json").stat().st_size, metadata(episode_id))


def test_parse_standard_commands(tmp_path: Path) -> None:
    command = parse_commands(
        [f"bcecmd bos sync {tmp_path}/ bos:/bucket/task/ --concurrency 16 --sync-type dest-not-exist"]
    )[0]
    assert command.source == tmp_path
    assert command.destination == "bos:/bucket/task/"
    assert upload_argv(command, False) == [
        "bcecmd", "bos", "sync", f"{tmp_path}/", "bos:/bucket/task/",
        "--concurrency", "16", "--exclude", "abort/*", "--yes",
    ]


def test_episode_upload_work_isolates_prefix_and_replaces_concurrency(
    tmp_path: Path,
) -> None:
    episode = write_episode(tmp_path, "episode")
    parent = SyncCommand(
        tmp_path,
        "bos:/bucket/task/",
        (
            "--concurrency",
            "16",
            "--sync-type",
            "dest-not-exist",
            "--follow-symlink",
        ),
    )

    command = EpisodeUploadWork(parent, episode).command(8)

    assert command.source == episode.directory
    assert command.destination == "bos:/bucket/task/episode/"
    assert command.options == (
        "--concurrency",
        "8",
        "--sync-type",
        "dest-not-exist",
        "--follow-symlink",
    )


def test_parse_allows_explicit_follow_symlink_for_expert_input(
    tmp_path: Path,
) -> None:
    command = parse_commands(
        [
            f"bcecmd bos sync {tmp_path}/ bos:/bucket/task/ "
            "--follow-symlink --concurrency 2"
        ]
    )[0]

    assert upload_argv(command, True)[5:9] == [
        "--follow-symlink",
        "--concurrency",
        "2",
        "--sync-type",
    ]


def test_station_command_derives_task_and_date(tmp_path: Path) -> None:
    source = tmp_path / "2026-08-27" / "fold_cloth-01"
    source.mkdir(parents=True)
    command = station_sync_command(
        source,
        "folding the cloth",
        Path(__file__).parents[1] / "config" / "station.example.json",
    )
    assert command.destination == "bos:/datainfra-demo/fold_cloth/2026-08-27/"
    assert command.options == (
        "--concurrency", "16", "--sync-type", "dest-not-exist"
    )


def test_station_command_appends_dagger_to_task_route(tmp_path: Path) -> None:
    source = tmp_path / "2026-08-27" / "fold_cloth_dagger"
    source.mkdir(parents=True)

    command = station_sync_command(
        source,
        "folding the cloth",
        Path(__file__).parents[1] / "config" / "station.example.json",
        dagger=True,
    )

    assert command.destination == (
        "bos:/datainfra-demo/fold_cloth_dagger/2026-08-27/"
    )


def test_station_command_rejects_non_date_parent(tmp_path: Path) -> None:
    source = tmp_path / "today" / "fold_cloth"
    source.mkdir(parents=True)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        station_sync_command(
            source,
            "folding the cloth",
            Path(__file__).parents[1] / "config" / "station.example.json",
        )


def test_episode_task_must_match_environment_exactly(tmp_path: Path) -> None:
    episode = write_episode(tmp_path, "episode")
    validate_episode_tasks((episode,), "folding the cloth")
    with pytest.raises(ValueError, match="ARX5_TASK_DESCRIPTION"):
        validate_episode_tasks((episode,), "Folding the cloth")


def test_coarse_gate_rejects_tiny_and_missing_stream(tmp_path: Path) -> None:
    episode = write_episode(tmp_path, "episode")
    episode.metadata["streams"].pop()
    issues = coarse_issues(episode, policy(min_mcap_bytes=9))
    assert any("MCAP 小于" in issue.reason for issue in issues)
    assert any("camera_overview_aligned_depth" in issue.reason for issue in issues)


def test_production_policy_uses_300_mib_mcap_floor() -> None:
    current = UploadPolicy.load(
        Path(__file__).parents[1] / "config" / "bos-upload-validation.v1.toml"
    )

    assert current.min_mcap_bytes == 300 * 1024 * 1024


def test_coarse_gate_includes_300_mib_boundary(tmp_path: Path) -> None:
    current = policy(min_mcap_bytes=300 * 1024 * 1024)
    episode = write_episode(tmp_path, "episode")

    below = Episode(
        episode.source_root,
        episode.directory,
        episode.relative_dir,
        current.min_mcap_bytes - 1,
        episode.metadata_size,
        episode.metadata,
    )
    boundary = Episode(
        episode.source_root,
        episode.directory,
        episode.relative_dir,
        current.min_mcap_bytes,
        episode.metadata_size,
        episode.metadata,
    )

    assert any("MCAP 小于" in issue.reason for issue in coarse_issues(below, current))
    assert not any(
        "MCAP 小于" in issue.reason for issue in coarse_issues(boundary, current)
    )


def test_coarse_gate_accepts_explicit_rgb_only_contract(tmp_path: Path) -> None:
    episode = write_episode(tmp_path, "episode")
    episode.metadata.clear()
    episode.metadata.update(metadata("episode", rgb_only=True))

    assert coarse_issues(episode, policy()) == ()

    episode.metadata["streams"].pop()
    issues = coarse_issues(episode, policy())
    assert any("camera_overview_color" in issue.reason for issue in issues)


def test_invalid_episode_moves_to_abort_after_enter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_episode(tmp_path, "bad", size=1)
    command = SyncCommand(tmp_path, "bos:/bucket/task/", ())
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    grouped = _collect((command,), policy(min_mcap_bytes=2))
    assert grouped[command] == ()
    assert (tmp_path / ABORT_DIRECTORY / "bad" / "episode.mcap").is_file()


def test_sample_is_ten_percent_capped_at_fifteen(tmp_path: Path) -> None:
    episodes = tuple(write_episode(tmp_path, f"episode-{index:03}") for index in range(200))
    selected = choose_samples(episodes, policy())
    assert len(selected) == 15
    assert episodes[0] in selected
    assert episodes[-1] in selected


def test_parse_bos_listing_extracts_object_sizes() -> None:
    listing = json.dumps({"objects": [{"key": "a/episode.mcap", "size": 42}]})
    assert parse_bos_listing(listing, "bos:/bucket/a/") == {
        "bos:/bucket/a/episode.mcap": 42
    }


def test_bce_progress_probe_accumulates_only_positive_deltas(tmp_path: Path) -> None:
    task_dir = tmp_path / "task_progress"
    multipart_dir = tmp_path / "multiupload_infos" / "ak"
    task_dir.mkdir()
    multipart_dir.mkdir(parents=True)
    task = task_dir / "SYNC-1-1"
    multipart = multipart_dir / "upload"
    task.write_text(json.dumps({"syncSuccBytes": 10}))
    multipart.write_text(
        json.dumps(
            {
                "fileSize": 100,
                "partSize": 10,
                "completePartList": [{}],
            }
        )
    )
    probe = BceProgressProbe(tmp_path)

    assert probe.sample() == 0
    task.write_text(json.dumps({"syncSuccBytes": 30}))
    multipart.write_text(
        json.dumps(
            {
                "fileSize": 100,
                "partSize": 10,
                "completePartList": [{}, {}, {}],
            }
        )
    )
    assert probe.sample() == 40
    multipart.unlink()
    assert probe.sample() == 40


class ConcurrentRunner:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.argv: list[list[str]] = []
        self.lock = threading.Lock()

    def stream(self, argv, stall_seconds, log, progress) -> int:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.argv.append(list(argv))
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return 0


def test_upload_round_runs_bounded_episode_processes(tmp_path: Path) -> None:
    episodes = tuple(
        write_episode(tmp_path, f"episode-{index}") for index in range(3)
    )
    parent = SyncCommand(
        tmp_path,
        "bos:/bucket/task/",
        ("--concurrency", "16", "--sync-type", "dest-not-exist"),
    )
    runner = ConcurrentRunner()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    results = _run_upload_round(
        tuple(EpisodeUploadWork(parent, episode) for episode in episodes),
        policy(),
        runner,
        True,
        log_dir,
        0,
        StaticProgress(),
    )

    assert set(results.values()) == {0}
    assert runner.max_active == 2
    assert len(runner.argv) == 3
    assert all(
        argv[argv.index("--concurrency") + 1] == "8" for argv in runner.argv
    )


class FakeBos(BosClient):
    def __init__(self, objects: dict[str, int], values: dict[str, dict[str, object]]) -> None:
        self.objects = objects
        self.values = values

    def list(self, prefix: str) -> dict[str, int]:
        return self.objects

    def metadata(self, path: str, target: Path) -> dict[str, object]:
        return self.values[path]


def test_post_upload_reuses_coarse_gate(tmp_path: Path) -> None:
    episode = write_episode(tmp_path, "episode")
    command = SyncCommand(tmp_path, "bos:/bucket/task/", ())
    mcap = remote_path(command, episode, "episode.mcap")
    metadata_path = remote_path(command, episode, "metadata.json")
    client = FakeBos(
        {mcap: episode.mcap_size, metadata_path: episode.metadata_size},
        {metadata_path: episode.metadata},
    )
    verify_bos(command, (episode,), policy(), client)
    client.values[metadata_path]["streams"] = []
    with pytest.raises(ValueError, match="BOS 粗筛失败"):
        verify_bos(command, (episode,), policy(), client)


class FakeRunner:
    def __init__(self, command: SyncCommand, episode: Episode) -> None:
        self.command = command
        self.episode = episode
        self.uploaded = False

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--sync-type time-size")
        if argv[2] == "ls":
            objects = []
            if self.uploaded:
                objects = [
                    {
                        "key": remote_path(
                            self.command, self.episode, "episode.mcap"
                        ).removeprefix("bos:/bucket/"),
                        "size": self.episode.mcap_size,
                    },
                    {
                        "key": remote_path(
                            self.command, self.episode, "metadata.json"
                        ).removeprefix("bos:/bucket/"),
                        "size": self.episode.metadata_size,
                    },
                ]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"objects": objects}))
        if argv[2] == "cp":
            Path(argv[4]).write_text(json.dumps(self.episode.metadata))
            return subprocess.CompletedProcess(argv, 0, "")
        raise AssertionError(argv)

    def stream(
        self,
        argv: list[str],
        stall_seconds: int,
        log: io.TextIOBase,
        progress,
    ) -> int:
        self.uploaded = True
        return 0


class StaticProgress:
    def sample(self) -> int:
        return 0


def test_progress_watchdog_resets_only_for_completed_bytes() -> None:
    class MutableProgress:
        value = 0

        def sample(self) -> int:
            return self.value

    progress = MutableProgress()
    now = [0.0]
    watchdog = UploadProgressWatchdog(progress, 10, lambda: now[0])

    now[0] = 9.0
    assert not watchdog.stalled()
    progress.value = 1
    assert not watchdog.stalled()
    now[0] = 18.0
    assert not watchdog.stalled()
    now[0] = 19.0
    assert watchdog.stalled()


@pytest.mark.parametrize(("full_check", "expected_checks"), ((True, 1), (False, 0)))
def test_execute_runs_preflight_upload_and_postcheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_check: bool,
    expected_checks: int,
) -> None:
    episode = write_episode(tmp_path, "episode")
    command = SyncCommand(tmp_path, "bos:/bucket/task/", ("--concurrency", "16"))
    runner = FakeRunner(command, episode)
    current = policy()
    current = UploadPolicy(
        min_mcap_bytes=current.min_mcap_bytes,
        sample_fraction=current.sample_fraction,
        max_samples=current.max_samples,
        conversion_profile=current.conversion_profile,
        rgb_encoding=current.rgb_encoding,
        depth_encoding=current.depth_encoding,
        width=current.width,
        height=current.height,
        processes=current.processes,
        concurrency_per_process=current.concurrency_per_process,
        progress_stall_seconds=current.progress_stall_seconds,
        retry_delays_seconds=(),
    )
    output = io.StringIO()
    checked: list[Episode] = []
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    monkeypatch.setattr(sys, "stdout", output)
    execute(
        (command,),
        current,
        full_check=full_check,
        runner=runner,
        deep_validate=checked.append,
        progress_factory=StaticProgress,
    )
    assert runner.uploaded
    assert len(checked) == expected_checks
    assert "UPLOAD COMPLETE" in output.getvalue()
    assert f"full_check={str(full_check).lower()}" in output.getvalue()


class RetryRunner:
    def __init__(self, command: SyncCommand, episodes: tuple[Episode, ...]) -> None:
        self.command = command
        self.episodes = {episode.episode_id: episode for episode in episodes}
        self.attempts = {episode.episode_id: 0 for episode in episodes}
        self.uploaded: set[str] = set()
        self.lock = threading.Lock()

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "dest-not-exist")
        if argv[2] == "ls":
            objects = []
            for episode_id in sorted(self.uploaded):
                episode = self.episodes[episode_id]
                for name, size in (
                    ("episode.mcap", episode.mcap_size),
                    ("metadata.json", episode.metadata_size),
                ):
                    objects.append(
                        {
                            "key": remote_path(
                                self.command,
                                episode,
                                name,
                            ).removeprefix("bos:/bucket/"),
                            "size": size,
                        }
                    )
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"objects": objects}),
            )
        if argv[2] == "cp":
            episode_id = argv[3].rstrip("/").split("/")[-2]
            Path(argv[4]).write_text(json.dumps(self.episodes[episode_id].metadata))
            return subprocess.CompletedProcess(argv, 0, "")
        raise AssertionError(argv)

    def stream(self, argv, stall_seconds, log, progress) -> int:
        episode_id = argv[4].rstrip("/").split("/")[-1]
        with self.lock:
            self.attempts[episode_id] += 1
            attempt = self.attempts[episode_id]
            if episode_id != "episode-a" or attempt > 1:
                self.uploaded.add(episode_id)
                return 0
        return 1


def test_execute_retries_only_unverified_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episodes = (
        write_episode(tmp_path, "episode-a"),
        write_episode(tmp_path, "episode-b"),
    )
    command = SyncCommand(tmp_path, "bos:/bucket/task/", ("--concurrency", "16"))
    runner = RetryRunner(command, episodes)
    current = policy()
    current = UploadPolicy(
        min_mcap_bytes=current.min_mcap_bytes,
        sample_fraction=current.sample_fraction,
        max_samples=current.max_samples,
        conversion_profile=current.conversion_profile,
        rgb_encoding=current.rgb_encoding,
        depth_encoding=current.depth_encoding,
        width=current.width,
        height=current.height,
        processes=2,
        concurrency_per_process=8,
        progress_stall_seconds=current.progress_stall_seconds,
        retry_delays_seconds=(0,),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    execute(
        (command,),
        current,
        full_check=False,
        runner=runner,
        progress_factory=StaticProgress,
    )

    assert runner.attempts == {"episode-a": 2, "episode-b": 1}
