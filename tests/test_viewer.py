from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

from arx5_collection.adapters.viewer.cli import DatasetIndex


class DatasetIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "meta").mkdir()
        (self.root / "videos/chunk-000/observation.images.cam_high").mkdir(parents=True)
        info = {
            "codebase_version": "v2.1",
            "total_episodes": 1,
            "total_frames": 100,
            "total_videos": 1,
            "chunks_size": 1000,
            "fps": 50,
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": {"observation.images.cam_high": {"dtype": "video"}},
        }
        (self.root / "meta/info.json").write_text(json.dumps(info))
        (self.root / "meta/episodes.jsonl").write_text(
            json.dumps({"episode_index": 0, "tasks": ["fold cloth"], "length": 100})
            + "\n"
        )
        self.video = (
            self.root
            / "videos/chunk-000/observation.images.cam_high/episode_000000.mp4"
        )
        self.video.write_bytes(b"video")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_reads_episode_and_reports_training_samples(self) -> None:
        dataset = DatasetIndex.read(self.root)
        payload = dataset.payload()
        episode = dataset.episode_payload(0)
        self.assertEqual(payload["total_episodes"], 1)
        self.assertEqual(payload["episodes"][0]["duration_s"], 2.0)
        self.assertEqual(episode["training_samples"], 100)
        self.assertEqual(episode["fps"], 50)
        self.assertEqual(episode["action_horizon"], 50)
        self.assertEqual(episode["video"]["key"], "observation.images.cam_high")
        self.assertFalse(episode["provenance"]["available"])
        self.assertEqual(
            dataset.video_path(0, "observation.images.cam_high"), self.video
        )

    def test_rejects_inconsistent_episode_count(self) -> None:
        info_path = self.root / "meta/info.json"
        info = json.loads(info_path.read_text())
        info["total_episodes"] = 2
        info_path.write_text(json.dumps(info))
        with self.assertRaisesRegex(ValueError, "disagree"):
            DatasetIndex.read(self.root)

    @unittest.skipUnless(
        importlib.util.find_spec("pyarrow"), "pyarrow is not installed"
    )
    def test_builds_and_pads_action_chunk(self) -> None:
        import pyarrow as arrow
        import pyarrow.parquet as parquet

        data_dir = self.root / "data/chunk-000"
        data_dir.mkdir(parents=True)
        rows = 100
        table = arrow.table(
            {
                "index": list(range(rows)),
                "frame_index": list(range(rows)),
                "timestamp": [index / 50 for index in range(rows)],
                "action": [[float(index)] * 14 for index in range(rows)],
                "observation.state": [[float(index)] * 14 for index in range(rows)],
            }
        )
        parquet.write_table(table, data_dir / "episode_000000.parquet")
        chunk = DatasetIndex.read(self.root).action_chunk_payload(0, 99)
        self.assertEqual(len(chunk["actions"]), 50)
        self.assertEqual(chunk["padding_steps"], 49)
        self.assertEqual(chunk["actions"][0], chunk["actions"][-1])

        timeline = DatasetIndex.read(self.root).sample_timeline_payload(0)
        self.assertEqual(
            timeline["point_format"], ["dataset_index", "frame_index", "timestamp"]
        )
        self.assertEqual(timeline["points"][50], [50, 50, 1.0])


if __name__ == "__main__":
    unittest.main()
