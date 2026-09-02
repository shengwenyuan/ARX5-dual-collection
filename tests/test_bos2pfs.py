from pathlib import Path

import pytest

from arx5_collection.bos2pfs import BucketLinkCreated
from arx5_collection.bos2pfs import BucketLinkSpec
from arx5_collection.bos2pfs import BucketLinkStatus
from arx5_collection.bos2pfs import mounted_report_path
from arx5_collection.bos2pfs import validate_report
from arx5_collection.bos2pfs import wait_for_bucket_link


class FakeClient:
    def __init__(self, statuses: list[BucketLinkStatus]) -> None:
        self.statuses = statuses
        self.created = 0

    def create(self, spec: BucketLinkSpec, name: str) -> BucketLinkCreated:
        self.created += 1
        return BucketLinkCreated("dflow-1", "request-1")

    def describe(self, instance_id: str, bucket_link_id: str) -> BucketLinkStatus:
        return self.statuses.pop(0)


def spec(tmp_path: Path) -> BucketLinkSpec:
    return BucketLinkSpec(
        endpoint="pfs.bj.baidubce.com",
        instance_id="pfs-test",
        bucket="bucket",
        bucket_prefix="task/2026-09-01/",
        pfs_path="/swy/tmp/task-0901/2026-09-01",
        mounted_path=str(tmp_path / "tmp/task-0901/2026-09-01"),
        throughput_limit_bytes=1_572_864_000,
        conflict_policy=2,
        report_prefix=".baidu_l2_bucketlink_dflow/arx5/",
    )


def test_wait_persists_identity_and_resumes_without_duplicate_create(tmp_path: Path) -> None:
    journal = tmp_path / "state.json"
    first = FakeClient(
        [
            BucketLinkStatus(1, 20),
            BucketLinkStatus(
                2,
                100,
                "bos://bucket/report",
                source="bos://bucket/task/2026-09-01/",
                destination="/swy/tmp/task-0901/2026-09-01",
            ),
        ]
    )
    result = wait_for_bucket_link(spec(tmp_path), "run-1", journal, first, sleep=lambda _: None)
    assert result.status == 2
    assert first.created == 1

    resumed = FakeClient(
        [
            BucketLinkStatus(
                2,
                100,
                "bos://bucket/report",
                source="bos://bucket/task/2026-09-01/",
                destination="/swy/tmp/task-0901/2026-09-01",
            )
        ]
    )
    wait_for_bucket_link(
        spec(tmp_path),
        "run-1",
        journal,
        resumed,
        sleep=lambda _: None,
        create_missing=False,
    )
    assert resumed.created == 0


def test_failed_transfer_and_report_never_pass_the_gate(tmp_path: Path) -> None:
    client = FakeClient([BucketLinkStatus(3, error="permission denied")])
    with pytest.raises(RuntimeError, match="permission denied"):
        wait_for_bucket_link(
            spec(tmp_path), "run-1", tmp_path / "state.json", client, sleep=lambda _: None
        )
    with pytest.raises(RuntimeError, match="2 failed files"):
        validate_report("totalCount: 3\nskippedCount: 0\nfailedCount: 2\n")


def test_success_rejects_server_side_request_drift(tmp_path: Path) -> None:
    client = FakeClient(
        [
            BucketLinkStatus(
                2,
                100,
                "bos://bucket/report",
                source="bos://bucket/another/",
                destination="/swy/tmp/task-0901/2026-09-01",
            )
        ]
    )
    with pytest.raises(RuntimeError, match="source differs"):
        wait_for_bucket_link(
            spec(tmp_path), "run-1", tmp_path / "state.json", client, sleep=lambda _: None
        )


def test_report_parser_and_mounted_path() -> None:
    assert validate_report(
        "Summary\ntotalCount: 4\nskippedCount: 0\nfailedCount: 0\n"
    ) == {"totalCount": 4, "skippedCount": 0, "failedCount": 0}
    assert mounted_report_path("bos://bucket/reports/result", Path("/mnt/bos")) == Path(
        "/mnt/bos/bucket/reports/result"
    )
