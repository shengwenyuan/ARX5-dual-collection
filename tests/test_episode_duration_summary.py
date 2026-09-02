from __future__ import annotations

import json
from pathlib import Path

import pytest


from arx5_collection.collection.episode.duration import format_duration
from arx5_collection.collection.episode.duration import summarize


def write_episode(root: Path, name: str, outcome: str, duration_s: float) -> None:
    episode = root / outcome / name
    episode.mkdir(parents=True)
    (episode / "metadata.json").write_text(
        json.dumps(
            {
                "episode_id": name,
                "outcome": outcome,
                "timing": {"duration_s": duration_s},
            }
        )
    )


def test_counts_every_episode_below_the_root(tmp_path: Path) -> None:
    write_episode(tmp_path, "one", "success", 10.25)
    write_episode(tmp_path, "two", "success", 20.5)
    write_episode(tmp_path, "three", "aborted", 4.0)

    summary = summarize(tmp_path)

    assert summary.episode_count == 3
    assert summary.duration_s == pytest.approx(34.75)


def test_block_prunes_exact_directory_names_only(tmp_path: Path) -> None:
    write_episode(tmp_path / "abort", "blocked", "aborted", 100.0)
    write_episode(tmp_path / "logs" / "nested", "blocked-log", "success", 100.0)
    write_episode(tmp_path / "abortions", "kept", "success", 10.0)

    summary = summarize(tmp_path, ("abort", "logs"))

    assert summary.episode_count == 1
    assert summary.duration_s == pytest.approx(10.0)


def test_episode_directory_is_not_searched_below_its_metadata(tmp_path: Path) -> None:
    write_episode(tmp_path, "episode", "success", 10.0)
    write_episode(tmp_path / "success" / "episode", "nested-copy", "success", 20.0)

    summary = summarize(tmp_path)

    assert summary.episode_count == 1
    assert summary.duration_s == pytest.approx(10.0)


def test_invalid_episode_metadata_fails_instead_of_silently_skipping(
    tmp_path: Path,
) -> None:
    write_episode(tmp_path, "one", "success", 1.0)
    broken = tmp_path / "broken" / "metadata.json"
    broken.parent.mkdir()
    broken.write_text("{}")

    with pytest.raises(ValueError, match="episode_id"):
        summarize(tmp_path)


def test_duration_format_does_not_wrap_after_24_hours() -> None:
    assert format_duration(90_061.125) == "25:01:01.125"
