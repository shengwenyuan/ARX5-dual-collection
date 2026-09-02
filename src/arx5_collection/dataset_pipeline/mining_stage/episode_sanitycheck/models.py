from __future__ import annotations

from dataclasses import dataclass

from arx5_collection.dataset_pipeline.source.models import FrameGroup


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
