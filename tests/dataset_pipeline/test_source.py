from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from arx5_collection.dataset_pipeline.execution.coordinator import _remove_stage
from arx5_collection.dataset_pipeline.execution.models import EpisodeCandidate
from arx5_collection.dataset_pipeline.execution.models import FileIdentity
from arx5_collection.dataset_pipeline.source.staging import MountedEpisodeSource
from arx5_collection.dataset_pipeline.source.staging import SourceChangedError
from arx5_collection.dataset_pipeline.source.staging import StageValidationError
from arx5_collection.dataset_pipeline.source.staging import validate_stage


class MountedEpisodeSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source_root = self.root / "source"
        self.source_dir = self.source_root / "task" / "episode-a"
        self.source_dir.mkdir(parents=True)
        (self.source_dir / "episode.mcap").write_bytes(b"mcap-data")
        (self.source_dir / "metadata.json").write_text('{"episode_id":"episode-a"}\n')
        self.candidate = self._candidate()
        self.source = MountedEpisodeSource(self.source_root)
        self.target = self.root / "streaming" / "run" / "staging" / "episode-a"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stages_two_files_and_commits_receipt(self) -> None:
        receipt = self.source.stage(self.candidate, self.target)

        self.assertEqual(receipt, validate_stage(self.target))
        self.assertEqual((self.target / "episode.mcap").read_bytes(), b"mcap-data")
        self.assertEqual(
            (self.target / "metadata.json").read_text(),
            '{"episode_id":"episode-a"}\n',
        )
        self.assertEqual(
            sorted(path.name for path in self.target.iterdir()),
            ["episode.mcap", "metadata.json", "stage.json"],
        )
        self.assertFalse((self.target / "episode.mcap").is_symlink())

    def test_direct_stage_links_frozen_source_without_copying(self) -> None:
        source = MountedEpisodeSource(self.source_root, "direct")

        receipt = source.stage(self.candidate, self.target)

        self.assertEqual(receipt.materialization, "direct")
        self.assertEqual(receipt, validate_stage(self.target))
        self.assertTrue((self.target / "episode.mcap").is_symlink())
        self.assertEqual(
            (self.target / "episode.mcap").resolve(),
            self.source_dir / "episode.mcap",
        )

        _remove_stage(self.root / "streaming" / "run", "episode-a")

        self.assertFalse(self.target.exists())
        self.assertEqual((self.source_dir / "episode.mcap").read_bytes(), b"mcap-data")

    def test_direct_stage_rejects_changed_or_retargeted_source(self) -> None:
        source = MountedEpisodeSource(self.source_root, "direct")
        source.stage(self.candidate, self.target)
        mcap = self.source_dir / "episode.mcap"
        mcap.write_bytes(b"new-bytes")
        os.utime(mcap, ns=(self.candidate.mcap.mtime_ns + 1,) * 2)

        with self.assertRaisesRegex(SourceChangedError, "source changed"):
            validate_stage(self.target)

        mcap.write_bytes(b"mcap-data")
        os.utime(mcap, ns=(self.candidate.mcap.mtime_ns,) * 2)
        outside = self.root / "outside.mcap"
        outside.write_bytes(b"mcap-data")
        link = self.target / "episode.mcap"
        link.unlink()
        link.symlink_to(outside)
        with self.assertRaisesRegex(SourceChangedError, "source changed"):
            validate_stage(self.target)

    def test_rejects_source_changed_before_copy(self) -> None:
        mcap = self.source_dir / "episode.mcap"
        mcap.write_bytes(b"changed")
        os.utime(mcap, ns=(self.candidate.mcap.mtime_ns + 1,) * 2)

        with self.assertRaises(SourceChangedError):
            self.source.stage(self.candidate, self.target)

        self.assertFalse(self.target.exists())

    def test_reports_disappearing_frozen_source_as_source_change(self) -> None:
        (self.source_dir / "episode.mcap").unlink()

        with self.assertRaisesRegex(SourceChangedError, "missing path"):
            self.source.stage(self.candidate, self.target)

        self.assertFalse(self.target.exists())

    def test_rejects_source_changed_during_copy_and_cleans_partial(self) -> None:
        from arx5_collection.dataset_pipeline.source import staging as source_module

        real_copy = source_module._copy_file
        calls = 0

        def copy_and_change(source: Path, target: Path) -> None:
            nonlocal calls
            real_copy(source, target)
            calls += 1
            if calls == 1:
                metadata = self.source_dir / "metadata.json"
                metadata.write_text("changed metadata")
                os.utime(metadata, ns=(self.candidate.metadata.mtime_ns + 1,) * 2)

        with patch.object(source_module, "_copy_file", side_effect=copy_and_change):
            with self.assertRaises(SourceChangedError):
                self.source.stage(self.candidate, self.target)

        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".episode-a.*")), [])

    def test_copy_failure_leaves_no_visible_or_partial_stage(self) -> None:
        with patch(
            "arx5_collection.dataset_pipeline.source.staging._copy_file",
            side_effect=OSError("injected copy failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected copy failure"):
                self.source.stage(self.candidate, self.target)

        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".episode-a.*")), [])

    def test_refuses_existing_target(self) -> None:
        self.source.stage(self.candidate, self.target)
        with self.assertRaises(FileExistsError):
            self.source.stage(self.candidate, self.target)

    def test_validation_rejects_corrupt_staged_file(self) -> None:
        self.source.stage(self.candidate, self.target)
        (self.target / "episode.mcap").write_bytes(b"short")

        with self.assertRaisesRegex(StageValidationError, "size mismatch"):
            validate_stage(self.target)

    def test_rejects_candidate_outside_source_root(self) -> None:
        outside = self.root / "outside" / "episode-a"
        outside.mkdir(parents=True)
        (outside / "episode.mcap").write_bytes(b"mcap-data")
        (outside / "metadata.json").write_text('{"episode_id":"episode-a"}\n')
        candidate = self._candidate(outside)

        with self.assertRaisesRegex(ValueError, "escapes configured root"):
            self.source.stage(candidate, self.target)

    def _candidate(self, source_dir: Path | None = None) -> EpisodeCandidate:
        directory = source_dir or self.source_dir
        return EpisodeCandidate(
            source_dir=directory,
            relative_dir=Path("task/episode-a"),
            include_path=Path("task"),
            episode_id="episode-a",
            source_session_id="w4/2026-08-25/task",
            collection_type="demonstration",
            outcome="success",
            task_id="eight-stream-collection",
            task_description="Record synchronized streams",
            mcap=_identity(directory / "episode.mcap"),
            metadata=_identity(directory / "metadata.json"),
        )


def _identity(path: Path) -> FileIdentity:
    stat = path.stat()
    return FileIdentity(stat.st_size, stat.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
