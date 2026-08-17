from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable
from typing import Sequence

from arx5_collection.cleaning.pipeline import clean_episode
from arx5_collection.cleaning.pipeline import inspect_episode
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.discovery import discover_episode_dirs
from arx5_collection.pi05_dataset.exporter import export_lerobot
from arx5_collection.pi05_dataset.selection_pipeline import select_dataset
from arx5_collection.pi05_dataset.validate import compute_openpi_norm_stats
from arx5_collection.pi05_dataset.validate import validate_lerobot
from arx5_collection.pi05_dataset.validate import validate_openpi


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


def _handle_select_pi05(args: argparse.Namespace) -> int:
    selection = select_dataset(
        _selected_episode_dirs(args),
        args.audit_root,
        args.output_root,
        args.task,
        GripperCalibration(
            args.left_gripper_open,
            args.left_gripper_closed,
            args.gripper_tolerance,
        ),
        GripperCalibration(
            args.right_gripper_open,
            args.right_gripper_closed,
            args.gripper_tolerance,
        ),
    )
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


def _handle_validate_openpi(args: argparse.Namespace) -> int:
    report = validate_openpi(args.dataset_home, args.repo_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _handle_norm_stats(args: argparse.Namespace) -> int:
    report = compute_openpi_norm_stats(
        args.dataset_home,
        args.repo_id,
        args.output_dir,
        max_frames=args.max_frames,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


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

    select_parser = subparsers.add_parser(
        "select-pi05",
        help="build 50 Hz π0.5 sample and motion-segment indexes",
    )
    select_parser.add_argument("--input-root", type=Path, action="append", required=True)
    select_parser.add_argument("--audit-root", type=Path, required=True)
    select_parser.add_argument("--output-root", type=Path, required=True)
    select_parser.add_argument("--task", required=True)
    select_parser.add_argument("--left-gripper-open", type=float, required=True)
    select_parser.add_argument("--left-gripper-closed", type=float, required=True)
    select_parser.add_argument("--right-gripper-open", type=float, required=True)
    select_parser.add_argument("--right-gripper-closed", type=float, required=True)
    select_parser.add_argument("--gripper-tolerance", type=float, default=0.05)
    select_parser.add_argument("--outcome", action="append", choices=("success", "fail", "aborted"))
    select_parser.set_defaults(handler=_handle_select_pi05)

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

    openpi_parser = subparsers.add_parser(
        "validate-openpi",
        help="run the pinned openpi π0.5 transforms on a local dataset sample",
    )
    openpi_parser.add_argument("--dataset-home", type=Path, required=True)
    openpi_parser.add_argument("--repo-id", required=True)
    openpi_parser.set_defaults(handler=_handle_validate_openpi)

    norm_parser = subparsers.add_parser(
        "compute-openpi-norm-stats",
        help="compute fresh state/action statistics with pinned openpi transforms",
    )
    norm_parser.add_argument("--dataset-home", type=Path, required=True)
    norm_parser.add_argument("--repo-id", required=True)
    norm_parser.add_argument("--output-dir", type=Path, required=True)
    norm_parser.add_argument("--max-frames", type=int)
    norm_parser.set_defaults(handler=_handle_norm_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: CommandHandler = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
