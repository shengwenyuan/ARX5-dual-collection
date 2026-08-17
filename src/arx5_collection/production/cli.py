from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Iterator, TextIO

from arx5_collection.episode.cli import (
    load_request,
    non_negative_int,
    run_episode_loop,
)
from arx5_collection.reset import ResetState

from .checks import CheckFailure, CheckResult
from .config import load_station_config, validate_task_streams
from .devices import DeviceIdentityVerifier
from .events import NullEventEmitter, UnixDatagramEventEmitter
from .orchestrator import GIB, ProductionSession
from .triggers import AutoTriggerFactory
from arx5_collection.station.inventory import D405Device
from arx5_collection.station.service import (
    StationInitializationService,
    StationInteraction,
)
from arx5_collection.station.store import StationConfigStore


DEFAULT_STATION_CONFIG = Path("/var/lib/arx5-collection/station.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arx5-collect")
    subcommands = parser.add_subparsers(dest="command", required=True)

    station = subcommands.add_parser("station", help="initialize station hardware")
    station_commands = station.add_subparsers(dest="station_command", required=True)
    configure = station_commands.add_parser(
        "configure", help="discover, validate, and bind all station devices"
    )
    configure.add_argument(
        "--station-config", type=Path, default=DEFAULT_STATION_CONFIG
    )
    configure.add_argument(
        "--log-dir", type=Path, default=Path("/tmp/arx5-station-configure")
    )

    devices = subcommands.add_parser("devices", help="verify configured device identities")
    devices.add_argument(
        "--station-config", type=Path, default=DEFAULT_STATION_CONFIG
    )

    run = subcommands.add_parser("run", help="run one long-lived collection Session")
    run.add_argument("--station-config", type=Path, default=DEFAULT_STATION_CONFIG)
    run.add_argument("--task-config", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument(
        "--session-log-root", type=Path, default=Path("/reports/session-logs")
    )
    run.add_argument("--episodes", type=non_negative_int, default=0)
    run.add_argument("--min-free-gib", type=positive_int, default=80)
    run.add_argument("--readiness-timeout-s", type=positive_float, default=30.0)
    run.add_argument("--control-socket", type=Path)
    run.add_argument("--event-socket", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "devices":
            return run_devices(args.station_config)
        if args.command == "station":
            return run_station_configure(args.station_config, args.log_dir)
        return run_session(args)
    except CheckFailure as error:
        for result in error.results:
            render_check(result, sys.stderr)
        print(str(error), file=sys.stderr)
        return 2
    except (EOFError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Session interrupted during startup or shutdown", file=sys.stderr)
        return 130


def run_devices(station_path: Path, stdout: TextIO | None = None) -> int:
    output = stdout or sys.stdout
    station = load_configured_station(station_path)
    identities = DeviceIdentityVerifier(station).inspect()
    print(
        json.dumps(
            [
                {
                    "id": identity.id,
                    "kind": identity.kind,
                    "configured_serial": identity.configured_serial,
                    "detected_serial": identity.detected_serial,
                    "link": identity.link,
                    "matched": identity.matched,
                    "detail": identity.detail,
                }
                for identity in identities
            ],
            ensure_ascii=False,
        ),
        file=output,
    )
    return 0 if all(identity.matched for identity in identities) else 2


class ConsoleStationInteraction(StationInteraction):
    def __init__(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def choose_station_id(self, default: str) -> str:
        return self._input(f"Station ID [{default}]: ") or default

    def prompt_left_arm_movement(self) -> None:
        self._input(
            "Both arms are in gravity compensation. Press ENTER, then gently move "
            "only the physical LEFT arm: "
        )

    def choose_camera(
        self,
        role: str,
        candidates: Sequence[D405Device],
        used_serials: frozenset[str],
    ) -> str:
        available = [
            camera for camera in candidates if camera.serial_number not in used_serials
        ]
        print(f"Bind D405 role={role}", file=self.stdout)
        for index, camera in enumerate(available, start=1):
            print(
                f"  {index}. serial={camera.serial_number} "
                f"model={camera.name} usb={camera.usb_type}",
                file=self.stdout,
            )
        value = self._input("Select number or enter sticker serial: ")
        try:
            index = int(value)
        except ValueError:
            return value
        if index < 1 or index > len(available):
            raise ValueError(f"camera selection index out of range: {index}")
        return available[index - 1].serial_number

    def prompt_pedal(self, role: str) -> None:
        semantic = "SPACE / activate" if role == "activate" else "A / abort"
        print(f"Press pedal for {semantic} once", file=self.stdout, flush=True)

    def report(self, message: str) -> None:
        print(message, file=self.stdout, flush=True)

    def _input(self, prompt: str) -> str:
        print(prompt, end="", file=self.stdout, flush=True)
        line = self.stdin.readline()
        if line == "":
            raise EOFError("station configure input closed")
        return line.strip()


def run_station_configure(
    station_path: Path,
    log_dir: Path,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    interaction = ConsoleStationInteraction(stdin, stdout)
    service = StationInitializationService(
        store=StationConfigStore(station_path),
        log_dir=log_dir,
    )
    service.configure(interaction)
    return 0


def run_session(args: argparse.Namespace) -> int:
    station = load_configured_station(args.station_config)
    validate_task_streams(args.task_config)
    request = load_request(args.task_config, args.output_root, args.station_config)
    session_log_dir = args.session_log_root / _session_id()
    event_sink = (
        UnixDatagramEventEmitter(
            args.event_socket,
            warning_sink=lambda message: print(
                f"WARNING {message}", file=sys.stderr, flush=True
            ),
        )
        if args.event_socket is not None
        else NullEventEmitter()
    )

    def check_sink(result: CheckResult) -> None:
        render_check(result, sys.stdout)

    def home_state_sink(state: ResetState) -> None:
        render_reset_state(state, sys.stderr)
        if state is ResetState.RESETTING:
            event_sink.emit("episode.state", {"state": "homing"})

    session = ProductionSession(
        station=station,
        output_root=args.output_root,
        log_dir=session_log_dir,
        software_version=_software_version(),
        min_free_bytes=args.min_free_gib * GIB,
        readiness_timeout_s=args.readiness_timeout_s,
        home_state_sink=home_state_sink,
        home_timing_sink=lambda phase, elapsed_s: render_home_timing(
            phase, elapsed_s, sys.stderr
        ),
        check_sink=check_sink,
        warning_sink=lambda message: print(f"WARNING {message}", file=sys.stderr),
    )
    try:
        with termination_as_interrupt(), session:
            print(f"SESSION READY logs={session_log_dir}", flush=True)
            trigger_factory = AutoTriggerFactory(
                status_sink=lambda message: print(message, file=sys.stderr, flush=True),
                remote_socket=args.control_socket,
            )
            with trigger_factory.open(station) as trigger:
                event_sink.emit(
                    "session.ready",
                    {"session_log_dir": str(session_log_dir)},
                )
                runtime = session.create_runtime(request, trigger)
                return run_episode_loop(
                    runtime,
                    request,
                    episodes=args.episodes,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    event_sink=event_sink,
                )
    finally:
        event_sink.emit("session.stopped")


def render_check(result: CheckResult, output: TextIO) -> None:
    state = "PASS" if result.passed else "FAIL"
    print(f"{state} [{result.phase.value}] {result.name}: {result.detail}", file=output)


def load_configured_station(path: Path):
    if not path.is_file():
        raise ValueError(
            f"station configuration is missing: {path}; "
            "run 'arx5-collect station configure' first"
        )
    return load_station_config(path)


def render_reset_state(state: ResetState, output: TextIO) -> None:
    if state is ResetState.RESETTING:
        message = "RESETTING: moving both arms to Vendor home"
    else:
        message = "RESET_COMPLETE: gravity compensation restored"
    print(message, file=output, flush=True)


def render_home_timing(phase: str, elapsed_s: float, output: TextIO) -> None:
    print(f"HOME_TIMING {phase}={elapsed_s:.3f}s", file=output, flush=True)


@contextmanager
def termination_as_interrupt() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def positive_float(value: str) -> float:
    result = float(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _software_version() -> str:
    try:
        return version("arx5-dual-collection")
    except PackageNotFoundError:
        return "0.1.0"


def _session_id() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
