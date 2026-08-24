from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TextIO

from .adapters.keyboard import KeyboardTrigger
from .models import (
    EpisodeBlocked,
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeState,
    StreamSpec,
)
from .ports import RecordTrigger
from .runtime import EpisodeRuntime


RuntimeFactory = Callable[[EpisodeRequest, RecordTrigger], EpisodeRuntime]
TriggerFactory = Callable[[str], AbstractContextManager[RecordTrigger]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record ARX5 collection episodes")
    parser.add_argument("--task-config", type=Path, required=True)
    parser.add_argument("--station-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--episodes", type=non_negative_int, default=0)
    parser.add_argument("--trigger-key", type=one_character, default=" ")
    return parser


def load_request(
    task_config: Path,
    output_root: Path,
    station_config: Path,
) -> EpisodeRequest:
    config = json.loads(task_config.read_text())
    require_exact_keys(config, {"task_id", "task_description", "streams"}, "task")
    if not config["streams"]:
        raise ValueError("task must contain at least one stream")

    streams = []
    for stream in config["streams"]:
        require_exact_keys(
            stream,
            {"id", "topic", "required", "expected_hz"},
            "stream",
        )
        streams.append(
            StreamSpec(
                id=stream["id"],
                topic=stream["topic"],
                required=stream["required"],
                expected_hz=stream["expected_hz"],
            )
        )

    return EpisodeRequest(
        task_id=config["task_id"],
        task_description=config["task_description"],
        output_root=output_root,
        station_config=station_config,
        streams=tuple(streams),
    )


def run_cli(
    runtime_factory: RuntimeFactory,
    argv: Sequence[str] | None = None,
    trigger_factory: TriggerFactory | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    request = load_request(args.task_config, args.output_root, args.station_config)
    make_trigger = trigger_factory or (lambda key: KeyboardTrigger(key=key))

    with make_trigger(args.trigger_key) as trigger:
        runtime = runtime_factory(request, trigger)
        return run_episode_loop(
            runtime,
            request,
            episodes=args.episodes,
            stdout=output,
            stderr=error_output,
        )


def run_episode_loop(
    runtime: EpisodeRuntime,
    request: EpisodeRequest,
    episodes: int = 0,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    for partial in runtime.store.list_partials():
        print(f"partial episode found: {partial}", file=error_output)

    previous_sink = runtime.state_sink

    def state_sink(state: EpisodeState) -> None:
        if previous_sink is not None:
            previous_sink(state)
        if state is EpisodeState.RECORDING:
            print(
                "RECORDING: activate=success, abort=abort and continue, "
                "Ctrl+C=abort and exit",
                file=error_output,
                flush=True,
            )
        elif state is EpisodeState.FINALIZING:
            print("FINALIZING", file=error_output, flush=True)

    runtime.state_sink = state_sink
    completed = 0
    try:
        while episodes == 0 or completed < episodes:
            print(
                "READY: activate trigger starts; Ctrl+C exits the Session",
                file=error_output,
                flush=True,
            )
            try:
                result = runtime.run_once(request)
            except EpisodeBlocked as error:
                render_session_blocked(
                    result="not_started",
                    reason=error.reason,
                    safety=error.safety,
                    output=error_output,
                )
                continue
            print(
                json.dumps(
                    {
                        "episode_id": result.episode_id,
                        "outcome": result.outcome.value,
                        "mcap_path": str(result.mcap_path),
                        "metadata_path": str(result.metadata_path),
                    }
                ),
                file=output,
                flush=True,
            )
            completed += 1
            if result.outcome is EpisodeOutcome.ABORTED and result.errors == (
                "recording interrupted",
            ):
                return 0
            if result.outcome is EpisodeOutcome.FAIL and result.session_blocked:
                render_session_blocked(
                    result="fail",
                    reason="; ".join(result.errors),
                    safety=(
                        "Episode stop hooks completed; dual-arm "
                        "G_COMPENSATION confirmed"
                    ),
                    output=error_output,
                )
            elif result.outcome is EpisodeOutcome.FAIL:
                print(
                    "EPISODE FAILED - SESSION READY: " + "; ".join(result.errors),
                    file=error_output,
                    flush=True,
                )
    except KeyboardInterrupt:
        return 0
    finally:
        runtime.state_sink = previous_sink
    return 0


def render_session_blocked(
    result: str,
    reason: str,
    safety: str,
    output: TextIO,
) -> None:
    title = (
        "EPISODE FAILED - SESSION BLOCKED"
        if result == "fail"
        else "EPISODE NOT STARTED - SESSION BLOCKED"
    )
    print(
        "\a\n"
        "============================================================\n"
        f"{title}\n"
        f"result: {result}\n"
        f"reason: {reason}\n"
        f"safety: {safety}\n"
        "action: inspect devices, then activate to recheck or Ctrl+C to exit\n"
        "============================================================\n",
        file=output,
        flush=True,
    )


def require_exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} keys must be exactly {sorted(expected)}")


def non_negative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return result


def one_character(value: str) -> str:
    if len(value) != 1:
        raise argparse.ArgumentTypeError("must be one character")
    return value
