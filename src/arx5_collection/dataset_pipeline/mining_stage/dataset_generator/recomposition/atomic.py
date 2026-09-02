from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from typing import Iterator

from arx5_collection.dataset_pipeline.persistence.artifacts import write_json


@contextmanager
def preserved_staging_directory(target: Path) -> Iterator[Path]:
    """Atomically publish a composition, retaining failed staging for diagnosis."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.composition.", dir=target.parent)
    )
    try:
        yield staging
        if not staging.is_dir():
            raise FileNotFoundError(f"composition staging disappeared: {staging}")
        if target.exists():
            raise FileExistsError(target)
        staging.chmod(0o755)
        os.replace(staging, target)
    except BaseException as error:
        try:
            reports = staging / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            write_json(
                reports / "failure.json",
                {
                    "schema_version": 1,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "detail": str(error),
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "staging": str(staging),
                    "intended_output": str(target),
                },
            )
        except OSError:
            pass
        raise
