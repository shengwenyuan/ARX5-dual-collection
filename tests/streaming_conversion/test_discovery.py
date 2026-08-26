from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from arx5_collection.streaming_conversion.config import SourceConfig
from arx5_collection.streaming_conversion.discovery import discover_episodes


class EpisodeDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovers_any_depth_and_prunes_exact_block_names(self) -> None:
        _episode(self.root / "task" / "date" / "nested" / "episode-a", "episode-a", 7)
        _episode(self.root / "task" / "date" / "abort" / "episode-b", "episode-b", 11)
        _episode(self.root / "task" / "date" / "aborted" / "episode-c", "episode-c", 13)

        result = discover_episodes(
            SourceConfig(
                root=self.root,
                include_paths=(Path("task/date"),),
                block=("abort",),
            )
        )

        self.assertEqual(
            [item.episode_id for item in result.candidates],
            ["episode-a", "episode-c"],
        )
        self.assertEqual(result.total_mcap_bytes, 20)
        self.assertEqual(result.blocked_dirs, (Path("task/date/abort"),))
        self.assertEqual(
            result.candidates[0].source_session_id,
            "w4/2026-08-25/task/date/nested",
        )

    def test_episode_boundary_stops_recursive_descent(self) -> None:
        parent = self.root / "task" / "episode-parent"
        _episode(parent, "episode-parent", 3)
        _episode(parent / "episode-child", "episode-child", 5)

        result = discover_episodes(
            SourceConfig(self.root, (Path("task"),), ())
        )

        self.assertEqual([item.episode_id for item in result.candidates], ["episode-parent"])

    def test_rejects_overlapping_include_paths(self) -> None:
        (self.root / "task" / "date").mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "include paths overlap"):
            discover_episodes(
                SourceConfig(
                    self.root,
                    (Path("task"), Path("task/date")),
                    (),
                )
            )

    def test_rejects_duplicate_episode_ids(self) -> None:
        _episode(self.root / "one" / "same-id", "same-id", 3)
        _episode(self.root / "two" / "same-id", "same-id", 5)

        with self.assertRaisesRegex(ValueError, "duplicate episode id"):
            discover_episodes(
                SourceConfig(self.root, (Path("one"), Path("two")), ())
            )

    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as outside_temporary:
            outside = Path(outside_temporary)
            (outside / "data").mkdir()
            inside = self.root / "task"
            inside.mkdir()
            (inside / "escape").symlink_to(outside / "data", target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "escapes configured root"):
                discover_episodes(
                    SourceConfig(self.root, (Path("task"),), ())
                )

    def test_rejects_metadata_and_directory_id_mismatch(self) -> None:
        _episode(self.root / "task" / "wrong-dir", "metadata-id", 3)

        with self.assertRaisesRegex(ValueError, "does not match metadata"):
            discover_episodes(
                SourceConfig(self.root, (Path("task"),), ())
            )


def _episode(path: Path, episode_id: str, mcap_bytes: int) -> None:
    path.mkdir(parents=True)
    (path / "episode.mcap").write_bytes(b"m" * mcap_bytes)
    (path / "metadata.json").write_text(
        json.dumps(
            {
                "episode_id": episode_id,
                "collection_type": "demonstration",
                "outcome": "success",
                "station": {"id": "w4"},
                "timing": {"started_at": "2026-08-25T01:02:03Z"},
                "task": {"id": "fold", "description": "folding the cloth"},
            }
        )
    )


if __name__ == "__main__":
    unittest.main()
