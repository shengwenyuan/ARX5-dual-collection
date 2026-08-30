from __future__ import annotations

from io import TextIOBase

from .models import CompositionPlan


class CompositionAlignmentCancelled(RuntimeError):
    pass


def align_plan(plan: CompositionPlan, stdin: TextIOBase, stdout: TextIOBase) -> None:
    print("LeRobot snapshot composition", file=stdout)
    print(f"  backend: {plan.config.output.backend}", file=stdout)
    print(f"  output: {plan.config.output.path}", file=stdout)
    print(f"  repo_id: {plan.config.output.repo_id}", file=stdout)
    for source in _sources(plan):
        count = sum(item.source.name == source.name for item in plan.selected)
        frames = sum(
            item.episode.length for item in plan.selected if item.source.name == source.name
        )
        operation = "hardlink-or-copy" if source.backend == "lerobot-v2.1" else "whole-shard-copy"
        print(
            f"  source {source.name}: {source.backend}, episodes={count}, frames={frames}, video={operation}",
            file=stdout,
        )
    print(
        f"  total: episodes={len(plan.selected)}, frames={plan.frame_count}, "
        f"videos={plan.video_count}, tasks={len(plan.tasks)}",
        file=stdout,
    )
    print(f"  plan: {plan.fingerprint}", file=stdout)
    print("Press Enter to materialize; any text cancels.", file=stdout, flush=True)
    answer = stdin.readline()
    if answer == "" or answer.rstrip("\r\n"):
        raise CompositionAlignmentCancelled("composition was not confirmed with an empty line")


def _sources(plan: CompositionPlan):
    seen = set()
    for item in plan.selected:
        if item.source.name not in seen:
            seen.add(item.source.name)
            yield item.source
