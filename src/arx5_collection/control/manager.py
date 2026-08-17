from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, IO, Protocol

from arx5_collection.episode.adapters.remote import send_trigger
from arx5_collection.episode.ports import TriggerEvent
from arx5_collection.production.events import EVENT_SCHEMA_VERSION


class ControlConflict(RuntimeError):
    """The requested action is incompatible with the authoritative state."""


class ControlStatus(str, Enum):
    OFFLINE = "OFFLINE"
    STARTING = "STARTING"
    READY = "READY"
    HOMING = "HOMING"
    RECORDING = "RECORDING"
    FINALIZING = "FINALIZING"
    ERROR = "ERROR"
    SHUTTING_DOWN = "SHUTTING_DOWN"


@dataclass(frozen=True, slots=True)
class CollectorControlConfig:
    runtime_dir: Path
    station_config: Path
    task_config: Path
    output_root: Path
    session_log_root: Path
    min_free_gib: int = 80
    readiness_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        for label, path in (
            ("runtime_dir", self.runtime_dir),
            ("station_config", self.station_config),
            ("task_config", self.task_config),
            ("output_root", self.output_root),
            ("session_log_root", self.session_log_root),
        ):
            if not path.is_absolute():
                raise ValueError(f"{label} must be absolute")
        if self.min_free_gib <= 0 or self.readiness_timeout_s <= 0:
            raise ValueError("control limits must be positive")


class SessionProcess(Protocol):
    pid: int
    stdout: IO[str] | None
    stderr: IO[str] | None

    def poll(self) -> int | None:
        ...

    def wait(self, timeout: float | None = None) -> int:
        ...


PopenFactory = Callable[[Sequence[str]], SessionProcess]
CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
SignalGroup = Callable[[int, int], None]


