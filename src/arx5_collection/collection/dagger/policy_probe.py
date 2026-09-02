from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from .config import DaggerCollectorSettings
from .models import InferenceTicket
from .observation import Pi05Observation, RgbFrame
from .policy_client import (
    Pi05PolicyRequest,
    Pi05PolicyResponse,
    RtcPolicyContext,
)


class PolicyProbeTransport(Protocol):
    def infer(self, request: Pi05PolicyRequest) -> Pi05PolicyResponse: ...


@dataclass(frozen=True, slots=True)
class RtcPolicyProbeResult:
    bootstrap_s: float
    rtc_s: float
    prefix_steps: int
    prefix_max_error: float


def run_rtc_policy_probe(
    settings: DaggerCollectorSettings,
    transport: PolicyProbeTransport,
) -> RtcPolicyProbeResult:
    """Exercise bootstrap and RTC wire paths without touching robot devices."""
    profile = settings.checkpoint_profile
    rollout = settings.rtc_rollout
    if profile.policy_type != "training_time_rtc" or rollout is None:
        raise ValueError("policy probe requires a training-time RTC profile")

    observation = _zero_observation(settings)
    bootstrap_request = _request(settings, observation, "probe-bootstrap")
    started = monotonic()
    bootstrap = transport.infer(bootstrap_request)
    bootstrap_s = monotonic() - started
    bootstrap_ticket = _validate_response(settings, bootstrap_request, bootstrap)

    prefix_steps = rollout.initial_delay_steps
    prefix = bootstrap_ticket.action_chunk[:prefix_steps]
    rtc_request = _request(
        settings,
        observation,
        "probe-rtc",
        RtcPolicyContext(prefix_steps, prefix),
    )
    started = monotonic()
    rtc = transport.infer(rtc_request)
    rtc_s = monotonic() - started
    rtc_ticket = _validate_response(settings, rtc_request, rtc)

    prefix_max_error = max(
        abs(actual - expected)
        for actual_row, expected_row in zip(
            rtc_ticket.action_chunk[:prefix_steps], prefix, strict=True
        )
        for actual, expected in zip(actual_row, expected_row, strict=True)
    )
    if prefix_max_error > profile.hard_prefix_tolerance:
        raise RuntimeError(
            "RTC hard-prefix round-trip exceeded tolerance: "
            f"error={prefix_max_error}, tolerance={profile.hard_prefix_tolerance}"
        )
    return RtcPolicyProbeResult(
        bootstrap_s=bootstrap_s,
        rtc_s=rtc_s,
        prefix_steps=prefix_steps,
        prefix_max_error=prefix_max_error,
    )


def _zero_observation(settings: DaggerCollectorSettings) -> Pi05Observation:
    image = settings.checkpoint_profile.input
    frame = RgbFrame(
        bytes(image.width * image.height * image.channels),
        stamp_ns=0,
        width=image.width,
        height=image.height,
    )
    return Pi05Observation(
        state=(0.0,) * settings.execution.action_dimension,
        camera_high=frame,
        camera_left_wrist=frame,
        camera_right_wrist=frame,
        cutoff_ns=0,
    )


def _request(
    settings: DaggerCollectorSettings,
    observation: Pi05Observation,
    inference_id: str,
    rtc: RtcPolicyContext | None = None,
) -> Pi05PolicyRequest:
    return Pi05PolicyRequest(
        session_id="policy-probe",
        episode_id="policy-probe",
        control_epoch=0,
        inference_id=inference_id,
        checkpoint_sha256=settings.checkpoint_sha256,
        prompt=settings.prompt,
        observation=observation,
        rtc=rtc,
    )


def _validate_response(
    settings: DaggerCollectorSettings,
    request: Pi05PolicyRequest,
    response: Pi05PolicyResponse,
) -> InferenceTicket:
    expected = (
        request.session_id,
        request.episode_id,
        request.control_epoch,
        request.inference_id,
        request.checkpoint_sha256,
    )
    actual = (
        response.session_id,
        response.episode_id,
        response.control_epoch,
        response.inference_id,
        response.checkpoint_sha256,
    )
    if actual != expected:
        raise RuntimeError("policy probe response correlation mismatch")
    return InferenceTicket(
        inference_id=response.inference_id,
        control_epoch=response.control_epoch,
        checkpoint_sha256=response.checkpoint_sha256,
        action_chunk=response.action_chunk,
        execution=settings.execution,
    )
