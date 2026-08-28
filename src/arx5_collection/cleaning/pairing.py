from __future__ import annotations

from bisect import bisect_left
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass

from arx5_collection.capture import CaptureProfile
from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import CleaningPolicy
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.cleaning.models import ImagePair
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.cleaning.models import camera_topic
from arx5_collection.cleaning.models import required_topics


@dataclass(frozen=True, slots=True)
class CameraPairingStats:
    paired_count: int
    color_only_count: int
    depth_only_count: int

    def to_dict(self) -> dict[str, int]:
        return {
            "paired_count": self.paired_count,
            "color_only_count": self.color_only_count,
            "depth_only_count": self.depth_only_count,
        }


@dataclass(frozen=True, slots=True)
class PairingResult:
    frame_groups: tuple[FrameGroup, ...]
    camera_stats: dict[str, CameraPairingStats]
    common_start_ns: int | None
    common_end_ns: int | None
    eligible_overview_pairs: int
    rejected_cross_camera: int
    rejected_arm_age: int

    @property
    def coverage(self) -> float:
        if self.eligible_overview_pairs == 0:
            return 0.0
        return len(self.frame_groups) / self.eligible_overview_pairs


def _pair_same_camera(
    color_refs: tuple[MessageRef, ...],
    depth_refs: tuple[MessageRef, ...],
) -> tuple[tuple[ImagePair, ...], CameraPairingStats]:
    colors: dict[int, list[MessageRef]] = defaultdict(list)
    depths: dict[int, list[MessageRef]] = defaultdict(list)
    for ref in color_refs:
        colors[ref.header_stamp_ns].append(ref)
    for ref in depth_refs:
        depths[ref.header_stamp_ns].append(ref)

    pairs: list[ImagePair] = []
    color_only = 0
    depth_only = 0
    for stamp in sorted(set(colors) | set(depths)):
        color_items = sorted(colors.get(stamp, ()), key=lambda ref: ref.sequence)
        depth_items = sorted(depths.get(stamp, ()), key=lambda ref: ref.sequence)
        pair_count = min(len(color_items), len(depth_items))
        pairs.extend(ImagePair(color_items[index], depth_items[index]) for index in range(pair_count))
        color_only += len(color_items) - pair_count
        depth_only += len(depth_items) - pair_count
    return (
        tuple(pairs),
        CameraPairingStats(
            paired_count=len(pairs),
            color_only_count=color_only,
            depth_only_count=depth_only,
        ),
    )


def _nearest_pair(
    pairs: tuple[ImagePair, ...],
    stamps: tuple[int, ...],
    target_ns: int,
    tolerance_ns: int,
) -> ImagePair | None:
    if not pairs:
        return None
    insertion = bisect_left(stamps, target_ns)
    candidates = []
    if insertion < len(pairs):
        candidates.append(pairs[insertion])
    if insertion > 0:
        candidates.append(pairs[insertion - 1])
    selected = min(candidates, key=lambda pair: (abs(pair.stamp_ns - target_ns), pair.stamp_ns))
    return selected if abs(selected.stamp_ns - target_ns) <= tolerance_ns else None


def _latest_arm(
    samples: tuple[ArmSample, ...],
    stamps: tuple[int, ...],
    cutoff_ns: int,
    max_age_ns: int,
) -> ArmSample | None:
    index = bisect_right(stamps, cutoff_ns) - 1
    if index < 0:
        return None
    sample = samples[index]
    age_ns = cutoff_ns - sample.ref.header_stamp_ns
    return sample if 0 <= age_ns <= max_age_ns else None


def build_frame_groups(scan: EpisodeScan, policy: CleaningPolicy = CleaningPolicy()) -> PairingResult:
    paired: dict[str, tuple[ImagePair, ...]] = {}
    camera_stats: dict[str, CameraPairingStats] = {}
    for role in ("left", "right", "overview"):
        color_refs = scan.refs_by_topic[camera_topic(role, "color")]
        if scan.capture_profile is CaptureProfile.RGB_ONLY:
            pairs = tuple(ImagePair(color, None) for color in color_refs)
            stats = CameraPairingStats(len(pairs), 0, 0)
        else:
            pairs, stats = _pair_same_camera(
                color_refs,
                scan.refs_by_topic[camera_topic(role, "aligned_depth")],
            )
        paired[role] = pairs
        camera_stats[role] = stats

    first_stamps = []
    last_stamps = []
    for topic in required_topics(scan.capture_profile):
        refs = scan.refs_by_topic[topic]
        if not refs:
            return PairingResult((), camera_stats, None, None, 0, 0, 0)
        stamps = [ref.header_stamp_ns for ref in refs]
        first_stamps.append(min(stamps))
        last_stamps.append(max(stamps))
    common_start = max(first_stamps)
    common_end = min(last_stamps)
    if common_start > common_end:
        return PairingResult((), camera_stats, common_start, common_end, 0, 0, 0)

    left_pairs = tuple(sorted(paired["left"], key=lambda pair: (pair.stamp_ns, pair.color.sequence)))
    right_pairs = tuple(sorted(paired["right"], key=lambda pair: (pair.stamp_ns, pair.color.sequence)))
    overview_pairs = tuple(sorted(paired["overview"], key=lambda pair: (pair.stamp_ns, pair.color.sequence)))
    left_pair_stamps = tuple(pair.stamp_ns for pair in left_pairs)
    right_pair_stamps = tuple(pair.stamp_ns for pair in right_pairs)

    left_arm = tuple(sorted(scan.left_arm, key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence)))
    right_arm = tuple(sorted(scan.right_arm, key=lambda sample: (sample.ref.header_stamp_ns, sample.ref.sequence)))
    left_arm_stamps = tuple(sample.ref.header_stamp_ns for sample in left_arm)
    right_arm_stamps = tuple(sample.ref.header_stamp_ns for sample in right_arm)

    groups: list[FrameGroup] = []
    eligible_overview = 0
    rejected_cross_camera = 0
    rejected_arm_age = 0
    for overview in overview_pairs:
        if not common_start <= overview.stamp_ns <= common_end:
            continue
        eligible_overview += 1
        left = _nearest_pair(
            left_pairs,
            left_pair_stamps,
            overview.stamp_ns,
            policy.cross_camera_tolerance_ns,
        )
        right = _nearest_pair(
            right_pairs,
            right_pair_stamps,
            overview.stamp_ns,
            policy.cross_camera_tolerance_ns,
        )
        if left is None or right is None:
            rejected_cross_camera += 1
            continue
        cutoff = max(overview.stamp_ns, left.stamp_ns, right.stamp_ns)
        selected_left_arm = _latest_arm(left_arm, left_arm_stamps, cutoff, policy.arm_max_age_ns)
        selected_right_arm = _latest_arm(right_arm, right_arm_stamps, cutoff, policy.arm_max_age_ns)
        if selected_left_arm is None or selected_right_arm is None:
            rejected_arm_age += 1
            continue
        groups.append(
            FrameGroup(
                frame_group_id=len(groups),
                overview=overview,
                left=left,
                right=right,
                observation_cutoff_ns=cutoff,
                left_arm=selected_left_arm,
                right_arm=selected_right_arm,
            )
        )

    return PairingResult(
        frame_groups=tuple(groups),
        camera_stats=camera_stats,
        common_start_ns=common_start,
        common_end_ns=common_end,
        eligible_overview_pairs=eligible_overview,
        rejected_cross_camera=rejected_cross_camera,
        rejected_arm_age=rejected_arm_age,
    )
