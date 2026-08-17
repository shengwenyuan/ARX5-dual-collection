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

from arx5_collection.episode.adapters.keyboard import KeyboardTrigger
from arx5_collection.episode.cli import (
    load_request,
    non_negative_int,
    one_character,
    run_episode_loop,
)
from arx5_collection.reset import ResetState

from .checks import CheckFailure, CheckResult
from .config import load_station_config, validate_task_streams
from .devices import DeviceIdentityVerifier
from .orchestrator import GIB, ProductionSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arx5-collect")
    subcommands = parser.add_subparsers(dest="command", required=True)

    devices = subcommands.add_parser("devices", help="verify configured device identities")
    devices.add_argument("--station-config", type=Path, required=True)

    run = subcommands.add_parser("run", help="run one long-lived collection Session")
    run.add_argument("--station-config", type=Path, required=True)
    run.add_argument("--task-config", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument(
        "--session-log-root", type=Path, default=Path("/reports/session-logs")
    )
    run.add_argument("--episodes", type=non_negative_int, default=0)
    run.add_argument("--trigger-key", type=one_character, default=" ")
    run.add_argument("--min-free-gib", type=positive_int, default=80)
    run.add_argument("--readiness-timeout-s", type=positive_float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "devices":
            return run_devices(args.station_config)
        return run_session(args)
    except CheckFailure as error:
        for result in error.results:
            render_check(result, sys.stderr)
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Session interrupted during startup or shutdown", file=sys.stderr)
        return 130


def run_devices(station_path: Path, stdout: TextIO | None = None) -> int:
    output = stdout or sys.stdout
    station = load_station_config(station_path)
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


def run_session(args: argparse.Namespace) -> int:
    station = load_station_config(args.station_config)
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
        with KeyboardTrigger(key=args.trigger_key) as trigger:
            runtime = session.create_runtime(request, trigger)
            return run_episode_loop(
                runtime,
                request,
                episodes=args.episodes,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )


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
