from __future__ import annotations

from dataclasses import dataclass

from arx5_collection.cleaning.models import MessageRef


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
