from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .models import EpisodeOutcome


@dataclass(frozen=True, slots=True)
class PendingEpisode:
    episode_id: str
    partial_dir: Path
    final_dir: Path
    mcap_path: Path
    metadata_path: Path


@dataclass(frozen=True, slots=True)
class StoredEpisode:
    episode_id: str
    directory: Path
    mcap_path: Path
    metadata_path: Path


class EpisodeStore:
    def __init__(
        self,
        root: Path,
        min_free_bytes: int = 0,
        fail_directory: str = "fail",
    ) -> None:
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must not be negative")
        if not fail_directory or Path(fail_directory).name != fail_directory:
            raise ValueError("fail_directory must be a single path name")
        self.root = root
        self.min_free_bytes = min_free_bytes
        self.fail_directory = fail_directory

    def prepare(self, episode_id: str) -> PendingEpisode:
        self._validate_episode_id(episode_id)
        self.root.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(self.root).free
        if free_bytes < self.min_free_bytes:
            raise OSError(
                f"insufficient disk space: {free_bytes} < {self.min_free_bytes} bytes"
            )

        partial_dir = self.root / f".{episode_id}.partial"
        final_dir = self.root / episode_id
        if partial_dir.exists() or final_dir.exists():
            raise FileExistsError(f"episode already exists: {episode_id}")

        partial_dir.mkdir()
        return PendingEpisode(
            episode_id=episode_id,
            partial_dir=partial_dir,
            final_dir=final_dir,
            mcap_path=partial_dir / "episode.mcap",
            metadata_path=partial_dir / "metadata.json",
        )

    def commit(
        self,
        pending: PendingEpisode,
        outcome: EpisodeOutcome,
    ) -> StoredEpisode:
        if not pending.partial_dir.is_dir():
            raise FileNotFoundError(pending.partial_dir)

        entries = {path.name for path in pending.partial_dir.iterdir()}
        if entries != {"episode.mcap", "metadata.json"}:
            raise RuntimeError("partial episode must contain exactly MCAP and metadata JSON")
        if not pending.mcap_path.is_file() or not pending.metadata_path.is_file():
            raise RuntimeError("episode artifacts must be regular files")

        outcome_roots = {
            EpisodeOutcome.SUCCESS: self.root,
            EpisodeOutcome.FAIL: self.root / self.fail_directory,
            EpisodeOutcome.ABORTED: self.root / "aborted",
        }
        final_dir = outcome_roots[outcome] / pending.episode_id
        if final_dir.exists():
            raise FileExistsError(f"episode already exists: {pending.episode_id}")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        pending.partial_dir.rename(final_dir)
        return StoredEpisode(
            episode_id=pending.episode_id,
            directory=final_dir,
            mcap_path=final_dir / "episode.mcap",
            metadata_path=final_dir / "metadata.json",
        )

    def list_partials(self) -> tuple[Path, ...]:
        if not self.root.exists():
            return ()
        return tuple(
            path
            for path in sorted(self.root.glob(".*.partial"))
            if path.is_dir()
        )

    @staticmethod
    def _validate_episode_id(episode_id: str) -> None:
        if not episode_id or Path(episode_id).name != episode_id or episode_id in {".", ".."}:
            raise ValueError("episode_id must be a single path name")
