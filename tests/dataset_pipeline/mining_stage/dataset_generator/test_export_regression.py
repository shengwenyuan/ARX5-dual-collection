from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator import (
    export_lerobot,
)


CAMERAS = ("cam_high", "cam_left_wrist", "cam_right_wrist")
TOPICS = {
    "cam_high": "/camera/overview/color/image_raw",
    "cam_left_wrist": "/camera/left/color/image_raw",
    "cam_right_wrist": "/camera/right/color/image_raw",
}
MOTORS = (
    "left_j1",
    "left_j2",
    "left_j3",
    "left_j4",
    "left_j5",
    "left_j6",
    "left_gripper",
    "right_j1",
    "right_j2",
    "right_j3",
    "right_j4",
    "right_j5",
    "right_j6",
    "right_gripper",
)


class SemanticLeRobotDataset:
    created: SemanticLeRobotDataset | None = None

    def __init__(self, root: Path, creation: dict[str, object]) -> None:
        self.root = root
        self.creation = creation
        self.episodes: list[list[dict[str, object]]] = []
        self.frames: list[dict[str, object]] = []
        root.mkdir(parents=True)

    @classmethod
    def create(cls, **kwargs: object) -> SemanticLeRobotDataset:
        root = Path(kwargs.pop("root"))
        cls.created = cls(root, _json_value(kwargs))
        return cls.created

    def add_frame(self, frame: dict[str, object]) -> None:
        value = {
            "observation.state": {
                "dtype": str(frame["observation.state"].dtype),
                "values": frame["observation.state"].tolist(),
            },
            "action": {
                "dtype": str(frame["action"].dtype),
                "values": frame["action"].tolist(),
            },
            "task": frame["task"],
        }
        for camera in CAMERAS:
            image = frame[f"observation.images.{camera}"]
            value[f"observation.images.{camera}"] = {
                "mode": image.mode,
                "size": list(image.size),
                "bytes": list(image.tobytes()),
            }
        self.frames.append(value)

    def save_episode(self) -> None:
        self.episodes.append(self.frames)
        self.frames = []


class ExportSemanticRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source" / "episode-a"
        self.selection = self.root / "selection"
        self.output = self.root / "output"
        self.source.mkdir(parents=True)
        self.selection.mkdir()
        (self.source / "episode.mcap").write_bytes(b"fixed-mcap-input")
        (self.source / "metadata.json").write_text(
            json.dumps({"episode_id": "episode-a", "outcome": "success"})
        )
        self._write_selection()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_matches_pre_migration_semantic_export(self) -> None:
        modules = _lerobot_modules()
        with (
            patch.dict(sys.modules, modules),
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator.extract_selected_rgb",
                side_effect=_extract_rgb,
            ),
        ):
            exported = export_lerobot(
                self.source,
                self.selection,
                self.output,
                "local/export-regression",
                mode="image",
            )

        dataset = SemanticLeRobotDataset.created
        self.assertIsNotNone(dataset)
        self.assertEqual(exported, self.output / "lerobot/local/export-regression")
        self.assertEqual(dataset.creation, _expected_creation())
        self.assertEqual(dataset.episodes, _expected_episodes())
        self.assertEqual(
            _normalized_report(self.output / "reports/conversion.json"),
            _expected_report(),
        )
        self.assertEqual(
            _read_jsonl(self.output / "reports/source_manifest.jsonl"),
            _expected_source_manifest(),
        )

    def _write_selection(self) -> None:
        samples = [
            _sample(2, "episode-a--001", 2.25),
            _sample(0, "episode-a--000", 0.25),
            _sample(3, None, 3.25),
            _sample(1, "episode-a--000", 1.25),
        ]
        segments = [
            {
                "segment_id": "episode-a--000",
                "source_episode_id": "episode-a",
                "frame_count": 2,
                "task": "fold cloth",
            },
            {
                "segment_id": "episode-a--001",
                "source_episode_id": "episode-a",
                "frame_count": 1,
                "task": "fold cloth",
            },
        ]
        source_manifest = [
            {
                "schema_version": 1,
                "segment_id": "episode-a--000",
                "source_episode_id": "episode-a",
                "source_session_id": "station-a/session-a",
                "split_group": "episode-a",
                "collection_type": "demonstration",
                "training_class": "demonstration",
                "intervention_id": None,
            },
            {
                "schema_version": 1,
                "segment_id": "episode-a--001",
                "source_episode_id": "episode-a",
                "source_session_id": "station-a/session-a",
                "split_group": "episode-a",
                "collection_type": "demonstration",
                "training_class": "demonstration",
                "intervention_id": None,
            },
        ]
        selection = {
            "filter_version": "pi05-arx-filter-v2-equal-eef-distance",
            "state_action_version": "arx5-measured-position-proxy-v1",
            "gripper_calibration": {
                "contract_id": "arx5-gripper-v1",
                "left": {"open_value": -3.4, "closed_value": 0.0},
                "right": {"open_value": -3.4, "closed_value": 0.0},
            },
            "sampling_contract": {
                "mode": "equal_eef_distance",
                "action_horizon": 50,
            },
            "source_composition": {"demonstration": 2, "dagger": 0},
            "mixture": {"demonstration": 0.75, "dagger": 0.25},
            "weighting_applied": True,
        }
        _write_jsonl(self.selection / "sample_index.jsonl", samples)
        _write_jsonl(self.selection / "segments.jsonl", segments)
        _write_jsonl(self.selection / "source_manifest.jsonl", source_manifest)
        (self.selection / "selection.json").write_text(json.dumps(selection))


