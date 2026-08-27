from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import textwrap
import unittest

from arx5_collection.streaming_conversion.config import PrefetchRuntimeConfig
from arx5_collection.streaming_conversion.config import StreamingConversionConfig


class StreamingConfigTest(unittest.TestCase):
    def test_loads_platform_independent_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile())

            config = StreamingConversionConfig.load(path)

        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.source.root, Path("/mnt/bos/datainfra-demo"))
        self.assertEqual(
            config.source.include_paths,
            (Path("fold_cloth/2026-08-21"), Path("fold_cloth/2026-08-22")),
        )
        self.assertEqual(config.source.block, ("aborted", "logs"))
        self.assertEqual(config.runtime.workers, 20)
        self.assertEqual(config.recipe.task, "folding the cloth")
        self.assertEqual(
            config.output.dated_path(date(2026, 8, 25)),
            Path("/mnt/pfs/swy/dataset/lerobot/local/fold_cloth_2026-08-25"),
        )
        self.assertEqual(
            config.output.repo_id_for(
                Path("/mnt/pfs/swy/dataset/lerobot/local/custom")
            ),
            "local/custom",
        )

    def test_rejects_non_positive_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile().replace("workers = 20", "workers = 0"))

            with self.assertRaisesRegex(ValueError, "positive integer"):
                StreamingConversionConfig.load(path)

    def test_loads_bounded_prefetch_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "streaming.toml"
            path.write_text(_prefetch_profile(root))

            config = StreamingConversionConfig.load(path)

        self.assertEqual(config.schema_version, 2)
        self.assertIsInstance(config.runtime, PrefetchRuntimeConfig)
        runtime = config.runtime
        assert isinstance(runtime, PrefetchRuntimeConfig)
        self.assertEqual(runtime.stage_workers, 16)
        self.assertEqual(runtime.conversion_workers, 64)
        self.assertEqual(runtime.prefetch_target_bytes, 1_500_000_000_000)
        self.assertEqual(runtime.prefetch_max_bytes, 2_000_000_000_000)
        self.assertEqual(runtime.prefetch_max_episodes, 128)

    def test_prefetch_profile_enforces_paths_and_hard_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cases = (
                (
                    'streaming_root = "/outside/streaming"',
                    "must be below runtime.pfs_root",
                ),
                ("stage_workers = 33", "must not exceed 32"),
                ("conversion_workers = 113", "must not exceed 112"),
                (
                    "prefetch_max_bytes = 2_000_000_000_001",
                    "must not exceed 2000000000000",
                ),
                ("prefetch_max_episodes = 129", "must not exceed 128"),
                (
                    "prefetch_target_bytes = 2_000_000_000_000\n"
                    "prefetch_max_bytes = 1_500_000_000_000",
                    "must not exceed prefetch_max_bytes",
                ),
            )
            baseline = _prefetch_profile(root)
            originals = (
                f'streaming_root = "{root / "pfs" / "streaming"}"',
                "stage_workers = 16",
                "conversion_workers = 64",
                "prefetch_max_bytes = 2_000_000_000_000",
                "prefetch_max_episodes = 128",
                "prefetch_target_bytes = 1_500_000_000_000\n"
                "prefetch_max_bytes = 2_000_000_000_000",
            )
            for (replacement, reason), original in zip(cases, originals, strict=True):
                path = root / f"case-{len(replacement)}.toml"
                path.write_text(baseline.replace(original, replacement))
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(ValueError, reason):
                        StreamingConversionConfig.load(path)

    def test_rejects_absolute_include_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(
                _profile().replace(
                    '"fold_cloth/2026-08-21",',
                    '"/fold_cloth/2026-08-21",',
                )
            )

            with self.assertRaisesRegex(ValueError, "normalized relative paths"):
                StreamingConversionConfig.load(path)

    def test_rejects_block_path_instead_of_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile().replace('block = ["aborted", "logs"]', 'block = ["bad/logs"]'))

            with self.assertRaisesRegex(ValueError, "one path component"):
                StreamingConversionConfig.load(path)

    def test_repo_dataset_base_must_match_dataset_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(
                _profile().replace(
                    'repo_id = "local/fold_cloth"',
                    'repo_id = "local/another_name"',
                )
            )

            with self.assertRaisesRegex(ValueError, "must equal"):
                StreamingConversionConfig.load(path)


def _profile() -> str:
    return textwrap.dedent(
        """
        schema_version = 1

        [source]
        root = "/mnt/bos/datainfra-demo"
        include_paths = [
          "fold_cloth/2026-08-21",
          "fold_cloth/2026-08-22",
        ]
        block = ["aborted", "logs"]

        [runtime]
        streaming_root = "/mnt/pfs/swy/dataset/streaming"
        workers = 20

        [output]
        lerobot_root = "/mnt/pfs/swy/dataset/lerobot"
        dataset_name = "fold_cloth"
        repo_id = "local/fold_cloth"

        [recipe]
        name = "pi05-equal-eef-v3"
        profile = "config/fold_cloth.toml"
        task = "folding the cloth"
        """
    )


def _prefetch_profile(root: Path) -> str:
    pfs_root = root / "pfs"
    return textwrap.dedent(
        f'''
        schema_version = 2

        [source]
        root = "{root / "source"}"
        include_paths = ["fold_cloth/2026-08-21"]
        block = []

        [runtime]
        pfs_root = "{pfs_root}"
        streaming_root = "{pfs_root / "streaming"}"
        stage_workers = 16
        conversion_workers = 64
        prefetch_target_bytes = 1_500_000_000_000
        prefetch_max_bytes = 2_000_000_000_000
        prefetch_max_episodes = 128

        [output]
        lerobot_root = "{pfs_root / "lerobot"}"
        dataset_name = "fold_cloth"
        repo_id = "local/fold_cloth"

        [recipe]
        name = "pi05-equal-eef-v3"
        profile = "config/fold_cloth.toml"
        task = "folding the cloth"
        '''
    )


if __name__ == "__main__":
    unittest.main()
