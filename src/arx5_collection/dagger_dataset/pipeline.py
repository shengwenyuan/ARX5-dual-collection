from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from arx5_collection.cleaning.reader import load_metadata

from .classifier import classify_authority
from .models import AuthorityClassification
from .models import AuthorityEventRecord
from .reader import read_authority_events
from .store import write_authority_artifacts


EventReader = Callable[[Path], tuple[AuthorityEventRecord, ...]]


def classify_dagger_episode(
    episode_dir: Path,
    audit_root: Path,
    *,
    event_reader: EventReader = read_authority_events,
) -> tuple[AuthorityClassification, Path]:
    metadata = load_metadata(episode_dir)
    try:
        events = event_reader(episode_dir)
    except (KeyError, TypeError, ValueError) as error:
        dagger_metadata = metadata.get("dagger")
        intervention_count = (
            int(dagger_metadata.get("intervention_count", 0))
            if isinstance(dagger_metadata, dict)
            else 0
        )
        result = AuthorityClassification(
            episode_id=str(metadata.get("episode_id", episode_dir.name)),
            valid=False,
            issues=(f"authority reader failed: {error}",),
            episode_monotonic_anchor_ns=None,
            episode_bag_anchor_ns=None,
            bag_anchor_spread_ns=None,
            event_count=0,
            intervention_count=intervention_count,
            segments=(),
        )
    else:
        result = classify_authority(metadata, events)
    output = write_authority_artifacts(audit_root, result)
    return result, output
