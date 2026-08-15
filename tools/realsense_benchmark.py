#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import multiprocessing
import queue
import time
from pathlib import Path
from typing import Any

from arx5_collection.metrics import timing_summary


def stream_config(rs: Any, serial: str, width: int, height: int, fps: int) -> Any:
    config = rs.config()
    config.enable_device(serial)
    config.enable_stream(rs.stream.color, width, height, rs.format.yuyv, fps)
    config.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
    return config


def run(
    duration_s: float,
    width: int,
    height: int,
    fps: int,
    startup_timeout_s: float = 20.0,
    selected_serials: list[str] | None = None,
    startup_stagger_s: float = 0.0,
) -> dict[str, Any]:
    import pyrealsense2 as rs

    context = rs.context()
    available_serials = [
        device.get_info(rs.camera_info.serial_number) for device in context.query_devices()
    ]
    serials = selected_serials or available_serials
    result: dict[str, Any] = {
        "settings": {"width": width, "height": height, "fps": fps},
        "duration_s": duration_s,
        "serials": serials,
        "cameras": [],
        "status": "FAILED",
    }
    missing_serials = sorted(set(serials) - set(available_serials))
    if missing_serials:
        result["error"] = f"requested RealSense devices not found: {missing_serials}"
        return result
    if selected_serials is None and len(serials) != 3:
        result["error"] = f"expected 3 RealSense devices, found {len(serials)}"
        return result

    try:
        process_context = multiprocessing.get_context("spawn")
        report_queue = process_context.Queue()
        processes = [
            process_context.Process(
                target=collect_camera_process,
                args=(
                    serial,
                    duration_s,
                    width,
                    height,
                    fps,
                    index * startup_stagger_s,
                    report_queue,
                ),
            )
            for index, serial in enumerate(serials)
        ]
        for process in processes:
            process.start()

        deadline = time.monotonic() + startup_timeout_s + duration_s
        for process in processes:
            process.join(max(0.0, deadline - time.monotonic()))

        timed_out = [process for process in processes if process.is_alive()]
        for process in timed_out:
            process.terminate()
        for process in timed_out:
            process.join(3.0)
            if process.is_alive():
                process.kill()
                process.join()

        reports: dict[str, dict[str, Any]] = {}
        while True:
            try:
                report = report_queue.get_nowait()
            except queue.Empty:
                break
            reports[report["serial"]] = report

        for serial, process in zip(serials, processes, strict=True):
            if serial not in reports:
                reason = "worker timeout" if process in timed_out else f"worker exit code {process.exitcode}"
                reports[serial] = {"serial": serial, "error": reason}

        result["cameras"] = [reports[serial] for serial in serials]
        errors = [report.get("error") for report in result["cameras"] if report.get("error")]
        if errors:
            raise RuntimeError("; ".join(errors))
        result["status"] = "PASSED"
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def collect_camera_process(
    serial: str,
    duration_s: float,
    width: int,
    height: int,
    fps: int,
    startup_delay_s: float,
    report_queue: Any,
) -> None:
    import pyrealsense2 as rs

    pipeline = rs.pipeline()
    align = rs.align(rs.stream.color)
    timestamps_ns: list[int] = []
    frame_numbers: list[int] = []
    device_timestamps_ms: list[float] = []
    dimensions = None
    timestamp_domain = None
    started_ns = time.monotonic_ns()
    try:
        time.sleep(startup_delay_s)
        pipeline.start(stream_config(rs, serial, width, height, fps))
        stream_started_ns = time.monotonic_ns()
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            frames = align.process(pipeline.wait_for_frames(5000))
            color = frames.get_color_frame()
            depth = frames.get_depth_frame()
            if not color or not depth:
                raise RuntimeError(f"{serial} missing aligned color or depth")
            timestamps_ns.append(time.monotonic_ns())
            frame_numbers.append(color.get_frame_number())
            device_timestamps_ms.append(color.get_timestamp())
            timestamp_domain = str(color.get_frame_timestamp_domain())
            dimensions = {
                "color": [color.get_width(), color.get_height()],
                "aligned_depth": [depth.get_width(), depth.get_height()],
            }
        report = {
            "serial": serial,
            "startup_s": (stream_started_ns - started_ns) / 1e9,
            **timing_summary(timestamps_ns),
            "first_frame_number": frame_numbers[0] if frame_numbers else None,
            "last_frame_number": frame_numbers[-1] if frame_numbers else None,
            "first_device_timestamp_ms": device_timestamps_ms[0] if device_timestamps_ms else None,
            "last_device_timestamp_ms": device_timestamps_ms[-1] if device_timestamps_ms else None,
            "timestamp_domain": timestamp_domain,
            "dimensions": dimensions,
        }
    except BaseException as error:
        report = {"serial": serial, "error": f"{type(error).__name__}: {error}"}
    finally:
        try:
            pipeline.stop()
        except RuntimeError:
            pass
    report_queue.put(report)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--startup-timeout", type=float, default=20.0)
    parser.add_argument("--serial", action="append", dest="serials")
    parser.add_argument("--startup-stagger", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(
        args.duration,
        args.width,
        args.height,
        args.fps,
        args.startup_timeout,
        args.serials,
        args.startup_stagger,
    )
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    raise SystemExit(0 if result["status"] == "PASSED" else 1)


if __name__ == "__main__":
    main()