class CollectorManager:
    """Own the single production Session process and its structured control plane."""

    def __init__(
        self,
        config: CollectorControlConfig,
        popen_factory: PopenFactory | None = None,
        command_runner: CommandRunner | None = None,
        signal_group: SignalGroup = os.killpg,
    ) -> None:
        self.config = config
        self.popen_factory = popen_factory or self._popen
        self.command_runner = command_runner or self._run_command
        self.signal_group = signal_group
        self.trigger_socket = config.runtime_dir / "trigger.sock"
        self.event_socket = config.runtime_dir / "events.sock"
        self._lock = threading.RLock()
        self._status = ControlStatus.OFFLINE
        self._error: str | None = None
        self._process: SessionProcess | None = None
        self._expected_stop = False
        self._session_started_at: str | None = None
        self._recording_started_at: str | None = None
        self._devices: list[dict[str, Any]] = []
        self._logs: deque[dict[str, Any]] = deque(maxlen=2000)
        self._next_log_sequence = 1
        self._event_receiver: socket.socket | None = None
        self._event_stop = threading.Event()
        self._event_thread: threading.Thread | None = None

    @property
    def status(self) -> ControlStatus:
        with self._lock:
            return self._status

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            status = self._status.value
            error = self._error
            session_started_at = self._session_started_at
            recording_started_at = self._recording_started_at
            devices = list(self._devices)
            logs = list(self._logs)[-300:]
        task = self._load_task()
        free_bytes = self._free_bytes()
        return {
            "schema_version": 1,
            "status": status,
            "error": error,
            "task": task,
            "output_root": str(self.config.output_root),
            "session_started_at": session_started_at,
            "recording_started_at": recording_started_at,
            "disk": {"free_bytes": free_bytes},
            "devices": devices,
            "episodes": discover_episodes(self.config.output_root),
            "logs": logs,
        }

    def logs_since(self, sequence: int) -> list[dict[str, Any]]:
        if sequence < 0:
            raise ValueError("log sequence must not be negative")
        with self._lock:
            return [entry for entry in self._logs if entry["sequence"] > sequence]

    def inspect_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._status not in {ControlStatus.OFFLINE, ControlStatus.ERROR}:
                raise ControlConflict("device inspection requires Session OFFLINE")
        result = self.command_runner(
            (
                "arx5-collect",
                "devices",
                "--station-config",
                str(self.config.station_config),
            )
        )
        self._append_log("devices", result.stdout.strip())
        if result.stderr.strip():
            self._append_log("devices:error", result.stderr.strip())
        try:
            devices = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("devices returned invalid JSON") from error
        if not isinstance(devices, list):
            raise RuntimeError("devices response must be a JSON array")
        with self._lock:
            self._devices = devices
        if result.returncode != 0:
            raise RuntimeError("one or more configured devices did not match")
        return devices

    def start_session(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ControlConflict("a collection Session is already active")
            if self._status not in {ControlStatus.OFFLINE, ControlStatus.ERROR}:
                raise ControlConflict(f"cannot start Session from {self._status.value}")
            self._status = ControlStatus.STARTING
            self._error = None
            self._expected_stop = False
            self._recording_started_at = None
            self._session_started_at = utc_now()

        self._start_event_receiver()
        argv = self._session_argv()
        self._append_log("control", "starting arx5-collect run")
        try:
            process = self.popen_factory(argv)
        except BaseException as error:
            self._stop_event_receiver()
            with self._lock:
                self._status = ControlStatus.ERROR
                self._error = str(error)
            raise
        with self._lock:
            self._process = process
        self._start_log_reader(process.stdout, "stdout")
        self._start_log_reader(process.stderr, "stderr")
        threading.Thread(
            target=self._watch_process,
            args=(process,),
            name="arx5-session-watcher",
            daemon=True,
        ).start()

    def trigger(self, event: TriggerEvent) -> None:
        with self._lock:
            status = self._status
        allowed = (
            {ControlStatus.READY, ControlStatus.RECORDING}
            if event is TriggerEvent.ACTIVATE
            else {ControlStatus.RECORDING}
        )
        if status not in allowed:
            raise ControlConflict(f"{event.value} is not allowed from {status.value}")
        try:
            send_trigger(self.trigger_socket, event)
        except OSError as error:
            raise RuntimeError("Session trigger socket is unavailable") from error
        self._append_log("control", f"trigger={event.value}")

    def stop_session(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise ControlConflict("no active Session to stop")
            if self._status is not ControlStatus.READY:
                raise ControlConflict("Session can only stop from READY")
            self._expected_stop = True
            self._status = ControlStatus.SHUTTING_DOWN
        self._append_log("control", "ordered Session shutdown requested")
        try:
            self.signal_group(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                self._expected_stop = True
                self._status = ControlStatus.SHUTTING_DOWN
                try:
                    self.signal_group(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=45.0)
            except subprocess.TimeoutExpired:
                try:
                    self.signal_group(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._stop_event_receiver()

    def handle_event(self, envelope: Mapping[str, Any]) -> None:
        if envelope.get("schema_version") != EVENT_SCHEMA_VERSION:
            raise ValueError("unsupported collector event schema")
        event_type = envelope.get("type")
        payload = envelope.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            raise ValueError("invalid collector event envelope")

        with self._lock:
            if event_type == "session.ready":
                self._status = ControlStatus.READY
            elif event_type == "session.stopped":
                self._append_log_locked("control", "collector reported session.stopped")
            elif event_type == "episode.state":
                state = payload.get("state")
                states = {
                    "homing": ControlStatus.HOMING,
                    "recording": ControlStatus.RECORDING,
                    "finalizing": ControlStatus.FINALIZING,
                    "ready": ControlStatus.READY,
                }
                if state not in states:
                    raise ValueError(f"unsupported episode state: {state}")
                self._status = states[state]
                if state == "recording":
                    self._recording_started_at = str(envelope.get("timestamp") or utc_now())
                elif state == "ready":
                    self._recording_started_at = None
            elif event_type == "episode.result":
                self._append_log_locked(
                    "episode",
                    f"{payload.get('episode_id')} outcome={payload.get('outcome')}",
                )
            else:
                raise ValueError(f"unsupported collector event type: {event_type}")

    def _session_argv(self) -> tuple[str, ...]:
        return (
            "arx5-collect",
            "run",
            "--station-config",
            str(self.config.station_config),
            "--task-config",
            str(self.config.task_config),
            "--output-root",
            str(self.config.output_root),
            "--session-log-root",
            str(self.config.session_log_root),
            "--min-free-gib",
            str(self.config.min_free_gib),
            "--readiness-timeout-s",
            str(self.config.readiness_timeout_s),
            "--control-socket",
            str(self.trigger_socket),
            "--event-socket",
            str(self.event_socket),
        )

    @staticmethod
    def _popen(argv: Sequence[str]) -> SessionProcess:
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    @staticmethod
    def _run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
        )

    def _start_event_receiver(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.event_socket.unlink(missing_ok=True)
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        receiver.bind(str(self.event_socket))
        os.chmod(self.event_socket, 0o660)
        receiver.settimeout(0.2)
        self._event_stop.clear()
        self._event_receiver = receiver
        self._event_thread = threading.Thread(
            target=self._event_loop,
            name="arx5-event-receiver",
            daemon=True,
        )
        self._event_thread.start()

    def _stop_event_receiver(self) -> None:
        self._event_stop.set()
        if self._event_thread is not None:
            self._event_thread.join(timeout=1.0)
        if self._event_receiver is not None:
            self._event_receiver.close()
        self._event_thread = None
        self._event_receiver = None
        self.event_socket.unlink(missing_ok=True)
        self.trigger_socket.unlink(missing_ok=True)

    def _event_loop(self) -> None:
        assert self._event_receiver is not None
        while not self._event_stop.is_set():
            try:
                encoded = self._event_receiver.recv(65_536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                envelope = json.loads(encoded)
                if not isinstance(envelope, dict):
                    raise ValueError("collector event must be an object")
                self.handle_event(envelope)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                self._append_log("control:error", f"invalid collector event: {error}")

    def _watch_process(self, process: SessionProcess) -> None:
        return_code = process.wait()
        with self._lock:
            expected = self._expected_stop
            self._process = None
            self._recording_started_at = None
            self._session_started_at = None
            if expected and return_code in {0, 130, -signal.SIGTERM}:
                self._status = ControlStatus.OFFLINE
                self._error = None
            else:
                self._status = ControlStatus.ERROR
                self._error = f"collector exited with {return_code}"
        self._append_log("control", f"collector exited return_code={return_code}")
        self._stop_event_receiver()

    def _start_log_reader(self, stream: IO[str] | None, name: str) -> None:
        if stream is None:
            return

        def read_lines() -> None:
            try:
                for line in stream:
                    message = line.rstrip()
                    if message:
                        self._append_log(name, message)
            finally:
                stream.close()

        threading.Thread(
            target=read_lines,
            name=f"arx5-{name}-reader",
            daemon=True,
        ).start()

    def _append_log(self, source: str, message: str) -> None:
        with self._lock:
            self._append_log_locked(source, message)

    def _append_log_locked(self, source: str, message: str) -> None:
        self._logs.append(
            {
                "sequence": self._next_log_sequence,
                "timestamp": utc_now(),
                "source": source,
                "message": message,
            }
        )
        self._next_log_sequence += 1

    def _load_task(self) -> dict[str, str]:
        try:
            payload = json.loads(self.config.task_config.read_text())
            return {
                "id": str(payload["task_id"]),
                "description": str(payload["task_description"]),
            }
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            return {"id": "unavailable", "description": f"task unavailable: {error}"}

    def _free_bytes(self) -> int:
        try:
            self.config.output_root.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.config.output_root).free
        except OSError:
            return 0


def discover_episodes(root: Path, limit: int = 100) -> list[dict[str, Any]]:
    if limit <= 0 or not root.exists():
        return []
    metadata_paths = list(root.glob("*/metadata.json"))
    metadata_paths.extend(root.glob("aborted/*/metadata.json"))
    episodes: list[dict[str, Any]] = []
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text())
            timing = metadata["timing"]
            mcap_path = metadata_path.with_name("episode.mcap")
            warnings = [
                warning
                for stream in metadata.get("streams", [])
                for warning in stream.get("warnings", [])
            ]
            warnings.extend(metadata.get("errors", []))
            episodes.append(
                {
                    "id": str(metadata["episode_id"]),
                    "started_at": str(timing["started_at"]),
                    "duration_s": float(timing["duration_s"]),
                    "outcome": str(metadata["outcome"]),
                    "size_bytes": mcap_path.stat().st_size,
                    "warning": warnings[0] if warnings else None,
                    "path": str(metadata_path.parent),
                }
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    episodes.sort(key=lambda episode: episode["started_at"], reverse=True)
    return episodes[:limit]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
