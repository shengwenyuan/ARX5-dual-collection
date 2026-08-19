from __future__ import annotations

import threading
import unittest

try:
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve
except ImportError:  # Production base image intentionally omits DAgger transport deps.
    msgpack_numpy = None
    serve = None

from arx5_collection.dagger.observation import (
    Pi05Observation,
    RgbFrame,
)
from arx5_collection.dagger.openpi_transport import OpenPiDaggerTransport
from arx5_collection.dagger.policy_client import Pi05PolicyRequest


RGB = b"\x00" * (640 * 360 * 3)


@unittest.skipIf(serve is None, "openpi-client transport dependencies are unavailable")
class OpenPiTransportIntegrationTest(unittest.TestCase):
    def test_real_codec_and_websocket_round_trip_correlated_envelope(self) -> None:
        def handler(connection) -> None:
            connection.send(
                msgpack_numpy.packb(
                    {
                        "service": "arx5-dagger-policy",
                        "checkpoint_sha256": "a" * 64,
                    }
                )
            )
            request = msgpack_numpy.unpackb(connection.recv())
            self.assertEqual(
                request["observation"]["images"]["cam_high"].shape,
                (3, 360, 640),
            )
            connection.send(
                msgpack_numpy.packb(
                    {
                        "session_id": request["session_id"],
                        "episode_id": request["episode_id"],
                        "control_epoch": request["control_epoch"],
                        "inference_id": request["inference_id"],
                        "checkpoint_sha256": request["checkpoint_sha256"],
                        "actions": [[0.0] * 14] * 50,
                        "started_at_ns": 10,
                        "completed_at_ns": 20,
                    }
                )
            )

        with serve(handler, "127.0.0.1", 0, compression=None, max_size=None) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                port = server.socket.getsockname()[1]
                with OpenPiDaggerTransport(
                    "127.0.0.1", port, "a" * 64, timeout_s=2.0
                ) as transport:
                    response = transport.infer(self.request())
                self.assertEqual(response.session_id, "session-1")
                self.assertEqual(response.inference_id, "inference-1")
                self.assertEqual(len(response.action_chunk), 50)
            finally:
                server.shutdown()
                thread.join(2.0)

    @staticmethod
    def request() -> Pi05PolicyRequest:
        frame = RgbFrame(RGB, 100)
        return Pi05PolicyRequest(
            "session-1",
            "episode-1",
            0,
            "inference-1",
            "a" * 64,
            "task",
            Pi05Observation((0.0,) * 14, frame, frame, frame, 100),
        )


if __name__ == "__main__":
    unittest.main()
