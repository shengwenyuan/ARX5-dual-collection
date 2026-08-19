from __future__ import annotations

import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Protocol
from uuid import uuid4

from .models import InferenceTicket, PolicyExecutionProfile
from .observation import Pi05Observation, Pi05ObservationEncoder, VlaObservationStep


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Pi05PolicyRequest:
    session_id: str
    episode_id: str
    control_epoch: int
    inference_id: str
    checkpoint_sha256: str
    prompt: str
    observation: Pi05Observation

    def __post_init__(self) -> None:
        if not self.session_id or not self.episode_id or not self.inference_id:
            raise ValueError("policy request identifiers must not be empty")
        if self.control_epoch < 0 or not self.prompt:
            raise ValueError("policy request epoch and prompt are invalid")
        normalized = self.checkpoint_sha256.lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "checkpoint_sha256", normalized)


@dataclass(frozen=True, slots=True)
class Pi05PolicyResponse:
    session_id: str
    episode_id: str
    control_epoch: int
    inference_id: str
    checkpoint_sha256: str
    action_chunk: tuple[tuple[float, ...], ...]
    started_at_ns: int
    completed_at_ns: int

    def __post_init__(self) -> None:
        if not self.session_id or not self.episode_id or not self.inference_id:
            raise ValueError("policy response identifiers must not be empty")
        if self.control_epoch < 0:
            raise ValueError("policy response epoch must not be negative")
        normalized = self.checkpoint_sha256.lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "checkpoint_sha256", normalized)
        if self.started_at_ns < 0 or self.completed_at_ns < self.started_at_ns:
            raise ValueError("response inference timestamps are invalid")


class ObservationSource(Protocol):
    def capture(self) -> VlaObservationStep: ...


class Pi05PolicyTransport(Protocol):
    def infer(self, request: Pi05PolicyRequest) -> Pi05PolicyResponse: ...


class StalePolicyResponseError(RuntimeError):
    pass


class AsyncPi05PolicyClient:
    """Run one PI-style inference at a time outside the Session thread."""

    def __init__(
        self,
        session_id: str,
        prompt: str,
        checkpoint_sha256: str,
        observations: ObservationSource,
        encoder: Pi05ObservationEncoder,
        transport: Pi05PolicyTransport,
        execution: PolicyExecutionProfile,
    ) -> None:
        if not session_id or not prompt:
            raise ValueError("session_id and prompt must not be empty")
        normalized = checkpoint_sha256.lower()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("checkpoint_sha256 must contain 64 hexadecimal characters")
        self.session_id = session_id
        self.prompt = prompt
        self.checkpoint_sha256 = normalized
        self.observations = observations
        self.encoder = encoder
        self.transport = transport
        self.execution = execution
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dagger-policy"
        )
        self._lock = Lock()
        self._active_epoch = 0
        self._closed = False

    def begin_epoch(self, control_epoch: int) -> None:
        if control_epoch < 0:
            raise ValueError("control_epoch must not be negative")
        with self._lock:
            if control_epoch < self._active_epoch:
                raise ValueError("control_epoch must not move backwards")
            self._active_epoch = control_epoch

    def submit(
        self,
        episode_id: str,
        control_epoch: int,
        inference_id: str | None = None,
    ) -> Future[InferenceTicket]:
        if not episode_id:
            raise ValueError("episode_id must not be empty")
        inference_id = inference_id or uuid4().hex
        if not inference_id:
            raise ValueError("inference_id must not be empty")
        with self._lock:
            if self._closed:
                raise RuntimeError("policy client is closed")
            if control_epoch != self._active_epoch:
                raise StalePolicyResponseError("policy request epoch is not active")
        return self._executor.submit(
            self._infer, episode_id, control_epoch, inference_id
        )

    def close(self, timeout_s: float | None = None) -> None:
        del timeout_s
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _infer(
        self, episode_id: str, control_epoch: int, inference_id: str
    ) -> InferenceTicket:
        self._require_active_epoch(control_epoch)
        observation = self.encoder.encode(self.observations.capture())
        request = Pi05PolicyRequest(
            session_id=self.session_id,
            episode_id=episode_id,
            control_epoch=control_epoch,
            inference_id=inference_id,
            checkpoint_sha256=self.checkpoint_sha256,
            prompt=self.prompt,
            observation=observation,
        )
        response = self.transport.infer(request)
        self._validate_response(response, episode_id, control_epoch, inference_id)
        self._require_active_epoch(control_epoch)
        return InferenceTicket(
            inference_id=inference_id,
            control_epoch=control_epoch,
            checkpoint_sha256=response.checkpoint_sha256,
            action_chunk=response.action_chunk,
            execution=self.execution,
        )

    def _require_active_epoch(self, control_epoch: int) -> None:
        with self._lock:
            if control_epoch != self._active_epoch:
                raise StalePolicyResponseError("policy response belongs to an old epoch")

    def _validate_response(
        self,
        response: Pi05PolicyResponse,
        episode_id: str,
        control_epoch: int,
        inference_id: str,
    ) -> None:
        expected = (
            self.session_id,
            episode_id,
            control_epoch,
            inference_id,
            self.checkpoint_sha256,
        )
        actual = (
            response.session_id,
            response.episode_id,
            response.control_epoch,
            response.inference_id,
            response.checkpoint_sha256,
        )
        if actual != expected:
            raise RuntimeError("policy response correlation mismatch")
