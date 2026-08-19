from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .policy_client import Pi05PolicyRequest, Pi05PolicyResponse


class OpenPiDaggerTransport:
    """Bounded synchronous WebSocket transport using openpi's msgpack codec."""

    def __init__(
        self,
        host: str,
        port: int,
        checkpoint_sha256: str,
        timeout_s: float = 30.0,
    ) -> None:
        if not host or not 0 < port <= 65535 or timeout_s <= 0:
            raise ValueError("policy transport host, port, and timeout are invalid")
        import numpy as np
        from openpi_client import msgpack_numpy
        import websockets.sync.client

        self._numpy = np
        self._codec = msgpack_numpy
        self._timeout_s = timeout_s
        self._connection = websockets.sync.client.connect(
            f"ws://{host}:{port}",
            compression=None,
            max_size=None,
            open_timeout=timeout_s,
            close_timeout=min(timeout_s, 5.0),
        )
        metadata = self._recv_mapping()
        if metadata.get("service") != "arx5-dagger-policy":
            self.close()
            raise RuntimeError("connected server is not an ARX5 DAgger policy service")
        if str(metadata.get("checkpoint_sha256", "")).lower() != checkpoint_sha256.lower():
            self.close()
            raise RuntimeError("policy server checkpoint SHA-256 mismatch")

    def __enter__(self) -> OpenPiDaggerTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def infer(self, request: Pi05PolicyRequest) -> Pi05PolicyResponse:
        payload = policy_request_to_wire(request, self._numpy)
        self._connection.send(self._codec.packb(payload))
        response = self._recv_mapping()
        return policy_response_from_wire(response)

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None

    def _recv_mapping(self) -> Mapping[str, Any]:
        if self._connection is None:
            raise RuntimeError("policy transport is closed")
        wire = self._connection.recv(timeout=self._timeout_s)
        if isinstance(wire, str):
            raise RuntimeError(f"policy server error: {wire}")
        value = self._codec.unpackb(wire)
        if not isinstance(value, Mapping):
            raise RuntimeError("policy server response is not a mapping")
        return value


def policy_request_to_wire(request: Pi05PolicyRequest, numpy_module: Any) -> dict[str, Any]:
    observation = request.observation
    return {
        "session_id": request.session_id,
        "episode_id": request.episode_id,
        "control_epoch": request.control_epoch,
        "inference_id": request.inference_id,
        "checkpoint_sha256": request.checkpoint_sha256,
        "prompt": request.prompt,
        "observation": {
            "state": numpy_module.asarray(observation.state, dtype=numpy_module.float64),
            "images": {
                "cam_high": _image_array(observation.camera_high, numpy_module),
                "cam_left_wrist": _image_array(
                    observation.camera_left_wrist, numpy_module
                ),
                "cam_right_wrist": _image_array(
                    observation.camera_right_wrist, numpy_module
                ),
            },
        },
    }


def policy_response_from_wire(response: Mapping[str, Any]) -> Pi05PolicyResponse:
    try:
        actions = tuple(tuple(float(value) for value in row) for row in response["actions"])
        return Pi05PolicyResponse(
            session_id=str(response["session_id"]),
            episode_id=str(response["episode_id"]),
            control_epoch=int(response["control_epoch"]),
            inference_id=str(response["inference_id"]),
            checkpoint_sha256=str(response["checkpoint_sha256"]),
            action_chunk=actions,
            started_at_ns=int(response["started_at_ns"]),
            completed_at_ns=int(response["completed_at_ns"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"invalid policy response: {error}") from error


def _image_array(frame: Any, numpy_module: Any) -> Any:
    # LeRobotAlohaDataConfig expects raw camera fields in CHW order and its
    # official AlohaInputs transform converts them to HWC for the model.
    return (
        numpy_module.frombuffer(frame.data, dtype=numpy_module.uint8)
        .reshape(frame.height, frame.width, 3)
        .transpose(2, 0, 1)
    )
