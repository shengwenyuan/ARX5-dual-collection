from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any
from typing import Mapping
from typing import Sequence

from ..models import AuthorityClass
from ..models import AuthorityClassification
from ..models import AuthorityEventRecord
from ..models import AuthorityEventType
from ..models import AuthoritySegment


@dataclass(frozen=True, slots=True)
class AuthorityAlignmentPolicy:
    monotonic_anchor_tolerance_ns: int
    bag_anchor_tolerance_ns: int

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> AuthorityAlignmentPolicy:
        keys = {"monotonic_anchor_tolerance_ns", "bag_anchor_tolerance_ns"}
        if set(params) != keys:
            raise ValueError(f"dagger_authority params must be exactly {sorted(keys)}")
        result = cls(
            int(params["monotonic_anchor_tolerance_ns"]),
            int(params["bag_anchor_tolerance_ns"]),
        )
        if (
            min(result.monotonic_anchor_tolerance_ns, result.bag_anchor_tolerance_ns)
            <= 0
        ):
            raise ValueError("dagger authority anchor tolerances must be positive")
        return result


@dataclass(frozen=True, slots=True)
class _MetadataSegment:
    owner: str
    started_offset_ns: int
    ended_offset_ns: int
    intervention_id: int | None


@dataclass(frozen=True, slots=True)
class _OpenInterval:
    authority_class: AuthorityClass
    started_offset_ns: int
    intervention_id: int | None


