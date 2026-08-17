from __future__ import annotations

import argparse
import json
import os
import signal
import socketserver
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from arx5_collection.episode.ports import TriggerEvent

from .manager import CollectorControlConfig, CollectorManager, ControlConflict


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: Path, manager: CollectorManager) -> None:
        self.manager = manager
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        self.socket_path = socket_path
        super().__init__(str(socket_path), CollectorControlHandler)
        # The socket lives in a private Compose volume shared only with the
        # unprivileged UI bridge; no TCP control port is exposed.
        os.chmod(socket_path, 0o666)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.socket_path.unlink(missing_ok=True)


class CollectorControlHandler(BaseHTTPRequestHandler):
    server: ThreadingUnixHTTPServer

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/v1/snapshot":
            self._json(200, self.server.manager.snapshot())
            return
        if route.path == "/v1/logs":
            query = parse_qs(route.query)
            try:
                sequence = int(query.get("after", ["0"])[0])
                logs = self.server.manager.logs_since(sequence)
            except ValueError as error:
                self._error(400, str(error))
                return
            self._json(200, {"logs": logs})
            return
        self._error(404, "unknown endpoint")

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        try:
            payload = self._read_json()
            if route == "/v1/devices/inspect":
                self._require_empty(payload)
                self._json(200, {"devices": self.server.manager.inspect_devices()})
            elif route == "/v1/session/start":
                self._require_empty(payload)
                self.server.manager.start_session()
                self._json(202, {"accepted": True})
            elif route == "/v1/session/stop":
                self._require_empty(payload)
                self.server.manager.stop_session()
                self._json(202, {"accepted": True})
            elif route == "/v1/session/trigger":
                if set(payload) != {"event"}:
                    raise ValueError("trigger payload requires only event")
                self.server.manager.trigger(TriggerEvent(payload["event"]))
                self._json(202, {"accepted": True})
            else:
                self._error(404, "unknown endpoint")
        except ControlConflict as error:
            self._error(409, str(error))
        except (TypeError, ValueError) as error:
            self._error(400, str(error))
        except RuntimeError as error:
            self._error(503, str(error))

    def log_message(self, format: str, *args: object) -> None:
        return None

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length > 16_384:
            raise ValueError("request body is too large")
        body = self.rfile.read(length)
        if not body:
            return {}
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("request body must be JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    @staticmethod
    def _require_empty(payload: dict[str, Any]) -> None:
        if payload:
            raise ValueError("endpoint does not accept parameters")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arx5-collector-control")
    parser.add_argument(
        "--api-socket",
        type=Path,
        default=Path("/run/arx5-control/api.sock"),
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path("/run/arx5-control/collector"),
    )
    parser.add_argument(
        "--station-config",
        type=Path,
        default=Path("/var/lib/arx5-collection/station.json"),
    )
    parser.add_argument("--task-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--session-log-root",
        type=Path,
        default=Path("/reports/session-logs"),
    )
    parser.add_argument("--min-free-gib", type=int, default=80)
    parser.add_argument("--readiness-timeout-s", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = CollectorManager(
        CollectorControlConfig(
            runtime_dir=args.runtime_dir,
            station_config=args.station_config,
            task_config=args.task_config,
            output_root=args.output_root,
            session_log_root=args.session_log_root,
            min_free_gib=args.min_free_gib,
            readiness_timeout_s=args.readiness_timeout_s,
        )
    )
    server = ThreadingUnixHTTPServer(args.api_socket, manager)
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        manager.close()
        server.server_close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
