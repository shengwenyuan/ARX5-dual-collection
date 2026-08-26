from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arx5_collection.episode.models import EpisodeOutcome
from arx5_collection.episode.store import EpisodeStore


class EpisodeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "episodes"
        self.store = EpisodeStore(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_prepare_returns_partial_paths(self) -> None:
        pending = self.store.prepare("episode-001")
        self.assertEqual(pending.partial_dir, self.root / ".episode-001.partial")
        self.assertEqual(pending.mcap_path.name, "episode.mcap")
        self.assertEqual(pending.metadata_path.name, "metadata.json")
        self.assertTrue(pending.partial_dir.is_dir())

    def test_prepare_rejects_duplicate_and_invalid_ids(self) -> None:
        self.store.prepare("episode-001")
        with self.assertRaises(FileExistsError):
            self.store.prepare("episode-001")
        for episode_id in ("", "../episode", "folder/episode", ".", ".."):
            with self.subTest(episode_id=episode_id):
                with self.assertRaises(ValueError):
                    self.store.prepare(episode_id)

    def test_prepare_checks_disk_space_before_creating_partial(self) -> None:
        store = EpisodeStore(self.root, min_free_bytes=100)
        disk_usage = shutil_usage(total=1_000, used=950, free=50)
        with patch("arx5_collection.episode.store.shutil.disk_usage", return_value=disk_usage):
            with self.assertRaises(OSError):
                store.prepare("episode-001")
        self.assertFalse((self.root / ".episode-001.partial").exists())

    def test_commit_requires_exactly_two_files(self) -> None:
        missing = self.store.prepare("missing")
        missing.mcap_path.write_bytes(b"mcap")
        with self.assertRaises(RuntimeError):
            self.store.commit(missing, EpisodeOutcome.SUCCESS)
        self.assertTrue(missing.partial_dir.exists())

        extra = self.store.prepare("extra")
        extra.mcap_path.write_bytes(b"mcap")
        extra.metadata_path.write_text("{}")
        (extra.partial_dir / "extra.txt").write_text("unexpected")
        with self.assertRaises(RuntimeError):
            self.store.commit(extra, EpisodeOutcome.SUCCESS)
        self.assertTrue(extra.partial_dir.exists())

    def test_commit_renames_complete_episode(self) -> None:
        pending = self.store.prepare("episode-001")
        pending.mcap_path.write_bytes(b"mcap")
        pending.metadata_path.write_text("{}")

        stored = self.store.commit(pending, EpisodeOutcome.SUCCESS)

        self.assertFalse(pending.partial_dir.exists())
        self.assertEqual(stored.directory, self.root / "episode-001")
        self.assertEqual(
            {path.name for path in stored.directory.iterdir()},
            {"episode.mcap", "metadata.json"},
        )

    def test_aborted_commit_uses_abort_subdirectory(self) -> None:
        pending = self.store.prepare("episode-aborted")
        pending.mcap_path.write_bytes(b"mcap")
        pending.metadata_path.write_text("{}")

        stored = self.store.commit(pending, EpisodeOutcome.ABORTED)

        self.assertEqual(
            stored.directory,
            self.root / "abort" / "episode-aborted",
        )
        self.assertTrue(stored.mcap_path.is_file())

    def test_failed_commit_uses_fail_subdirectory(self) -> None:
        pending = self.store.prepare("episode-failed")
        pending.mcap_path.write_bytes(b"mcap")
        pending.metadata_path.write_text("{}")

        stored = self.store.commit(pending, EpisodeOutcome.FAIL)

        self.assertEqual(stored.directory, self.root / "fail" / "episode-failed")
        self.assertTrue(stored.metadata_path.is_file())

    def test_failed_commit_supports_mode_specific_subdirectory(self) -> None:
        store = EpisodeStore(self.root, fail_directory="dagger_fail")
        pending = store.prepare("episode-dagger-failed")
        pending.mcap_path.write_bytes(b"mcap")
        pending.metadata_path.write_text("{}")

        stored = store.commit(pending, EpisodeOutcome.FAIL)

        self.assertEqual(
            stored.directory,
            self.root / "dagger_fail" / "episode-dagger-failed",
        )

    def test_list_partials_only_reports(self) -> None:
        first = self.store.prepare("first")
        second = self.store.prepare("second")
        partials = self.store.list_partials()
        self.assertEqual(partials, (first.partial_dir, second.partial_dir))
        self.assertTrue(first.partial_dir.exists())
        self.assertTrue(second.partial_dir.exists())


def shutil_usage(total: int, used: int, free: int) -> object:
    return type("usage", (), {"total": total, "used": used, "free": free})()


if __name__ == "__main__":
    unittest.main()
