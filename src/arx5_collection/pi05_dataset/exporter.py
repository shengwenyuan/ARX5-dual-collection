from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
from typing import Callable
from typing import Sequence

from arx5_collection.artifacts import message_ref_from_artifact
from arx5_collection.artifacts import read_json
from arx5_collection.artifacts import read_jsonl
from arx5_collection.artifacts import write_json
from arx5_collection.artifacts import write_jsonl
from arx5_collection.atomic import staged_directory
from arx5_collection.pi05_dataset.discovery import episode_lookup
from arx5_collection.pi05_dataset.images import extract_selected_rgb
from arx5_collection.pi05_dataset.lerobot_contract import CAMERA_KEYS
from arx5_collection.pi05_dataset.lerobot_contract import DATASET_FPS
from arx5_collection.pi05_dataset.lerobot_contract import IMAGE_SIZE
from arx5_collection.pi05_dataset.lerobot_contract import LEROBOT_COMMIT
from arx5_collection.pi05_dataset.lerobot_contract import MOTOR_NAMES
from arx5_collection.pi05_dataset.lerobot_contract import OPENPI_COMMIT
from arx5_collection.pi05_dataset.lerobot_contract import lerobot_features
from arx5_collection.pi05_dataset.video import VideoEncodingConfig
from arx5_collection.pi05_dataset.video import configured_lerobot_encoder


