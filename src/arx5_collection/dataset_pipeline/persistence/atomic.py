from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterator


@contextmanager
def staged_directory(target: Path, *, precreate: bool = True) -> Iterator[Path]:
    """Publish a new directory with one same-filesystem rename.

    This guarantees atomic visibility to readers. It does not provide fsync-based
    crash durability or coordination between concurrent writers.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    if not precreate:
        temporary.rmdir()
    try:
        yield temporary
        if not temporary.is_dir():
            raise FileNotFoundError(f"staged directory was not created: {temporary}")
        if target.exists():
            raise FileExistsError(target)
        temporary.chmod(0o755)
        os.replace(temporary, target)
    except BaseException:
        if temporary.is_dir():
            shutil.rmtree(temporary)
        elif temporary.exists():
            temporary.unlink()
        raise
