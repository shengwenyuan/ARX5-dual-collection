from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SegmentProvenance:
    collection_type: str
    training_class: str
    intervention_id: int | None = None
    authority_segment_id: str | None = None
    source_started_bag_timestamp_ns: int | None = None
    source_ended_bag_timestamp_ns: int | None = None

    def __post_init__(self) -> None:
        if self.collection_type not in {"demonstration", "dagger"}:
            raise ValueError("invalid segment collection type")
        if not self.training_class:
            raise ValueError("training class must not be empty")
        if self.collection_type == "dagger":
            if self.training_class != "expert_correction":
                raise ValueError("DAgger selection only accepts expert corrections")
            if self.intervention_id is None or self.intervention_id <= 0:
                raise ValueError("DAgger provenance requires intervention_id")
            if not self.authority_segment_id:
                raise ValueError("DAgger provenance requires authority_segment_id")
            if (
                self.source_started_bag_timestamp_ns is None
                or self.source_ended_bag_timestamp_ns is None
                or self.source_ended_bag_timestamp_ns
                < self.source_started_bag_timestamp_ns
            ):
                raise ValueError("DAgger provenance requires ordered bag boundaries")


def provenance_row(
    segment_id: str,
    source_episode_id: str,
    provenance: SegmentProvenance,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "segment_id": segment_id,
        "source_episode_id": source_episode_id,
        "split_group": source_episode_id,
        "collection_type": provenance.collection_type,
        "training_class": provenance.training_class,
        "intervention_id": provenance.intervention_id,
        "authority_segment_id": provenance.authority_segment_id,
        "source_started_bag_timestamp_ns": provenance.source_started_bag_timestamp_ns,
        "source_ended_bag_timestamp_ns": provenance.source_ended_bag_timestamp_ns,
        "sample_weight": 1.0,
    }
