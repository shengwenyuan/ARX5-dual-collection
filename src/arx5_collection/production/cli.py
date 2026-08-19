from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TextIO

from arx5_collection.episode.cli import (
    load_request,
    non_negative_int,
    run_episode_loop,
)
from arx5_collection.reset import ResetState

from .checks import CheckFailure, CheckResult
from .config import load_configured_station, validate_task_streams
from .devices import DeviceIdentityVerifier
from .lifecycle import termination_as_interrupt
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
    add_session_arguments(run)

    dagger = subcommands.add_parser("dagger", help="run DAgger collection modes")
    dagger_commands = dagger.add_subparsers(dest="dagger_command", required=True)
    shadow = dagger_commands.add_parser(
        "shadow", help="record policy inference without granting command authority"
    )
    add_session_arguments(shadow)
    shadow.add_argument("--policy-config", type=Path, required=True)
    takeover_dry_run = dagger_commands.add_parser(
        "takeover-dry-run",
        help="validate Take-over authority without model or action output",
    )
    add_session_arguments(takeover_dry_run)
    takeover_dry_run.add_argument("--policy-config", type=Path, required=True)
    checkpoint_sha = dagger_commands.add_parser(
        "checkpoint-sha", help="compute the deterministic SHA-256 of a checkpoint tree"
    )
    checkpoint_sha.add_argument("checkpoint", type=Path)
    return parser


def add_session_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--station-config", type=Path, default=DEFAULT_STATION_CONFIG)
    parser.add_argument("--task-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--session-log-root", type=Path, default=Path("/reports/session-logs")
    )
    parser.add_argument("--episodes", type=non_negative_int, default=0)
    parser.add_argument("--min-free-gib", type=positive_int, default=80)
    parser.add_argument("--readiness-timeout-s", type=positive_float, default=30.0)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "devices":
            return run_devices(args.station_config)
        if args.command == "station":
            return run_station_configure(args.station_config, args.log_dir)
        if args.command == "dagger":
            if args.dagger_command == "checkpoint-sha":
                return run_checkpoint_sha(args.checkpoint)
            if args.dagger_command == "takeover-dry-run":
                return run_dagger_takeover_dry_run(args)
            return run_dagger_shadow(args)
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

    def check_sink(result: CheckResult) -> None:
        render_check(result, sys.stdout)

    session = ProductionSession(
        station=station,
        output_root=args.output_root,
        log_dir=session_log_dir,
        software_version=_software_version(),
        min_free_bytes=args.min_free_gib * GIB,
        readiness_timeout_s=args.readiness_timeout_s,
        home_state_sink=lambda state: render_reset_state(state, sys.stderr),
        home_timing_sink=lambda phase, elapsed_s: render_home_timing(
            phase, elapsed_s, sys.stderr
        ),
        check_sink=check_sink,
        warning_sink=lambda message: print(f"WARNING {message}", file=sys.stderr),
    )
    with termination_as_interrupt(), session:
        print(f"SESSION READY logs={session_log_dir}", flush=True)
        trigger_factory = AutoTriggerFactory(
            status_sink=lambda message: print(message, file=sys.stderr, flush=True)
        )
        with trigger_factory.open(station) as trigger:
            runtime = session.create_runtime(request, trigger)
            return run_episode_loop(
                runtime,
                request,
                episodes=args.episodes,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )


def run_dagger_shadow(args: argparse.Namespace) -> int:
    from arx5_collection.dagger.application import DaggerApplicationBuilder

    return DaggerApplicationBuilder().build_shadow(_dagger_run_spec(args)).run()


def run_dagger_takeover_dry_run(args: argparse.Namespace) -> int:
    from arx5_collection.dagger.application import DaggerApplicationBuilder

    return (
        DaggerApplicationBuilder()
        .build_takeover_dry_run(_dagger_run_spec(args))
        .run()
    )


def _dagger_run_spec(args: argparse.Namespace):
    from arx5_collection.dagger.application import DaggerRunSpec

    return DaggerRunSpec(
        station_config=args.station_config,
        task_config=args.task_config,
        policy_config=args.policy_config,
        output_root=args.output_root,
        session_log_root=args.session_log_root,
        episodes=args.episodes,
        min_free_gib=args.min_free_gib,
        readiness_timeout_s=args.readiness_timeout_s,
        software_version=_software_version(),
        session_id=_session_id(),
    )


def run_checkpoint_sha(checkpoint: Path, stdout: TextIO | None = None) -> int:
    from arx5_collection.dagger.checkpoint import checkpoint_tree_sha256

    print(checkpoint_tree_sha256(checkpoint), file=stdout or sys.stdout)
    return 0


def render_check(result: CheckResult, output: TextIO) -> None:
    state = "PASS" if result.passed else "FAIL"
    print(f"{state} [{result.phase.value}] {result.name}: {result.detail}", file=output)


def render_reset_state(state: ResetState, output: TextIO) -> None:
    if state is ResetState.RESETTING:
        message = "RESETTING: moving both arms to Vendor home"
    else:
        message = "RESET_COMPLETE: gravity compensation restored"
    print(message, file=output, flush=True)


def render_home_timing(phase: str, elapsed_s: float, output: TextIO) -> None:
    print(f"HOME_TIMING {phase}={elapsed_s:.3f}s", file=output, flush=True)


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
