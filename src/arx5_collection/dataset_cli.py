from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable
from typing import Sequence

from arx5_collection.cleaning.pipeline import clean_episode
from arx5_collection.cleaning.pipeline import inspect_episode
from arx5_collection.cleaning.reader import load_metadata
from arx5_collection.dagger_dataset.pipeline import classify_dagger_episode
from arx5_collection.dagger_dataset.selection import select_equal_eef_dagger_dataset
from arx5_collection.gripper import ARX5_GRIPPER_CALIBRATION
from arx5_collection.gripper import GripperCalibration
from arx5_collection.pi05_dataset.discovery import discover_episode_dirs
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy
from arx5_collection.pi05_dataset.exporter import export_lerobot
from arx5_collection.pi05_dataset.mixing import mix_selections
from arx5_collection.pi05_dataset.selection_pipeline import select_dataset
from arx5_collection.pi05_dataset.selection_pipeline import DatasetSelection
from arx5_collection.pi05_dataset.selection_pipeline import select_equal_eef_dataset
from arx5_collection.pi05_dataset.validate import validate_lerobot
from arx5_collection.streaming_conversion.alignment import AlignmentCancelled
from arx5_collection.streaming_conversion.application import StreamingRunRequest
from arx5_collection.streaming_conversion.application import execute_streaming_conversion


CommandHandler = Callable[[argparse.Namespace], int]


def _episode_dirs(
    input_roots: Path | Sequence[Path],
    outcomes: set[str] | None = None,
) -> list[Path]:
    return discover_episode_dirs(input_roots, outcomes)


def _selected_episode_dirs(args: argparse.Namespace) -> list[Path]:
    outcomes = set(args.outcome) if args.outcome else None
    episodes = _episode_dirs(args.input_root, outcomes)
    if not episodes:
        raise SystemExit(f"no committed Episodes found under {args.input_root}")
    return episodes


def _selected_dagger_episode_dirs(args: argparse.Namespace) -> list[Path]:
    episodes = [
        episode
        for episode in _selected_episode_dirs(args)
        if load_metadata(episode).get("collection_type") == "dagger"
    ]
    if not episodes:
        raise SystemExit(f"no committed DAgger Episodes found under {args.input_root}")
    return episodes


def _handle_inspect(args: argparse.Namespace) -> int:
    result = inspect_episode(args.episode)
    print(json.dumps(result.quality, indent=2, sort_keys=True))
    return 0


def _handle_clean(args: argparse.Namespace) -> int:
    summaries = []
    for episode_dir in _selected_episode_dirs(args):
        result = clean_episode(episode_dir, args.output_root)
        summaries.append(
            {
                "episode_id": result.quality["episode_id"],
                "grade": result.quality["grade"],
                "frame_groups": len(result.frame_groups),
                "output_dir": str(result.output_dir),
            }
        )
    print(json.dumps({"episodes": summaries}, indent=2, sort_keys=True))
    return 0


def _handle_classify_dagger(args: argparse.Namespace) -> int:
    summaries = []
    for episode_dir in _selected_dagger_episode_dirs(args):
        result, output = classify_dagger_episode(episode_dir, args.audit_root)
        summaries.append(
            {
                "episode_id": result.episode_id,
                "valid": result.valid,
                "expert_corrections": len(result.expert_segments),
                "issues": list(result.issues),
                "output_dir": str(output),
            }
        )
    print(json.dumps({"episodes": summaries}, indent=2, sort_keys=True))
    return 0 if all(summary["valid"] for summary in summaries) else 2


def _gripper_calibrations() -> tuple[GripperCalibration, GripperCalibration]:
    return ARX5_GRIPPER_CALIBRATION, ARX5_GRIPPER_CALIBRATION


