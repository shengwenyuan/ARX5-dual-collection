from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import textwrap
import unittest

from arx5_collection.dataset_pipeline.configuration.run import BufferedRuntimeConfig
from arx5_collection.dataset_pipeline.configuration.run import PrefetchRuntimeConfig
from arx5_collection.dataset_pipeline.configuration.run import DatasetPipelineConfig


class DatasetPipelineConfigTest(unittest.TestCase):
    def test_loads_platform_independent_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile())

            config = DatasetPipelineConfig.load(path)

        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.source.root, Path("/mnt/bos/datainfra-demo"))
        self.assertEqual(
            config.source.include_paths,
            (Path("fold_cloth/2026-08-21"), Path("fold_cloth/2026-08-22")),
        )
        self.assertEqual(config.source.block, ("aborted", "logs"))
        self.assertEqual(config.source.materialization, "copy")
        self.assertEqual(config.runtime.workers, 20)
        self.assertEqual(config.recipe.task_source, "metadata.task.description")
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

    def test_loads_episode_metadata_task_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile())
            config = DatasetPipelineConfig.load(path)

        self.assertEqual(config.recipe.task_source, "metadata.task.description")

    def test_rejects_non_positive_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile().replace("workers = 20", "workers = 0"))

            with self.assertRaisesRegex(ValueError, "positive integer"):
                DatasetPipelineConfig.load(path)

    def test_loads_bounded_prefetch_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "streaming.toml"
            path.write_text(_prefetch_profile(root))

            config = DatasetPipelineConfig.load(path)

        self.assertEqual(config.schema_version, 2)
        self.assertIsInstance(config.runtime, PrefetchRuntimeConfig)
        runtime = config.runtime
        assert isinstance(runtime, PrefetchRuntimeConfig)
        self.assertEqual(runtime.stage_workers, 16)
        self.assertEqual(runtime.conversion_workers, 64)
        self.assertEqual(runtime.prefetch_target_bytes, 1_500_000_000_000)
        self.assertEqual(runtime.prefetch_max_bytes, 2_000_000_000_000)
        self.assertEqual(runtime.prefetch_max_episodes, 128)

    def test_legacy_prefetch_profile_enforces_paths_and_watermark_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cases = (
                (
                    'streaming_root = "/outside/streaming"',
                    "must be below runtime.pfs_root",
                ),
                (
                    "prefetch_target_bytes = 2_000_000_000_000\n"
                    "prefetch_max_bytes = 1_500_000_000_000",
                    "must not exceed prefetch_max_bytes",
                ),
            )
            baseline = _prefetch_profile(root)
            originals = (
                f'streaming_root = "{root / "pfs" / "streaming"}"',
                "prefetch_target_bytes = 1_500_000_000_000\n"
                "prefetch_max_bytes = 2_000_000_000_000",
            )
            for (replacement, reason), original in zip(cases, originals, strict=True):
                path = root / f"case-{len(replacement)}.toml"
                path.write_text(baseline.replace(original, replacement))
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(ValueError, reason):
                        DatasetPipelineConfig.load(path)

    def test_loads_host_independent_buffered_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "streaming.toml"
            path.write_text(_buffered_profile(root))

            config = DatasetPipelineConfig.load(path)

        self.assertEqual(config.schema_version, 3)
        self.assertIsInstance(config.runtime, BufferedRuntimeConfig)
        runtime = config.runtime
        assert isinstance(runtime, BufferedRuntimeConfig)
        self.assertEqual(runtime.ready_low_bytes, 128_000_000_000)
        self.assertEqual(runtime.ready_high_bytes, 256_000_000_000)
        self.assertEqual(runtime.temporary_hard_max_bytes, 2_000_000_000_000)
        self.assertEqual(runtime.min_free_bytes, 100_000_000_000)

    def test_buffered_profile_rejects_invalid_watermarks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            baseline = _buffered_profile(root)
            cases = (
                (
                    "ready_low_bytes = 300_000_000_000",
                    "ready_low_bytes must not exceed ready_high_bytes",
                ),
                (
                    "temporary_hard_max_bytes = 200_000_000_000",
                    "ready_high_bytes must not exceed temporary_hard_max_bytes",
                ),
                ("min_free_bytes = -1", "non-negative integer"),
            )
            originals = (
                "ready_low_bytes = 128_000_000_000",
                "temporary_hard_max_bytes = 2_000_000_000_000",
                "min_free_bytes = 100_000_000_000",
            )
            for (replacement, reason), original in zip(cases, originals, strict=True):
                path = root / f"case-{len(replacement)}.toml"
                path.write_text(baseline.replace(original, replacement))
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(ValueError, reason):
                        DatasetPipelineConfig.load(path)

    def test_loads_direct_pfs_source_and_rejects_unsafe_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            baseline = _buffered_profile(root).replace(
                f'root = "{root / "source"}"',
                f'root = "{root / "pfs" / "tmp" / "source"}"\n'
                '        materialization = "direct"',
            )
            path = root / "direct.toml"
            path.write_text(baseline)

            config = DatasetPipelineConfig.load(path)

            self.assertEqual(config.source.materialization, "direct")
            for profile, reason in (
                (
                    baseline.replace(
                        f'root = "{root / "pfs" / "tmp" / "source"}"',
                        f'root = "{root / "outside"}"',
                    ),
                    "source.root must be below runtime.pfs_root",
                ),
                (
                    baseline.replace(
                        'materialization = "direct"', 'materialization = "move"'
                    ),
                    "must be 'copy' or 'direct'",
                ),
                (
                    baseline.replace(
                        f'root = "{root / "pfs" / "tmp" / "source"}"',
                        f'root = "{root / "pfs" / "tmp" / "group" / "source"}"',
                    ),
                    "must be pfs_root/tmp/<dataset>",
                ),
            ):
                path.write_text(profile)
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(ValueError, reason):
                        DatasetPipelineConfig.load(path)

            target = root / "pfs" / "tmp" / "target"
            target.mkdir(parents=True)
            link = root / "pfs" / "tmp" / "source"
            link.symlink_to(target, target_is_directory=True)
            path.write_text(baseline)
            with self.assertRaisesRegex(ValueError, "must not be a symbolic link"):
                DatasetPipelineConfig.load(path)

    def test_rejects_direct_source_with_legacy_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(
                _profile().replace(
                    'block = ["aborted", "logs"]',
                    'block = ["aborted", "logs"]\nmaterialization = "direct"',
                )
            )

            with self.assertRaisesRegex(ValueError, "requires a PFS runtime"):
                DatasetPipelineConfig.load(path)

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
                DatasetPipelineConfig.load(path)

    def test_rejects_block_path_instead_of_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(
                _profile().replace(
                    'block = ["aborted", "logs"]', 'block = ["bad/logs"]'
                )
            )

            with self.assertRaisesRegex(ValueError, "one path component"):
                DatasetPipelineConfig.load(path)

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
                DatasetPipelineConfig.load(path)


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
        task_source = "metadata.task.description"
        """
    )


def _prefetch_profile(root: Path) -> str:
    pfs_root = root / "pfs"
    return textwrap.dedent(
        f"""
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
        task_source = "metadata.task.description"
        """
    )


def _buffered_profile(root: Path) -> str:
    pfs_root = root / "pfs"
    return textwrap.dedent(
        f"""
        schema_version = 3

        [source]
        root = "{root / "source"}"
        include_paths = ["fold_cloth/2026-08-21"]
        block = []

        [runtime]
        pfs_root = "{pfs_root}"
        streaming_root = "{pfs_root / "streaming"}"
        stage_workers = 16
        conversion_workers = 64
        ready_low_bytes = 128_000_000_000
        ready_high_bytes = 256_000_000_000
        temporary_hard_max_bytes = 2_000_000_000_000
        max_staged_episodes = 128
        min_free_bytes = 100_000_000_000

        [output]
        lerobot_root = "{pfs_root / "lerobot"}"
        dataset_name = "fold_cloth"
        repo_id = "local/fold_cloth"

        [recipe]
        name = "pi05-equal-eef-v3"
        profile = "config/fold_cloth.toml"
        task_source = "metadata.task.description"
        """
    )


if __name__ == "__main__":
    unittest.main()
