from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arx5_collection.dataset_pipeline.cli import _episode_dirs
from arx5_collection.dataset_pipeline.cli import build_parser


class DatasetCliTest(unittest.TestCase):
    def test_every_subcommand_registers_a_handler(self) -> None:
        commands = (
            [
                "classify-dagger",
                "--input-root",
                "/input",
                "--audit-root",
                "/audit",
                "--recipe",
                "builtin:pi05-equal-eef-v3",
            ],
            [
                "to-lerobot",
                "--input-root",
                "/input",
                "--selection-dir",
                "/selection",
                "--output-root",
                "/output",
                "--repo-id",
                "local/data",
            ],
            ["validate-pi05", "--dataset-root", "/data", "--repo-id", "local/data"],
            ["build", "--config", "/config.toml"],
            [
                "bucketlink-to-lerobot",
                "--config",
                "/config.toml",
                "--run-id",
                "run-1",
            ],
            ["compose-lerobot", "--config", "/composition.toml"],
        )

        parser = build_parser()
        for argv in commands:
            with self.subTest(command=argv[0]):
                self.assertTrue(callable(parser.parse_args(argv).handler))

    def test_discovers_nested_committed_episodes_and_filters_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            committed = root / "batch-a" / "episode-a"
            committed.parent.mkdir()
            committed.mkdir()
            (committed / "episode.mcap").touch()
            (committed / "metadata.json").write_text('{"outcome": "success"}')
            aborted = root / "batch-b" / "episode-b"
            aborted.mkdir(parents=True)
            (aborted / "episode.mcap").touch()
            (aborted / "metadata.json").write_text('{"outcome": "aborted"}')

            self.assertEqual(_episode_dirs(root, {"success"}), [committed])

    def test_combines_roots_and_rejects_duplicate_episode_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = [root / "one", root / "two"]
            for item in roots:
                episode = item / "same-id"
                episode.mkdir(parents=True)
                (episode / "episode.mcap").touch()
                (episode / "metadata.json").write_text('{"outcome": "success"}')

            with self.assertRaisesRegex(ValueError, "duplicate episode id"):
                _episode_dirs(roots)


if __name__ == "__main__":
    unittest.main()
