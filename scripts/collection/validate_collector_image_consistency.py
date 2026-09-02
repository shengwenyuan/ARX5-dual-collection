#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass


SOURCE_ROOT = "/opt/arx5-runtime/src/arx5_collection"
REVISION_LABEL = "org.opencontainers.image.revision"
MANIFEST_SCRIPT = r"""
import hashlib
import json
from pathlib import Path

root = Path("/opt/arx5-runtime/src/arx5_collection")
files = sorted(path for path in root.rglob("*.py") if path.is_file())
digest = hashlib.sha256()
for path in files:
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(json.dumps({"file_count": len(files), "sha256": digest.hexdigest()}))
"""


@dataclass(frozen=True, slots=True)
class ImageIdentity:
    image: str
    revision: str
    file_count: int
    source_sha256: str


def _run(argv: list[str]) -> str:
    result = subprocess.run(
        argv,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def inspect_image(image: str) -> ImageIdentity:
    labels = json.loads(
        _run(
            ["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"]
        )
    )
    revision = str(labels.get(REVISION_LABEL, "")).strip()
    if not revision or revision == "unknown":
        raise RuntimeError(f"image {image} has no concrete source revision label")
    manifest = json.loads(
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python3",
                image,
                "-c",
                MANIFEST_SCRIPT,
            ]
        )
    )
    source_sha256 = str(manifest["sha256"])
    if len(source_sha256) != hashlib.sha256().digest_size * 2:
        raise RuntimeError(f"image {image} returned an invalid source manifest")
    return ImageIdentity(
        image=image,
        revision=revision,
        file_count=int(manifest["file_count"]),
        source_sha256=source_sha256,
    )


def require_consistent(left: ImageIdentity, right: ImageIdentity) -> None:
    if left.revision != right.revision:
        raise RuntimeError(
            f"collector source revision mismatch: {left.image}={left.revision}, "
            f"{right.image}={right.revision}"
        )
    if (left.file_count, left.source_sha256) != (
        right.file_count,
        right.source_sha256,
    ):
        raise RuntimeError(
            "collector shared Python package mismatch: "
            f"{left.image}={left.file_count}/{left.source_sha256}, "
            f"{right.image}={right.file_count}/{right.source_sha256}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify production and DAgger Collectors package identical source"
    )
    parser.add_argument("--production-image", default="arx5-dual-collection:production")
    parser.add_argument("--dagger-image", default="arx5-dual-collection:dagger")
    args = parser.parse_args()

    production = inspect_image(args.production_image)
    dagger = inspect_image(args.dagger_image)
    require_consistent(production, dagger)
    print(
        "PASS Collector shared source consistency: "
        f"revision={production.revision} files={production.file_count} "
        f"sha256={production.source_sha256}"
    )


if __name__ == "__main__":
    main()
