from __future__ import annotations

import hashlib
from pathlib import Path


_DIGEST_DOMAIN = b"ARX5_CHECKPOINT_TREE_SHA256_V1\0"


def checkpoint_tree_sha256(checkpoint: str | Path) -> str:
    """Hash a checkpoint directory deterministically without creating a manifest."""
    root = Path(checkpoint)
    if not root.is_dir():
        raise ValueError(f"checkpoint is not a directory: {root}")
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    if any(path.is_symlink() for path in entries):
        raise ValueError("checkpoint must not contain symbolic links")
    files = [path for path in entries if path.is_file()]
    if not files:
        raise ValueError("checkpoint directory contains no files")

    digest = hashlib.sha256(_DIGEST_DOMAIN)
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        size = path.stat().st_size
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()
