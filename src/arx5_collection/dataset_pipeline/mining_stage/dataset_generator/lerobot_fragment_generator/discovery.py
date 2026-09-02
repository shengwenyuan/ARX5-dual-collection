from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def discover_episode_dirs(
    input_roots: Path | Iterable[Path],
    outcomes: set[str] | None = None,
) -> list[Path]:
    roots = (input_roots,) if isinstance(input_roots, Path) else tuple(input_roots)
    discovered: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(root)
        if (root / "episode.mcap").is_file() and (root / "metadata.json").is_file():
            candidates = (root,)
        else:
            candidates = tuple(
                path.parent
                for path in root.rglob("episode.mcap")
                if (path.parent / "metadata.json").is_file()
            )
        for episode_dir in candidates:
            if outcomes is not None:
                metadata = json.loads((episode_dir / "metadata.json").read_text())
                if metadata.get("outcome") not in outcomes:
                    continue
            discovered.append(episode_dir)

    by_id: dict[str, Path] = {}
    for episode_dir in sorted(set(discovered), key=str):
        episode_id = episode_dir.name
        if previous := by_id.get(episode_id):
            raise ValueError(
                f"duplicate episode id {episode_id!r}: {previous} and {episode_dir}"
            )
        by_id[episode_id] = episode_dir
    return [by_id[episode_id] for episode_id in sorted(by_id)]


def episode_lookup(input_roots: Path | Iterable[Path]) -> dict[str, Path]:
    return {path.name: path for path in discover_episode_dirs(input_roots)}