def _sample(index: int, segment_id: str | None, offset: float) -> dict[str, object]:
    return {
        "source_sample_index": index,
        "segment_id": segment_id,
        "training_eligible": segment_id is not None,
        "state": [offset + motor / 100 for motor in range(14)],
        "action": [offset + motor / 10 for motor in range(14)],
        "images": {
            camera: {
                "topic": TOPICS[camera],
                "sequence": index,
                "header_stamp_ns": 1_000_000_000 + index,
                "bag_timestamp_ns": 2_000_000_000 + index,
            }
            for camera in CAMERAS
        },
    }


def _extract_rgb(
    episode_dir: Path,
    refs: set[object],
    output_dir: Path,
) -> dict[tuple[str, int], Path]:
    self_contained = episode_dir / "episode.mcap"
    if self_contained.read_bytes() != b"fixed-mcap-input":
        raise AssertionError("unexpected source data")
    output_dir.mkdir()
    paths = {}
    for ref in refs:
        camera = next(key for key, topic in TOPICS.items() if topic == ref.topic)
        camera_index = CAMERAS.index(camera)
        color = (
            ref.sequence * 10 + camera_index,
            camera_index + 20,
            200 - ref.sequence,
        )
        path = output_dir / f"{camera}-{ref.sequence}.png"
        Image.new("RGB", (2, 1), color).save(path)
        paths[(ref.topic, ref.sequence)] = path
    return paths


def _lerobot_modules() -> dict[str, ModuleType]:
    lerobot = ModuleType("lerobot")
    common = ModuleType("lerobot.common")
    datasets = ModuleType("lerobot.common.datasets")
    module = ModuleType("lerobot.common.datasets.lerobot_dataset")
    module.LeRobotDataset = SemanticLeRobotDataset
    lerobot.common = common
    common.datasets = datasets
    datasets.lerobot_dataset = module
    return {
        "lerobot": lerobot,
        "lerobot.common": common,
        "lerobot.common.datasets": datasets,
        "lerobot.common.datasets.lerobot_dataset": module,
    }


def _json_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _expected_creation() -> dict[str, object]:
    features = {
        "observation.state": {
            "dtype": "float32",
            "shape": [14],
            "names": [list(MOTORS)],
        },
        "action": {
            "dtype": "float32",
            "shape": [14],
            "names": [list(MOTORS)],
        },
    }
    for camera in CAMERAS:
        features[f"observation.images.{camera}"] = {
            "dtype": "image",
            "shape": [3, 360, 640],
            "names": ["channels", "height", "width"],
        }
    return {
        "repo_id": "local/export-regression",
        "fps": 50,
        "robot_type": "arx5_dual",
        "features": features,
        "use_videos": False,
        "image_writer_processes": 0,
        "image_writer_threads": 0,
        "video_backend": "pyav",
    }


def _expected_episodes() -> list[list[dict[str, object]]]:
    return [
        [_expected_frame(0, 0.25), _expected_frame(1, 1.25)],
        [_expected_frame(2, 2.25)],
    ]


def _expected_frame(index: int, offset: float) -> dict[str, object]:
    frame = {
        "observation.state": {
            "dtype": "float32",
            "values": _float32([offset + motor / 100 for motor in range(14)]),
        },
        "action": {
            "dtype": "float32",
            "values": _float32([offset + motor / 10 for motor in range(14)]),
        },
        "task": "fold cloth",
    }
    for camera_index, camera in enumerate(CAMERAS):
        color = [index * 10 + camera_index, camera_index + 20, 200 - index]
        frame[f"observation.images.{camera}"] = {
            "mode": "RGB",
            "size": [2, 1],
            "bytes": color * 2,
        }
    return frame


def _float32(values: list[float]) -> list[float]:
    return np.asarray(values, dtype=np.float32).tolist()


def _normalized_report(path: Path) -> dict[str, object]:
    report = json.loads(path.read_text())
    report["source_selection_dir"] = "<selection>"
    report["output_root"] = "<dataset>"
    report["source_manifest"] = "<source_manifest>"
    return report


def _expected_report() -> dict[str, object]:
    return {
        "repo_id": "local/export-regression",
        "openpi_commit": "15a9616a00943ada6c20a0f158e3adb39df2ccac",
        "lerobot_commit": "0cf864870cf29f4738d3ade893e6fd13fbd7cdb5",
        "fps": 50,
        "mode": "image",
        "image_size": [640, 360],
        "image_color": "RGB",
        "source_selection_dir": "<selection>",
        "output_root": "<dataset>",
        "episode_count": 2,
        "frame_count": 3,
        "state_action_order": list(MOTORS),
        "state_action_version": "arx5-measured-position-proxy-v1",
        "filter_version": "pi05-arx-filter-v2-equal-eef-distance",
        "gripper_calibration": {
            "contract_id": "arx5-gripper-v1",
            "left": {"open_value": -3.4, "closed_value": 0.0},
            "right": {"open_value": -3.4, "closed_value": 0.0},
        },
        "tasks": ["fold cloth"],
        "sampling_contract": {
            "mode": "equal_eef_distance",
            "action_horizon": 50,
        },
        "source_manifest": "<source_manifest>",
        "source_composition": {"demonstration": 2, "dagger": 0},
        "mixture": {"demonstration": 0.75, "dagger": 0.25},
        "weighting_applied": True,
    }


def _expected_source_manifest() -> list[dict[str, object]]:
    base = {
        "schema_version": 1,
        "source_episode_id": "episode-a",
        "source_session_id": "station-a/session-a",
        "split_group": "episode-a",
        "collection_type": "demonstration",
        "training_class": "demonstration",
        "intervention_id": None,
    }
    return [
        {**base, "segment_id": "episode-a--000", "lerobot_episode_index": 0},
        {**base, "segment_id": "episode-a--001", "lerobot_episode_index": 1},
    ]


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


if __name__ == "__main__":
    unittest.main()
