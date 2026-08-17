from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arx5_collection.atomic import staged_directory


class StagedDirectoryTest(unittest.TestCase):
    def test_publishes_complete_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "artifact"
            with staged_directory(target) as staged:
                self.assertFalse(target.exists())
                (staged / "value.txt").write_text("complete")

            self.assertEqual((target / "value.txt").read_text(), "complete")

    def test_failure_removes_stage_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "artifact"
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with staged_directory(target) as staged:
                    (staged / "partial.txt").write_text("partial")
                    raise RuntimeError("stop")

            self.assertFalse(target.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_non_precreated_stage_supports_builders_that_create_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "artifact"
            with staged_directory(target, precreate=False) as staged:
                self.assertFalse(staged.exists())
                staged.mkdir()

            self.assertTrue(target.is_dir())

    def test_existing_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "artifact"
            target.mkdir()
            (target / "value.txt").write_text("original")

            with self.assertRaises(FileExistsError):
                with staged_directory(target):
                    pass

            self.assertEqual((target / "value.txt").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