def _print_selection(selection: DatasetSelection) -> None:
    print(
        json.dumps(
            {
                "selected_source_episodes": len(selection.episodes),
                "segments": sum(len(episode.segments) for episode in selection.episodes),
                "excluded_episodes": list(selection.excluded_episodes),
                "output_dir": str(selection.output_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _handle_select_pi05(args: argparse.Namespace) -> int:
    left_gripper, right_gripper = _gripper_calibrations()
    selection = select_dataset(
        _selected_episode_dirs(args),
        args.audit_root,
        args.output_root,
        args.task,
        left_gripper,
        right_gripper,
    )
    _print_selection(selection)
    return 0


def _handle_select_pi05_eef(args: argparse.Namespace) -> int:
    left_gripper, right_gripper = _gripper_calibrations()
    policy = EqualEefPolicy(
        eef_distance_m=args.eef_distance_mm / 1000.0,
        gripper_delta_threshold=args.gripper_delta_threshold,
        max_sample_interval_ns=round(args.max_sample_interval_ms * 1_000_000),
    )
    selection = select_equal_eef_dataset(
        _selected_episode_dirs(args),
        args.audit_root,
        args.output_root,
        args.task,
        left_gripper,
        right_gripper,
        policy,
    )
    _print_selection(selection)
    return 0


def _handle_select_pi05_eef_dagger(args: argparse.Namespace) -> int:
    left_gripper, right_gripper = _gripper_calibrations()
    policy = EqualEefPolicy(
        eef_distance_m=args.eef_distance_mm / 1000.0,
        gripper_delta_threshold=args.gripper_delta_threshold,
        max_sample_interval_ns=round(args.max_sample_interval_ms * 1_000_000),
    )
    selection = select_equal_eef_dagger_dataset(
        _selected_dagger_episode_dirs(args),
        args.audit_root,
        args.output_root,
        args.task,
        left_gripper,
        right_gripper,
        policy,
    )
    _print_selection(selection)
    return 0


def _named_paths(values: Sequence[str], label: str) -> dict[str, Path]:
    result = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path:
            raise SystemExit(f"{label} must use NAME=PATH")
        if name in result:
            raise SystemExit(f"duplicate {label} name: {name}")
        result[name] = Path(path)
    return result


def _named_weights(values: Sequence[str]) -> dict[str, float]:
    result = {}
    for value in values:
        name, separator, weight = value.partition("=")
        if not separator or not name or not weight:
            raise SystemExit("--weight must use NAME=FLOAT")
        if name in result:
            raise SystemExit(f"duplicate --weight name: {name}")
        result[name] = float(weight)
    return result


def _handle_mix_selections(args: argparse.Namespace) -> int:
    output = mix_selections(
        _named_paths(args.input, "--input"),
        args.output_root,
        _named_weights(args.weight),
    )
    print(json.dumps({"selection_dir": str(output)}, indent=2, sort_keys=True))
    return 0


def _handle_to_lerobot(args: argparse.Namespace) -> int:
    output = export_lerobot(
        args.input_root,
        args.selection_dir,
        args.output_root,
        args.repo_id,
        mode=args.mode,
        dataset_root=args.dataset_root,
    )
    print(json.dumps({"dataset_root": str(output)}, indent=2, sort_keys=True))
    return 0


def _handle_validate_pi05(args: argparse.Namespace) -> int:
    report = validate_lerobot(args.dataset_root, args.repo_id, expected_task=args.expected_task)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _handle_stream_to_lerobot(args: argparse.Namespace) -> int:
    request = StreamingRunRequest(
        config_path=args.config,
        output_override=args.output,
        run_id=args.run_id,
        resume_run_id=args.resume,
        retry_failed=args.retry_failed,
    )
    try:
        result = execute_streaming_conversion(request, sys.stdin, sys.stdout)
    except AlignmentCancelled as error:
        print(f"cancelled: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "output": str(result.snapshot.output_path),
                "repo_id": result.snapshot.repo_id,
                "source_episodes": result.snapshot.source_episode_count,
                "lerobot_episodes": result.snapshot.episode_count,
                "frames": result.snapshot.frame_count,
                "committed": result.committed,
                "excluded": result.excluded,
                "discarded": result.discarded,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-root", type=Path, action="append", required=True)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--outcome", action="append", choices=("success", "fail", "aborted"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arx5-dataset")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="inspect committed Episodes without writing artifacts")
    inspect_parser.add_argument("episode", type=Path)
    inspect_parser.set_defaults(handler=_handle_inspect)

    clean_parser = subparsers.add_parser("clean", help="write quality and model-independent frame indexes")
    clean_parser.add_argument("--input-root", type=Path, action="append", required=True)
    clean_parser.add_argument("--output-root", type=Path, required=True)
    clean_parser.add_argument("--outcome", action="append", choices=("success", "fail", "aborted"))
    clean_parser.set_defaults(handler=_handle_clean)

    classify_parser = subparsers.add_parser(
        "classify-dagger",
        help="validate sparse authority events and write fixed semantic intervals",
    )
    classify_parser.add_argument("--input-root", type=Path, action="append", required=True)
    classify_parser.add_argument("--audit-root", type=Path, required=True)
    classify_parser.add_argument("--outcome", action="append", choices=("success", "fail", "aborted"))
    classify_parser.set_defaults(handler=_handle_classify_dagger)

    select_parser = subparsers.add_parser(
        "select-pi05",
        help="build 50 Hz π0.5 sample and motion-segment indexes",
    )
    _add_selection_arguments(select_parser)
    select_parser.set_defaults(handler=_handle_select_pi05)

    eef_parser = subparsers.add_parser(
        "select-pi05-eef",
        help="build an equal-EEF-distance π0.5 trajectory index",
    )
    _add_selection_arguments(eef_parser)
    eef_parser.add_argument(
        "--eef-distance-mm",
        type=float,
        default=5.0,
        help="endpoint EEF translation threshold in millimetres",
    )
    eef_parser.add_argument(
        "--gripper-delta-threshold",
        type=float,
        default=0.02,
        help="trigger threshold after gripper normalization to [0,1]",
    )
    eef_parser.add_argument(
        "--max-sample-interval-ms",
        type=float,
        default=100.0,
        help="maximum real Header-time gap between retained trajectory samples",
    )
    eef_parser.set_defaults(handler=_handle_select_pi05_eef)

    dagger_eef_parser = subparsers.add_parser(
        "select-pi05-eef-dagger",
        help="apply the equal-EEF recipe independently to complete corrections",
    )
    _add_selection_arguments(dagger_eef_parser)
    dagger_eef_parser.add_argument("--eef-distance-mm", type=float, default=5.0)
    dagger_eef_parser.add_argument("--gripper-delta-threshold", type=float, default=0.02)
    dagger_eef_parser.add_argument("--max-sample-interval-ms", type=float, default=100.0)
    dagger_eef_parser.set_defaults(handler=_handle_select_pi05_eef_dagger)

    mix_parser = subparsers.add_parser(
        "mix-selections",
        help="merge compatible selections before one LeRobot export",
    )
    mix_parser.add_argument("--input", action="append", required=True, metavar="NAME=PATH")
    mix_parser.add_argument(
        "--weight",
        action="append",
        default=[],
        metavar="NAME=FLOAT",
        help="record a future dataloader weight without duplicating samples",
    )
    mix_parser.add_argument("--output-root", type=Path, required=True)
    mix_parser.set_defaults(handler=_handle_mix_selections)

    export_parser = subparsers.add_parser(
        "to-lerobot",
        help="decode selected RGB frames and export a LeRobot v2.1 dataset",
    )
    export_parser.add_argument("--input-root", type=Path, action="append", required=True)
    export_parser.add_argument("--selection-dir", type=Path, required=True)
    export_parser.add_argument("--output-root", type=Path, required=True)
    export_parser.add_argument("--repo-id", required=True)
    export_parser.add_argument("--mode", choices=("video", "image"), default="video")
    export_parser.add_argument(
        "--dataset-root",
        type=Path,
        help="optional exact LeRobot output directory instead of <output-root>/lerobot/<repo-id>",
    )
    export_parser.set_defaults(handler=_handle_to_lerobot)

    validate_parser = subparsers.add_parser(
        "validate-pi05",
        help="load a local LeRobot dataset with the π0.5 action horizon",
    )
    validate_parser.add_argument("--dataset-root", type=Path, required=True)
    validate_parser.add_argument("--repo-id", required=True)
    validate_parser.add_argument("--expected-task")
    validate_parser.set_defaults(handler=_handle_validate_pi05)

    stream_parser = subparsers.add_parser(
        "stream-to-lerobot",
        help="stream frozen cloud Episodes into one immutable LeRobot snapshot",
    )
    stream_parser.add_argument("--config", type=Path, required=True)
    stream_parser.add_argument("--output", type=Path)
    run_identity = stream_parser.add_mutually_exclusive_group()
    run_identity.add_argument("--run-id")
    run_identity.add_argument("--resume", metavar="RUN_ID")
    stream_parser.add_argument("--retry-failed", action="store_true")
    stream_parser.set_defaults(handler=_handle_stream_to_lerobot)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: CommandHandler = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