def _offset_ns(value: object, label: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if number < 0:
        raise ValueError(f"{label} must be non-negative")
    return round(number * 1_000_000_000)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _metadata_contract(
    metadata: Mapping[str, Any],
) -> tuple[str, int, int, tuple[_MetadataSegment, ...]]:
    if metadata.get("collection_type") != "dagger":
        raise ValueError("metadata collection_type is not dagger")
    episode_id = str(metadata.get("episode_id", ""))
    if not episode_id:
        raise ValueError("metadata episode_id is missing")
    duration_ns = _offset_ns(
        _mapping(metadata.get("timing"), "timing")["duration_s"],
        "timing.duration_s",
    )
    dagger = _mapping(metadata.get("dagger"), "dagger")
    intervention_count = int(dagger["intervention_count"])
    if intervention_count < 0:
        raise ValueError("dagger.intervention_count must not be negative")
    raw_segments = dagger.get("control_segments")
    if not isinstance(raw_segments, list):
        raise ValueError("dagger.control_segments must be an array")
    segments = []
    previous_end = 0
    for index, raw in enumerate(raw_segments):
        segment = _mapping(raw, f"dagger.control_segments[{index}]")
        owner = str(segment["owner"])
        if owner not in {"model", "human"}:
            raise ValueError(f"invalid control owner: {owner}")
        start = _offset_ns(segment["started_offset_s"], "started_offset_s")
        end = _offset_ns(segment["ended_offset_s"], "ended_offset_s")
        if start < previous_end or end < start or end > duration_ns:
            raise ValueError("metadata control segments are not ordered within Episode")
        intervention_id = (
            int(segment["intervention_id"]) if "intervention_id" in segment else None
        )
        if owner == "human" and (intervention_id is None or intervention_id <= 0):
            raise ValueError("human metadata segment requires intervention_id")
        if owner == "model" and intervention_id is not None:
            raise ValueError("model metadata segment must not have intervention_id")
        segments.append(_MetadataSegment(owner, start, end, intervention_id))
        previous_end = end
    return episode_id, duration_ns, intervention_count, tuple(segments)


def _validate_event_order(events: Sequence[AuthorityEventRecord]) -> None:
    for previous, current in zip(events, events[1:]):
        if current.sequence != previous.sequence + 1:
            raise ValueError("authority sequence is not consecutive")
        if current.monotonic_time_ns < previous.monotonic_time_ns:
            raise ValueError("authority monotonic time is not ordered")
        if current.bag_timestamp_ns < previous.bag_timestamp_ns:
            raise ValueError("authority bag time is not ordered")
        if current.control_epoch < previous.control_epoch:
            raise ValueError("authority control epoch regressed")


def _anchor_candidates(
    events: Sequence[AuthorityEventRecord],
    metadata_segments: Sequence[_MetadataSegment],
    duration_ns: int,
) -> list[int]:
    candidates: list[int] = []
    segment_index = 0
    active: _MetadataSegment | None = None
    state = "resume"
    expected_intervention = 0
    for event in events:
        if state == "resume":
            if event.event_type is AuthorityEventType.POLICY_ACTIVE:
                if event.intervention_id != expected_intervention:
                    raise ValueError("POLICY_ACTIVE intervention id mismatch")
                if segment_index >= len(metadata_segments):
                    raise ValueError(
                        "POLICY_ACTIVE has no matching model metadata segment"
                    )
                active = metadata_segments[segment_index]
                if active.owner != "model":
                    raise ValueError(
                        "POLICY_ACTIVE does not match a model metadata segment"
                    )
                candidates.append(event.monotonic_time_ns - active.started_offset_ns)
                state = "policy"
                continue
            if event.event_type is AuthorityEventType.FAULT_HOLD:
                state = "fault"
                continue
            raise ValueError(
                f"unexpected {event.event_type.name} while policy is pending"
            )
        if state == "policy":
            if event.event_type is AuthorityEventType.TAKEOVER_REQUESTED:
                assert active is not None
                expected_intervention += 1
                if event.intervention_id != expected_intervention:
                    raise ValueError(
                        "TAKEOVER_REQUESTED intervention id is not consecutive"
                    )
                candidates.append(event.monotonic_time_ns - active.ended_offset_ns)
                segment_index += 1
                active = None
                state = "handover"
                continue
            if event.event_type is AuthorityEventType.FAULT_HOLD:
                assert active is not None
                candidates.append(event.monotonic_time_ns - active.ended_offset_ns)
                segment_index += 1
                active = None
                state = "fault"
                continue
            raise ValueError(
                f"unexpected {event.event_type.name} during policy control"
            )
        if state == "handover":
            if event.event_type is AuthorityEventType.HUMAN_ACTIVE:
                if event.intervention_id != expected_intervention:
                    raise ValueError("HUMAN_ACTIVE intervention id mismatch")
                if segment_index >= len(metadata_segments):
                    raise ValueError("HUMAN_ACTIVE has no matching metadata segment")
                active = metadata_segments[segment_index]
                if (
                    active.owner != "human"
                    or active.intervention_id != expected_intervention
                ):
                    raise ValueError(
                        "HUMAN_ACTIVE does not match human metadata segment"
                    )
                candidates.append(event.monotonic_time_ns - active.started_offset_ns)
                state = "human"
                continue
            if event.event_type is AuthorityEventType.FAULT_HOLD:
                state = "fault"
                continue
            raise ValueError(f"unexpected {event.event_type.name} during handover")
        if state == "human":
            if event.event_type is AuthorityEventType.RESUME_REQUESTED:
                assert active is not None
                if event.intervention_id != expected_intervention:
                    raise ValueError("RESUME_REQUESTED intervention id mismatch")
                candidates.append(event.monotonic_time_ns - active.ended_offset_ns)
                segment_index += 1
                active = None
                state = "resume"
                continue
            if event.event_type is AuthorityEventType.FAULT_HOLD:
                assert active is not None
                candidates.append(event.monotonic_time_ns - active.ended_offset_ns)
                segment_index += 1
                active = None
                state = "fault"
                continue
            raise ValueError(f"unexpected {event.event_type.name} during human control")
        raise ValueError("authority events continue after FAULT_HOLD")
    if active is not None:
        if active.ended_offset_ns != duration_ns:
            raise ValueError("active metadata segment does not end at Episode boundary")
        segment_index += 1
    if segment_index != len(metadata_segments):
        raise ValueError("authority events do not cover metadata control segments")
    return candidates


def _append_interval(
    rows: list[AuthoritySegment],
    episode_id: str,
    opened: _OpenInterval,
    ended_offset_ns: int,
    bag_anchor_ns: int,
    *,
    complete: bool,
) -> None:
    if ended_offset_ns < opened.started_offset_ns:
        raise ValueError("authority interval end precedes start")
    eligible = opened.authority_class is AuthorityClass.EXPERT_CORRECTION and complete
    reason = None
    if opened.authority_class is not AuthorityClass.EXPERT_CORRECTION:
        reason = f"authority_{opened.authority_class.value}"
    elif not complete:
        reason = "incomplete_correction"
    rows.append(
        AuthoritySegment(
            segment_id=f"{episode_id}--authority-{len(rows):03d}",
            authority_class=opened.authority_class,
            started_offset_ns=opened.started_offset_ns,
            ended_offset_ns=ended_offset_ns,
            started_bag_timestamp_ns=bag_anchor_ns + opened.started_offset_ns,
            ended_bag_timestamp_ns=bag_anchor_ns + ended_offset_ns,
            intervention_id=opened.intervention_id,
            complete=complete,
            training_eligible=eligible,
            exclusion_reason=reason,
        )
    )


def _build_intervals(
    episode_id: str,
    duration_ns: int,
    events: Sequence[AuthorityEventRecord],
    monotonic_anchor_ns: int,
    bag_anchor_ns: int,
) -> tuple[AuthoritySegment, ...]:
    rows: list[AuthoritySegment] = []
    opened = _OpenInterval(AuthorityClass.RESUME, 0, None)
    state = AuthorityClass.RESUME
    intervention_id: int | None = None
    for event in events:
        offset = event.monotonic_time_ns - monotonic_anchor_ns
        if not 0 <= offset <= duration_ns:
            raise ValueError("authority event falls outside Episode duration")
        if event.event_type is AuthorityEventType.POLICY_ACTIVE:
            next_class = AuthorityClass.POLICY
            next_intervention = None
        elif event.event_type is AuthorityEventType.TAKEOVER_REQUESTED:
            next_class = AuthorityClass.HANDOVER
            next_intervention = event.intervention_id
        elif event.event_type is AuthorityEventType.HUMAN_ACTIVE:
            next_class = AuthorityClass.EXPERT_CORRECTION
            next_intervention = event.intervention_id
        elif event.event_type is AuthorityEventType.RESUME_REQUESTED:
            next_class = AuthorityClass.RESUME
            next_intervention = event.intervention_id
        else:
            next_class = AuthorityClass.FAULT
            next_intervention = event.intervention_id or intervention_id
        complete = (
            state is not AuthorityClass.EXPERT_CORRECTION
            or event.event_type is AuthorityEventType.RESUME_REQUESTED
        )
        _append_interval(
            rows,
            episode_id,
            opened,
            offset,
            bag_anchor_ns,
            complete=complete,
        )
        state = next_class
        intervention_id = next_intervention
        opened = _OpenInterval(next_class, offset, next_intervention)
    _append_interval(
        rows,
        episode_id,
        opened,
        duration_ns,
        bag_anchor_ns,
        complete=opened.authority_class is not AuthorityClass.EXPERT_CORRECTION,
    )
    return tuple(row for row in rows if row.ended_offset_ns > row.started_offset_ns)


def classify_authority(
    metadata: Mapping[str, Any],
    events: Sequence[AuthorityEventRecord],
    policy: AuthorityAlignmentPolicy,
) -> AuthorityClassification:
    episode_id = str(metadata.get("episode_id", "unknown"))
    intervention_count = 0
    try:
        episode_id, duration_ns, intervention_count, metadata_segments = (
            _metadata_contract(metadata)
        )
        dagger = _mapping(metadata["dagger"], "dagger")
        if "shadow" in dagger:
            if events:
                raise ValueError("shadow Episode must not contain authority events")
            return AuthorityClassification(
                episode_id, True, ("shadow_episode",), None, None, None, 0, 0, ()
            )
        _validate_event_order(events)
        candidates = _anchor_candidates(events, metadata_segments, duration_ns)
        takeover_ids = [
            event.intervention_id
            for event in events
            if event.event_type is AuthorityEventType.TAKEOVER_REQUESTED
        ]
        if len(takeover_ids) != intervention_count:
            raise ValueError("authority intervention count does not match metadata")
        if not candidates:
            if events or metadata_segments:
                raise ValueError("cannot derive Episode time anchor")
            return AuthorityClassification(
                episode_id, True, ("no_authority_activity",), None, None, None, 0, 0, ()
            )
        anchor_spread = max(candidates) - min(candidates)
        if anchor_spread > policy.monotonic_anchor_tolerance_ns:
            raise ValueError(
                f"metadata/event monotonic anchor spread {anchor_spread} ns exceeds "
                f"{policy.monotonic_anchor_tolerance_ns} ns"
            )
        monotonic_anchor = round(median(candidates))
        bag_candidates = [
            event.bag_timestamp_ns - (event.monotonic_time_ns - monotonic_anchor)
            for event in events
        ]
        bag_spread = max(bag_candidates) - min(bag_candidates)
        if bag_spread > policy.bag_anchor_tolerance_ns:
            raise ValueError(
                f"authority bag anchor spread {bag_spread} ns exceeds "
                f"{policy.bag_anchor_tolerance_ns} ns"
            )
        bag_anchor = min(bag_candidates)
        segments = _build_intervals(
            episode_id,
            duration_ns,
            events,
            monotonic_anchor,
            bag_anchor,
        )
        return AuthorityClassification(
            episode_id=episode_id,
            valid=True,
            issues=(),
            episode_monotonic_anchor_ns=monotonic_anchor,
            episode_bag_anchor_ns=bag_anchor,
            bag_anchor_spread_ns=bag_spread,
            event_count=len(events),
            intervention_count=intervention_count,
            segments=segments,
        )
    except (KeyError, TypeError, ValueError) as error:
        return AuthorityClassification(
            episode_id=episode_id,
            valid=False,
            issues=(str(error),),
            episode_monotonic_anchor_ns=None,
            episode_bag_anchor_ns=None,
            bag_anchor_spread_ns=None,
            event_count=len(events),
            intervention_count=intervention_count,
            segments=(),
        )
