from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path("scripts/dataset/benchmark_streaming_prefetch.py").resolve()


class BenchmarkToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "selection.jsonl"
        rows = [
            self._row("episode-a", "w3/2026-08-25/task", 10),
            self._row("episode-b", "w3/2026-08-25/task", 30),
            self._row("episode-c", "w4/2026-08-26/task", 20),
        ]
        self.manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_exact_whitelist(self) -> None:
        output = self.root / "whitelist.jsonl"
        self._run("--output", str(output), "--episode-id", "episode-c")

        rows = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual([row["episode_id"] for row in rows], ["episode-c"])
        summary = json.loads(
            output.with_suffix(".jsonl.summary.json").read_text()
        )
        self.assertEqual(summary["strategy"], "explicit_whitelist")

    def test_stratified_sample_is_deterministic(self) -> None:
        first = self.root / "first.jsonl"
        second = self.root / "second.jsonl"
        self._run("--output", str(first), "--count", "2")
        self._run("--output", str(second), "--count", "2")

        self.assertEqual(first.read_text(), second.read_text())
        self.assertEqual(len(first.read_text().splitlines()), 2)

    def _run(self, *args: str) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path("src").resolve())
        subprocess.run(
            [sys.executable, str(SCRIPT), "sample", "--manifest", str(self.manifest), *args],
            check=True,
            env=environment,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _row(episode_id: str, session: str, size: int) -> dict[str, object]:
        return {
            "episode_id": episode_id,
            "source_session_id": session,
            "source_dir": f"/source/{episode_id}",
            "relative_dir": f"success/{episode_id}",
            "collection_type": "demonstration",
            "outcome": "success",
            "metadata_task": {"id": "task", "description": "task"},
            "training_task": "folding the cloth",
            "mcap": {"size": size, "mtime_ns": 1},
            "metadata": {"size": 1, "mtime_ns": 1},
        }


if __name__ == "__main__":
    unittest.main()
