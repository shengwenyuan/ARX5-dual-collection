"""Temporary compatibility aliases for historical BOS task descriptions."""

from __future__ import annotations


LEGACY_EIGHT_STREAM_DESCRIPTION = (
    "Record synchronized dual-arm state and three aligned RGB-D camera streams"
)

# TODO(remove-legacy-bos-task-alias): Delete this alias after every historical
# BOS Episode carrying the generic eight-stream description has been backfilled
# with its real task description and all derived snapshots have been rebuilt.
TEMPORARY_TASK_ALIASES = {
    LEGACY_EIGHT_STREAM_DESCRIPTION: "folding the cloth",
}


def normalize_historical_task(task: str) -> str:
    return TEMPORARY_TASK_ALIASES.get(task, task)
