from __future__ import annotations

import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from arx5_collection.collection.dagger.checkpoint import checkpoint_tree_sha256
from arx5_collection.collection.runtime.cli import run_checkpoint_sha


class CheckpointDigestTest(unittest.TestCase):
    def test_digest_is_stable_and_covers_paths_and_contents(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "params").mkdir()
            (root / "params" / "a").write_bytes(b"alpha")
            (root / "metadata").write_bytes(b"beta")

            first = checkpoint_tree_sha256(root)
            second = checkpoint_tree_sha256(root)
            (root / "params" / "a").write_bytes(b"changed")
            changed = checkpoint_tree_sha256(root)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed)

    def test_empty_checkpoint_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "contains no files"):
                checkpoint_tree_sha256(directory)

    def test_cli_prints_digest(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "params").write_bytes(b"weights")
            output = StringIO()
            self.assertEqual(run_checkpoint_sha(root, output), 0)
            self.assertEqual(output.getvalue().strip(), checkpoint_tree_sha256(root))


if __name__ == "__main__":
    unittest.main()