def export_lerobot(
    source_roots: Path | Sequence[Path],
    selection_dir: Path,
    output_root: Path,
    repo_id: str,
    *,
    mode: str = "video",
    dataset_root: Path | None = None,
    video: VideoEncodingConfig | None = None,
    phase_reporter: Callable[[str, float], None] | None = None,
) -> Path:
    if mode not in {"video", "image"}:
        raise ValueError("mode must be video or image")
    if "/" not in repo_id or repo_id.startswith("/") or repo_id.endswith("/"):
        raise ValueError("repo_id must use the '<owner>/<dataset>' form")

    import numpy as np
    from PIL import Image
    from lerobot.common.datasets.lerobot_dataset import LeRobotDataset

    sample_rows = read_jsonl(selection_dir / "sample_index.jsonl")
    segment_rows = read_jsonl(selection_dir / "segments.jsonl")
    selection_report = read_json(selection_dir / "selection.json")
    source_manifest_path = selection_dir / "source_manifest.jsonl"
    source_manifest = (
        read_jsonl(source_manifest_path) if source_manifest_path.is_file() else []
    )
    samples_by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in sample_rows:
        if sample["training_eligible"]:
            samples_by_segment[str(sample["segment_id"])].append(sample)
    if not segment_rows:
        raise ValueError("selection contains no training segments")
    source_episodes = episode_lookup(source_roots)

    final_root = dataset_root if dataset_root is not None else output_root / "lerobot" / repo_id
    if final_root.exists():
        raise FileExistsError(final_root)
    report_path = output_root / "reports" / "conversion.json"
    if report_path.exists():
        raise FileExistsError(report_path)
    output_root.mkdir(parents=True, exist_ok=True)
    cache_parent = Path(tempfile.mkdtemp(prefix=".rgb-cache.", dir=output_root))
    try:
        with staged_directory(final_root, precreate=False) as temporary_root:
            dataset = LeRobotDataset.create(
                repo_id=repo_id,
                fps=DATASET_FPS,
                root=temporary_root,
                robot_type="arx5_dual",
                features=lerobot_features(mode),
                use_videos=mode == "video",
                image_writer_processes=0,
                image_writer_threads=0,
                video_backend="pyav",
            )
            segments_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for segment in segment_rows:
                segments_by_episode[str(segment["source_episode_id"])].append(segment)

            exported_frames = 0
            exported_segment_ids = []
            with configured_lerobot_encoder(video, phase_reporter):
                for episode_id, episode_segments in segments_by_episode.items():
                    refs = {
                        message_ref_from_artifact(sample["images"][camera])
                        for segment in episode_segments
                        for sample in samples_by_segment[str(segment["segment_id"])]
                        for camera in CAMERA_KEYS
                    }
                    episode_cache = cache_parent / episode_id
                    try:
                        source_episode = source_episodes[episode_id]
                    except KeyError as error:
                        raise ValueError(
                            f"selected episode {episode_id!r} is missing from input roots"
                        ) from error
                    started = time.monotonic()
                    image_paths = extract_selected_rgb(source_episode, refs, episode_cache)
                    _report_phase(phase_reporter, "decode_rgb", started)
                    for segment in episode_segments:
                        segment_id = str(segment["segment_id"])
                        rows = sorted(
                            samples_by_segment[segment_id],
                            key=lambda row: row["source_sample_index"],
                        )
                        if len(rows) != int(segment["frame_count"]):
                            raise ValueError(
                                f"sample count does not match segment manifest: {segment_id}"
                            )
                        started = time.monotonic()
                        for row in rows:
                            frame = {
                                "observation.state": np.asarray(
                                    row["state"], dtype=np.float32
                                ),
                                "action": np.asarray(row["action"], dtype=np.float32),
                                "task": str(segment["task"]),
                            }
                            for camera in CAMERA_KEYS:
                                ref = message_ref_from_artifact(row["images"][camera])
                                with Image.open(
                                    image_paths[(ref.topic, ref.sequence)]
                                ) as image:
                                    frame[f"observation.images.{camera}"] = (
                                        image.convert("RGB").copy()
                                    )
                            dataset.add_frame(frame)
                            exported_frames += 1
                        _report_phase(phase_reporter, "materialize_frames", started)
                        started = time.monotonic()
                        dataset.save_episode()
                        _report_phase(phase_reporter, "save_episode", started)
                        exported_segment_ids.append(segment_id)
                    shutil.rmtree(episode_cache)

        report_dir = output_root / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        width, height = IMAGE_SIZE
        report = {
            "repo_id": repo_id,
            "openpi_commit": OPENPI_COMMIT,
            "lerobot_commit": LEROBOT_COMMIT,
            "fps": DATASET_FPS,
            "mode": mode,
            "image_size": [width, height],
            "image_color": "RGB",
            "source_selection_dir": str(selection_dir.resolve()),
            "output_root": str(final_root.resolve()),
            "episode_count": len(segment_rows),
            "frame_count": exported_frames,
            "state_action_order": list(MOTOR_NAMES),
            "state_action_version": selection_report["state_action_version"],
            "filter_version": selection_report["filter_version"],
            "gripper_calibration": selection_report["gripper_calibration"],
            "tasks": sorted({str(segment["task"]) for segment in segment_rows}),
        }
        if video is not None:
            report["video_encoding"] = video.as_report()
        if "sampling_contract" in selection_report:
            report["sampling_contract"] = selection_report["sampling_contract"]
        if source_manifest:
            source_by_segment = {
                str(row["segment_id"]): row for row in source_manifest
            }
            if set(source_by_segment) != set(exported_segment_ids):
                raise ValueError(
                    "source manifest does not match exported selection segments"
                )
            exported_source_manifest = [
                {
                    **source_by_segment[segment_id],
                    "lerobot_episode_index": index,
                }
                for index, segment_id in enumerate(exported_segment_ids)
            ]
            report["source_manifest"] = str(
                (report_dir / "source_manifest.jsonl").resolve()
            )
            report["source_composition"] = selection_report.get(
                "source_composition", {}
            )
            write_jsonl(
                report_dir / "source_manifest.jsonl",
                exported_source_manifest,
            )
        if "mixture" in selection_report:
            report["mixture"] = selection_report["mixture"]
            report["weighting_applied"] = selection_report.get(
                "weighting_applied", False
            )
        write_json(report_path, report)
    finally:
        if cache_parent.exists():
            shutil.rmtree(cache_parent)
    return final_root


def _report_phase(
    reporter: Callable[[str, float], None] | None,
    name: str,
    started: float,
) -> None:
    if reporter is not None:
        reporter(name, max(time.monotonic() - started, 0.0))
