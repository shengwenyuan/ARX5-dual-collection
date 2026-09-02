from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable
from typing import Sequence

from arx5_collection.dataset_pipeline.source.reader import load_metadata
from arx5_collection.dataset_pipeline.execution.bucketlink import BucketLinkRunRequest
from arx5_collection.dataset_pipeline.execution.bucketlink import (
    execute_bucketlink_conversion,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority import (
    classify_dagger_episode,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority.classifier import (
    AuthorityAlignmentPolicy,
)
from arx5_collection.dataset_pipeline.configuration.recipe import DatasetPipelineRecipe
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.recomposition.alignment import (
    CompositionAlignmentCancelled,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.recomposition.application import (
    CompositionRequest,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.recomposition.application import (
    execute_composition,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator.discovery import (
    discover_episode_dirs,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator import (
    export_lerobot,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.utils import (
    validate_lerobot,
)
from arx5_collection.dataset_pipeline.execution.confirmation import AlignmentCancelled
from arx5_collection.dataset_pipeline.application import DatasetPipelineRequest
from arx5_collection.dataset_pipeline.application import DatasetPipelineResult
from arx5_collection.dataset_pipeline.application import execute_dataset_pipeline


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


def _handle_classify_dagger(args: argparse.Namespace) -> int:
    recipe = DatasetPipelineRecipe.load(args.recipe)
    unit = next(
        item
        for item in recipe.pipeline.stage("action_mining").units
        if item.type == "dagger_authority"
    )
    policy = AuthorityAlignmentPolicy.from_params(unit.params)
    summaries = []
    for episode_dir in _selected_dagger_episode_dirs(args):
        result, output = classify_dagger_episode(episode_dir, args.audit_root, policy)
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
    report = validate_lerobot(
        args.dataset_root, args.repo_id, expected_task=args.expected_task
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _handle_build(args: argparse.Namespace) -> int:
    request = DatasetPipelineRequest(
        config_path=args.config,
        output_override=args.output,
        run_id=args.run_id,
        resume_run_id=args.resume,
        retry_failed=args.retry_failed,
    )
    try:
        result = execute_dataset_pipeline(request, sys.stdin, sys.stdout)
    except AlignmentCancelled as error:
        print(f"cancelled: {error}", file=sys.stderr)
        return 2
    _print_pipeline_result(result)
    return 0


def _handle_bucketlink(args: argparse.Namespace) -> int:
    request = BucketLinkRunRequest(
        config_path=args.config,
        output_override=args.output,
        run_id=args.run_id,
        resume_run_id=args.resume,
        retry_failed=args.retry_failed,
    )
    try:
        result = execute_bucketlink_conversion(request, sys.stdin, sys.stdout)
    except AlignmentCancelled as error:
        print(f"cancelled: {error}", file=sys.stderr)
        return 2
    _print_pipeline_result(result)
    return 0


def _print_pipeline_result(result: DatasetPipelineResult) -> None:
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


def _handle_compose_lerobot(args: argparse.Namespace) -> int:
    try:
        result = execute_composition(
            CompositionRequest(args.config), sys.stdin, sys.stdout
        )
    except CompositionAlignmentCancelled as error:
        print(f"cancelled: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(result.output_path),
                "repo_id": result.repo_id,
                "backend": result.backend,
                "episodes": result.episode_count,
                "frames": result.frame_count,
                "videos": result.video_count,
                "tasks": list(result.tasks),
                "plan_fingerprint": result.plan_fingerprint,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arx5-dataset")
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify_parser = subparsers.add_parser(
        "classify-dagger",
        help="validate sparse authority events and write fixed semantic intervals",
    )
    classify_parser.add_argument(
        "--input-root", type=Path, action="append", required=True
    )
    classify_parser.add_argument("--audit-root", type=Path, required=True)
    classify_parser.add_argument("--recipe", required=True)
    classify_parser.add_argument(
        "--outcome", action="append", choices=("success", "fail", "aborted")
    )
    classify_parser.set_defaults(handler=_handle_classify_dagger)

    export_parser = subparsers.add_parser(
        "to-lerobot",
        help="decode selected RGB frames and export a LeRobot v2.1 dataset",
    )
    export_parser.add_argument(
        "--input-root", type=Path, action="append", required=True
    )
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

    pipeline_parser = subparsers.add_parser(
        "build",
        help="stream frozen cloud Episodes into one immutable LeRobot snapshot",
    )
    pipeline_parser.add_argument("--config", type=Path, required=True)
    pipeline_parser.add_argument("--output", type=Path)
    run_identity = pipeline_parser.add_mutually_exclusive_group()
    run_identity.add_argument("--run-id")
    run_identity.add_argument("--resume", metavar="RUN_ID")
    pipeline_parser.add_argument("--retry-failed", action="store_true")
    pipeline_parser.set_defaults(handler=_handle_build)

    bucketlink_parser = subparsers.add_parser(
        "bucketlink-to-lerobot",
        help="import one BOS task/date batch to PFS, then build LeRobot",
    )
    bucketlink_parser.add_argument("--config", type=Path, required=True)
    bucketlink_parser.add_argument("--output", type=Path)
    bucketlink_identity = bucketlink_parser.add_mutually_exclusive_group(required=True)
    bucketlink_identity.add_argument("--run-id")
    bucketlink_identity.add_argument("--resume", metavar="RUN_ID")
    bucketlink_parser.add_argument("--retry-failed", action="store_true")
    bucketlink_parser.set_defaults(handler=_handle_bucketlink)

    compose_parser = subparsers.add_parser(
        "compose-lerobot",
        help="select and recompose immutable LeRobot snapshots without reading MCAP",
    )
    compose_parser.add_argument("--config", type=Path, required=True)
    compose_parser.set_defaults(handler=_handle_compose_lerobot)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: CommandHandler = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
