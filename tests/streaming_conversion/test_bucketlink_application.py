from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from arx5_collection.bos2pfs import BucketLinkCreated, BucketLinkStatus
from arx5_collection.bucketlink_conversion import BucketLinkRunRequest
from arx5_collection.bucketlink_conversion import execute_bucketlink_conversion
from arx5_collection.bucketlink_conversion import load_bucketlink_config
from arx5_collection.streaming_conversion.discovery import discover_episodes


class TTY(StringIO):
    def isatty(self) -> bool:
        return True


class SuccessfulClient:
    def __init__(self, target: Path) -> None:
        self.target = target

    def create(self, spec, name):
        episode = self.target / "episode-a"
        episode.mkdir()
        (episode / "episode.mcap").write_bytes(b"mcap")
        (episode / "metadata.json").write_text(
            json.dumps(
                {
                    "episode_id": "episode-a",
                    "outcome": "success",
                    "task": {"id": "task", "description": "do task"},
                    "station": {"id": "w4"},
                    "timing": {"started_at": "2026-09-01T00:00:00Z"},
                }
            )
        )
        return BucketLinkCreated("dflow-1", "request-1")

    def describe(self, instance_id, bucket_link_id):
        return BucketLinkStatus(
            2,
            100,
            "bos://bucket/report",
            source="bos://bucket/task/2026-09-01/",
            destination="/swy/tmp/task-0901/2026-09-01",
        )


class DaggerFailClient(SuccessfulClient):
    def create(self, spec, name):
        episode = self.target / "dagger_fail" / "episode-a"
        episode.mkdir(parents=True)
        (episode / "episode.mcap").write_bytes(b"mcap")
        (episode / "metadata.json").write_text(
            json.dumps(
                {
                    "episode_id": "episode-a",
                    "collection_type": "dagger",
                    "outcome": "fail",
                    "task": {"id": "task", "description": "do task"},
                    "station": {"id": "w4"},
                    "timing": {"started_at": "2026-09-01T00:00:00Z"},
                }
            )
        )
        return BucketLinkCreated("dflow-1", "request-1")


def write_config(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "tmp" / "task-0901"
    target = source / "2026-09-01"
    config = tmp_path / "bucketlink.toml"
    config.write_text(
        f'''schema_version = 1

[bucketlink]
endpoint = "pfs.bj.baidubce.com"
instance_id = "pfs-test"
bucket = "bucket"
bucket_prefix = "task/2026-09-01/"
pfs_path = "/swy/tmp/task-0901/2026-09-01"
mounted_path = "{target}"
throughput_limit_bytes = 1572864000
conflict_policy = 2
report_prefix = ".baidu_l2_bucketlink_dflow/arx5/"

[source]
root = "{source}"
include_paths = ["2026-09-01"]
block = ["fail", "abort", "logs"]

[runtime]
pfs_root = "{tmp_path}"
streaming_root = "{tmp_path / 'streaming'}"
conversion_workers = 4
temporary_hard_max_bytes = 3
min_free_bytes = 0

[output]
lerobot_root = "{tmp_path / 'lerobot'}"
dataset_name = "task_2026-09-01"
repo_id = "local/task_2026-09-01"

[recipe]
name = "pi05-equal-eef-v3"
profile = "config/conversion.pi05-equal-eef-v3.toml"
task_source = "metadata.task.description"
'''
    )
    return config, target


def test_config_fixes_direct_mode_and_validates_mount_mapping(tmp_path: Path) -> None:
    config_path, target = write_config(tmp_path)
    spec, conversion = load_bucketlink_config(config_path)
    assert spec.mounted_path == str(target)
    assert conversion.source.materialization == "direct"


def test_config_rejects_a_user_materialization_switch(tmp_path: Path) -> None:
    config_path, _ = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text().replace(
            '[source]\n', '[source]\nmaterialization = "copy"\n'
        )
    )
    with pytest.raises(ValueError, match="omit fixed materialization"):
        load_bucketlink_config(config_path)


def test_successful_transfer_enters_existing_conversion_boundary(tmp_path: Path) -> None:
    config_path, target = write_config(tmp_path)
    sentinel = object()
    with patch(
        "arx5_collection.bucketlink_conversion.execute_streaming_config",
        return_value=sentinel,
    ) as execute:
        result = execute_bucketlink_conversion(
            BucketLinkRunRequest(config_path, tmp_path / "lerobot/output", "run-1", None),
            TTY("\n"),
            TTY(),
            client=SuccessfulClient(target),
            report_reader=lambda _: "totalCount: 2\nskippedCount: 0\nfailedCount: 0\n",
        )
    assert result is sentinel
    assert execute.call_args.args[0].source.materialization == "direct"
    assert (target / "episode-a/episode.mcap").is_file()


def test_dagger_fail_survives_bucketlink_discovery_gate(tmp_path: Path) -> None:
    config_path, target = write_config(tmp_path)
    sentinel = object()

    def assert_discovery(config, *_args, **_kwargs):
        discovery = discover_episodes(config.source)
        assert [item.relative_dir.as_posix() for item in discovery.candidates] == [
            "2026-09-01/dagger_fail/episode-a"
        ]
        candidate = discovery.candidates[0]
        assert (candidate.collection_type, candidate.outcome) == ("dagger", "fail")
        return sentinel

    with patch(
        "arx5_collection.bucketlink_conversion.execute_streaming_config",
        side_effect=assert_discovery,
    ):
        result = execute_bucketlink_conversion(
            BucketLinkRunRequest(config_path, tmp_path / "lerobot/output", "run-1", None),
            TTY("\n"),
            TTY(),
            client=DaggerFailClient(target),
            report_reader=lambda _: "totalCount: 2\nskippedCount: 0\nfailedCount: 0\n",
        )

    assert result is sentinel


def test_report_failure_blocks_conversion(tmp_path: Path) -> None:
    config_path, target = write_config(tmp_path)
    with (
        patch("arx5_collection.bucketlink_conversion.execute_streaming_config") as execute,
        pytest.raises(RuntimeError, match="failed files"),
    ):
        execute_bucketlink_conversion(
            BucketLinkRunRequest(config_path, tmp_path / "lerobot/output", "run-1", None),
            TTY("\n"),
            TTY(),
            client=SuccessfulClient(target),
            report_reader=lambda _: "totalCount: 2\nskippedCount: 0\nfailedCount: 1\n",
        )
    execute.assert_not_called()
