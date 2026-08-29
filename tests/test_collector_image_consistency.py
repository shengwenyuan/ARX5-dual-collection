from pathlib import Path

import pytest

from tools.validate_collector_image_consistency import ImageIdentity
from tools.validate_collector_image_consistency import require_consistent


ROOT = Path(__file__).resolve().parents[1]


def identity(image: str, revision: str, digest: str = "a" * 64) -> ImageIdentity:
    return ImageIdentity(image, revision, 42, digest)


def test_collector_targets_inherit_one_installed_application_layer() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile.bringup").read_text()

    assert "FROM runtime-base AS production" in dockerfile
    assert "FROM runtime-base AS dagger-collector" in dockerfile
    assert dockerfile.count("COPY src/ /opt/arx5-runtime/src/") == 1
    assert dockerfile.count("/opt/arx5-runtime") >= 1
    assert "mcap-linux-amd64" in dockerfile
    assert "/usr/local/bin/mcap --version" in dockerfile


def test_matching_revision_and_package_manifest_pass() -> None:
    require_consistent(identity("production", "abc123"), identity("dagger", "abc123"))


@pytest.mark.parametrize(
    ("left", "right", "message"),
    (
        (
            identity("production", "abc123"),
            identity("dagger", "def456"),
            "source revision mismatch",
        ),
        (
            identity("production", "abc123"),
            identity("dagger", "abc123", "b" * 64),
            "shared Python package mismatch",
        ),
    ),
)
def test_mismatched_collectors_fail(
    left: ImageIdentity,
    right: ImageIdentity,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        require_consistent(left, right)
