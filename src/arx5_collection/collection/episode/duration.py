from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Summary:
    episode_count: int
    duration_s: float


def summarize(
    root: Path,
    blocked_names: Iterable[str] = (),
) -> Summary:
    if not root.is_dir():
        raise ValueError(f"episode root is not a directory: {root}")
    blocked = frozenset(blocked_names)
    _validate_blocked_names(blocked)

    episode_count = 0
    duration_s = 0.0
    for path in iter_episode_metadata(root, blocked):
        episode_count += 1
        duration_s += _read_duration(path)
    return Summary(episode_count, duration_s)


def iter_episode_metadata(
    root: Path,
    blocked_names: frozenset[str],
) -> Iterator[Path]:
    """Yield one metadata file per Episode without entering blocked directories."""
    for directory, child_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=_raise_os_error,
    ):
        child_names[:] = sorted(
            name for name in child_names if name not in blocked_names
        )
        if "metadata.json" not in file_names:
            continue
        yield Path(directory) / "metadata.json"
        child_names.clear()


def _validate_blocked_names(blocked_names: frozenset[str]) -> None:
    invalid = sorted(
        name
        for name in blocked_names
        if not name or name in {".", ".."} or Path(name).name != name
    )
    if invalid:
        raise ValueError(f"blocked values must be directory names: {invalid}")


def _raise_os_error(error: OSError) -> None:
    raise error


def _read_duration(path: Path) -> float:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Episode metadata {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Episode metadata must be an object: {path}")

    episode_id = payload.get("episode_id")
    if not isinstance(episode_id, str) or not episode_id:
        raise ValueError(f"Episode metadata has no valid episode_id: {path}")
    timing = payload.get("timing")
    duration_s = timing.get("duration_s") if isinstance(timing, dict) else None
    if (
        isinstance(duration_s, bool)
        or not isinstance(duration_s, (int, float))
        or not math.isfinite(duration_s)
        or duration_s < 0
    ):
        raise ValueError(f"Episode {episode_id} has invalid timing.duration_s")
    return float(duration_s)


def format_duration(duration_s: float) -> str:
    total_ms = round(duration_s * 1000)
    hours, remainder_ms = divmod(total_ms, 3_600_000)
    minutes, remainder_ms = divmod(remainder_ms, 60_000)
    seconds, milliseconds = divmod(remainder_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def render(root: Path, summary: Summary) -> str:
    return "\n".join(
        (
            f"ROOT={root.resolve()}",
            f"EPISODES={summary.episode_count}",
            f"DURATION_SECONDS={summary.duration_s:.3f}",
            f"DURATION={format_duration(summary.duration_s)}",
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sum committed Episode durations below one directory"
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--block",
        action="append",
        default=[],
        metavar="DIRECTORY_NAME",
        help="skip an exactly matching directory name; may be repeated",
    )
    args = parser.parse_args()
    try:
        summary = summarize(args.directory, args.block)
    except ValueError as error:
        parser.error(str(error))
    print(render(args.directory, summary))


if __name__ == "__main__":
    main()
