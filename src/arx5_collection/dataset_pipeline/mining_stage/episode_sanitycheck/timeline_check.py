from __future__ import annotations

from dataclasses import dataclass

from arx5_collection.dataset_pipeline.source.models import LEFT_ARM_TOPIC
from arx5_collection.dataset_pipeline.source.models import MessageRef
from arx5_collection.dataset_pipeline.source.models import RIGHT_ARM_TOPIC
from arx5_collection.dataset_pipeline.source.models import required_topics

from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)
from arx5_collection.dataset_pipeline.execution.unit_runtime import require_output
from arx5_collection.dataset_pipeline.execution.unit_runtime import TimedRunner


@dataclass(frozen=True, slots=True)
class TimelineStats:
    count: int
    first_stamp_ns: int | None
    last_stamp_ns: int | None
    max_positive_gap_ns: int
    duplicate_count: int
    non_monotonic_count: int

    def to_dict(self) -> dict[str, int | None]:
        return {
            "count": self.count,
            "first_stamp_ns": self.first_stamp_ns,
            "last_stamp_ns": self.last_stamp_ns,
            "max_positive_gap_ns": self.max_positive_gap_ns,
            "duplicate_count": self.duplicate_count,
            "non_monotonic_count": self.non_monotonic_count,
        }


def audit_timeline(refs: tuple[MessageRef, ...]) -> TimelineStats:
    first = refs[0].header_stamp_ns if refs else None
    last = refs[-1].header_stamp_ns if refs else None
    max_gap = 0
    duplicates = 0
    non_monotonic = 0
    for previous, current in zip(refs, refs[1:]):
        gap = current.header_stamp_ns - previous.header_stamp_ns
        if gap == 0:
            duplicates += 1
        elif gap < 0:
            non_monotonic += 1
        else:
            max_gap = max(max_gap, gap)
    return TimelineStats(
        count=len(refs),
        first_stamp_ns=first,
        last_stamp_ns=last,
        max_positive_gap_ns=max_gap,
        duplicate_count=duplicates,
        non_monotonic_count=non_monotonic,
    )


def run(
    context: EpisodePipelineContext,
    unit: UnitSpec,
    timed: TimedRunner,
) -> None:
    scan = require_output(context.scan, "mcap_check")

    def operation() -> (
        tuple[dict[str, dict[str, int | None]], bool, tuple[str, ...], tuple[str, ...]]
    ):
        timeline = {
            topic: audit_timeline(scan.refs_by_topic[topic]).to_dict()
            for topic in required_topics(scan.capture_profile)
        }
        has_errors = any(
            stats["duplicate_count"] or stats["non_monotonic_count"]
            for stats in timeline.values()
        )
        camera_gap = int(unit.params["camera_gap_warning_ns"])
        arm_gap = int(unit.params["arm_gap_warning_ns"])
        excessive = tuple(
            topic
            for topic, stats in timeline.items()
            if stats["max_positive_gap_ns"]
            > (arm_gap if topic in (LEFT_ARM_TOPIC, RIGHT_ARM_TOPIC) else camera_gap)
        )
        issues = []
        if has_errors:
            issues.append(
                "one or more streams contain duplicate/non-monotonic Header timestamps"
            )
        for topic in excessive:
            issues.append(
                f"stream {topic} has a "
                f"{timeline[topic]['max_positive_gap_ns']} ns gap exceeding "
                "the warning threshold"
            )
        return timeline, has_errors, excessive, tuple(issues)

    (
        context.timeline,
        context.timeline_has_errors,
        context.excessive_gap_topics,
        context.issues,
    ) = timed(unit.type, operation)
