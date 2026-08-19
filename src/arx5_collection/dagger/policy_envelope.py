from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OpenPiPolicy(Protocol):
    def infer(self, observation: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class CorrelatedPolicyEnvelope:
    """Echo DAgger authority identifiers around an official openpi policy call."""

    def __init__(
        self,
        policy: OpenPiPolicy,
        checkpoint_sha256: str,
        wall_clock_ns: Callable[[], int],
    ) -> None:
        normalized = checkpoint_sha256.lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        self.policy = policy
        self.checkpoint_sha256 = normalized
        self.wall_clock_ns = wall_clock_ns

    def infer(self, request: Mapping[str, Any]) -> dict[str, Any]:
        session_id = _string(request, "session_id")
        episode_id = _string(request, "episode_id")
        inference_id = _string(request, "inference_id")
        prompt = _string(request, "prompt")
        control_epoch = request.get("control_epoch")
        if not isinstance(control_epoch, int) or isinstance(control_epoch, bool) or control_epoch < 0:
            raise ValueError("control_epoch must be a non-negative integer")
        requested_sha = _string(request, "checkpoint_sha256").lower()
        if requested_sha != self.checkpoint_sha256:
            raise ValueError("requested checkpoint SHA-256 does not match the loaded policy")
        observation = request.get("observation")
        if not isinstance(observation, Mapping):
            raise ValueError("observation must be a mapping")
        official_observation = dict(observation)
        official_observation["prompt"] = prompt

        started_at_ns = self.wall_clock_ns()
        result = self.policy.infer(official_observation)
        completed_at_ns = self.wall_clock_ns()
        if "actions" not in result:
            raise ValueError("openpi response does not contain actions")
        return {
            "session_id": session_id,
            "episode_id": episode_id,
            "control_epoch": control_epoch,
            "inference_id": inference_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "actions": result["actions"],
            "started_at_ns": started_at_ns,
            "completed_at_ns": completed_at_ns,
        }


def _string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value
